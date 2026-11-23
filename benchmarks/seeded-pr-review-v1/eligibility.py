"""Bind the candidate order and known public/prior corpus exclusions."""

import argparse
import os
import random
import re
from pathlib import Path

from freeze import ROOT, digest, read, write

SEED = 20261120
PR = re.compile(r'github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)', re.I)


def corpus_material(root, prior_path):
    prior = read(prior_path)
    from_prior = {s.lower() for s in prior['repositories']}
    ids = {s.lower() for s in prior['pull_requests']}
    qodo = root / 'qodo-100.jsonl'
    martian = sorted((root / 'martian-offline').glob('*.json'))
    known = [prior_path, qodo, *martian]
    if len(prior['repositories']) != 303 or len(qodo.read_text().splitlines()) != 100 or len(martian) != 5:
        raise ValueError('EXCLUSION_CORPUS_INCOMPLETE')
    for path in known[1:]:
        ids.update(f'{repo.lower()}#{number}' for repo, number in PR.findall(path.read_text()))
    if sum(len(read(p)) for p in martian) != 50:
        raise ValueError('MARTIAN_CORPUS_SIZE')
    # Prior corpus uses repository-level exclusion, including M2 and the local
    # ReviewBench PRs. Search its indexed repository spellings case-insensitively.
    repos = from_prior | {s.rsplit('#', 1)[0] for s in ids}
    return repos, ids, {str(p): digest(p.read_bytes()) for p in known}


def upstream_comparison(root, row):
    case = row['case']
    remote_path = root / f'{case}-upstream-files.json'
    remote = read(remote_path)
    text = (root / 'inputs' / f'{case}.upstream.diff').read_text()
    local = {}
    for block in re.split(r'^diff --git ', text, flags=re.M)[1:]:
        header, _, content = block.partition('\n')
        path = header.split(' b/', maxsplit=1)[1]
        _, _, hunks = content.partition('\n@@ ')
        if path in local or not hunks:
            raise ValueError('LOCAL_UPSTREAM_HUNKS')
        local[path] = '@@ ' + hunks

    def semantic_hunks(patch):
        return [line.rsplit(' @@', 1)[0] + ' @@' if line.startswith('@@ ') else line
                for line in patch.splitlines()]

    if (len(remote) != len(local) or {entry['filename'] for entry in remote} != set(local) or
            any(semantic_hunks(entry['patch']) != semantic_hunks(local[entry['filename']]) for entry in remote)):
        raise ValueError('GITHUB_UPSTREAM_HUNKS_MISMATCH')
    return dict(api_sha256=digest(remote_path.read_bytes()), compared_files=len(remote), status='MATCH',
                normalization='Ignore the optional function name on unified hunk headers.')


def eligibility(root, prior_path):
    repos, ids, sources = corpus_material(root, prior_path)
    dispositions = read(root / 'dispositions.json')
    chosen = {row['number']: row for row in read(root / 'freeze.json')}
    groups = []
    for name, full_name in [('humanize', 'python-humanize/humanize'), ('luxon', 'moment/luxon')]:
        source_log, identity_path = root / f'{name}-pulls.json', root / f'{name}-repo.json'
        source, identity = read(source_log), read(identity_path)
        if (len(source) != 30 or identity['full_name'] != full_name or identity['fork'] or
                identity.get('parent') or identity.get('source') or identity['license']['spdx_id'] != 'MIT'):
            raise ValueError('UPSTREAM_REPOSITORY_IDENTITY')
        aliases = set(dispositions[name]['aliases']) | {full_name.lower()}
        if repos & aliases:
            raise ValueError('EXCLUDED_REPOSITORY')
        excluded, ordered = [], []
        for pr in source:
            reason = None
            if not pr['merged_at']:
                reason = 'NOT_MERGED'
            elif not '2025-01-01' <= pr['merged_at'][:10] <= '2026-11-19':
                reason = 'MERGE_DATE'
            elif pr['base']['ref'] != identity['default_branch']:
                reason = 'TARGET_BRANCH'
            if reason:
                excluded.append(dict(number=pr['number'], reason=reason))
            else:
                ordered.append(pr['number'])
        ordered.sort()
        random.Random(SEED).shuffle(ordered)
        fitted = []
        for number in ordered:
            path = root / f'{name}-{number}.json'
            pr = read(path)
            disposition = dispositions[name]['ranks'][str(number)]
            if f'{full_name.lower()}#{number}' in ids:
                raise ValueError('EXCLUDED_PR')
            fitted.append(dict(number=number, disposition=disposition, changed_files=pr['changed_files'],
                               change_lines=pr['additions']+pr['deletions'],
                               candidate_metadata_sha256=digest(path.read_bytes()),
                               selected=chosen[number]['case'] if number in chosen else None))
        selected = [f['number'] for f in fitted if f['selected']]
        if len(selected) != 2 or sum(chosen[n]['injected'] for n in selected) != 1:
            raise ValueError('REPOSITORY_QUOTA')
        replacements = [f['number'] for f in fitted if f['disposition'].startswith('Reserve')]
        groups.append(dict(repository=full_name, aliases=sorted(aliases), default_branch=identity['default_branch'],
                           source_sha256=digest(source_log.read_bytes()), identity_sha256=digest(identity_path.read_bytes()),
                           repository_license='MIT', excluded=excluded, seeded_order=fitted, selected=selected,
                           reserves_preliminary=replacements))
    local = {str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in [
        ROOT / 'benchmarks/reviewbench/c1/main-cells.json', ROOT / 'benchmarks/reviewbench/samples/manifest.json']}
    write(root / 'eligibility.json', dict(status='PASS', seed=SEED,
          selection_scope='Latest 30 closed PRs updated at acquisition, independently sorted and shuffled within repository.',
          groups=groups, indexed_repositories=len(repos), indexed_prs=len(ids), exclusion_sources=sources,
          local_reference_digests=local, martian_revision=(root / 'martian-revision').read_text().strip(),
          upstream_comparisons={row['case']: upstream_comparison(root, row) for row in chosen.values()},
          limitations=['Historical lineage is based on repository aliases and public fork metadata.',
                       'No universal historical tree/diff-hash index exists for the known exclusion corpora.']))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('private', type=Path)
    parser.add_argument('prior_exclusions', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    eligibility(args.private.resolve(strict=True), args.prior_exclusions.resolve(strict=True))


if __name__ == '__main__':
    main()
