"""Create the answer-free release manifest and verify its exact public head."""

import argparse
import base64
import os
import secrets
import shutil
import subprocess
from collections import Counter
from pathlib import Path

from freeze import ROOT, canonical, digest, read, write
from paired import (ARMS, MODEL, REQUEST_PEAK, DurableLedger, Exchange, base_payload,
                    configuration, diff_list, load_inputs, preflight, review_cell)
from mea.investigation import spending

REPO = 'a27497/mea-review'
DIR = 'benchmarks/seeded-pr-review-v1'
PUBLIC = f'{DIR}/FROZEN-MANIFEST.json'
PROTOCOL = f'{DIR}/PROTOCOL.md'
MAIN = '432e5e70a14a3d0cd6c767100e0374dc37e41961'


def offline_review(phase, snapshots):
    results = []
    out = phase / 'holdout-offline'
    out.mkdir(mode=0o700, exist_ok=False)
    for case, snapshot in snapshots.items():
        view, _, _ = base_payload(snapshot)
        side, path = next(iter(sorted(view.sources)))
        value = dict(path=path, revision=snapshot['revisions'][side], start=1, end=120, summary='Offline page bound')
        plan = dict(model=MODEL, finish_reason='tool_calls', message=dict(role='assistant', content=None, tool_calls=[
            dict(id='fixture-call', type='function', function=dict(name='read_source', arguments=canonical(value).decode()))]))
        final = dict(model=MODEL, finish_reason='stop', message=dict(role='assistant', content='{"candidates":[]}'))
        mock = dict(plan=plan, finish=final, direct=final)
        for arm in ARMS:
            directory = out / case / arm
            exchange = Exchange(directory, 'holdout-offline', case, arm, diff_list(snapshot), None, None)
            cell = review_cell(snapshot,directory,arm,exchange,mock=mock)
            if cell['status'] != 'COMPLETED' or any(e['posts'] for e in exchange.events):
                raise ValueError('HOLDOUT_OFFLINE_TOOL_REPLAY')
            results.append(dict(case=case,arm=arm,status=cell['status'],requests=exchange.events))
    write(phase/'holdout-offline.json', dict(status='PASS', cells=results, posts=0))


def seal(phase, private, dev):
    rows = read(private/'freeze.json')
    snapshots = load_inputs(private,'holdout')
    bounds = preflight(snapshots)
    if read(private/'preflight.json')['bounds'] != bounds:
        raise ValueError('PREFLIGHT_DIFF_BOUND')
    cfg = configuration()
    dev_run = read(phase/'dev-live/summary.json')
    gates = [
        dev_run['status'] == 'PASS',
        dev_run['complete_pairs'] == 4,
        digest(cfg) == dev_run['config_sha256'],
        read(phase/'dev-leak-audit.json')['status']=='PASS',
        (phase/'dev-score/replay-1.json').read_bytes() == (phase/'dev-score/replay-2.json').read_bytes(),
    ]
    if not all(gates):
        raise ValueError('DEV_RELEASE_GATE')
    previous = read(dev/'freeze.json')
    if any({r[field] for r in rows} & {r[field] for r in previous}
           for field in ('repository','orig','clean_tree','upstream_diff_sha256')):
        raise ValueError('DEV_HOLDOUT_OVERLAP')
    if any(len({r[field] for r in rows})!=24 for field in ('orig','clean_tree','upstream_diff_sha256')):
        raise ValueError('HOLDOUT_DUPLICATE_IDENTITY')
    assertions, hashes = [], {}
    gold_counts = Counter()
    for row in rows:
        name = row['case']
        receipt_path = private/'assertions'/name/'receipt.json'
        receipt = read(receipt_path)
        if receipt['status']!='PASS':
            raise ValueError('ASSERTION_GATE')
        assertions.extend(receipt['phases'])
        gold = read(private/'gold'/f'{name}.json')
        view, _, _ = base_payload(snapshots[name])
        for defect in gold:
            a, = defect['anchors']
            if (a['start'] != a['end'] or a['revision'] != row['head'] or
                    (a['side'],a['path'],a['start']) not in view.changes):
                raise ValueError('GOLD_ANCHOR_GATE')
        gold_counts.update({row['repository']:len(gold)})
        hashes[name] = dict(input=row['file_sha256'], diff=row['frozen_git_diff_sha256'],
                            gold=digest(gold), assertions=digest(receipt_path.read_bytes()))
    if sorted(gold_counts.values()) != [8]*4:
        raise ValueError('GOLD_QUOTAS')
    peak = DurableLedger(phase/'phase-ledger.jsonl', cfg['phase_cap_nano']-cfg['prior_peak_nano']).spent_peak()
    total = peak + cfg['prior_peak_nano'] + 72 * REQUEST_PEAK
    if total > cfg['phase_cap_nano']:
        raise ValueError('CASH_GATE')
    offline_review(phase,snapshots)
    evidence = dict(salt=secrets.token_hex(32), cases=hashes, freeze_sha256=digest(rows),
                    exclusion_sha256=digest((phase/'holdout/ranked-pool.json').read_bytes()),
                    selection_sha256=digest((phase/'holdout/selection.json').read_bytes()),
                    bounds=bounds, config_sha256=digest(cfg),
                    dev_summary_sha256=digest(dev_run), offline_sha256=digest((phase/'holdout-offline.json').read_bytes()),
                    scoring_policy_sha256=digest((ROOT/PROTOCOL).read_bytes()))
    write(phase/'holdout-private-seal.json', evidence)
    reviewer_input = phase/'review-inputs'
    (reviewer_input/'inputs').mkdir(mode=0o700,parents=True,exist_ok=False)
    shutil.copyfile(private/'freeze.json',reviewer_input/'freeze.json')
    for case in snapshots:
        for suffix in ('.json','.diff'):
            name=case+suffix
            shutil.copyfile(private/'inputs'/name,reviewer_input/'inputs'/name)
    if load_inputs(reviewer_input,'holdout')!=snapshots:
        raise ValueError('ISOLATED_INPUT_COPY')
    public = dict(schema='phase10-holdout-freeze-v1', cohort='holdout',
                  status_at_freeze='NOT_RUN', paired_prs=24, planned_cells=72, seeded_defects=32,
                  repositories=[dict(repository=repo, prs=6, single_injection=2, double_injection=3,
                                     unchanged_controls=1, seeded_defects=8) for repo in sorted(gold_counts)],
                  seed=20261120, private_commitment_sha256=digest(evidence),
                  scoring_policy_sha256=evidence['scoring_policy_sha256'], configuration=cfg,
                  dev_completed_pairs=4, offline_cells=72,
                  assertion_checks=len(assertions), isolated_expected_failures=sum(r['expected']==17 for r in assertions),
                  holdout_cash_ceiling_nano=72*REQUEST_PEAK, phase_ceiling_at_freeze_nano=total)
    write(ROOT/PUBLIC,public)


def gh(*args):
    return subprocess.check_output(['gh',*args],cwd=ROOT)


def verify_remote(phase, pr_number, ci_id):
    import json
    def git_bytes(*args):
        return subprocess.check_output(['git',*args],cwd=ROOT)
    head=git_bytes('rev-parse','HEAD').decode().strip()
    pr=json.loads(gh('pr','view',str(pr_number),'--repo',REPO,'--json','headRefOid,baseRefName,url,state'))
    ci=json.loads(gh('run','view',str(ci_id),'--repo',REPO,'--json','headSha,workflowName,event,status,conclusion,jobs,url'))
    if (pr['headRefOid']!=head or pr['baseRefName']!='main' or pr['state']!='OPEN' or
            ci['headSha']!=head or ci['workflowName']!='CI' or ci['event']!='pull_request' or
            ci['conclusion']!='success' or not any(j['name']=='offline-tests' and j['conclusion']=='success' for j in ci['jobs'])):
        raise ValueError('REMOTE_CHECKS_GATE')
    identities = {}
    paths=git_bytes('diff','--name-only',MAIN,head).decode().splitlines()
    if set(paths)-{p for p in paths if p.startswith(DIR+'/')} or PUBLIC not in paths or PROTOCOL not in paths:
        raise ValueError('UNAPPROVED_PUBLIC_SCOPE')
    for path in paths:
        remote=json.loads(gh('api',f'repos/{REPO}/contents/{path}?ref={head}'))
        local=git_bytes('show',head+':'+path)
        if base64.b64decode(remote['content']) != local:
            raise ValueError('GITHUB_FILE_BINDING')
        identities[path]=digest(local)
    manifest=read(ROOT/PUBLIC)
    private=read(phase/'holdout-private-seal.json')
    if manifest['private_commitment_sha256'] != digest(private):
        raise ValueError('PRIVATE_FREEZE_COMMITMENT')
    write(phase/'holdout-release.json',dict(status='VERIFIED',head=head,pr=pr['url'],ci=ci['url'],
        config_sha256=private['config_sha256'],freeze_sha256=private['freeze_sha256'],
        dev_summary_sha256=private['dev_summary_sha256'], local_and_github_sha256=identities,
        manifest_sha256=identities[PUBLIC],protocol_sha256=identities[PROTOCOL]))


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    commands=parser.add_subparsers(dest='action',required=True)
    build=commands.add_parser('seal')
    build.add_argument('phase',type=Path)
    build.add_argument('private',type=Path)
    build.add_argument('dev',type=Path)
    remote=commands.add_parser('verify')
    remote.add_argument('phase',type=Path)
    remote.add_argument('pr_number',type=int)
    remote.add_argument('ci_id',type=int)
    args=parser.parse_args()
    os.umask(0o077)
    if args.action=='seal':
        seal(*(p.resolve(strict=True) for p in (args.phase,args.private,args.dev)))
    else:
        verify_remote(args.phase.resolve(strict=True),args.pr_number,args.ci_id)
