"""Audit exact wire inputs and replay the bounded tool journal without a provider."""

import argparse
import json
import os
import re
from pathlib import Path

from freeze import canonical, digest, read, write
from pilot import CASES, arms, diff_list, load_inputs, request_bound
from mea.investigation import real_protocol as protocol

CREDENTIAL_PATTERN = re.compile(r'\b(?:sk-[a-zA-Z0-9_-]{20,}|gh[pousr]_[a-zA-Z0-9]{20,})\b')


def verify_body(case, arm, body, snapshot, forbidden):
    raw = canonical(body).decode()
    if any(piece in raw for piece in forbidden) or CREDENTIAL_PATTERN.search(raw):
        raise ValueError(f'FORBIDDEN_CONTENT_{case}_{arm}')
    user = json.loads(body['messages'][1]['content'])
    if set(user) & {'pr', 'pull_request', 'title', 'body', 'gold', 'gold_id', 'credentials'}:
        raise ValueError(f'UNAUTHORIZED_METADATA_{case}_{arm}')
    request_bound(body, snapshot, arm)
    if arm == 'agent':
        view = arms.direct_view(snapshot)
        for group in user['observed_evidence']:
            for _, number, ref, quote in group['rows']:
                citation = view.citation(group['side'], group['path'], number)
                if (citation['revision'] != group['revision'] or citation['id'] != ref or
                        citation['quote'] != quote or citation['blob_sha'] != group['blob_sha']):
                    raise ValueError('MODEL_EVIDENCE_SOURCE')
    elif len(body['messages']) != 2 or 'tools' in body:
        raise ValueError('DIRECT_INDEPENDENCE')
    return dict(request_sha256=digest(body), diff_sha256=digest(user['initial_diffs']),
                diff_lines=sum(len(f['diff'].splitlines()) for f in user['initial_diffs']))


def audit(root, mode):
    snapshots = load_inputs(root)
    gold = [g for case in CASES for g in read(root / 'gold' / f'{case}.json')]
    forbidden = [g[k] for g in gold for k in ('id', 'secret_marker', 'summary')]
    forbidden += [line for g in gold for line in g['bodies']]
    for case in CASES:
        forbidden += [f'PHASE10_{case[-1].upper()}_{suffix}' for suffix in ('PASS', 'OBSERVED')]
        spec = next(s for s in read(root / 'sample-plan.json') if s['case'] == case)
        metadata = read(root / spec['metadata'])
        forbidden.append(metadata['html_url'])
        if len(metadata['title']) > 25:
            forbidden.append(metadata['title'])
    directory = root / mode
    rows = []
    for case, snapshot in snapshots.items():
        for arm in ('agent', 'direct'):
            cell = directory / case / arm
            result = read(cell / 'result.json')
            inputs = sorted(cell.glob('*.input.json'))
            wire = []
            for path in inputs:
                verified = verify_body(case, arm, read(path), snapshot, forbidden)
                admission = read(path.with_name(path.name.replace('.input.', '.admission.')))
                if admission['body_sha256'] != verified['request_sha256']:
                    raise ValueError('ADMISSION_INPUT_BINDING')
                wire.append(verified)
            tool_checks = []
            gaps = []
            uncommitted = []
            if arm == 'agent' and (cell / 'journal.json').exists():
                task = read(cell / 'journal.json')
                view = protocol.view_class(task)(task['view'])
                if view.value != arms.direct_view(snapshot).value:
                    raise ValueError('UNFROZEN_VIEW')
                receipts = {r['request_body_sha256']: r for r in task['requests']}
                if any(r['request_sha256'] not in receipts for r in wire):
                    raise ValueError('JOURNAL_WIRE_BINDING')
                started = {e['tool_call_id']: e for e in task['events'] if e['kind'] == 'tool_started'}
                for event in (e for e in task['events'] if e['kind'] == 'tool_result'):
                    r = task['results'][event['result_index']]
                    action = started[event['tool_call_id']]
                    if (r != view.execute(action['tool'], action['args']) or
                            r['content_sha256'] != event['content_sha256']):
                        raise ValueError('TOOL_REPLAY_MISMATCH')
                    if any(part in canonical(r).decode() or part in canonical(action).decode() for part in forbidden):
                        raise ValueError('FORBIDDEN_TOOL_CONTENT')
                    tool_checks.append(dict(call_id=event['tool_call_id'], tool=action['tool'], args=action['args'],
                                            result_sha256=r['content_sha256'], revision=r['revision']))
                    if r['unknown'] or r['truncated'] or r.get('read', {}).get('continuation'):
                        gaps.append(dict(call_id=event['tool_call_id'], unknown=r['unknown'], truncated=r['truncated'],
                                         read=r.get('read')))
                for call_id in sorted(set(started) - {e['call_id'] for e in tool_checks}):
                    action = started[call_id]
                    discarded = view.execute(action['tool'], action['args'])
                    reply = protocol.tool_message(call_id, discarded)
                    size = len(canonical(discarded)) + len(canonical(reply))
                    if (task['reason'] != 'OUTPUT_BUDGET' or
                            task['usage']['output_bytes'] + size <= task['budget']['output_bytes'] or
                            any(part in canonical(discarded).decode() or part in canonical(action).decode()
                                for part in forbidden)):
                        raise ValueError('UNVERIFIED_STARTED_TOOL')
                    uncommitted.append(dict(tool=action['tool'], args=action['args'], call_id=call_id,
                                            reason='OUTPUT_BUDGET', output_bytes=size,
                                            result_sha256=discarded['content_sha256']))
            used = result.get('http_posts_attempted', 0)
            if mode == 'live' and used != len(wire):
                raise ValueError('WIRE_POST_COUNT')
            if wire and any(r['diff_sha256'] != digest(diff_list(snapshot)) for r in wire):
                raise ValueError('PAIR_DIFF_MISMATCH')
            rows.append(dict(case=case, arm=arm, requests=wire, http_posts=used,
                             tool_checks=tool_checks, uncommitted_tools=uncommitted, context_gaps=gaps))
    audit_doc = dict(status='PASS', mode=mode, rows=rows, checked_gold_markers=len(gold),
                     model_inputs=sum(len(r['requests']) for r in rows),
                     tool_outputs=sum(len(r['tool_checks']) for r in rows),
                     uncommitted_tools=sum(len(r['uncommitted_tools']) for r in rows), detected_leaks=0,
                     checks=['Exact frozen diffs, source lines and tool replay', 'No PR metadata fields or PR URLs',
                             'Exact private markers, gold IDs and answer text', 'Credential pattern scan'],
                     interpretation='No detected bundle leakage. Public pretraining exposure is unmeasured.')
    write(root / f'{mode}-leak-audit.json', audit_doc)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('private', type=Path)
    parser.add_argument('mode', choices=('offline', 'live'))
    args = parser.parse_args()
    os.umask(0o077)
    audit(args.private.resolve(strict=True), args.mode)


if __name__ == '__main__':
    main()
