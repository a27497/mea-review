"""Prepare and verify a private cohort of exact Git trees and independent assertions."""

import argparse
import io
import os
import re
import subprocess
import tarfile
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from freeze import commit, digest, git, patch, read, revision, source, validate, write
from mea.investigation.tools import LIMITS
from mea.patch import parse
from paired import base_payload, preflight


def comparable(text):
    return [re.sub(r'^(@@ [^@]+ @@).*', r'\1', line) for line in text.splitlines()]


def mutated_tree(repo, clean, base, mutations, env, area):
    with tempfile.TemporaryDirectory(dir=area) as work:
        index_env = dict(env, GIT_INDEX_FILE=str(Path(work) / 'index'))
        git(repo, 'read-tree', clean, env=index_env)
        for path in sorted({m['path'] for m in mutations}):
            contents = source(repo, clean, path)['text']
            lines = contents.split('\n')
            edits = {}
            for m in [m for m in mutations if m['path'] == path]:
                old, new = m['before'], m['after']
                site = m.get('site', old)
                wrapped = '\n' + contents
                boundary = '\n' + site + '\n'
                if (old == new or site.splitlines().count(old) != 1 or
                        wrapped.count(boundary) != 1 or new in lines):
                    raise ValueError('NON_UNIQUE_MUTATION')
                index = wrapped[:wrapped.index(boundary)].count('\n') + site.splitlines().index(old)
                if index in edits or lines[index] != old:
                    raise ValueError('OVERLAPPING_MUTATION')
                edits[index] = new
            for index, new in edits.items():
                lines[index] = new
            contents = '\n'.join(lines)
            blob = git(repo, 'hash-object', '-w', '--stdin', stdin=contents.encode()).decode().strip()
            mode = git(repo, 'ls-tree', clean, '--', path).decode().split()[0]
            git(repo, 'update-index', '--cacheinfo', f'{mode},{blob},{path}', env=index_env)
        tree = git(repo, 'write-tree', env=index_env).decode().strip()
    return commit(tree, repo, base, env)


def build_case(phase, spec, group, output):
    case, number = spec['case'], spec['number']
    folder = phase / 'upstream' / group['repo'].rsplit('/', 1)[1]
    repo = folder / 'objects'
    meta = read(folder / 'pr' / f'{number}.json')
    remote = read(folder / 'pr' / f'{number}.files.json')
    ranked = next(r for r in group['seeded'] if r['number'] == number)
    orig = revision(repo, f'refs/pilot/{number}^{{commit}}')
    if (meta['head']['sha'] != orig or ranked['sha'] != orig or meta['base']['repo']['full_name'] != group['repo'] or
            ranked['branch'] != meta['base']['ref'] or ranked['merge_sha'] != meta['merge_commit_sha']):
        raise ValueError('UPSTREAM_IDENTITY')
    target = revision(repo, meta['merge_commit_sha'] + '^1')
    base = git(repo, 'merge-base', target, orig).decode().strip()
    clean_tree = revision(repo, orig + '^{tree}')
    env = dict(os.environ, GIT_AUTHOR_NAME='Benchmark', GIT_COMMITTER_NAME='Benchmark',
               GIT_AUTHOR_EMAIL='benchmark@invalid.example', GIT_COMMITTER_EMAIL='benchmark@invalid.example',
               GIT_AUTHOR_DATE=meta['merged_at'], GIT_COMMITTER_DATE=meta['merged_at'])
    clean = commit(clean_tree, repo, base, env)
    upstream_raw = patch(repo, base, orig)
    if (patch(repo, base, clean) != upstream_raw or revision(repo, clean + '^{tree}') != clean_tree or
            len(remote) != meta['changed_files'] or not 1 <= len(remote) <= 8):
        raise ValueError('INCOMPLETE_UPSTREAM_DIFF')
    upstream = {}
    for file in remote:
        path = file['filename']
        previous = file.get('previous_filename', path)
        hunk = patch(repo, base, orig, path).decode().partition('\n@@ ')[2]
        if previous != path or not hunk or comparable('@@ ' + hunk) != comparable(file['patch']):
            raise ValueError('UPSTREAM_HUNK_MISMATCH')
        upstream[path] = '@@ ' + hunk
    mutations = spec['mutations']
    for m in mutations:
        if m['path'] not in upstream or not any(line in (m['before'], ' ' + m['before'], '+' + m['before'])
                                                for line in upstream[m['path']].splitlines()):
            raise ValueError('MUTATION_OUTSIDE_CHANGED_HUNK')
    head = mutated_tree(repo, clean, base, mutations, env, output) if mutations else clean
    subsets = []
    for i in range(len(mutations)):
        own = mutated_tree(repo, clean, base, [mutations[i]], env, output)
        others = mutations[:i] + mutations[i+1:]
        reverted = mutated_tree(repo, clean, base, others, env, output) if others else clean
        subsets.append(dict(only=own, reverted=reverted))
    sources = []
    omitted = []
    inventory = []
    total_bytes = 0
    for file in remote:
        path, status = file['filename'], file['status']
        if status not in ('added', 'modified', 'removed'):
            raise ValueError('UNSUPPORTED_FILE_ACTION')
        sides = {}
        side_hash = {}
        for side, sha in [('base', base), ('head', head)]:
            if (status, side) in (('added', 'base'), ('removed', 'head')):
                continue
            found = source(repo, sha, path)
            side_hash[side] = {k: found[k] for k in ('sha256', 'blob_sha')}
            size = len(found['text'].encode())
            if (size > LIMITS['source_bytes'] or len(found['text'].splitlines()) > LIMITS['lines'] or
                    total_bytes + size > LIMITS['total_bytes']):
                omitted.append(dict(path=path, side=side, reason='FULL_SOURCE_BYTE_BOUND'))
            else:
                sides[side] = found
                total_bytes += size
        raw_patch = patch(repo, base, head, path).decode().partition('\n@@ ')[2]
        if not raw_patch:
            raise ValueError('FINAL_HUNK_MISSING')
        hunk = '@@ ' + raw_patch
        parse(hunk, sides)
        sources.append(dict(path=path, base_path=path, status=status, patch=hunk, sides=sides))
        inventory.append(dict(path=path, status=status, sides=side_hash, hunk_sha256=digest(hunk.encode())))
    for path in spec['context']:
        base_source, head_source = source(repo, base, path), source(repo, head, path)
        cost = 2 * len(head_source['text'].encode())
        if (base_source != head_source or path in upstream or cost // 2 > LIMITS['source_bytes'] or
                total_bytes + cost > LIMITS['total_bytes']):
            raise ValueError('CONTEXT_SOURCE_BOUND_OR_CHANGE')
        sources.append(dict(path=path, base_path=path, status='context', patch=None,
                            sides={'base': base_source, 'head': head_source}))
        inventory.append(dict(path=path, status='context', sides={side: {k: s[k] for k in ('sha256', 'blob_sha')}
                              for side, s in [('base', base_source), ('head', head_source)]}, hunk_sha256=None))
        total_bytes += cost
    license_file = spec['license']
    before_license = source(repo, base, license_file)
    license = source(repo, head, license_file)
    if license != before_license or ('Permission is hereby granted' not in license['text']):
        raise ValueError('LICENSE_NOT_BOUND')
    license_dir = output / 'licenses' / case
    license_dir.mkdir(parents=True, exist_ok=True)
    (license_dir / Path(license_file).name).write_text(license['text'])
    final_diff = patch(repo, base, head)
    snapshot = validate(dict(schema='mea-snapshot-v1', id=case,
        pr=dict(url=meta['html_url'], repository=group['repo'], number=number, title='', body=''),
        revisions=dict(base=base, head=head, target_base=target),
        provenance=dict(kind='KNOWN_HISTORICAL_DEVELOPMENT', captured_at=datetime.now(timezone.utc).isoformat(),
                        state_at_capture='closed', merged_at_capture=True, pending_review_version=False,
                        capture_sha256=digest(final_diff), source_authentication='NOT_INDEPENDENTLY_VERIFIED'),
        scope=dict(reported_changed_files=len(sources), complete_listing=True, omitted_sources=omitted), files=sources))
    view, _, _ = base_payload(snapshot)
    gold = []
    for i, m in enumerate(mutations, 1):
        rows = source(repo, head, m['path'])['text'].splitlines()
        line = rows.index(m['after']) + 1
        coordinate = ('head', m['path'], line)
        if coordinate not in view.changes or view.citation(*coordinate)['quote'] != m['after']:
            raise ValueError('UNREACHABLE_GOLD_ANCHOR')
        gold.append(dict(id=f'{case}:D{i}', summary=m['summary'], bodies=[m['behavior']], cross_file=m['cross_file'],
                         locations=[dict(path=m['path'], line=line)], secret_marker=f'PRIVATE-{case}-D{i}',
                         anchors=[dict(path=m['path'], side='head', revision=head, start=line, end=line)]))
    write(output / 'inputs' / f'{case}.json', snapshot)
    (output / 'inputs' / f'{case}.diff').write_bytes(final_diff)
    (output / 'inputs' / f'{case}.upstream.diff').write_bytes(upstream_raw)
    write(output / 'gold' / f'{case}.json', gold)
    return dict(case=case, repository=group['repo'], number=number, base=base, head=head, clean=clean, orig=orig,
                target_at_merge=target, clean_tree=clean_tree, frozen_tree=revision(repo, head+'^{tree}'),
                upstream_diff_sha256=digest(upstream_raw), frozen_git_diff_sha256=digest(final_diff),
                input_sha256=digest(snapshot), file_sha256=digest((output/'inputs'/f'{case}.json').read_bytes()),
                license_sha256=license['sha256'], files=inventory, omitted_sources=omitted,
                injected=bool(mutations), gold_count=len(gold), subset_revisions=subsets)


def prove_assertion(phase, output, row, spec):
    repo = phase / 'upstream' / row['repository'].rsplit('/',1)[1] / 'objects'
    tests = [m['assertion'] for m in spec['mutations']] or [spec['assertion']]
    family = spec['runtime']
    exe = '/usr/bin/python3' if family == 'python' else str(phase / 'locked-tools/node_modules/.bin/tsx')
    suffix = 'py' if family == 'python' else 'ts'
    out = output / 'assertions' / row['case']
    out.mkdir(parents=True, exist_ok=True)
    recorded = []
    for i, code in enumerate(tests, 1):
        marker = f'PRIVATE-{row["case"]}-D{i}'
        body = (f'import sys\ndef verify(condition):\n    if not condition:\n        print("{marker}")\n        sys.exit(17)\n'
                if family == 'python' else
                f'function verify(condition: boolean) {{ if (!condition) {{ console.log("{marker}"); process.exit(17); }} }}\n')
        body += code + '\n'
        definition = out / f'{i:02}.{suffix}'
        definition.write_text(body)
        revisions = [('clean', row['clean'], 0)]
        if row['injected']:
            pair = row['subset_revisions'][i-1]
            revisions += [('isolated', pair['only'], 17), ('combined', row['head'], 17), ('restored', pair['reverted'], 0)]
        for name, revision_id, expected in revisions:
            with tempfile.TemporaryDirectory(dir=output) as folder:
                checkout = Path(folder)
                archive = git(repo, 'archive', '--format=tar', revision_id)
                with tarfile.open(fileobj=io.BytesIO(archive)) as archive_file:
                    archive_file.extractall(checkout, filter='data')
                target = checkout / ('verify.' + suffix)
                target.write_text(body)
                env = dict(PATH=os.defpath + ':/usr/local/bin', HOME=folder, TZ='UTC',
                           PYTHONPATH=str(checkout / 'src') + ':' + folder, PYTHONDONTWRITEBYTECODE='1')
                proc = subprocess.run([exe, str(target)], cwd=checkout, env=env, timeout=90,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                (out / f'{i:02}-{name}.log').write_bytes(proc.stdout)
                ok = proc.returncode == expected and (marker.encode() in proc.stdout) == bool(expected)
                recorded.append(dict(id=i, tree_variant=name, revision=revision_id, exit=proc.returncode,
                                     expected=expected, pass_=ok, stdout_sha256=digest(proc.stdout),
                                     assertion_sha256=digest(body.encode())))
    result = dict(case=row['case'], status='PASS' if all(r['pass_'] for r in recorded) else 'FAIL', phases=recorded,
                  runtime=family, lock_sha256=digest((phase/'locked-tools'/'package-lock.json').read_bytes()) if
                  family != 'python' else None)
    write(out / 'receipt.json', result)
    return result


def prepare(phase, output, plan):
    groups = {g['repo']: g for g in read(phase/'holdout/ranked-pool.json')['groups']}
    stats = {repo: Counter(len(s['mutations']) for s in plan if s['repo'] == repo) for repo in groups}
    if len(plan) != 24 or len({s['case'] for s in plan}) != 24 or any(c != {0:1,1:2,2:3} for c in stats.values()):
        raise ValueError('HOLDOUT_QUOTAS')
    def one(spec):
        try:
            row = build_case(phase, spec, groups[spec['repo']], output)
            proof = prove_assertion(phase, output, row, spec)
            return dict(case=spec['case'], row=row, proof=proof, status=proof['status'])
        except (OSError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as exc:
            return dict(case=spec['case'], status='FAIL', reason=type(exc).__name__, detail=str(exc))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, plan))
    write(output/'preparation.json', results)
    if any(r['status'] != 'PASS' for r in results):
        return 1
    rows = [r['row'] for r in results]
    if len({r['orig'] for r in rows}) != 24 or len({r['clean_tree'] for r in rows}) != 24 or sum(r['gold_count'] for r in rows) != 32:
        raise ValueError('HOLDOUT_DUPLICATE_OR_GOLD_COUNT')
    bounds = preflight({r['case']: read(output/'inputs'/f"{r['case']}.json") for r in rows})
    write(output/'freeze.json', rows)
    write(output/'preflight.json', dict(status='PASS', bounds=bounds, gold=32, cases=24))
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', type=Path)
    parser.add_argument('plan', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    args.output.mkdir(mode=0o700, exist_ok=False)
    raise SystemExit(prepare(args.phase.resolve(strict=True), args.output.resolve(strict=True), read(args.plan)))
