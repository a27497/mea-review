"""One isolated ReviewBench arm over a validated, hash-locked snapshot."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import direct_contract
from mea import review
from mea.contract import canonical, decode, digest, validate
from mea.investigation import context_budget, real_protocol as protocol
from mea.investigation.decision import stop, tool
from mea.investigation.mock_provider import Sender
from mea.investigation.provider_view import View
from mea.investigation.validation import validate_candidates
from mea.patch import parse

DIRECT_PROMPT = direct_contract.PROMPT.replace(
    'the exact supplied BASE/HEAD sources', 'the exact supplied BASE/HEAD unified diff')
DIRECT_PROMPT += '\nCite one changed line per finding, with the unified-diff prefix removed.'
MOCK_TEXT = 'Synthetic offline contract exercise'
BUDGET = protocol.budget({'tokens': 60000})
ARMS = ('static', 'direct', 'agent')


def new_task(view):
    return dict(view=view.value, identity=dict(view_sha256=view.sha256, policy_version=protocol.POLICY),
                goal=protocol.GOAL, budget=BUDGET, requests=[], results=[], candidates=[], rejected=[],
                usage=dict(rounds=0, tools=0, output_bytes=0, tokens_accounted=0))


EVIDENCE_KEYS = ('revisions', 'objective', 'files', 'initial_diffs', 'coverage')


def production_path(path):
    parts = path.lower().split('/')
    name = parts[-1]
    return (name.endswith('.py') and name not in {'setup.py', 'conftest.py'}
            and not name.startswith('test_') and not name.endswith('_test.py')
            and not set(parts[:-1]) & {'test', 'tests', 'testing', 'docs', 'examples', 'fixtures', '.github'})


def initial_requests(view):
    task = new_task(view)
    agent, reserved, _ = protocol.prepare_request(task)
    context = json.loads(agent['messages'][1]['content'])
    evidence = {key: context[key] for key in EVIDENCE_KEYS}
    patches = {f['path']: f['patch'] for f in view.value['files']}
    if ({f['path'] for f in evidence['initial_diffs']} != patches.keys()
            or len(evidence['initial_diffs']) != len(patches)):
        raise ValueError('INITIAL_PATH_OR_OMISSION_MISSING')
    for item in evidence['initial_diffs']:
        patch_text = patches[item['path']]
        if ('diff' in item and item['diff'] != patch_text or 'diff' not in item
                and (not item.get('omission') or item.get('content_sha256') != digest(patch_text.encode()))):
            raise ValueError('INITIAL_DIFF_PROJECTION_MISMATCH')
    direct = {key: agent[key] for key in ('model', 'thinking', 'temperature', 'max_tokens', 'stream')}
    direct.update(messages=[dict(role='system', content=DIRECT_PROMPT),
                            dict(role='user', content=canonical(evidence).decode())],
                  response_format=dict(type='json_object'))
    direct_tokens = context_budget.estimate(dict(direct, tools=[]))['input_reservation'] + protocol.OUTPUT_TOKENS
    if (len(canonical(direct)) > direct_contract.MAX_INPUT_BYTES or direct_tokens > BUDGET['tokens']
            or reserved > BUDGET['tokens']):
        raise ValueError('DIRECT_OR_AGENT_INITIAL_REQUEST_BOUND')
    return direct, agent


def visible_body(body):
    """The P0 citation validator consumes only the actually visible patches."""
    evidence = json.loads(body['messages'][1]['content'])
    evidence['initial_diffs'] = [f for f in evidence['initial_diffs'] if 'diff' in f]
    return dict(body, messages=[body['messages'][0], dict(role='user', content=canonical(evidence).decode())])


def agent_plan(context):
    feedback = context['tool_feedback']
    head = context['revisions']['head']
    text = MOCK_TEXT
    patches = [f for f in context['initial_diffs'] if 'diff' in f and production_path(f['path'])]
    anchor = next(((f['path'], row['head']) for f in patches for row in parse(f['diff'], {})
                   if row['kind'] == 'addition'), None)
    omitted = {f['path'] for f in context['initial_diffs'] if 'diff' not in f}
    head_paths = [f['path'] for f in context['files'] if 'head' in f['available_sides']]
    if not feedback:
        extra = next((path for path in head_paths if path in omitted), None)
        if anchor and extra:
            return tool('read_source', head, text, path=extra, start=1, end=2)
        if not anchor:
            candidate = next((path for path in head_paths if production_path(path)), None)
            if candidate:
                return tool('read_diff', head, text, path=candidate)
    elif not anchor:
        refs = next((r['evidence'] for r in feedback if r['tool'] == 'read_diff'), [])
        if refs:
            anchor = (refs[0]['path'], refs[0]['line'])
    if not anchor:
        return stop('EVIDENCE_INSUFFICIENT', text)
    path, line = anchor
    for item in (feedback[-1]['evidence'] if feedback else []):
        if (item['path'], item['line']) == anchor and feedback[-1]['tool'] == 'read_source':
            return stop('HYPOTHESIS_SUPPORTED', text, [
                dict(title=text, trigger=text, impact=text,
                     change=dict(path=path, side='head', line=line, revision=head),
                     evidence_ids=[item['id']])])
    if feedback and feedback[-1]['tool'] == 'read_source' and len(feedback) >= 2:
        return stop('EVIDENCE_INSUFFICIENT', text)
    return tool('read_source', head, text, path=path, start=line, end=line)


def first_addition(evidence):
    for diff in evidence['initial_diffs']:
        for row in parse(diff['diff'], {}):
            if row['kind'] == 'addition':
                return diff['path'], row['head'], row['text'][1:]
    raise ValueError('HEAD_CHANGE_REQUIRED')


def validate_direct(body, raw):
    evidence = json.loads(body['messages'][1]['content'])
    parsed = decode(raw)
    direct_contract.schema_check(parsed, direct_contract.SCHEMA)
    status = parsed['status']
    if (bool(parsed['findings']) != (status == 'findings') or
            status == 'abstain' and not parsed['reason'].strip()):
        raise ValueError('DIRECT_STATUS_MISMATCH')
    available = {}
    for diff in evidence['initial_diffs']:
        for row in parse(diff['diff'], {}):
            if row['kind'] in {'addition', 'deletion'}:
                side = 'head' if row['kind'] == 'addition' else 'base'
                file = diff['path' if side == 'head' else 'base_path']
                available[side, file, row[side]] = row['text'][1:]
    accepted, rejected, seen = [], [], set()
    for row in parsed['findings']:
        side, file, start = row['side'], row['path'], row['start']
        anchor = (side, file, start)
        if (row['end'] != start or row['revision'] != evidence['revisions'][side]
                or available.get(anchor) != row['quote'] or anchor in seen):
            rejected.append(dict(finding=row, reason='DIRECT_CITATION_OR_DUPLICATE'))
        else:
            seen.add(anchor)
            accepted.append(finding(side, file, start, row['title'] + '. ' + row['impact']))
    return parsed, accepted, rejected


def finding(side, path, line, message):
    return dict(side=side, file=path, start_line=line, end_line=line, message=message)


def direct_mock(body):
    evidence = json.loads(body['messages'][1]['content'])
    path, line, quote = first_addition(evidence)
    if not quote:
        return canonical(dict(status='no_findings', reason=MOCK_TEXT, findings=[]))
    return canonical(dict(status='findings', reason=MOCK_TEXT, findings=[
        dict(category='correctness', severity='low', title=MOCK_TEXT, impact=MOCK_TEXT,
             path=path, side='head', revision=evidence['revisions']['head'],
             start=line, end=line, quote=quote)]))


def export(row, findings, arm):
    return dict(pr=dict(repo='https://github.com/' + row['repository'], pr_number=row['number'],
                        base=row['base'], head=row['head']),
                agent='mea-' + arm, findings=[
                    dict(producer='mea-' + arm, **{k: v for k, v in item.items() if k != 'side'})
                    for item in findings if item['side'] == 'head'])


def agent_mock(view, plan):
    # Reuse the native protocol and frozen-view tools. This harness owns no
    # product worker, authorization, key, network transport or spending DB.
    sender = Sender(plan=plan, delay=0)
    task = new_task(view)
    usage = task['usage']

    def outcome(status, reason):
        return dict(task, status=status, reason=reason)

    for index in range(BUDGET['rounds']):
        try:
            body, reservation, projection = protocol.prepare_request(task)
        except ValueError as exc:
            return outcome('STOPPED', str(exc))
        receipt = sender.collect(sender.start(body), protocol.REQUEST_SECONDS, lambda: False, lambda: None)
        if receipt.get('http_posts_attempted') != 0:
            raise ValueError('BENCHMARK_MOCK_TRANSPORT_REQUIRED')
        count = protocol.usage(receipt.get('usage'), reservation)
        usage['rounds'] += 1
        usage['tokens_accounted'] += count['total_tokens'] if count is not None else reservation
        usage['output_bytes'] += len(canonical(receipt))
        record = dict(id=f'mock-request-{index + 1}', state='RECEIVED', request_body=body,
                      request_body_sha256=digest(body), context_manifest=projection,
                      context_manifest_sha256=digest(projection), receipt=receipt,
                      tool_messages=[], result_indices=[], result_sha256s=[])
        task['requests'].append(record)
        if usage['output_bytes'] > BUDGET['output_bytes']:
            return outcome('STOPPED', 'OUTPUT_BUDGET')
        try:
            batch = protocol.decisions(receipt, protocol.prior_ids(task, index))
            for decision, _ in batch:
                if decision['kind'] == 'tool':
                    view.validate_call(decision['tool'], decision['args'])
        except (ValueError, KeyError, TypeError, RecursionError):
            return outcome('STOPPED', 'INVALID_MODEL_DECISION')
        if sum(d['kind'] == 'tool' for d, _ in batch) > BUDGET['tools'] - usage['tools']:
            return outcome('STOPPED', 'TOOL_BUDGET')
        record.update(decision_applied=True, native_calls=[call for _, call in batch])
        if len(batch) == 1:
            record['native_call'] = batch[0][1]
        for decision, call in batch:
            if decision['kind'] == 'finish':
                accepted, rejected = validate_candidates(view, decision['candidates'], task['results'], detailed=True)
                if usage['output_bytes'] + len(canonical([accepted, rejected])) > BUDGET['output_bytes']:
                    return outcome('STOPPED', 'OUTPUT_BUDGET')
                task.update(candidates=accepted, rejected=rejected)
                return outcome('COMPLETED', 'CANDIDATE_VALIDATION_REJECTED' if rejected else decision['reason'])
            result = view.execute(decision['tool'], decision['args'])
            feedback = protocol.tool_message(call['id'], result)
            size = len(canonical(result)) + len(canonical(feedback))
            if usage['output_bytes'] + size > BUDGET['output_bytes']:
                return outcome('STOPPED', 'OUTPUT_BUDGET')
            usage['output_bytes'] += size
            usage['tools'] += 1
            i = len(task['results'])
            task['results'].append(result)
            record['tool_messages'].append(feedback)
            record['result_indices'].append(i)
            record['result_sha256s'].append(result['content_sha256'])
            if len(batch) == 1:
                record.update(tool_message=feedback, result_index=i, result_sha256=result['content_sha256'])
    return outcome('STOPPED', 'REQUEST_BUDGET')


def execute(value, arm):
    if arm == 'static':
        result = review.review(value)
        findings = [finding('head', row['path'], row['line'], row['title'] + '. ' + row['message'])
                    for row in result['findings']]
        return dict(mode='STATIC_RULES_NOT_AI', status='COMPLETED', reason='STATIC_RULES_COMPLETE',
                    raw=result, findings=findings, rejected=[], request_count=0, tool_count=0)
    view = View.from_input(value)
    direct, initial_agent = initial_requests(view)
    if arm == 'direct':
        visible = visible_body(direct)
        try:
            raw = direct_mock(visible)
        except ValueError as exc:
            if str(exc) != 'HEAD_CHANGE_REQUIRED':
                raise
            raw = canonical(dict(status='abstain', reason='No changed HEAD line in the initial projection',
                                 findings=[]))
        parsed, findings, rejected = validate_direct(visible, raw)
        return dict(mode='MOCK_CONTRACT_NOT_AI', status='COMPLETED', reason=parsed['status'],
                    raw=parsed, request_body=direct, findings=findings, rejected=rejected,
                    request_count=1, tool_count=0)
    task = agent_mock(view, agent_plan if arm == 'agent' else invalid_plan)
    if not task['requests'] or task['requests'][0]['request_body'] != initial_agent:
        raise ValueError('AGENT_INITIAL_REQUEST_DRIFT')
    findings = [finding(c['change']['side'], c['change']['path'], c['change']['line'],
                        '. '.join(c[k] for k in ('title', 'trigger', 'impact'))) for c in task['candidates']]
    return dict(mode='MOCK_PROVIDER_NOT_AI', status=task['status'], reason=task['reason'],
                raw=task, findings=findings, rejected=task['rejected'], request_count=len(task['requests']),
                tool_count=task['usage']['tools'])


def invalid_plan(context):
    return tool('read_source', context['revisions']['head'], MOCK_TEXT,
                path=context['files'][0]['path'], start=2, end=1)


def guard(snapshot):
    def hook(event, values):
        if event.startswith('socket.') or event in {'subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn'}:
            raise RuntimeError('OFFLINE_EXTERNAL_OPERATION_BLOCKED')
        if event == 'open' and isinstance(values[0], (str, bytes)):
            path = Path(values[0] if isinstance(values[0], str) else values[0].decode()).resolve()
            mode = values[1]
            if path == snapshot and mode in ('r', 'rb'):
                return
            # The only additional files available to the arm are Python modules.
            python_roots = (ROOT / 'mea', Path(sys.base_prefix) / 'lib')
            if path.suffix in ('.py', '.pyc') and mode in ('r', 'rb') and any(
                path.is_relative_to(root) for root in python_roots
            ):
                return
            raise RuntimeError('OFFLINE_DATA_ACCESS_BLOCKED')
    return hook


def isolation_probe(snapshot):
    import socket
    blocked = []
    for action in (lambda: (snapshot.parent.parent / 'judge-only' / 'probe.json').read_bytes(), socket.socket):
        try:
            action()
        except RuntimeError as exc:
            blocked.append(str(exc))
    return {'blocked': blocked}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('arm', choices=(*ARMS, 'invalid', 'isolation'))
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('sha256')
    args = parser.parse_args()
    if args.snapshot.is_symlink():
        raise ValueError('PLAIN_SNAPSHOT_REQUIRED')
    snapshot = args.snapshot.resolve(strict=True)
    if snapshot.parent.name != 'inputs' or not snapshot.is_file():
        raise ValueError('RUNNER_INPUT_BOUNDARY')
    sys.addaudithook(guard(snapshot))
    value = validate(decode(snapshot.read_bytes()))
    if digest(value) != args.sha256:
        raise ValueError('RUNNER_INPUT_HASH_MISMATCH')
    result = isolation_probe(snapshot) if args.arm == 'isolation' else execute(value, args.arm)
    result.update(id=value['id'], snapshot_sha256=args.sha256, model_requests=0, judge_requests=0)
    sys.stdout.buffer.write(canonical(result) + b'\n')


if __name__ == '__main__':
    main()
