"""M3 ReviewBench v4: scoring arithmetic, gold deduplication, bootstrap, blind packet and reveal round
trip, exclusion list and the Direct arm contract. Synthetic data only; no network, model or judge."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

V4 = Path(__file__).resolve().parents[1] / 'benchmarks' / 'reviewbench' / 'v4'
sys.path[:0] = [str(V4), str(V4.parent)]
import arms  # noqa: E402
import judge  # noqa: E402
import plan as planning  # noqa: E402
import provider  # noqa: E402
import score  # noqa: E402
from mea.contract import canonical, digest  # noqa: E402
from mea.investigation import native, real_protocol as protocol, wire  # noqa: E402
from test_cross_file_tools import value  # noqa: E402


def counts(gold, decisions):
    return score.pr_counts(gold, [dict(verdict=v, gold_id=g) for v, g in decisions])


class ScoringTests(unittest.TestCase):
    def test_gold_comments_on_one_defect_become_one_gold_entry(self):
        comments = [dict(comment_id=1, defect='none-sub', path='a.py', line=3, body='None reaches -', summary='None subtraction'),
                    dict(comment_id=2, defect='none-sub', path='a.py', line=4, body='same here', summary=''),
                    dict(comment_id=3, defect='leak', path='b.py', line=9, body='file not closed', summary='Handle leak')]
        gold = score.dedupe_gold('o--r--1', comments)
        self.assertEqual([g['id'] for g in gold], ['o--r--1:D1', 'o--r--1:D2'])
        self.assertEqual((gold[0]['comment_ids'], gold[0]['summary'], len(gold[0]['locations'])), ([1, 2], 'None subtraction', 2))
        with self.assertRaisesRegex(ValueError, 'GOLD_DUPLICATE_COMMENT'):
            score.dedupe_gold('s', comments + [dict(comments[0])])
        with self.assertRaisesRegex(ValueError, 'GOLD_SUMMARY_REQUIRED'):
            score.dedupe_gold('s', [dict(comments[1])])

    def test_counts_claim_each_gold_once_and_penalize_unmatched(self):
        gold = ['g1', 'g2', 'g3']
        row = counts(gold, [('MATCH', 'g1'), ('MATCH', 'g1'), ('FALSE_POSITIVE', None), ('UNDETERMINED', None)])
        m = score.metrics(row)
        self.assertEqual((m['candidates'], m['matched'], m['covered'], m['gold'], m['undetermined']), (4, 1, 1, 3, 1))
        self.assertAlmostEqual(m['precision'], 1 / 4)
        self.assertAlmostEqual(m['recall'], 1 / 3)
        self.assertAlmostEqual(m['f1'], 2 * (1 / 4) * (1 / 3) / (1 / 4 + 1 / 3))
        self.assertAlmostEqual(m['precision_decided'], 1 / 3)
        empty = score.metrics(counts(gold, []))
        self.assertEqual((empty['precision'], empty['recall'], empty['f1']), (None, 0.0, None))
        with self.assertRaisesRegex(ValueError, 'MATCH_GOLD_UNKNOWN'):
            counts(gold, [('MATCH', 'g9')])
        with self.assertRaisesRegex(ValueError, 'NON_MATCH_WITH_GOLD'):
            counts(gold, [('FALSE_POSITIVE', 'g1')])

    def test_micro_pooling_mean_and_range_over_repetitions(self):
        gold = {'p1': ['a'], 'p2': ['b', 'c']}
        run1 = {'p1': counts(gold['p1'], [('MATCH', 'a')]), 'p2': counts(gold['p2'], [('FALSE_POSITIVE', None)])}
        run2 = {'p1': counts(gold['p1'], []), 'p2': counts(gold['p2'], [('MATCH', 'b'), ('MATCH', 'c')])}
        summary = score.arm_summary([run1, run2], ['p1', 'p2'])
        # run1: P=1/2 R=1/3 F1=0.4; run2: P=1 R=2/3 F1=0.8
        self.assertEqual([round(x, 6) for x in summary['f1']['runs']], [0.4, 0.8])
        self.assertAlmostEqual(summary['f1']['mean'], 0.6)
        self.assertEqual((round(summary['f1']['min'], 6), round(summary['f1']['max'], 6)), (0.4, 0.8))
        none = score.arm_summary([{'p1': counts(['a'], []), 'p2': counts(['b'], [])}], ['p1', 'p2'])
        self.assertEqual(none['f1']['mean'], 0.0)  # No candidates at all: F1 counts as 0.

    def test_bootstrap_is_seeded_and_brackets_the_point_estimate(self):
        prs = [f'p{i}' for i in range(12)]
        agent = [{p: counts(['g'], [('MATCH', 'g')] if i % 2 else [('FALSE_POSITIVE', None)]) for i, p in enumerate(prs)}]
        direct = [{p: counts(['g'], [('MATCH', 'g')] if i % 4 == 1 else []) for i, p in enumerate(prs)}]
        a = score.bootstrap_difference(agent, direct, prs, resamples=400, seed=7)
        b = score.bootstrap_difference(agent, direct, prs, resamples=400, seed=7)
        self.assertEqual(a, b)
        self.assertLessEqual(a['low'], a['point'])
        self.assertGreaterEqual(a['high'], a['point'])
        self.assertAlmostEqual(a['point'], score.mean_f1(agent, prs) - score.mean_f1(direct, prs))
        same = score.bootstrap_difference(agent, agent, prs, resamples=100, seed=1)
        self.assertEqual((same['point'], same['low'], same['high']), (0.0, 0.0, 0.0))
        self.assertNotEqual(score.bootstrap_difference(agent, direct, prs, resamples=400, seed=8), a)

    def test_stop_rule_at_ten_points(self):
        self.assertEqual(score.stop_decision(0.40, 0.31)['outcome'], 'NEGATIVE_RESULT_STOP')
        self.assertEqual(score.stop_decision(0.40, 0.30)['outcome'], 'THRESHOLD_MET')
        # Inclusive despite float subtraction: 0.3 - 0.2 is 0.09999999999999998.
        self.assertLess(0.3 - 0.2, score.THRESHOLD)
        self.assertEqual(score.stop_decision(0.3, 0.2)['outcome'], 'THRESHOLD_MET')
        self.assertEqual(score.stop_decision(0.3, 0.2000001)['outcome'], 'NEGATIVE_RESULT_STOP')
        self.assertEqual(score.stop_decision(0.3, 0.2000000005)['outcome'], 'NEGATIVE_RESULT_STOP')
        self.assertEqual(score.stop_decision(0.20, 0.35)['outcome'], 'NEGATIVE_RESULT_STOP')


def candidate(path='shop/pricing.py', line=2, title='t', trigger='x', impact='y'):
    return dict(path=path, side='head', line=line, title=title, trigger=trigger, impact=impact, citations=[], detail={})


class BlindPacketTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = arms.evaluation_snapshot(value())
        self.gold = [dict(id='s:D1', locations=[dict(path='shop/pricing.py', line=2)], summary='None subtraction', bodies=['c'])]
        a, b = candidate(title='None subtraction'), candidate(line=1, title='other')
        self.outputs = {('agent', 1): [a, b], ('direct', 1): [copy.deepcopy(a)], ('static', 1): []}
        self.prs = [dict(slug='s', snapshot=self.snapshot, gold=self.gold, outputs=self.outputs)]

    def judged(self, packet, verdicts):
        return judge.decisions_document(packet, {'s': {c['candidate_id']: verdicts(c) for c in packet['prs'][0]['candidates']}})

    def test_packet_hides_arms_merges_duplicates_and_reveal_restores_them(self):
        packet, mapping = judge.build_packet(self.prs, seed=11)
        text = json.dumps(packet)
        for word in ('agent', 'direct', 'static'):
            self.assertNotIn(f'"{word}"', text)
        self.assertEqual(len(packet['prs'][0]['candidates']), 2)  # The shared candidate appears once.
        self.assertEqual(len(mapping['entries']), 3)
        self.assertEqual(packet['prs'][0]['candidates'][0]['anchor_quote'] is not None, True)
        self.assertEqual(judge.build_packet(self.prs, seed=11), (packet, mapping))
        self.assertNotEqual(judge.build_packet(self.prs, seed=12)[0], packet)

        def verdict(c):
            return (dict(verdict='MATCH', gold_id='s:D1', novel_valid=None, reason='same') if c['title'] == 'None subtraction'
                    else dict(verdict='FALSE_POSITIVE', gold_id=None, novel_valid=False, reason='no'))
        labels = judge.reveal(packet, mapping, self.judged(packet, verdict))
        self.assertEqual([d['verdict'] for d in labels[('agent', 1)]['s']], ['MATCH', 'FALSE_POSITIVE'])
        self.assertEqual([d['verdict'] for d in labels[('direct', 1)]['s']], ['MATCH'])
        self.assertNotIn(('static', 1), labels)
        tampered = copy.deepcopy(packet)
        tampered['prs'][0]['candidates'][0]['title'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'REVEAL_BINDING'):
            judge.reveal(tampered, mapping, self.judged(packet, verdict))
        partial = self.judged(packet, verdict)
        partial['decisions']['s'].popitem()
        with self.assertRaisesRegex(ValueError, 'REVEAL_DECISIONS_INCOMPLETE'):
            judge.reveal(packet, mapping, partial)

    def test_judge_request_and_strict_response(self):
        packet, _ = judge.build_packet(self.prs, seed=11)
        pr = packet['prs'][0]
        body = judge.judge_request(pr)
        self.assertEqual((body['model'], body['thinking'], body['reasoning_effort']), ('deepseek-v4-pro', {'type': 'enabled'}, 'high'))
        self.assertNotIn('agent', body['messages'][1]['content'])
        ids = [c['candidate_id'] for c in pr['candidates']]
        good = dict(decisions=[dict(candidate_id=ids[0], verdict='MATCH', gold_id='s:D1', novel_valid=None, reason='r'),
                               dict(candidate_id=ids[1], verdict='UNDETERMINED', gold_id=None, novel_valid=None, reason='r')])
        parsed = judge.parse_judgement('```json\n' + json.dumps(good) + '\n```', pr)
        self.assertEqual(set(parsed), set(ids))
        for bad, code in [(dict(decisions=good['decisions'][:1]), 'JUDGE_CANDIDATE_SET'),
                          (dict(decisions=[dict(good['decisions'][0], gold_id='s:D9'), good['decisions'][1]]), 'JUDGE_MATCH_GOLD'),
                          (dict(decisions=[good['decisions'][0], dict(good['decisions'][1], gold_id='s:D1')]), 'JUDGE_NON_MATCH_GOLD'),
                          (dict(decisions=good['decisions'] + [good['decisions'][0]]), 'JUDGE_CANDIDATE_SET')]:
            with self.assertRaisesRegex(ValueError, code):
                judge.parse_judgement(json.dumps(bad), pr)
        with self.assertRaisesRegex(ValueError, 'JUDGE_JSON'):
            judge.parse_judgement('not json', pr)
        # Unhashable ids are the documented ValueError, not a TypeError that stops the judge loop.
        for field, bad in [('candidate_id', [ids[0]]), ('candidate_id', {'id': ids[0]}), ('gold_id', ['s:D1']), ('gold_id', {'g': 1})]:
            with self.subTest(field=field, bad=bad), self.assertRaisesRegex(ValueError, 'JUDGE_'):
                judge.parse_judgement(json.dumps(dict(decisions=[dict(good['decisions'][0], **{field: bad}), good['decisions'][1]])), pr)

    def test_agreement_sample_is_seeded_blind_and_scored(self):
        prs = []
        for i in range(5):
            outputs = {('agent', 1): [candidate(line=1, title=f'a{i}-{j}') for j in range(4)], ('direct', 1): [candidate(line=2, title=f'd{i}')]}
            prs.append(dict(slug=f's{i}', snapshot=self.snapshot, gold=self.gold, outputs=outputs))
        packet, mapping = judge.build_packet(prs, seed=3)
        doc = judge.decisions_document(packet, {pr['slug']: {c['candidate_id']: dict(
            verdict='FALSE_POSITIVE', gold_id=None, novel_valid=None, reason='r') for c in pr['candidates']} for pr in packet['prs']})
        review, key = judge.agreement_packet(packet, doc, mapping, share=0.2, seed=5)
        self.assertEqual((review['population'], review['sampled']), (25, 5))
        self.assertEqual(judge.agreement_packet(packet, doc, mapping, share=0.2, seed=5), (review, key))
        text = json.dumps(review)
        self.assertNotIn('FALSE_POSITIVE"', text.replace('MATCH|FALSE_POSITIVE|UNDETERMINED', ''))
        self.assertNotIn('"agent"', text)
        self.assertEqual(key['review_sha256'], digest(review))
        same = {i: dict(verdict='FALSE_POSITIVE', gold_id=None) for i in key['items']}
        result = judge.agreement(key, same)
        self.assertEqual((result['items'], result['verdict_agreement'], result['match_agreement']), (5, 1.0, 1.0))
        flipped = dict(same)
        first = sorted(flipped)[0]
        flipped[first] = dict(verdict='MATCH', gold_id='s:D1')
        self.assertEqual(judge.agreement(key, flipped)['verdict_agreement'], 0.8)
        with self.assertRaisesRegex(ValueError, 'AGREEMENT_ITEMS'):
            judge.agreement(key, {})


class PlanAndExclusionTests(unittest.TestCase):
    exclusions = dict(schema=planning.EXCLUSIONS_SCHEMA, repositories=['urllib3/urllib3', 'resplab/leap'],
                      pull_requests=['urllib3/urllib3#5316', 'other/lib#7'])

    def sample(self, repo, number, **extra):
        return dict(dict(repository=repo, number=number, pin='a' * 40, license='MIT', changed_files=3, changed_lines=80,
                         gold_defects=1, slug=f'{repo}#{number}'), **extra)

    def sets(self, dev, holdout):
        return dict(dev=dev, holdout=holdout)

    def test_exclusion_list_blocks_prs_and_repositories(self):
        self.assertEqual(planning.excluded_reason(self.sample('URLLIB3/urllib3', 9), self.exclusions), 'EXCLUDED_REPOSITORY')
        self.assertEqual(planning.excluded_reason(self.sample('other/lib', 7), self.exclusions), 'EXCLUDED_PULL_REQUEST')
        self.assertIsNone(planning.excluded_reason(self.sample('other/lib', 8), self.exclusions))
        holdout = [self.sample(f'h/r{i}', 1) for i in range(30)]
        self.assertTrue(planning.check_samples(self.sets([self.sample('d/r', 1)], holdout), self.exclusions))
        with self.assertRaisesRegex(ValueError, 'EXCLUDED_REPOSITORY:resplab/leap#48'):
            planning.check_samples(self.sets([self.sample('resplab/leap', 48)], holdout), self.exclusions)
        with self.assertRaisesRegex(ValueError, 'EXCLUDED_PULL_REQUEST'):
            planning.check_samples(self.sets([self.sample('other/lib', 7)], holdout), self.exclusions)

    def test_split_and_admission_rules(self):
        holdout = [self.sample(f'h/r{i}', 1) for i in range(30)]
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_SAMPLE'):
            planning.check_samples(self.sets([self.sample('h/r0', 1)], holdout), self.exclusions)
        with self.assertRaisesRegex(ValueError, 'REPOSITORY_IN_BOTH_SETS'):
            planning.check_samples(self.sets([self.sample('h/r0', 2)], holdout), self.exclusions)
        with self.assertRaisesRegex(ValueError, 'HOLDOUT_TOO_SMALL'):
            planning.check_samples(self.sets([], holdout[:29]), self.exclusions)
        for extra, code in [(dict(license='GPL-3.0'), 'LICENSE'), (dict(changed_lines=501), 'SIZE_BOUND'),
                            (dict(changed_files=16), 'SIZE_BOUND'), (dict(gold_defects=0), 'NO_GOLD')]:
            with self.assertRaisesRegex(ValueError, code):
                planning.check_samples(self.sets([self.sample('d/r', 1, **extra)], holdout), self.exclusions)

    def test_holdout_needs_frozen_plan_and_matching_approval(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'PLAN.json'
            path.write_bytes(canonical(dict(schema=planning.PLAN_SCHEMA, status='FROZEN')))
            plan, sha = planning.load_plan(path)
            with self.assertRaisesRegex(ValueError, 'PLAN_SHA256_MISMATCH'):
                planning.load_plan(path, '0' * 64)
            self.assertTrue(planning.holdout_admission(plan, sha, dict(plan_sha256=sha, coordinator_reply='msg-1')))
            for approval in (None, dict(plan_sha256='0' * 64, coordinator_reply='msg-1'), dict(plan_sha256=sha, coordinator_reply='')):
                with self.assertRaisesRegex(ValueError, 'HOLDOUT_APPROVAL_REQUIRED'):
                    planning.holdout_admission(plan, sha, approval)
            with self.assertRaisesRegex(ValueError, 'PLAN_NOT_FROZEN'):
                planning.holdout_admission(dict(plan, status='DRAFT'), sha, dict(plan_sha256=sha, coordinator_reply='m'))


class FakeConnection:
    def __init__(self, raw, status=200):
        self.raw, self.status, self.posts = raw, status, []

    def __call__(self, host, port, timeout):
        return self

    def request(self, method, path, body, headers):
        self.posts.append(json.loads(body))

    def getresponse(self):
        outer = self

        class R:
            status = outer.status

            def read(self, n):
                return outer.raw[:n]

            def getheader(self, name):
                return 'req-1' if name == 'x-request-id' else None
        return R()

    def close(self):
        pass


def completion(arguments, finish='tool_calls'):
    call = dict(id='call-1', type='function', function=dict(name=native.FINISH, arguments=json.dumps(arguments)))
    return json.dumps(dict(id='cmpl', object='chat.completion', model=protocol.MODEL,
                           choices=[dict(index=0, finish_reason=finish, message=dict(role='assistant', content=None, tool_calls=[call]))],
                           usage=dict(prompt_tokens=1000, completion_tokens=100, total_tokens=1100,
                                      prompt_cache_hit_tokens=0, prompt_cache_miss_tokens=1000))).encode()


def admit_many(path, cap, amount, attempts):
    ledger = provider.Ledger(path, cap)
    count = 0
    for _ in range(attempts):
        try:
            ledger.admit(amount)
            count += 1
        except ValueError:
            pass
    return count


def fake_provider():
    """Spawned-worker initializer: a fake DeepSeek key and connection that always finishes with zero candidates."""
    from mea.investigation import deepseek
    real = deepseek.Sender

    class Finish(FakeConnection):
        def __init__(self):
            super().__init__(completion(dict(reason='ZERO_CANDIDATES', summary='s', candidates=[])))
    deepseek.read_key = lambda *a: 'sk-' + 'a' * 32
    deepseek.Sender = lambda secret: real(secret, connect=lambda h, p, timeout: Finish())


class DirectArmTests(unittest.TestCase):
    def setUp(self):
        guard = patch('socket.socket.connect', side_effect=AssertionError('NO_NETWORK'))
        guard.start()
        self.addCleanup(guard.stop)
        self.snapshot = arms.evaluation_snapshot(value())
        self.body, self.context = arms.direct_request(self.snapshot)

    def finish(self, candidates, reason='HYPOTHESIS_SUPPORTED', finish='tool_calls'):
        conn = FakeConnection(completion(dict(reason=reason, summary='s', candidates=candidates), finish))
        receipt = wire.call(self.body, 'sk-' + 'a' * 32, connect=conn, validate=arms.validate_direct_body)
        return arms.direct_result(receipt, self.context), conn

    def anchor(self):
        rev = self.snapshot['revisions']['head']
        alias = next(a for a, i in self.context['aliases'].items() if a.startswith('h1-')
                     and any(c['id'] == i and ('head', c['path'], c['line']) in self.context['view'].changes for c in self.context['shown']))
        line = int(alias.split('-')[1])
        return dict(title='discount None', trigger='checkout() without discount', impact='TypeError',
                    change=dict(path='shop/pricing.py', side='head', line=line, revision=rev), evidence_ids=[alias])

    def test_pr_text_is_withheld_from_every_arm_input(self):
        raw = value()
        raw['pr']['title'], raw['pr']['body'] = 'Fix the None bug', 'Reviewer said discount None crashes'
        snapshot = arms.evaluation_snapshot(raw)
        self.assertEqual((snapshot['pr']['title'], snapshot['pr']['body']), ('', ''))
        self.assertEqual(snapshot['files'], raw['files'])
        body, _ = arms.direct_request(snapshot)
        self.assertNotIn('None bug', canonical(body).decode())
        self.assertNotIn('discount None crashes', canonical(body).decode())
        with self.assertRaisesRegex(ValueError, 'PR_TEXT_NOT_WITHHELD'):
            arms.static_candidates(raw)
        with self.assertRaisesRegex(ValueError, 'PR_TEXT_NOT_WITHHELD'):
            arms.direct_request(raw)
        profile = protocol.FIRST_TURN_PROFILES[protocol.FIRST_TURN_NO_PR_TEXT]
        self.assertEqual((profile['pull_request'], profile['initial_diff_bytes']), (False, 30000))
        planned = protocol.FIRST_TURN_PROFILES[protocol.FIRST_TURN_NO_PR_TEXT_PLANNED]
        self.assertFalse(planned['pull_request'])
        task = dict(identity=dict(first_turn=protocol.FIRST_TURN_NO_PR_TEXT_PLANNED, tool_profile=native.CROSS_FILE))
        self.assertTrue(protocol.task_prompt(task).endswith(protocol.PLANNING_NOTE))
        self.assertNotIn(protocol.PR_TEXT_NOTE, protocol.task_prompt(task))
        base = dict(identity=dict(first_turn=protocol.FIRST_TURN_NO_PR_TEXT, tool_profile=native.CROSS_FILE))
        self.assertNotIn(protocol.PLANNING_NOTE, protocol.task_prompt(base))

    def test_direct_uses_the_agent_finish_schema_and_validation(self):
        self.assertEqual(self.body['tools'], [t for t in native.definitions(native.CROSS_FILE) if t['function']['name'] == native.FINISH])
        self.assertEqual(self.body['tool_choice'], protocol.FINISH_CHOICE)
        result, conn = self.finish([self.anchor()])
        self.assertEqual(len(conn.posts), 1)
        self.assertEqual((result['status'], result['reason'], len(result['candidates'])), ('COMPLETED', 'HYPOTHESIS_SUPPORTED', 1))
        c = result['candidates'][0]
        self.assertEqual((c['path'], c['side'], c['title']), ('shop/pricing.py', 'head', 'discount None'))
        self.assertEqual(c['citations'][0]['path'], 'shop/pricing.py')
        # An evidence ID that was never shown is rejected by the shared validator.
        bad = dict(self.anchor(), evidence_ids=['h9-1'])
        result, _ = self.finish([bad])
        self.assertEqual((result['candidates'], result['rejected'][0]['reason']), ([], 'REFERENCE_INVALID_OR_UNOBSERVED'))
        # An anchor on an unchanged line is rejected.
        unchanged = self.anchor()
        self.assertNotIn(('head', 'web/format.ts', 3), self.context['view'].changes)
        unchanged['change'].update(path='web/format.ts', line=3)
        unchanged['evidence_ids'] = ['h2-3']
        result, _ = self.finish([unchanged])
        self.assertEqual(result['candidates'], [])

    def test_direct_failures_are_typed_outcomes(self):
        result, _ = self.finish([], reason='ZERO_CANDIDATES')
        self.assertEqual((result['status'], result['reason'], result['candidates']), ('COMPLETED', 'ZERO_CANDIDATES', []))
        result, _ = self.finish([self.anchor()], finish='length')
        self.assertEqual((result['status'], result['reason']), ('STOPPED', 'PROVIDER_OUTPUT_INCOMPLETE'))
        receipt = wire.call(self.body, 'sk-' + 'a' * 32, connect=FakeConnection(b'{"error":"busy"}', 503), validate=arms.validate_direct_body)
        self.assertEqual(arms.direct_result(receipt, self.context)['reason'], 'HTTP_ERROR')
        with self.assertRaisesRegex(ValueError, 'DIRECT_REQUEST_BOUND'):
            arms.validate_direct_body(dict(self.body, tools=native.definitions(native.CROSS_FILE)))

    def test_direct_payload_bound_drops_full_rows_but_keeps_the_diff(self):
        full = len(canonical(json.loads(self.body['messages'][1]['content'])))
        with patch.object(arms, 'DIRECT_MAX_BYTES', full - 1):
            body, context = arms.direct_request(self.snapshot)
        self.assertEqual(len(context['truncated']), 1)
        with patch.object(arms, 'DIRECT_MAX_BYTES', 200), self.assertRaisesRegex(ValueError, 'DIRECT_INPUT_BOUND'):
            arms.direct_request(self.snapshot)
        payload = json.loads(body['messages'][1]['content'])
        self.assertTrue(context['truncated'])
        dropped = [f for f in payload['files'] if f.get('head_rows_omitted')]
        self.assertTrue(dropped and all(f['diff'] for f in dropped))
        self.assertTrue(all(not a.startswith('h') or int(a[1:].split('-')[0]) not in
                            [i for i, f in enumerate(payload['files'], 1) if f.get('head_rows_omitted')] for a in context['aliases']))

    def test_cost_and_ledger_caps(self):
        usage = dict(prompt_tokens=1000, completion_tokens=100, total_tokens=1100, prompt_cache_hit_tokens=400, prompt_cache_miss_tokens=600)
        flash = provider.cost('deepseek-flash', usage, at=0)
        self.assertEqual(flash['peak'], 400 * 6 + 600 * 300 + 100 * 1200)
        pro = provider.cost('deepseek-v4-pro', usage, at=0)
        self.assertEqual(pro['peak'], 400 * 44 + 600 * 1320 + 100 * 3960)
        self.assertIsNone(provider.cost('deepseek-flash', None))
        with tempfile.TemporaryDirectory() as tmp:
            ledger = provider.Ledger(Path(tmp) / 'l.jsonl', cap_nano=1000)
            first = ledger.admit(600)
            with self.assertRaisesRegex(ValueError, 'LEDGER_CAP'):
                ledger.admit(500)  # The open reservation counts at its reserved bound.
            ledger.record(first, peak_nano=100, reserved_nano=600)
            ledger.admit(500)  # Settled at 100: 600 used now.
            self.assertEqual(ledger.spent_peak(), 600)
            with self.assertRaisesRegex(ValueError, 'REQUEST_USD_CAP'):
                provider.Ledger(Path(tmp) / 'm.jsonl', cap_nano=10 ** 12).admit(provider.REQUEST_CAP_NANO + 1)
            legacy = Path(tmp) / 'legacy.jsonl'
            legacy.write_text(json.dumps(dict(arm='direct', peak_nano=300, reserved_nano=900)) + '\n')
            self.assertEqual(provider.Ledger(legacy, 1000).spent_peak(), 300)

    def test_ledger_never_admits_past_its_cap_under_concurrent_processes(self):
        import multiprocessing
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'l.jsonl'
            ctx = multiprocessing.get_context('spawn')
            with ctx.Pool(4) as pool:
                admitted = sum(pool.starmap(admit_many, [(str(path), 1000, 7, 40)] * 4))
            ledger = provider.Ledger(path, 1000)
            self.assertEqual(admitted, 1000 // 7)
            self.assertLessEqual(ledger.spent_peak(), 1000)
            self.assertEqual(len([e for e in ledger.entries() if e['kind'] == 'reserve']), admitted)



class OperatorFlowTests(unittest.TestCase):
    """End-to-end m3.py flow on one synthetic sample with fake transports: run, judge, score, holdout guard."""

    def setUp(self):
        guard = patch('socket.socket.connect', side_effect=AssertionError('NO_NETWORK'))
        guard.start()
        self.addCleanup(guard.stop)
        import m3
        self.m3 = m3
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.private, self.runs = root / 'private', root / 'runs'
        snapshot = arms.evaluation_snapshot(value())
        sample = self.private / 'samples' / 'owned--fixture--1'
        sample.mkdir(parents=True)
        (sample / 'eval-snapshot.json').write_bytes(canonical(snapshot))
        gold = score.dedupe_gold('owned--fixture--1', [dict(comment_id=1, defect='k', path='shop/pricing.py', line=2,
                                                            body='discount None crashes', summary='None discount subtraction')])
        (sample / 'gold.json').write_bytes(canonical(dict(defects=gold)))
        row = dict(slug='owned--fixture--1', repository='owned/fixture', number=1, pin=snapshot['revisions']['head'],
                   input_sha256=digest(snapshot))
        self.plan = dict(schema=planning.PLAN_SCHEMA, status='DRAFT', samples=dict(dev=[row], holdout=[row]),
                         budget=dict(ledger_cap_usd=1), judge=dict(packet_seed='s'), statistics=dict(bootstrap_resamples=50, bootstrap_seed=1),
                         arms=dict(agent=dict(tool_profile=native.CROSS_FILE, first_turn=protocol.FIRST_TURN_NO_PR_TEXT,
                                              task_budget=protocol.budget(dict(output_bytes=500000)), scope_budget_usd=1, approval='unit test')),
                         frozen=dict(code_sha='f' * 40))
        self.plan_path = root / 'PLAN.json'
        self.plan_path.write_bytes(canonical(self.plan))
        self.anchor_line = 2

    def finish_body(self, body):
        payload = json.loads(body['messages'][1]['content'])
        rev = payload['revisions']['head']
        if 'files' in payload and isinstance(payload['files'][0], dict) and 'head_rows' in payload['files'][0]:
            ids = [r[1] for r in payload['files'][0]['head_rows'] if r[0] == self.anchor_line]
        else:
            ids = []
        cands = [dict(title='discount None', trigger='checkout() without discount', impact='TypeError',
                      change=dict(path='shop/pricing.py', side='head', line=self.anchor_line, revision=rev), evidence_ids=ids)] if ids else []
        return completion(dict(reason='HYPOTHESIS_SUPPORTED' if cands else 'ZERO_CANDIDATES', summary='s', candidates=cands))

    def cli(self, *argv):
        out = io.StringIO()
        with patch('sys.stdout', out):
            self.m3.main(['--private', str(self.private), *argv])
        return out.getvalue()

    def test_dev_round_runs_judges_and_scores(self):
        from mea.investigation import deepseek
        real_sender = deepseek.Sender

        class Conn(FakeConnection):
            def __init__(inner):
                super().__init__(b'')

            def request(inner, method, path, body, headers):
                inner.raw = self.finish_body(json.loads(body))

        def judge_call(body, secret, connect=None):
            user = json.loads(body['messages'][1]['content'])
            decisions = [dict(candidate_id=c['candidate_id'], verdict='MATCH', gold_id=user['gold'][0]['gold_id'], novel_valid=None, reason='same')
                         for c in user['candidates']]
            return dict(http_status=200, finish_reason='stop', content=json.dumps(dict(decisions=decisions)),
                        usage=dict(prompt_tokens=10, completion_tokens=5, total_tokens=15))
        def round_args(name):
            return ['--plan', str(self.plan_path), '--set', 'dev', '--round', name, '--runs', str(self.runs)]
        common = round_args('r1')
        with patch.object(deepseek, 'read_key', lambda *a: 'sk-' + 'a' * 32), \
                patch.object(self.m3.provider, 'direct_call', lambda body, secret, validate: wire.call(body, secret, connect=Conn(), validate=validate)), \
                patch.object(deepseek, 'Sender', lambda secret: real_sender(secret, connect=lambda h, p, timeout: Conn())), \
                patch.object(self.m3.provider, 'judge_call', judge_call), patch.object(self.m3, 'git_state', lambda: ('f' * 40, False)):
            self.cli('run', *common, '--arm', 'static', '--rep', '1')
            with self.assertRaisesRegex(SystemExit, 'confirm-real-provider'):
                self.cli('run', *common, '--arm', 'direct', '--rep', '1')
            self.cli('run', *common, '--arm', 'direct', '--rep', '1', '--confirm-real-provider')
            self.cli('run', *common, '--arm', 'agent', '--rep', '1', '--confirm-real-provider')
            with self.assertRaisesRegex(SystemExit, 'already ran'):
                self.cli('run', *common, '--arm', 'direct', '--rep', '1', '--confirm-real-provider')
            self.cli('judge', *common, '--confirm-real-provider')
            self.cli('score', *common)
            with self.assertRaisesRegex(SystemExit, 'workers'):
                self.cli('run', *round_args('r9'), '--arm', 'direct', '--rep', '1', '--confirm-real-provider', '--workers', '2')
            self.plan['concurrency'] = dict(max_in_flight_per_arm=4)
            self.plan_path.write_bytes(canonical(self.plan))
            parallel = round_args('r2')
            self.cli('run', *parallel, '--arm', 'direct', '--rep', '1', '--confirm-real-provider', '--workers', '4')
            with self.assertRaisesRegex(SystemExit, 'workers'):
                self.cli('run', *parallel, '--arm', 'direct', '--rep', '2', '--confirm-real-provider', '--workers', '5')
        base = self.runs / 'dev' / 'r1'
        direct = json.loads((base / 'direct' / 'rep1' / 'owned--fixture--1.json').read_text())
        agent = json.loads((base / 'agent' / 'rep1' / 'owned--fixture--1.json').read_text())
        self.assertEqual((direct['status'], len(direct['candidates'])), ('COMPLETED', 1))
        self.assertEqual(agent['status'], 'COMPLETED')
        result = json.loads((base / 'score.json').read_text())
        self.assertEqual(result['arms']['direct']['f1']['mean'], 1.0)
        self.assertEqual(result['arms']['static']['f1']['mean'], 0.0)
        self.assertIn(result['stop_rule']['outcome'], ('THRESHOLD_MET', 'NEGATIVE_RESULT_STOP'))
        ledger = [json.loads(l) for l in (self.private / 'ledger.jsonl').read_text().splitlines()]
        self.assertEqual(sorted({e['arm'] for e in ledger}), ['agent', 'direct', 'judge'])
        parallel = json.loads((self.runs / 'dev' / 'r2' / 'direct' / 'rep1' / 'owned--fixture--1.json').read_text())
        self.assertEqual((parallel['concurrency'], len(parallel['candidates'])), (4, 1))
        self.assertEqual(json.loads((self.runs / 'dev' / 'r2' / 'direct' / 'rep1' / 'manifest.json').read_text())['workers'], 4)
        self.assertEqual([len(b) for b in self.m3.agent_batches(list(range(35)), 4)], [8, 8, 8, 8, 3])
        self.assertEqual([len(b) for b in self.m3.agent_batches(list(range(8)), 4)], [2, 2, 2, 2])
        self.assertEqual([len(b) for b in self.m3.agent_batches(list(range(8)), 1)], [8])
        packet = (base / 'judge' / 'packet.json').read_text()
        self.assertNotIn('"agent"', packet)
        self.assertNotIn('"direct"', packet)

    def test_agent_batches_run_in_separate_processes_and_stores(self):
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor
        snapshot = arms.evaluation_snapshot(value())
        other = json.loads(json.dumps(value()))
        other['files'][0]['sides']['head']['text'] += ''
        rows = [dict(slug='owned--fixture--1', input_sha256=digest(snapshot))]
        out = self.runs / 'dev' / 'p' / 'agent' / 'rep1'
        ledger = self.private / 'ledger.jsonl'
        tags = dict(set='dev', round='p', rep=1, plan_sha256='0' * 64, code_sha='f' * 40, workers=2)
        with ProcessPoolExecutor(2, mp_context=multiprocessing.get_context('spawn'), initializer=fake_provider) as pool:
            futures = [pool.submit(self.m3.agent_batch, str(self.private), b, rows, out, ledger, 10 ** 9,
                                   dict(tags, rep=b), self.plan['arms']['agent']) for b in (1, 2)]
            lines = [f.result() for f in futures]
        self.assertEqual([l[0]['status'] for l in lines], ['COMPLETED', 'COMPLETED'])
        stores = sorted(p.name for p in (self.private / 'stores').iterdir())
        self.assertEqual(stores, ['mea-live-m3-dev-p-r1-b1', 'mea-live-m3-dev-p-r2-b2'])
        entries = provider.Ledger(ledger, 10 ** 9).entries()
        self.assertEqual(sorted(e['kind'] for e in entries), ['reserve', 'reserve', 'settle', 'settle'])

    def frozen_holdout(self, exclusions=None):
        row = dict(self.plan['samples']['holdout'][0], license='MIT', changed_files=1, changed_lines=2, gold_defects=1)
        listing = self.private / 'selection' / 'exclusions.json'
        listing.parent.mkdir(parents=True, exist_ok=True)
        listing.write_bytes(canonical(exclusions or dict(schema=planning.EXCLUSIONS_SCHEMA, repositories=['other/repo'], pull_requests=[])))
        frozen = dict(self.plan, status='FROZEN', samples=dict(dev=[], holdout=[row]), exclusions=dict(sha256=planning.file_sha256(listing)))
        self.plan_path.write_bytes(canonical(frozen))
        approval = Path(self.temp.name) / 'approval.json'
        approval.write_bytes(canonical(dict(plan_sha256=planning.file_sha256(self.plan_path), coordinator_reply='msg-1')))
        return approval, listing

    def test_holdout_requires_frozen_plan_approval_and_code_sha(self):
        common = ['--plan', str(self.plan_path), '--set', 'holdout', '--round', 'final', '--runs', str(self.runs), '--arm', 'static', '--rep', '1']
        with self.assertRaisesRegex(ValueError, 'PLAN_NOT_FROZEN'):
            self.cli('run', *common)
        frozen = dict(self.plan, status='FROZEN')
        self.plan_path.write_bytes(canonical(frozen))
        with self.assertRaisesRegex(ValueError, 'HOLDOUT_APPROVAL_REQUIRED'):
            self.cli('run', *common)
        approval, listing = self.frozen_holdout()
        with patch.object(planning, 'MIN_HOLDOUT', 1):
            with patch.object(self.m3, 'git_state', lambda: ('e' * 40, False)), self.assertRaisesRegex(SystemExit, 'frozen code SHA'):
                self.cli('run', *common, '--approval', str(approval))
            with patch.object(self.m3, 'git_state', lambda: ('f' * 40, False)):
                self.cli('run', *common, '--approval', str(approval))
                with self.assertRaisesRegex(SystemExit, 'already ran'):
                    self.cli('run', *common, '--approval', str(approval))
                # A completed first result is the sample; only an infrastructure stop or a missing output reruns.
                with self.assertRaisesRegex(SystemExit, 'infrastructure stop; kept: owned--fixture--1'):
                    self.cli('run', *common, '--approval', str(approval), '--only', 'owned--fixture--1', '--reason', 'not infra')
                with self.assertRaisesRegex(SystemExit, 'outside the set'):
                    self.cli('run', *common, '--approval', str(approval), '--only', 'owned--fixture--9', '--reason', 'typo')
                out = self.runs / 'holdout' / 'final' / 'static' / 'rep1'
                first = json.loads((out / 'owned--fixture--1.json').read_text())
                stopped = dict(first, status='STOPPED', reason='CONNECTION_OR_RESPONSE_UNCERTAIN')
                (out / 'owned--fixture--1.json').write_bytes(canonical(stopped))
                self.cli('run', *common, '--approval', str(approval), '--only', 'owned--fixture--1', '--reason', 'unit test infra rerun')
        reruns = [json.loads(l) for l in (out / 'reruns.jsonl').read_text().splitlines()]
        self.assertEqual(len(reruns), 1)
        self.assertEqual(reruns[0]['reason'], 'unit test infra rerun')
        # The billed first attempt is kept under superseded/<rerun>/, and the rerun writes a new output.
        self.assertEqual(reruns[0]['superseded'], [f"superseded/{reruns[0]['rerun']}/owned--fixture--1.json"])
        self.assertEqual(json.loads((out / reruns[0]['superseded'][0]).read_text()), stopped)
        self.assertEqual(json.loads((out / 'owned--fixture--1.json').read_text())['status'], first['status'])

    def test_holdout_applies_the_preregistered_sample_checks(self):
        common = ['--plan', str(self.plan_path), '--set', 'holdout', '--round', 'final', '--runs', str(self.runs), '--arm', 'static', '--rep', '1']
        approval, listing = self.frozen_holdout()
        with patch.object(self.m3, 'git_state', lambda: ('f' * 40, False)):
            with self.assertRaisesRegex(SystemExit, 'HOLDOUT_TOO_SMALL'):
                self.cli('run', *common, '--approval', str(approval))
            listing.write_bytes(listing.read_bytes() + b' ')
            with patch.object(planning, 'MIN_HOLDOUT', 1), self.assertRaisesRegex(SystemExit, 'SHA-256 recorded in PLAN.json'):
                self.cli('run', *common, '--approval', str(approval))
            approval, listing = self.frozen_holdout(dict(schema=planning.EXCLUSIONS_SCHEMA, repositories=['owned/fixture'], pull_requests=[]))
            with patch.object(planning, 'MIN_HOLDOUT', 1), self.assertRaisesRegex(SystemExit, 'EXCLUDED_REPOSITORY:owned/fixture#1'):
                self.cli('run', *common, '--approval', str(approval))
        self.assertFalse((self.runs / 'holdout').exists())

    def test_rerun_guards_stage_lock_and_judged_rounds(self):
        common = ['--plan', str(self.plan_path), '--set', 'dev', '--round', 'g', '--runs', str(self.runs), '--arm', 'static', '--rep', '1']
        out = self.runs / 'dev' / 'g' / 'static' / 'rep1'
        with patch.object(self.m3, 'git_state', lambda: ('f' * 40, False)):
            # --only is checked on a first run too.
            with self.assertRaisesRegex(SystemExit, 'outside the set'):
                self.cli('run', *common, '--only', 'owned--fixture--9')
            # A recorded attempt without results still makes the next run a rerun.
            with patch.object(self.m3, 'run_static', lambda *a: (_ for _ in ()).throw(RuntimeError('killed'))):
                with self.assertRaisesRegex(RuntimeError, 'killed'):
                    self.cli('run', *common)
            self.assertEqual(list(out.glob('*.json')), [])
            with self.assertRaisesRegex(SystemExit, 'already ran'):
                self.cli('run', *common)
            self.cli('run', *common, '--only', 'owned--fixture--1', '--reason', 'first attempt killed')
            first = json.loads((out / 'owned--fixture--1.json').read_text())
            # A failing rerun leaves the active result in place.
            (out / 'owned--fixture--1.json').write_bytes(canonical(dict(first, status='STOPPED', reason='HTTP_ERROR')))
            with patch.object(self.m3, 'load_eval', lambda *a: (_ for _ in ()).throw(ValueError('INPUT_SHA256_MISMATCH:x'))):
                with self.assertRaisesRegex(ValueError, 'INPUT_SHA256_MISMATCH'):
                    self.cli('run', *common, '--only', 'owned--fixture--1', '--reason', 'bad snapshot')
            self.assertEqual(json.loads((out / 'owned--fixture--1.json').read_text())['reason'], 'HTTP_ERROR')
            self.assertFalse((out / 'superseded').exists())
            # A second invocation on the same arm and repetition is refused while one holds the lock.
            with self.m3.arm_lock(out), self.assertRaisesRegex(SystemExit, 'another run holds'):
                self.cli('run', *common, '--only', 'owned--fixture--1', '--reason', 'parallel')
            # Once the round is judged, reruns go to a new round.
            (out.parent.parent / 'judge').mkdir(parents=True)
            (out.parent.parent / 'judge' / 'packet.json').write_bytes(b'{}')
            with self.assertRaisesRegex(SystemExit, 'already judged'):
                self.cli('run', *common, '--only', 'owned--fixture--1', '--reason', 'after judging')

    def test_score_refuses_outputs_changed_after_judging(self):
        common = ['--plan', str(self.plan_path), '--set', 'dev', '--round', 'sj', '--runs', str(self.runs)]
        calls = []
        def judge_call(body, secret, connect=None):
            calls.append(1)
            user = json.loads(body['messages'][1]['content'])
            decisions = [dict(candidate_id=c['candidate_id'], verdict='FALSE_POSITIVE', gold_id=None, novel_valid=None, reason='r')
                         for c in user['candidates']]
            return dict(http_status=200, finish_reason='stop', content=json.dumps(dict(decisions=decisions)), usage=dict(prompt_tokens=1, completion_tokens=1))
        static = self.runs / 'dev' / 'sj' / 'static' / 'rep1'
        with patch.object(self.m3, 'git_state', lambda: ('f' * 40, False)), patch.object(self.m3.provider, 'judge_call', judge_call), \
                patch('mea.investigation.deepseek.read_key', lambda *a: 'sk-' + 'a' * 32):
            self.cli('run', *common, '--arm', 'static', '--rep', '1')
            result = json.loads((static / 'owned--fixture--1.json').read_text())
            fake = dict(path='shop/pricing.py', side='head', line=2, title='t', trigger='g', impact='i', citations=[], candidate_id='x')
            (static / 'owned--fixture--1.json').write_bytes(canonical(dict(result, candidates=[fake])))
            self.cli('judge', *common, '--confirm-real-provider')
            self.cli('score', *common)
            (static / 'owned--fixture--1.json').write_bytes(canonical(dict(result, candidates=[dict(fake, title='changed')])))
            with self.assertRaisesRegex(SystemExit, 'changed after judging'):
                self.cli('score', *common)
        self.assertEqual(calls, [1])

    def test_agreement_with_explicit_runs_needs_no_private_directory(self):
        with patch.object(self.m3, 'PRIVATE', None), self.assertRaises(SystemExit):
            with patch('sys.stderr', io.StringIO()):
                self.m3.main(['score', '--plan', str(self.plan_path), '--set', 'dev', '--round', 'r1'])
        jdir = self.runs / 'dev' / 'a' / 'judge' / 'agreement'
        jdir.mkdir(parents=True)
        (jdir / 'judge-only-key.json').write_bytes(canonical(dict(items={})))
        reviewer = Path(self.temp.name) / 'reviewer.json'
        reviewer.write_bytes(canonical({}))
        with patch.object(self.m3, 'PRIVATE', None), patch.object(self.m3.judging, 'agreement', lambda key, rev: dict(n=0)), patch('sys.stdout', io.StringIO()):
            self.m3.main(['agreement', '--set', 'dev', '--round', 'a', '--runs', str(self.runs), '--reviewer', str(reviewer)])
        self.assertEqual(json.loads((jdir / 'agreement.json').read_text()), dict(n=0))

    def test_agent_rerun_gets_its_own_scope_and_store(self):
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor
        snapshot = arms.evaluation_snapshot(value())
        rows = [dict(slug='owned--fixture--1', input_sha256=digest(snapshot))]
        out = self.runs / 'dev' / 'p' / 'agent' / 'rep1'
        ledger = self.private / 'ledger.jsonl'
        tags = dict(set='dev', round='p', rep=1, plan_sha256='0' * 64, code_sha='f' * 40, workers=1)
        with ProcessPoolExecutor(1, mp_context=multiprocessing.get_context('spawn'), initializer=fake_provider) as pool:
            first = pool.submit(self.m3.agent_batch, str(self.private), 1, rows, out, ledger, 10 ** 9, tags, self.plan['arms']['agent']).result()
            again = pool.submit(self.m3.agent_batch, str(self.private), 1, rows, out, ledger, 10 ** 9, dict(tags, rerun='0a1b2c3d'),
                                self.plan['arms']['agent']).result()
        self.assertEqual([first[0]['requests'], again[0]['requests']], [1, 1])  # The rerun sent its own request.
        self.assertEqual(sorted(p.name for p in (self.private / 'stores').iterdir()), ['mea-live-m3-dev-p-r1-b1', 'mea-live-m3-dev-p-r1-b1-x0a1b2c3d'])
        self.assertEqual(sorted(p.name for p in (out / 'scopes').iterdir()), ['mea-live-m3-dev-p-r1-b1-x0a1b2c3d.json', 'mea-live-m3-dev-p-r1-b1.json'])

    def test_judge_admission_refusal_keeps_the_loop_and_writes_decisions(self):
        from mea.investigation import deepseek

        class Conn(FakeConnection):
            def __init__(inner):
                super().__init__(b'')

            def request(inner, method, path, body, headers):
                inner.raw = self.finish_body(json.loads(body))
        common = ['--plan', str(self.plan_path), '--set', 'dev', '--round', 'rj', '--runs', str(self.runs)]
        calls = []
        with patch.object(deepseek, 'read_key', lambda *a: 'sk-' + 'a' * 32), \
                patch.object(self.m3.provider, 'direct_call', lambda body, secret, validate: wire.call(body, secret, connect=Conn(), validate=validate)), \
                patch.object(self.m3.provider, 'judge_call', lambda *a, **k: calls.append(a)), patch.object(self.m3, 'git_state', lambda: ('f' * 40, False)):
            self.cli('run', *common, '--arm', 'direct', '--rep', '1', '--confirm-real-provider')
            self.plan['budget']['ledger_cap_usd'] = 0  # Every judge admission is refused by the ledger cap.
            self.plan_path.write_bytes(canonical(self.plan))
            out = io.StringIO()
            with patch('sys.stdout', out), self.assertRaisesRegex(SystemExit, 'judge admission refused .* for: owned--fixture--1'):
                self.m3.main(['--private', str(self.private), 'judge', *common, '--confirm-real-provider'])
        self.assertIn('JUDGE_BLOCKED', out.getvalue())
        self.assertEqual(calls, [])
        decisions = json.loads((self.runs / 'dev' / 'rj' / 'judge' / 'decisions.json').read_text())
        self.assertEqual(decisions['decisions'], {})


if __name__ == '__main__':
    unittest.main()
