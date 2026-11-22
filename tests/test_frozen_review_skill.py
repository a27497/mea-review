"""Replay the frozen-review skill with a synthetic fixture and a scripted reviewer."""

from collections import Counter
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from mea.contract import decode, digest
from mea.mcp_server import Server


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / '.agents/skills/mea-frozen-review/SKILL.md'
FIXTURE = ROOT / 'fixtures/review/v2.json'
DISPATCH_KINDS = ('read', 'retry', 'edit', 'publication')

# The child has no MEA imports or fixture path. It only receives the handoff
# and the broker's approved results over a JSON-lines pipe.
REVIEWER = r'''
import json
import sys


def send(value):
    print(json.dumps(value), flush=True)


def read(tool, **args):
    send(dict(kind='read', tool=tool, args=args))
    reply = json.loads(input())
    if not reply['accepted']:
        raise SystemExit(0)
    return reply['result']


name = sys.argv[1]
line = sys.stdin.readline()
if not line:
    raise SystemExit(1)
binding = json.loads(line)['binding']
head = binding['revisions']['head']
if name == 'snapshot-info':
    read('snapshot_info')
diff = read('read_diff', path='limits.py', revision=head)
caller = read('read_source', path='app.py', revision=head, start=3, end=4)
if name == 'missing-evidence':
    read('read_source', path='quota.py', revision=head, start=1, end=2)
if name in ('foreign-submission', 'critical-foreign-submission'):
    binding = dict(binding, input_sha256='f' * 64)

actions = []
if name != 'empty':
    anchor = next(row for row in diff['evidence'] if row['line'] == 4)
    call = next(row for row in caller['evidence'] if row['line'] == 4)
    candidate = dict(
        severity='HIGH', title='Quota returns None to the subtraction in available',
        trigger='available(used) calls quota(), which now returns None.',
        impact='Subtracting used from None raises TypeError.',
        change={key: anchor[key] for key in ('path', 'side', 'revision', 'line')},
        evidence_ids=[anchor['id'], call['id']],
    )
    if name in ('forged', 'missing-evidence', 'critical-invalid'):
        candidate['evidence_ids'].append('ev-' + '0' * 64)
    if name == 'stale':
        candidate['change']['revision'] = binding['revisions']['base']
    if name in ('forged-coordinate', 'critical-bad-coordinate'):
        candidate['change']['line'] = 999
    if name in ('critical-invalid', 'critical-bad-coordinate',
                'critical-foreign-submission', 'receipt-write-failure'):
        candidate['severity'] = 'CRITICAL'
    if name == 'critical-lowercase':
        candidate['severity'] = 'critical'
    actions.append(dict(kind='candidate', binding=binding, candidate=candidate))
    if candidate['severity'] in ('CRITICAL', 'critical'):
        actions.extend([
            dict(kind='read', tool='read_source',
                 args=dict(path='limits.py', revision=head, start=4, end=4)),
            dict(kind='retry'),
            dict(kind='edit'),
            dict(kind='publication'),
        ])
    if name == 'critical-lowercase':
        actions.append(dict(kind='candidate', binding=binding,
                            candidate=dict(candidate, severity='CRITICAL')))
actions.append(dict(kind='finished', binding=binding))
send(actions)
'''


def load_skill_adapter():
    # Execute the checkout's trusted adapter, never text from the snapshot or reviewer.
    opening = '```python\n'
    markdown = SKILL.read_text(encoding='utf-8')
    if markdown.count(opening) != 1:
        raise ValueError('Expected exactly one Python adapter block in SKILL.md')
    prefix, block = markdown.split(opening)
    source, closing, _ = block.partition('\n```')
    if not closing:
        raise ValueError('Missing closing fence for the skill adapter')
    checks = {}
    exec(compile('\n' * prefix.count('\n') + '\n' + source, str(SKILL), 'exec'), checks)
    return checks


def write_json(path, value):
    with path.open('x', encoding='utf-8') as handle:
        json.dump(value, handle, indent=2)
        handle.write('\n')


class ReplayOperator:
    def __init__(self, out, name, checks):
        raw = FIXTURE.read_bytes()
        self.server = Server(decode(raw))
        self.view = self.server.view
        self.binding = dict(input_sha256=self.view.value['input_sha256'],
                            snapshot_file_sha256=digest(raw), view_sha256=self.view.sha256,
                            revisions=self.view.value['revisions'])
        self.out = out
        self.name = name
        self.checks = checks
        self.critical = False
        self.post_critical = Counter(dict.fromkeys(DISPATCH_KINDS, 0))
        self.proposals = []
        self.observed = []
        self.events = []

    def event(self, kind, **details):
        self.events.append(dict(seq=len(self.events) + 1, kind=kind, **details))

    def dispatch(self, action):
        kind = action['kind']
        if self.critical:
            self.post_critical[kind] += 1
            raise AssertionError(f'dispatched after the critical report: {kind}')
        if kind != 'read':
            raise AssertionError(f'unexpected action: {kind}')
        tool, args = action['tool'], action['args']
        result = self.server.call(tool, args)
        logged = deepcopy(result)
        # Inject faults at the broker boundary without loading another snapshot.
        if self.name == 'foreign-result':
            logged['view_sha256'] = 'f' * 64
        elif self.name == 'call-mismatch':
            logged['revision'] = self.binding['revisions']['base']
        elif self.name == 'altered-result':
            logged['data'] = 'altered after capture'
        if self.name in ('foreign-result', 'call-mismatch'):
            logged['content_sha256'] = digest({k: v for k, v in logged.items() if k != 'content_sha256'})
        reason = self.checks['check_result'](self.view, tool, args, logged)
        self.observed.append(dict(tool=tool, args=args, result=logged, displayed=reason is None,
                                  injected=logged != result, error=reason))
        self.event('evidence_read', tool=tool, args=args, displayed=reason is None)
        return reason, logged

    def accept_reports(self, actions):
        for index, action in enumerate(actions):
            kind = action['kind']
            if kind == 'candidate':
                candidate = action['candidate']
                severity = candidate.get('severity')
                self.event('candidate_reported', severity=severity)
                self.proposals.append(candidate)
                if self.checks['requires_critical_stop'](severity):
                    self.critical = True
                    self.event('evidence_dispatch_closed',
                               ignored_actions=[row['kind'] for row in actions[index + 1:]])
            elif kind != 'finished':
                self.dispatch(action)
                continue

            reason = self.checks['check_submission'](self.binding, action.get('binding'))
            if self.critical:
                return 'BLOCKED_CRITICAL', reason
            if reason:
                return 'REJECTED_BINDING', reason
            if kind == 'finished':
                self.event('reviewer_finished')
                return 'COMPLETE', None
        raise AssertionError('reviewer did not finish')

    def exchange(self, session, author, reviewer):
        reason = self.checks['check_provenance'](author, reviewer)
        if reason:
            self.event('provenance_rejected', reason=reason)
            return 'REJECTED_PROVENANCE', reason

        self.event('provenance_admitted', author=author, reviewer=reviewer)
        self.send(session, dict(binding=self.binding, author_session_id=author, reviewer_session_id=reviewer))
        self.event('handoff_sent')
        while True:
            message = json.loads(session.stdout.readline())
            if isinstance(message, list):
                return self.accept_reports(message)
            reason, result = self.dispatch(message)
            self.send(session, dict(accepted=reason is None, result=None if reason else result))
            if reason:
                return 'REJECTED_BINDING', reason

    @staticmethod
    def send(session, value):
        print(json.dumps(value), file=session.stdin, flush=True)

    def receipt(self, state, reason, session, author, reviewer):
        displayed = [row for row in self.observed if row['displayed']]
        if reason:
            candidates = [dict(row, citation_validity='REJECTED', correctness='UNADJUDICATED', reason=reason)
                          for row in self.proposals]
        else:
            candidates = self.checks['assess'](self.view, self.proposals, [row['result'] for row in displayed])
            self.event('validated_from_observed_prefix', candidate_count=len(candidates), results=len(displayed))
            reason = next((row['reason'] for row in candidates if 'reason' in row), None)
            if reason and not self.critical:
                state = 'REJECTED_CANDIDATES'

        coverage = [
            dict(tool=row['tool'], args=row['args'], unknown=row['result'].get('unknown', []),
                 truncated=row['result'].get('truncated'), read_status=row['result'].get('read', {}).get('status'))
            for row in displayed
        ]
        return dict(
            **self.binding, author_session_id=author, reviewer_session_id=reviewer,
            session_launch=dict(method='fresh Python subprocess', pid=session.pid,
                                os_session_id=os.getsid(session.pid), author_id_source='synthetic fixture',
                                reviewer_id_source='launcher OS session ID'),
            capabilities=dict(exposed=['brokered MEA read-only tools'], separate_os_session=True,
                              isolated_python_imports=True, host_file_and_network_isolation='NOT_RUN'),
            run_state=state, reason=reason, candidates=candidates, coverage=coverage,
            mode='SYNTHETIC_FIXTURE_OFFLINE_REPLAY_SCRIPTED_MOCK', live_client_generation='NOT_RUN',
            paid_provider='NOT_RUN', github_write='NOT_RUN', fixture=str(FIXTURE.relative_to(ROOT)),
            observed_results=self.observed, events=self.events,
            post_critical_dispatches=dict(self.post_critical) if self.critical else None,
        )

    def run(self):
        with subprocess.Popen(
            [sys.executable, '-I', '-B', '-c', REVIEWER, self.name],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=self.out, env={'LANG': 'C.UTF-8'}, text=True, start_new_session=True,
        ) as session:
            session_id = os.getsid(session.pid)
            if session_id != session.pid or session_id == os.getsid(0):
                raise AssertionError('expected a separate scripted reviewer OS session')
            author_id = 'fixture-author'
            author = None if self.name == 'missing-author' else author_id
            reviewer = {'missing-reviewer': None, 'same-session': author_id + ' '}.get(
                self.name, f'python-os-session:{session_id}'
            )
            self.event('reviewer_launched', os_session_id=session_id)
            status = 0
            try:
                state, reason = self.exchange(session, author, reviewer)
                receipt = self.receipt(state, reason, session, author, reviewer)
                try:
                    write_json(self.out / 'receipt.json', receipt)
                except OSError as exc:
                    self.event('receipt_write_failed', error=type(exc).__name__)
                    write_json(self.out / 'receipt-failed.json', dict(receipt, receipt_write_error=type(exc).__name__))
                    status = 1
                else:
                    self.event('receipt_written')
            finally:
                session.stdin.close()
                try:
                    exit_code = session.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    session.kill()
                    raise
                self.event('review_ended', exit_code=exit_code, stderr=session.stderr.read())
        return status


def assert_dispatch_gate(test, operator):
    for kind in DISPATCH_KINDS:
        action = dict(kind=kind)
        if kind == 'read':
            action.update(tool='read_diff', args=dict(path='limits.py', revision=operator.binding['revisions']['head']))
        with test.assertRaisesRegex(AssertionError, f'dispatched after the critical report: {kind}'):
            operator.dispatch(action)


class FrozenReviewSkillTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.checks = load_skill_adapter()

    def setUp(self):
        self.out = Path(self.enterContext(TemporaryDirectory()))

    def assert_replay(self, name, state, reason=None, *, reads=2):
        operator = ReplayOperator(self.out, name, self.checks)
        self.assertEqual(operator.run(), 0)
        receipt = json.loads((self.out / 'receipt.json').read_bytes())
        self.assertEqual((receipt['run_state'], receipt['reason']), (state, reason))
        self.assertEqual({key: receipt[key] for key in operator.binding}, operator.binding)
        self.assertEqual(len(receipt['observed_results']), reads)
        self.assertEqual(operator.events[-1]['kind'], 'review_ended')
        self.assertEqual(operator.events[-1]['exit_code'], int(state == 'REJECTED_PROVENANCE'))
        self.assertEqual(operator.events[-1]['stderr'], '')
        if state == 'BLOCKED_CRITICAL':
            self.assert_critical_receipt(receipt, later_candidate=name == 'critical-lowercase')
            self.assertEqual(operator.events[-2]['kind'], 'receipt_written')
            assert_dispatch_gate(self, operator)
            self.assertEqual(operator.observed, receipt['observed_results'])
        if state == 'REJECTED_PROVENANCE':
            self.assertEqual(receipt['candidates'], [])
            self.assertNotIn('handoff_sent', [row['kind'] for row in operator.events])
        return operator, receipt

    def assert_critical_receipt(self, receipt, *, later_candidate=False, write_failed=False):
        self.assertEqual(receipt['post_critical_dispatches'], dict.fromkeys(DISPATCH_KINDS, 0))
        kinds = [row['kind'] for row in receipt['events']]
        after_report = kinds.index('candidate_reported') + 1
        closing = receipt['events'][after_report]
        ignored = list(DISPATCH_KINDS) + (['candidate'] if later_candidate else []) + ['finished']
        self.assertEqual(closing['ignored_actions'], ignored)
        allowed = ['evidence_dispatch_closed']
        if receipt['reason'] != 'SUBMISSION_BINDING_MISMATCH':
            allowed.append('validated_from_observed_prefix')
        if write_failed:
            allowed.append('receipt_write_failed')
        self.assertEqual(kinds[after_report:], allowed)
        self.assertEqual(len(receipt['candidates']), 1)

    def test_anchored(self):
        operator, receipt = self.assert_replay('anchored', 'COMPLETE')
        candidate, = receipt['candidates']
        self.assertEqual((candidate['citation_validity'], candidate['correctness']), ('VALID', 'UNADJUDICATED'))
        self.assertEqual(candidate['change'], dict(path='limits.py', side='head', line=4,
                                                  revision=operator.binding['revisions']['head']))
        self.assertEqual([(row['path'], row['line']) for row in candidate['citations']],
                         [('limits.py', 4), ('app.py', 4)])
        self.assertEqual(candidate['evidence_ids'], [row['id'] for row in candidate['citations']])

    def test_empty(self):
        _, receipt = self.assert_replay('empty', 'COMPLETE')
        self.assertEqual(receipt['candidates'], [])
        self.assertEqual(len(receipt['coverage']), 2)

    def test_forged(self):
        self.assert_replay('forged', 'REJECTED_CANDIDATES', 'REFERENCE_INVALID_OR_UNOBSERVED')

    def test_missing_evidence(self):
        _, receipt = self.assert_replay('missing-evidence', 'REJECTED_CANDIDATES',
                                        'REFERENCE_INVALID_OR_UNOBSERVED', reads=3)
        self.assertEqual(receipt['coverage'][-1]['read_status'], 'SOURCE_NOT_COLLECTED')

    def test_stale(self):
        self.assert_replay('stale', 'REJECTED_CANDIDATES', 'CANDIDATE_VERSION_MISMATCH')

    def test_foreign_submission(self):
        self.assert_replay('foreign-submission', 'REJECTED_BINDING', 'SUBMISSION_BINDING_MISMATCH')

    def assert_result_refused(self, name, reason):
        _, receipt = self.assert_replay(name, 'REJECTED_BINDING', reason, reads=1)
        row, = receipt['observed_results']
        self.assertTrue(row['injected'])
        self.assertFalse(row['displayed'])
        self.assertEqual(row['error'], reason)

    def test_foreign_result(self):
        self.assert_result_refused('foreign-result', 'RESULT_VIEW_MISMATCH')

    def test_altered_result(self):
        self.assert_result_refused('altered-result', 'RESULT_DIGEST_MISMATCH')

    def test_call_mismatch(self):
        self.assert_result_refused('call-mismatch', 'RESULT_CALL_MISMATCH')

    def test_missing_author(self):
        self.assert_replay('missing-author', 'REJECTED_PROVENANCE', 'MISSING_SESSION_ID', reads=0)

    def test_missing_reviewer(self):
        self.assert_replay('missing-reviewer', 'REJECTED_PROVENANCE', 'MISSING_SESSION_ID', reads=0)

    def test_same_session(self):
        self.assert_replay('same-session', 'REJECTED_PROVENANCE', 'SAME_SESSION_ID', reads=0)

    def test_critical_invalid(self):
        _, receipt = self.assert_replay('critical-invalid', 'BLOCKED_CRITICAL', 'REFERENCE_INVALID_OR_UNOBSERVED')
        candidate, = receipt['candidates']
        self.assertEqual((candidate['severity'], candidate['citation_validity']), ('CRITICAL', 'REJECTED'))

    def assert_fabricated_coordinate(self, name, state, severity):
        operator, receipt = self.assert_replay(name, state, 'DETERMINISTIC_VALIDATION_REJECTED')
        candidate, = receipt['candidates']
        self.assertEqual(candidate['change'], dict(path='limits.py', side='head', line=999,
                                                  revision=operator.binding['revisions']['head']))
        self.assertNotIn(('head', 'limits.py', 999), operator.view.changes)
        self.assertEqual((candidate['severity'], candidate['citation_validity']), (severity, 'REJECTED'))
        observed_ids = {ref['id'] for row in receipt['observed_results'] for ref in row['result']['evidence']}
        self.assertEqual(len(candidate['evidence_ids']), 2)
        self.assertLessEqual(set(candidate['evidence_ids']), observed_ids)

    def test_forged_coordinate(self):
        self.assert_fabricated_coordinate('forged-coordinate', 'REJECTED_CANDIDATES', 'HIGH')

    def test_critical_bad_coordinate(self):
        self.assert_fabricated_coordinate('critical-bad-coordinate', 'BLOCKED_CRITICAL', 'CRITICAL')

    def test_critical_lowercase(self):
        _, receipt = self.assert_replay('critical-lowercase', 'BLOCKED_CRITICAL', 'INVALID_SEVERITY')
        candidate, = receipt['candidates']
        self.assertEqual((candidate['severity'], candidate['citation_validity']), ('critical', 'REJECTED'))

    def test_snapshot_info_without_revision(self):
        operator, receipt = self.assert_replay('snapshot-info', 'COMPLETE', reads=3)
        inventory = receipt['observed_results'][0]
        self.assertEqual((inventory['tool'], inventory['args'], inventory['displayed']), ('snapshot_info', {}, True))
        for field in ('input_sha256', 'revisions'):
            with self.subTest(field=field):
                mismatched = dict(inventory['result'], **{field: None})
                self.assertEqual(self.checks['check_result'](operator.view, 'snapshot_info', {}, mismatched),
                                 'RESULT_VIEW_MISMATCH')

    def test_critical_foreign_submission(self):
        _, receipt = self.assert_replay('critical-foreign-submission', 'BLOCKED_CRITICAL',
                                        'SUBMISSION_BINDING_MISMATCH')
        candidate, = receipt['candidates']
        self.assertEqual((candidate['severity'], candidate['citation_validity']), ('CRITICAL', 'REJECTED'))

    def test_receipt_write_failure(self):
        receipt_path = self.out / 'receipt.json'
        receipt_path.write_text('pre-existing receipt', encoding='utf-8')
        process = subprocess.run(
            [sys.executable, '-B', '-m', 'test_frozen_review_skill', str(self.out)],
            cwd=ROOT, env={'PYTHONPATH': str(ROOT / 'tests'), 'LANG': 'C.UTF-8'},
            capture_output=True, text=True, timeout=15,
        )
        self.assertEqual(process.returncode, 1, process.stderr)
        self.assertEqual(process.stderr, '')
        self.assertEqual(receipt_path.read_text(encoding='utf-8'), 'pre-existing receipt')
        marker = json.loads((self.out / 'receipt-failed.json').read_bytes())
        self.assertEqual((marker['run_state'], marker['reason']), ('BLOCKED_CRITICAL', None))
        self.assertEqual(marker['receipt_write_error'], 'FileExistsError')
        self.assertEqual(len(marker['observed_results']), 2)
        self.assert_critical_receipt(marker, write_failed=True)


if __name__ == '__main__':
    # Exercise the receipt failure as a real operator process exit.
    operator = ReplayOperator(Path(sys.argv[1]), 'receipt-write-failure', load_skill_adapter())
    status = operator.run()
    assert_dispatch_gate(unittest.TestCase(), operator)
    raise SystemExit(status)
