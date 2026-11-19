"""Load the authored Round 3 examples into the existing offline review store."""
import time
from pathlib import Path

from ..contract import canonical, decode, digest
from ..investigation.tasks import SCHEMA, event, limits
from ..investigation.tools import NOTICE, TOOL_VERSION, View
from ..investigation.validation import RESULT_VERSION, validate_candidates

PACK = Path(__file__).resolve().parents[2] / 'fixtures/risk-review/demo-prs.json'
MODE = 'AUTHORED_OFFLINE_DEMO'


def read_reference(view, ref):
    side, path, revision = (ref[key] for key in ('side', 'path', 'revision'))
    if side not in ('base', 'head') or revision != view.value['revisions'][side]:
        raise ValueError('DEMO_REFERENCE_VERSION')
    if ref['availability'] == 'MISSING':
        if ((side, path) in view.sources or any(ref[key] is not None for key in ('start', 'end', 'quote'))
                or not any(row['path'] == path and row['side'] == side
                           for row in view.value['coverage']['omitted_sources'])):
            raise ValueError('DEMO_REFERENCE_EXPECTED_OMISSION')
        return None
    if ref['availability'] != 'AVAILABLE':
        raise ValueError('DEMO_REFERENCE_AVAILABILITY')
    result = view.execute('read_source', dict(path=path, revision=revision, start=ref['start'], end=ref['end']))
    refs = result['evidence']
    if (result['truncated'] or not refs or refs[-1]['line'] != ref['end']
            or [ev['quote'] for ev in refs] != ref['quote']):
        raise ValueError('DEMO_REFERENCE_TEXT')
    return result


def prepare(entry, default_pr):
    snapshot = entry['snapshot']
    view = View.from_input(snapshot)
    if snapshot['provenance']['capture_sha256'] != digest({key: snapshot[key] for key in
                                                            ('pr', 'revisions', 'scope', 'files')}):
        raise ValueError('DEMO_CAPTURE_DIGEST')
    results, proposed = [], []
    for finding in entry['findings']:
        anchor = finding['anchor']
        cited = {}
        for ref in (anchor, *finding['evidence']):
            result = read_reference(view, ref)
            if result:
                results.append(result)
                cited.update((ev['id'], ev) for ev in result['evidence'])
        change = next((ev for ev in cited.values()
                       if ev['side'] == anchor['side'] and ev['path'] == anchor['path']
                       and anchor['start'] <= ev['line'] <= anchor['end']
                       and (ev['side'], ev['path'], ev['line']) in view.changes), None)
        if not change:
            raise ValueError('DEMO_CHANGED_LINE_REQUIRED')
        proposed.append(dict(title=finding['title'], trigger=finding['body'], impact=finding['impact'],
                             change={key: change[key] for key in ('path', 'side', 'revision', 'line')},
                             evidence_ids=list(cited)))
    candidates, rejected = validate_candidates(view, proposed, results)
    if rejected:
        raise ValueError('DEMO_CANDIDATE_REJECTED')
    presentation = {key: entry[key] for key in ('author', 'risk', 'risk_summary')}
    if entry['status'] == 'NEEDS_RECHECK':
        presentation['recheck_reason'] = entry['recheck_reason']
    presentation['captured_at'] = entry['snapshot']['provenance']['captured_at']
    presentation['default'] = view.value['pr']['number'] == default_pr
    presentation['findings'] = {
        candidate['id']: {key: finding[key] for key in
                          ('risk', 'conclusion', 'suggested_action', 'anchor', 'evidence')}
        for candidate, finding in zip(candidates, entry['findings'], strict=True)
    }
    # This records the source of the text separately from the native reads
    # used to validate its citations. No investigator or provider was run.
    source = dict(kind=MODE, presentation=presentation)
    goal = '核对冻结源码与预写的审查发现'
    budget = limits(dict(tools=len(results)))
    identity = dict(view_sha256=view.sha256, input_sha256=view.value['input_sha256'],
                    revisions=view.value['revisions'], policy_version='authored-risk-review-demo-v1',
                    tool_version=TOOL_VERSION, result_version=RESULT_VERSION, budget=budget, goal=goal,
                    editorial_sha256=digest(entry))
    now = time.time()
    reason = 'EVIDENCE_INSUFFICIENT' if entry.get('recheck_reason') else 'HYPOTHESIS_SUPPORTED'
    task = dict(schema=SCHEMA, id='task-' + digest(identity), identity=identity, mode=MODE, provider='NONE',
                source=source, pr=view.value['pr'], view=view.value, status='COMPLETED', reason=reason,
                goal=goal, budget=budget, usage=dict(rounds=0, tools=len(results),
                                                     output_bytes=sum(len(canonical(r)) for r in results)),
                created_at=now, updated_at=now, events=[], results=results, candidates=candidates, rejected=[],
                active=None, requests=[], notice=NOTICE)
    event(task, 'created', 'Authored synthetic findings. Investigator and provider: NOT_RUN.')
    event(task, 'finished', 'Frozen evidence references checked with the native source reader.', reason=reason)
    judgments = {candidate['id']: entry['review'][finding['id']]
                 for candidate, finding in zip(candidates, entry['findings'], strict=True)
                 if finding['id'] in entry.get('review', {})}
    return task, judgments


def seed(service):
    pack = decode(PACK.read_bytes())
    if pack['schema'] != 'mea-round3-demo-v1' or pack['source'] != 'SYNTHETIC':
        raise ValueError('DEMO_PACK_IDENTITY')
    prepared = [prepare(entry, pack['active_pr']) for entry in pack['entries']]
    if sum(task['source']['presentation']['default'] for task, _ in prepared) != 1:
        raise ValueError('DEMO_DEFAULT_PR')
    default_review = None
    with service.store.launch_lock():
        for task, judgments in prepared:
            with service.store.transaction() as db:
                db.execute('INSERT OR IGNORE INTO tasks VALUES (?,?,?)',
                           (task['id'], digest(task['identity']), canonical(task).decode()))
            review = service.create(task['id'])
            if task['source']['presentation']['default']:
                default_review = review['id']
            recorded = {row['candidate_id'] for row in review['dispositions'] if row['task_id'] == task['id']}
            for candidate_id, disposition in judgments.items():
                if candidate_id not in recorded:
                    service.set_disposition(review['id'], task['id'], candidate_id, disposition)
    return default_review
