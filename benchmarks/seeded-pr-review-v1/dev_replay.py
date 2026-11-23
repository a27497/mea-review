"""Regression checks against the sealed failed pilot. No transport calls."""

import argparse
import json
import os
from pathlib import Path

import paired
from freeze import canonical, digest, read, write
from pilot import direct_request, direct_result, diff_list
from mea.investigation import native, real_protocol


def check(phase, source):
    snapshots = paired.load_inputs(source, 'dev')
    diffs = paired.preflight(snapshots)
    recorded = source / 'live'
    out = phase / 'dev-replay'
    out.mkdir(mode=0o700, exist_ok=False)
    rows = []
    for case, snapshot in snapshots.items():
        view, _, evidence = paired.base_payload(snapshot)
        journal = read(recorded / case / 'agent/journal.json')
        direct = read(recorded / case / 'direct/01.output.json')
        plan_error = journal['reason']
        if plan_error == 'TOOL_NOT_ALLOWED':
            r = journal['requests'][-1]
            try:
                native.parse_batch(r['receipt']['message'], parameters=real_protocol.request_parameters(journal, r))
            except native.Invalid as exc:
                if str(exc) != 'NATIVE_FUNCTION_NOT_ALLOWED':
                    raise
            else:
                raise ValueError('REGRESSION_TERMINAL_NATIVE_REFUSAL')
            rechecked = paired.final_result(r['receipt'], view, evidence, 'agent')
            if rechecked['reason'] != 'UNEXPECTED_TERMINAL_ENVELOPE':
                raise ValueError('REGRESSION_TERMINAL_TOOL_EXECUTION')
        elif plan_error == 'OUTPUT_BUDGET':
            applied = {r['tool_call_id'] for r in journal['events'] if r['kind'] == 'tool_result'}
            discarded, = [e for e in journal['events'] if e['kind'] == 'tool_started' and e['tool_call_id'] not in applied]
            raw = view.execute(discarded['tool'], discarded['args'])
            old_bytes = len(canonical(raw)) + len(canonical(real_protocol.tool_message(discarded['tool_call_id'], raw)))
            page, _, _ = paired.project_page(view, discarded, 1)
            if (journal['usage']['output_bytes'] + old_bytes <= journal['budget']['output_bytes'] or
                    len(canonical(page)) > paired.PAGE_BYTES):
                raise ValueError('REGRESSION_TOOL_OUTPUT_BOUND')
        else:
            raise ValueError('UNKNOWN_OLD_AGENT_STATUS')
        _, old_view, old_visible = direct_request(snapshot)
        if direct_result(direct, old_view, old_visible)['status'] != 'STOPPED':
            raise ValueError('REGRESSION_OLD_DIRECT_STATUS')
        mock = {'plan': journal['requests'][0]['receipt'], 'finish': direct, 'direct': direct}
        for arm in paired.ARMS:
            folder = out / case / arm
            exchange = paired.Exchange(folder, 'dev-offline', case, arm, diff_list(snapshot), None, None)
            result = paired.review_cell(snapshot, folder, arm, exchange, mock=mock)
            if result['status'] != 'COMPLETED' or any(e['posts'] for e in exchange.events):
                raise ValueError('REGRESSION_PAIRED_EXECUTION')
            if arm == 'agent':
                closing = read(folder / '02-finish/input.json')
                if 'tools' in closing or 'tool_choice' in closing or len(closing['messages']) != 2:
                    raise ValueError('REGRESSION_TOOL_FREE_FINISH')
            if arm == 'direct':
                source_candidates = json.loads(direct['message']['content'])['candidates']
                for c, origin in zip(result['candidates'], source_candidates, strict=True):
                    coord = origin['change']
                    if (c['path'], c['side'], c['line'], c['detail']['revision']) != (
                            coord.get('path'), coord.get('side'), coord.get('line'), coord.get('revision')):
                        raise ValueError('REGRESSION_COORDINATE_FABRICATION')
            write(folder / 'result.json', result)
            rows.append(dict(case=case, arm=arm, old_agent_terminal=plan_error, status=result['status'],
                             views=[e['body_sha256'] for e in exchange.events], posts=0))
    write(phase / 'dev-offline.json', dict(status='PASS', cells=rows, conservative_input_bounds=diffs,
          configuration_sha256=digest(paired.configuration()), freeze_sha256=digest(read(source / 'freeze.json')),
          whole_phase_peak_nano=paired.FULL_PLAN_PEAK, posts=0))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', type=Path)
    parser.add_argument('source', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    check(args.phase.resolve(strict=True), args.source.resolve(strict=True))
