"""Freeze licensed local Git inputs. The operator's sample plan stays private."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mea.contract import canonical, digest, git_blob_sha, validate
from mea.patch import parse


def read(path):
    return json.loads(path.read_bytes())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        tmp.write(canonical(value) + b'\n')
        tmp.flush()
        os.fsync(tmp.fileno())
    os.replace(tmp.name, path)


def git(repo, *args, env=None, stdin=None):
    return subprocess.check_output(['git', '-C', str(repo), *args], env=env, input=stdin)


def revision(repo, expression):
    return git(repo, 'rev-parse', '--verify', expression).decode().strip()


def source(repo, sha, path):
    raw = git(repo, 'show', f'{sha}:{path}')
    blob = revision(repo, f'{sha}:{path}')
    if git_blob_sha(raw) != blob:
        raise ValueError('GIT_BLOB_MISMATCH')
    return dict(text=raw.decode(), sha256=digest(raw), blob_sha=blob)


def patch(repo, base, head, *paths):
    return git(repo, 'diff', '--no-ext-diff', '--no-textconv', '--no-renames',
               '--no-color', '--full-index', '--unified=3', base, head, '--', *paths)


def commit(tree, repo, base, env):
    return git(repo, 'commit-tree', tree, '-p', base, '-m', 'Frozen review input', env=env).decode().strip()


def freeze(root, spec):
    case = spec['case']
    repo = root / spec['clone']
    pr = read(root / spec['metadata'])
    base_repo = pr['base']['repo']['full_name']
    orig = revision(repo, f"refs/pilot/{pr['number']}^{{commit}}")
    if orig != pr['head']['sha'] or base_repo != spec['repository']:
        raise ValueError('UPSTREAM_SOURCE_IDENTITY')
    merged_date = pr['merged_at'][:10]
    if not '2025-01-01' <= merged_date <= '2026-11-19':
        raise ValueError('MERGE_DATE')
    if pr['base']['ref'] != pr['base']['repo']['default_branch']:
        raise ValueError('MERGE_TARGET')
    target_at_merge = revision(repo, pr['merge_commit_sha'] + '^1')
    base = git(repo, 'merge-base', target_at_merge, orig).decode().strip()
    upstream_diff = patch(repo, base, orig)
    clean_tree = revision(repo, orig + '^{tree}')
    fixed_identity = {
        'GIT_AUTHOR_NAME': 'Benchmark', 'GIT_COMMITTER_NAME': 'Benchmark',
        'GIT_AUTHOR_EMAIL': 'benchmark@invalid.example', 'GIT_COMMITTER_EMAIL': 'benchmark@invalid.example',
        'GIT_AUTHOR_DATE': pr['merged_at'], 'GIT_COMMITTER_DATE': pr['merged_at'],
    }
    env = dict(os.environ, **fixed_identity)
    clean = commit(clean_tree, repo, base, env)
    if patch(repo, base, clean) != upstream_diff or revision(repo, clean + '^{tree}') != clean_tree:
        raise ValueError('CLEAN_TREE_OR_DIFF_MISMATCH')

    mutation = spec.get('mutation')
    head = clean
    if mutation:
        path = mutation['path']
        clean_source = source(repo, clean, path)
        old, new = mutation['before'], mutation['after']
        if clean_source['text'].count(old) != 1:
            raise ValueError('MUTATION_LOCATION')
        upstream_hunk = patch(repo, base, orig, path).decode().splitlines()
        if not all('+' + line in upstream_hunk for line in old.splitlines()):
            raise ValueError('MUTATION_OUTSIDE_UPSTREAM_CHANGE')
        new_source = clean_source['text'].replace(old, new)
        blob = git(repo, 'hash-object', '-w', '--stdin', stdin=new_source.encode()).decode().strip()
        with tempfile.TemporaryDirectory(dir=root) as temp:
            index_env = dict(env, GIT_INDEX_FILE=str(Path(temp) / 'git-index'))
            git(repo, 'read-tree', clean_tree, env=index_env)
            mode = git(repo, 'ls-tree', clean, '--', path).decode().split()[0]
            git(repo, 'update-index', '--cacheinfo', f'{mode},{blob},{path}', env=index_env)
            tree = git(repo, 'write-tree', env=index_env).decode().strip()
        head = commit(tree, repo, base, env)

    changes = git(repo, 'diff', '--name-status', '--no-renames', '-z', base, head).decode().split('\0')
    statuses = {'M': 'modified', 'A': 'added', 'D': 'removed'}
    files = []
    upstream_files = 0
    for status, path in zip(changes[:-1:2], changes[1::2], strict=True):
        sides = {}
        if status != 'A':
            sides['base'] = source(repo, base, path)
        if status != 'D':
            sides['head'] = source(repo, head, path)
        raw = patch(repo, base, head, path)
        position = raw.find(b'\n@@ ')
        if position < 0 or status not in statuses:
            raise ValueError('UNSUPPORTED_DIFF_FILE')
        hunk = raw[position + 1:].decode()
        rows = parse(hunk, sides)
        upstream_files += bool(patch(repo, base, orig, path))
        files.append(dict(path=path, base_path=path, status=statuses[status], sides=sides, patch=hunk))
        if not rows:
            raise ValueError('MISSING_HUNK')
    if not 1 <= len(files) <= 8 or upstream_files != pr['changed_files'] or len(files) != upstream_files:
        raise ValueError('UPSTREAM_FILE_LIST_MISMATCH')
    upstream_lines = sum(line.startswith((b'+', b'-')) and not line.startswith((b'+++ ', b'--- '))
                         for line in upstream_diff.splitlines())
    if upstream_lines != pr['additions'] + pr['deletions']:
        raise ValueError('UPSTREAM_DIFF_COUNT_MISMATCH')
    for path in spec['context']:
        sides = {side: source(repo, sha, path) for side, sha in (('base', base), ('head', head))}
        if sides['base'] != sides['head']:
            raise ValueError('CONTEXT_IS_CHANGED')
        files.append(dict(path=path, base_path=path, status='context', sides=sides, patch=None))

    license_files = {}
    for path in spec['license']:
        sides = {side: source(repo, sha, path) for side, sha in (('base', base), ('head', head))}
        if not all('MIT License' in s['text'] or 'Permission is hereby granted' in s['text'] for s in sides.values()):
            raise ValueError('LICENSE_UNVERIFIED')
        license_files[path] = {side: row['sha256'] for side, row in sides.items()}
        if sides['base'] != sides['head']:
            raise ValueError('LICENSE_CHANGED')
        license_path = root / 'licenses' / case / path
        license_path.parent.mkdir(parents=True, exist_ok=True)
        license_path.write_text(sides['head']['text'])

    frozen_diff = patch(repo, base, head)
    snapshot = validate(dict(
        schema='mea-snapshot-v1', id=case,
        pr=dict(url=pr['html_url'], repository=base_repo, number=pr['number'], title='', body=''),
        revisions=dict(base=base, head=head, target_base=target_at_merge),
        provenance=dict(kind='KNOWN_HISTORICAL_DEVELOPMENT', captured_at=datetime.now(timezone.utc).isoformat(),
                        state_at_capture='closed', merged_at_capture=True, pending_review_version=False,
                        capture_sha256=digest(frozen_diff), source_authentication='NOT_INDEPENDENTLY_VERIFIED'),
        scope=dict(reported_changed_files=len(files), complete_listing=True, omitted_sources=[]), files=files))
    write(root / 'inputs' / f'{case}.json', snapshot)
    (root / 'inputs' / f'{case}.diff').write_bytes(frozen_diff)
    (root / 'inputs' / f'{case}.upstream.diff').write_bytes(upstream_diff)
    gold = []
    if mutation:
        file = next(f for f in files if f['path'] == mutation['path'])
        line = file['sides']['head']['text'].splitlines().index(mutation['after']) + 1
        allowed = dict(path=file['path'], side='head', revision=head, start=line, end=line)
        if not any(r['kind'] == 'addition' and r['head'] == line for r in parse(file['patch'], file['sides'])):
            raise ValueError('GOLD_OUTSIDE_ADDITION')
        gold.append(dict(id=case + ':D1', locations=[dict(path=file['path'], line=line)], anchors=[allowed],
                         summary=mutation['summary'], bodies=[mutation['behavior']], secret_marker=mutation['marker']))
    write(root / 'gold' / f'{case}.json', gold)
    return dict(case=case, repository=base_repo, number=pr['number'], source_url=pr['html_url'],
                merged_at=pr['merged_at'], historical_target=target_at_merge, orig=orig, base=base,
                upstream_merge=pr['merge_commit_sha'], clean=clean, head=head,
                upstream_tree=clean_tree, clean_tree=revision(repo, clean + '^{tree}'),
                frozen_tree=revision(repo, head + '^{tree}'), injected=bool(mutation),
                upstream_diff_sha256=digest(upstream_diff), upstream_lines=upstream_lines,
                frozen_git_diff_sha256=digest(frozen_diff), input_sha256=digest(snapshot),
                file_sha256=digest((root / 'inputs' / f'{case}.json').read_bytes()),
                files=[{**{k: f[k] for k in ('path', 'base_path', 'status')},
                        'sides': {side: {k: s[k] for k in ('sha256', 'blob_sha')} for side, s in f['sides'].items()},
                        'hunk_sha256': digest(f['patch'].encode()) if f['patch'] else None} for f in files],
                licenses=license_files, changed_lines=len([r for f in files if f['patch']
                    for r in parse(f['patch'], f['sides']) if r['kind'] in {'addition', 'deletion'}]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('private', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    root = args.private.resolve(strict=True)
    if (root / 'freeze.json').exists():
        raise SystemExit('FREEZE_ALREADY_EXISTS')
    cases = read(root / 'sample-plan.json')
    if len(cases) != 4 or len({c['case'] for c in cases}) != 4:
        raise ValueError('FOUR_DISTINCT_DEV_CASES_REQUIRED')
    write(root / 'freeze.json', [freeze(root, case) for case in cases])


if __name__ == '__main__':
    main()
