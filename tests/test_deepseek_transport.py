"""Real DeepSeek transport and live lane with a fake HTTPS connection. No real request is sent."""
import calendar
import io
import json
import os
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mea.contract import canonical, digest
from mea.investigation import authorization, deepseek, live_run, native, real_protocol as p, spending
from mea.investigation.context_budget import expanded_feedback
from mea.investigation.provider_worker import Broker
from mea.investigation.real import Coordinator, create
from mea.investigation.tasks import Store, TERMINAL
from test_cross_file_tools import value

# A dummy value shaped like a DeepSeek key; never a real credential.
DUMMY = 'sk-' + 'a' * 32


def completion(calls, usage=None, status=200, model=p.MODEL, finish='tool_calls'):
    body = dict(id='cmpl-test', object='chat.completion', model=model,
                choices=[dict(index=0, finish_reason=finish, message=dict(role='assistant', content=None, tool_calls=calls))],
                usage=usage or dict(prompt_tokens=1000, completion_tokens=100, total_tokens=1100,
                                    prompt_cache_hit_tokens=400, prompt_cache_miss_tokens=600))
    return status, json.dumps(body).encode()


def call(identifier, name, **args):
    return dict(id=identifier, type='function', function=dict(name=name, arguments=json.dumps(dict(args, summary='test step'))))


class FakeConnection:
    """Scripted HTTPS connection: plan(body) returns (status, raw) or raises."""
    posts = []

    def __init__(self, plan):
        self.plan = plan

    def factory(self, host, port, timeout):
        self.host, self.port, self.timeout = host, port, timeout
        return self

    def request(self, method, path, body, headers):
        self.posts.append(dict(method=method, path=path, body=body, headers=headers, host=self.host, port=self.port))
        self.outcome = self.plan(json.loads(body))

    def getresponse(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        status, raw = self.outcome

        class R:
            def __init__(self):
                self.status = status
                self.stream = io.BytesIO(raw)
            def read(self, n):
                return self.stream.read(n)
            def getheader(self, name):
                return 'req-123' if name == 'x-request-id' else None
        return R()

    def close(self):
        pass


def scope(inputs, **extra):
    return dict(dict(id='mea-live-test', inputs=inputs, max_tasks=1, max_requests=6, tool_profile=native.CROSS_FILE,
                     first_turn=p.FIRST_TURN, approval='unit test, fake connection', budget_usd=1,
                     task_budget=p.budget(dict(output_bytes=500000))), **extra)


class LiveLaneTests(unittest.TestCase):
    def setUp(self):
        guard = patch('socket.socket.connect', side_effect=AssertionError('NO_NETWORK'))
        guard.start()
        self.addCleanup(guard.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.input = value()
        self.lane = authorization.live(scope([digest(self.input)]))
        FakeConnection.posts = []

    def start(self, plan, name='store'):
        store = Store(Path(self.temp.name) / name)
        coordinator = Coordinator(store, self.lane)
        coordinator.boot()
        self.conn = FakeConnection(plan)
        sender = deepseek.Sender(DUMMY, connect=self.conn.factory)
        broker = Broker(store, sender=sender, lane=self.lane)
        try:
            task = create(store, self.input, budget=self.lane.scope['task_budget'], lane=self.lane, confirmed=True,
                          first_turn=p.FIRST_TURN, tool_profile=native.CROSS_FILE)
            sender.validate = lambda body: p.validate_task_request(body, task)
            return store, live_run.drive(store, coordinator, broker, task['id'])
        finally:
            broker.close()

    def plan(self, body):
        feedback = expanded_feedback(body)
        rev = json.loads(body['messages'][1]['content'])['revisions']['head']
        if not feedback:
            return completion([call('c1', 'related_files', path='shop/pricing.py', revision=rev),
                               call('c2', 'read_diff', path='shop/pricing.py', revision=rev)])
        if len(feedback) == 2:
            return completion([call('c3', 'read_source', path='shop/cart.py', revision=rev, start=4, end=5)])
        anchor = next(e for f in feedback if f['tool'] == 'read_diff' for e in f['evidence'] if e['line'] == 2)
        caller = next(e for f in feedback if f['tool'] == 'read_source' for e in f['evidence'] if e['line'] == 5)
        return completion([call('c4', native.FINISH, reason='HYPOTHESIS_SUPPORTED', candidates=[dict(
            title='discount=None reaches subtraction', trigger='checkout() calls total() without discount',
            change=dict(path='shop/pricing.py', side='head', line=2, revision=rev),
            impact='TypeError for every checkout', evidence_ids=[anchor['id'], caller['id']])])])

    def test_live_lane_runs_cross_file_tools_through_the_worker_state_machine(self):
        store, task = self.start(self.plan)
        self.assertEqual((task['status'], task['reason']), ('COMPLETED', 'HYPOTHESIS_SUPPORTED'))
        self.assertEqual((task['mode'], task['provider']), ('REAL_PROVIDER_DEEPSEEK', 'DEEPSEEK_API'))
        self.assertEqual([r['tool'] for r in task['results']], ['related_files', 'read_diff', 'read_source'])
        self.assertEqual([c['path'] for c in task['candidates'][0]['citations']], ['shop/pricing.py', 'shop/cart.py'])
        posts = FakeConnection.posts
        self.assertEqual(len(posts), 3)
        for post, request in zip(posts, task['requests']):
            self.assertEqual((post['method'], post['path'], post['host'], post['port']), ('POST', '/chat/completions', 'api.deepseek.com', 443))
            self.assertEqual(post['headers']['Authorization'], 'Bearer ' + DUMMY)
            self.assertEqual(post['body'], canonical(request['request_body']))
            body = json.loads(post['body'])
            self.assertEqual(body['tools'], native.definitions(native.CROSS_FILE))
            self.assertEqual((body['model'], body['thinking'], body['stream']), ('deepseek-flash', {'type': 'disabled'}, False))
            self.assertIn('cross-file tool profile', body['messages'][0]['content'])
            self.assertEqual(request['receipt']['request_id'], 'req-123')
            self.assertEqual(request['cost_source'], 'PROVIDER_USAGE_AT_OFFICIAL_PRICE')
        self.assertNotIn(DUMMY, json.dumps(task))
        with store.connection() as db:
            ledger = spending.LIVE.summary(db)
            with self.assertRaisesRegex(ValueError, 'SHARED_BUDGET_NOT_INITIALIZED'):
                spending.SIMULATED.spec(db)
        self.assertEqual(ledger['cap_nano_usd'], spending.NANO)
        self.assertEqual([e['state'] for e in ledger['entries']], ['USAGE_ESTIMATED'] * 3)
        expected = {'off_peak': 400 * 3 + 600 * 150 + 100 * 600, 'peak': 400 * 6 + 600 * 300 + 100 * 1200}
        for entry in ledger['entries']:
            self.assertEqual(entry['estimated_nano_usd'], expected)
            self.assertEqual(entry['scheduled_nano_usd'], expected[entry['price_tier']])
            self.assertEqual(entry['accounted_nano_usd'], expected['peak'])
        self.assertNotIn(DUMMY, json.dumps(ledger))
        result = live_run.summary(task, ledger)
        self.assertEqual([t['tool'] for t in result['tool_sequence']], ['related_files', 'read_diff', 'read_source'])

    def test_last_request_is_finish_only_instead_of_a_budget_stop(self):
        def plan(body):
            names = [t['function']['name'] for t in body['tools']]
            rev = json.loads(body['messages'][1]['content'])['revisions']['head']
            if names == [native.FINISH]:
                return completion([call('end', native.FINISH, reason='EVIDENCE_INSUFFICIENT', candidates=[])])
            n = len(expanded_feedback(body)) + 1
            return completion([call(f'c{n}', 'read_source', path='shop/cart.py', revision=rev, start=1, end=n)])
        store, task = self.start(plan)
        self.assertEqual((task['status'], task['reason']), ('COMPLETED', 'EVIDENCE_INSUFFICIENT'))
        bodies = [json.loads(post['body']) for post in FakeConnection.posts]
        self.assertEqual(len(bodies), 6)
        self.assertEqual([json.loads(b['messages'][1]['content'])['finish_only'] for b in bodies], [False] * 5 + [True])
        self.assertEqual(bodies[-1]['tools'], p.terminal_tools(native.CROSS_FILE))
        self.assertEqual([b['tool_choice'] for b in bodies], ['required'] * 5 + [p.FINISH_CHOICE])

    def test_undeclared_tool_on_a_finish_only_request_is_refused(self):
        def plan(body):
            # A provider that ignores the declared tools and the named tool_choice.
            rev = json.loads(body['messages'][1]['content'])['revisions']['head']
            n = len(expanded_feedback(body)) + 1
            return completion([call(f'c{n}', 'read_source', path='shop/cart.py', revision=rev, start=1, end=n)])
        store, task = self.start(plan)
        self.assertEqual((task['status'], task['reason']), ('STOPPED', 'TOOL_NOT_ALLOWED'))
        bodies = [json.loads(post['body']) for post in FakeConnection.posts]
        self.assertEqual(len(bodies), 6)  # No request after the refusal.
        self.assertEqual(bodies[-1]['tools'], p.terminal_tools(native.CROSS_FILE))
        self.assertEqual(task['usage']['tools'], 5)  # The sixth response executed nothing.
        self.assertEqual(len(task['results']), 5)
        last = task['requests'][-1]
        self.assertFalse(last.get('decision_applied'))
        refused = [e for e in task['events'] if e['kind'] == 'undeclared_tool_refused']
        self.assertEqual(refused[0]['declared_tools'], [native.FINISH])
        rejected = [e for e in task['events'] if e['kind'] == 'decision_rejected'][-1]
        self.assertEqual((rejected['rejection_code'], rejected['diagnostic']['stage']), ('NATIVE_FUNCTION_NOT_ALLOWED', 'TOOL_NOT_ALLOWED'))
        # Earlier requests replay under their own full schema.
        self.assertEqual(len(p.bound_calls(task, 4)), 1)
        self.assertEqual(p.request_parameters(task, task['requests'][0]), native.parameters(native.CROSS_FILE))
        self.assertEqual(set(p.request_parameters(task, last)), {native.FINISH})

    def test_capture_gaps_reach_the_model_and_tool_results(self):
        from github_fixtures import Fetcher, HEAD, NUMBER, OWNER, REPO
        from mea.github_input import Response, capture
        tree = f'/repos/{OWNER}/{REPO}/git/trees/{HEAD}?recursive=1'
        report, snapshot = capture(f'{OWNER}/{REPO}#{NUMBER}', Fetcher(overrides={tree: Response(503, b'{}')}), now=0)
        self.input = snapshot
        self.lane = authorization.live(scope([digest(snapshot)]))
        seen = {}

        def plan(body):
            context = json.loads(body['messages'][1]['content'])
            feedback = expanded_feedback(body)
            if not feedback:
                seen['capture'] = context['capture']
                return completion([call('s1', 'search', query='total', revision=context['revisions']['head'])])
            seen['unknown'] = feedback[0]['unknown']
            return completion([call('end', native.FINISH, reason='EVIDENCE_INSUFFICIENT', candidates=[])])
        store, task = self.start(plan)
        self.assertEqual(task['reason'], 'EVIDENCE_INSUFFICIENT')
        self.assertEqual((seen['capture']['status'], seen['capture']['context_selection']), ('INCOMPLETE', 'FAILED'))
        self.assertEqual(seen['capture']['gaps'], [dict(kind='TRANSPORT', code='HTTP_503', request='tree', path=None, side=None)])
        self.assertIn('SNAPSHOT_CAPTURE_INCOMPLETE:context_selection=FAILED', seen['unknown'])
        self.assertIn('CAPTURE_GAP:TRANSPORT:HTTP_503:tree:-', seen['unknown'])
        self.assertEqual(task['view']['coverage']['capture']['context_selection'], 'FAILED')

    def test_http_error_stops_without_retry(self):
        store, task = self.start(lambda body: (500, b'{"error":{"message":"server"}}'))
        self.assertEqual((task['status'], task['reason']), ('STOPPED', 'PROVIDER_HTTP_ERROR'))
        self.assertEqual(len(FakeConnection.posts), 1)
        self.assertEqual(task['requests'][0]['receipt']['error'], 'HTTP_ERROR')

    def test_connection_failure_is_uncertain_and_keeps_the_reservation(self):
        store, task = self.start(lambda body: TimeoutError('read timed out'))
        self.assertEqual((task['status'], task['reason']), ('STOPPED', 'REQUEST_UNCERTAIN'))
        self.assertEqual(len(FakeConnection.posts), 1)
        request = task['requests'][0]
        self.assertEqual((request['state'], request['receipt']['error']), ('UNCERTAIN', 'CONNECTION_OR_RESPONSE_UNCERTAIN'))
        with store.connection() as db:
            entry = spending.LIVE.summary(db)['entries'][0]
        self.assertEqual((entry['state'], entry['accounted_nano_usd']), ('UNCERTAIN', entry['reserved_nano_usd']))

    def test_model_identity_and_secret_echo_are_handled(self):
        store, task = self.start(lambda body: completion([], model='other-model', finish='stop'))
        self.assertEqual((task['status'], task['reason']), ('STOPPED', 'PROVIDER_MODEL_MISMATCH'))
        store, task = self.start(lambda body: (400, json.dumps(dict(error='bad key ' + DUMMY)).encode()), 'second')
        self.assertEqual((task['status'], task['reason']), ('STOPPED', 'PROVIDER_HTTP_ERROR'))
        self.assertNotIn(DUMMY, json.dumps(task))

    def test_request_usd_cap_blocks_before_any_post(self):
        with patch.object(spending.LIVE, 'request_cap', 1):
            store, task = self.start(self.plan)
        self.assertEqual((task['status'], task['reason']), ('STOPPED', 'SHARED_REQUEST_USD_CAP'))
        self.assertEqual(FakeConnection.posts, [])

    def test_scope_admission(self):
        other = value()
        other['pr']['title'] = 'Different input'
        store = Store(Path(self.temp.name) / 'scope')
        Coordinator(store, self.lane).boot()
        broker = Broker(store, sender=deepseek.Sender(DUMMY), lane=self.lane)
        self.addCleanup(lambda: broker.lease.closed or broker.close())
        budget = self.lane.scope['task_budget']
        with self.assertRaisesRegex(ValueError, 'LIVE_AUTHORIZATION_SCOPE_MISMATCH'):
            create(store, other, budget=budget, lane=self.lane, confirmed=True, first_turn=p.FIRST_TURN, tool_profile=native.CROSS_FILE)
        with self.assertRaisesRegex(ValueError, 'LIVE_AUTHORIZATION_SCOPE_MISMATCH'):
            create(store, self.input, lane=self.lane, confirmed=True, first_turn=p.FIRST_TURN, tool_profile=native.CROSS_FILE)
        with self.assertRaisesRegex(ValueError, 'LIVE_AUTHORIZATION_SCOPE_MISMATCH'):
            create(store, self.input, lane=self.lane, confirmed=True)
        with self.assertRaisesRegex(ValueError, 'EXPLICIT_LIVE_START_REQUIRED'):
            create(store, self.input, lane=self.lane)
        good = scope([digest(self.input)])
        for change, code in [(dict(id='grant'), 'LIVE_SCOPE_ID'), (dict(budget_usd=31), 'LIVE_SCOPE_BUDGET'),
                             (dict(max_tasks=2), 'LIVE_SCOPE_CAPS'), (dict(max_requests=7), 'LIVE_SCOPE_CAPS'),
                             (dict(tool_profile='x'), 'LIVE_SCOPE_PROFILE'), (dict(inputs=['x']), 'LIVE_SCOPE_INPUTS'),
                             (dict(task_budget=dict(rounds=7)), 'LIVE_SCOPE_TASK_BUDGET'), (dict(task_budget=dict(rounds=6)), 'LIVE_SCOPE_TASK_BUDGET')]:
            with self.assertRaisesRegex(ValueError, code):
                authorization.live(dict(good, **change))
        with self.assertRaisesRegex(ValueError, 'LIVE_SCOPE_FIELDS'):
            authorization.live(dict(good, extra=1))

    def test_price_schedule(self):
        monday_peak = calendar.timegm((2026, 11, 16, 2, 30, 0))
        monday_gap = calendar.timegm((2026, 11, 16, 5, 0, 0))
        saturday = calendar.timegm((2026, 11, 21, 2, 30, 0))
        self.assertEqual([spending.tier(t) for t in (monday_peak, monday_gap, saturday)], ['peak', 'off_peak', 'off_peak'])


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'deepseek.env'

    def write(self, text, mode=0o600):
        if self.path.exists():
            self.path.unlink()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            handle.write(text)
        os.chmod(self.path, mode)

    def test_reads_only_a_private_owner_file(self):
        self.write('# comment\nDEEPSEEK_API_KEY=' + DUMMY + '\n')
        self.assertEqual(deepseek.read_key(self.path), DUMMY)
        self.write('export DEEPSEEK_API_KEY="' + DUMMY + '"\n')
        self.assertEqual(deepseek.read_key(self.path), DUMMY)
        for text, mode, code in [('DEEPSEEK_API_KEY=' + DUMMY, 0o644, 'CREDENTIAL_FILE_PERMISSIONS'),
                                 ('DEEPSEEK_API_KEY=\n', 0o600, 'CREDENTIAL_EMPTY'),
                                 ('OTHER=1\n', 0o600, 'CREDENTIAL_EMPTY'),
                                 ('DEEPSEEK_API_KEY=a b\n', 0o600, 'CREDENTIAL_FORMAT')]:
            self.write(text, mode)
            with self.assertRaises(ValueError) as raised:
                deepseek.read_key(self.path)
            self.assertEqual(str(raised.exception), code)
            self.assertNotIn(DUMMY, str(raised.exception))
        with self.assertRaisesRegex(ValueError, 'CREDENTIAL_FILE_MISSING'):
            deepseek.read_key(Path(self.temp.name) / 'absent')

    def test_cli_requires_confirmation_and_a_key_before_opening_a_store(self):
        self.write('DEEPSEEK_API_KEY=\n')
        snapshot = Path(self.temp.name) / 'snapshot.json'
        snapshot.write_bytes(canonical(value()))
        scope_file = Path(self.temp.name) / 'scope.json'
        scope_file.write_bytes(canonical(scope([digest(value())])))
        store = Path(self.temp.name) / 'store'
        args = ['--scope', str(scope_file), '--snapshot', str(snapshot), '--store', str(store),
                '--out', str(Path(self.temp.name) / 'out.json'), '--credentials', str(self.path)]
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(live_run.main(args), 2)
            self.assertEqual(live_run.main(args + ['--confirm-real-provider']), 2)
        self.assertIn('--confirm-real-provider is required', out.getvalue())
        self.assertIn('BLOCKED: CREDENTIAL_EMPTY', out.getvalue())
        self.assertFalse(store.exists())

    # M3 step 1 (CodeRabbit on f941812): operator-reachable store conditions are BLOCKED, not a traceback.
    def cli(self, budget_usd=1):
        self.write('DEEPSEEK_API_KEY=' + DUMMY + '\n')
        snapshot = Path(self.temp.name) / 'snapshot.json'
        snapshot.write_bytes(canonical(value()))
        scope_file = Path(self.temp.name) / 'scope.json'
        scope_file.write_bytes(canonical(scope([digest(value())], budget_usd=budget_usd)))
        self.store = Path(self.temp.name) / 'store'
        self.out = Path(self.temp.name) / 'out.json'
        return ['--scope', str(scope_file), '--snapshot', str(snapshot), '--store', str(self.store),
                '--out', str(self.out), '--credentials', str(self.path), '--confirm-real-provider']

    def finish_plan(self, body):
        return completion([call('c1', native.FINISH, reason='ZERO_CANDIDATES', candidates=[])])

    def test_cli_reports_broker_and_create_value_errors_as_blocked(self):
        conn = FakeConnection(self.finish_plan)
        FakeConnection.posts = []
        real_sender = deepseek.Sender
        sender = lambda secret: real_sender(secret, connect=conn.factory)
        # Normal: a run against a fresh store completes and writes its summary.
        args = self.cli()
        out = io.StringIO()
        with patch('socket.socket.connect', side_effect=AssertionError('NO_NETWORK')), \
                patch.object(live_run.deepseek, 'Sender', sender), redirect_stdout(out):
            self.assertEqual(live_run.main(args), 0)
        self.assertEqual(json.loads(self.out.read_text())['status'], 'COMPLETED')
        self.assertEqual(len(FakeConnection.posts), 1)
        # Violation: the same store with another budget_usd. Broker refuses the changed grant record
        # (before reaching the ledger) instead of exiting with a traceback.
        args = self.cli(budget_usd=2)
        out = io.StringIO()
        with patch.object(live_run.deepseek, 'Sender', sender), redirect_stdout(out):
            self.assertEqual(live_run.main(args), 2)
        self.assertIn('BLOCKED: LIVE_AUTHORIZATION_SCOPE_MISMATCH', out.getvalue())
        self.assertEqual(len(FakeConnection.posts), 1)
        # Boundary: create() refuses (task cap); the broker lease is released and shutdown ran.
        args = self.cli()
        out = io.StringIO()
        with patch.object(live_run.deepseek, 'Sender', sender), \
                patch.object(live_run, 'create', side_effect=ValueError('PHASE3_TASK_CAP')), redirect_stdout(out):
            self.assertEqual(live_run.main(args), 2)
        self.assertIn('BLOCKED: PHASE3_TASK_CAP', out.getvalue())
        lane = authorization.live(scope([digest(value())]))
        store = Store(self.store)
        Coordinator(store, lane).boot()
        Broker(store, sender=deepseek.Sender(DUMMY, connect=conn.factory), lane=lane).close()  # Lease is free.
        self.assertEqual(len(FakeConnection.posts), 1)

    def test_live_ledger_limits_are_per_store(self):
        # The live ledger lives inside one store: a fresh store starts at zero, so LIVE_CAP is a
        # per-store bound on budget_usd, not an account-level or phase-wide cap.
        lane = authorization.live(scope([digest(value())]))
        for name, used in (('a', 7), ('b', 0)):
            store = Store(Path(self.temp.name) / name)
            Coordinator(store, lane).boot()
            Broker(store, sender=deepseek.Sender(DUMMY), lane=lane).close()
            with store.connection() as db:
                summary = spending.LIVE.summary(db)
                self.assertEqual((summary['cap_nano_usd'], summary['accounted_nano_usd']), (spending.NANO, 0))
        self.assertEqual(spending.LIVE_CAP, 30 * spending.NANO)
        authorization.live(scope([digest(value())], budget_usd=30))  # Boundary: largest accepted per store.
        with self.assertRaisesRegex(ValueError, 'LIVE_SCOPE_BUDGET'):
            authorization.live(scope([digest(value())], budget_usd=31))

    def test_sender_requires_a_task_validator(self):
        with self.assertRaisesRegex(ValueError, 'LIVE_VALIDATOR_REQUIRED'):
            deepseek.Sender(DUMMY).start({})


if __name__ == '__main__':
    unittest.main()
