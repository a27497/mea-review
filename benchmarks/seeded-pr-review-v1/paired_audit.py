"""Independently reconstruct Phase 10 provider requests and bounded tool replies."""

import argparse
import os
from pathlib import Path

from audit import CREDENTIAL_PATTERN
from freeze import canonical, digest, read, write
from paired import (ARMS, base_payload, bounded, diff_list, load_inputs, plan_tools, project_page,
                    request, review, visible_tool_payload)


def audit(phase, source, cohort):
    snapshots = load_inputs(source, cohort)
    root = phase / f'{cohort}-live'
    rows = []
    for case, snapshot in snapshots.items():
        gold = read(source / 'gold' / f'{case}.json')
        forbidden = [snapshot['pr']['url']]
        for g in gold:
            forbidden.extend([g['id'], g['secret_marker'], g['summary'], *g['bodies']])
        view, base, _ = base_payload(snapshot)
        initial = diff_list(snapshot)
        for arm in ARMS:
            cell = root / case / arm
            result = read(cell / 'result.json')
            if result['input_sha256'] != digest(snapshot) or result['diff_sha256'] != digest(initial):
                raise ValueError('RESULT_INPUT_MISMATCH')
            if arm == 'static':
                if read(cell / 'raw.json') != review(snapshot):
                    raise ValueError('STATIC_REPLAY_MISMATCH')
                rows.append(dict(case=case, arm=arm, requests=[], tools=[], complete_diff=True))
                continue
            dirs = sorted(cell.glob('0*-*'))
            if result['posts'] != len(dirs):
                raise ValueError('UNRESOLVED_WIRE_REQUEST_COUNT')
            expected = {}
            projected = []
            tool_receipts = []
            if arm == 'agent' and dirs:
                plan = dict(base, available_files=[dict(path=f['path'], base_path=f['base_path'],
                                                        sides=sorted(f['sides'])) for f in snapshot['files']])
                expected['01-plan'] = request(plan, 'plan')
                plan_reply = read(cell / '01-plan/receipt.json')
                parsed, note = plan_tools(plan_reply, view)
                for i, (decision, call) in enumerate(parsed, 1):
                    page, raw, _ = project_page(view, decision, i)
                    recorded = read(cell / 'tools' / f'{i:02}.json')
                    if recorded != dict(id=call['id'], original=decision, page=page, result=raw):
                        raise ValueError('TOOL_LOG_MISMATCH')
                    projected.append(page)
                    tool_receipts.append(dict(id=call['id'], page=page, raw_sha256=raw['content_sha256']))
                    scan(case, recorded, forbidden)
                if len(dirs) == 2:
                    finish = visible_tool_payload(dict(base, tool_plan_status=note), projected)
                    expected['02-finish'] = request(finish, 'finish')
                if len(list((cell/'tools').glob('*.json'))) != len(projected):
                    raise ValueError('UNEXPECTED_TOOL_TRACE')
            elif arm == 'direct':
                expected['01-direct'] = request(base, 'direct')
            requests = []
            for req in dirs:
                body = read(req / 'input.json')
                intent = read(req / 'intent.json')
                receipt = read(req / 'receipt.json')
                scan(case, body, forbidden)
                if (expected.get(req.name) != body or digest(body) != intent['body_sha256'] or
                        bounded(body, initial) != intent['input_bound']):
                    raise ValueError('REQUEST_RECONSTRUCTION_MISMATCH')
                requests.append(dict(request_sha256=digest(body), receipt_sha256=digest(receipt),
                                     input_bound=intent['input_bound'], provider_posts=receipt['http_posts_attempted']))
            rows.append(dict(case=case, arm=arm, requests=requests, tools=tool_receipts, complete_diff=bool(requests)))
    write(phase / f'{cohort}-leak-audit.json', dict(status='PASS', cohort=cohort, rows=rows,
          reconstructed_model_requests=sum(len(r['requests']) for r in rows),
          reconstructed_tool_results=sum(len(r['tools']) for r in rows), detected_leaks=0))


def scan(case, record, forbidden):
    encoded = canonical(record).decode()
    if any(s in encoded for s in forbidden if s) or CREDENTIAL_PATTERN.search(encoded):
        raise ValueError('FORBIDDEN_CONTENT_' + case)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', type=Path)
    parser.add_argument('source', type=Path)
    parser.add_argument('cohort', choices=('dev', 'holdout'))
    args = parser.parse_args()
    os.umask(0o077)
    audit(args.phase.resolve(strict=True), args.source.resolve(strict=True), args.cohort)
