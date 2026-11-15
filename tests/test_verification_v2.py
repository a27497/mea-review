"""Verification version 2: intent-aware verdicts, companion patches and HEAD definitions. Synthetic inputs; no network."""
import copy
import unittest
from unittest.mock import patch

from test_verification import candidate, changed, receipt, snapshot, source, verdict
from mea.contract import canonical, decode, digest, validate
from mea.investigation import real_protocol as p, verification as v1, verification_v2 as v
from mea.investigation.provider_view import View



def filler(name, count):
    return ''.join(f'{name}_{i:03d} = {i}\n' for i in range(count))


def module(changed_line=False):
    """helper() at the top, target() in the middle, caller() at the bottom, far apart."""
    body = '        total += helper(item)' + (' + 1' if changed_line else '') + '\n'
    return ('def helper(x):\n    return x + 1\n\n\n' + filler('A', 60) +
            '\n\ndef target(items):\n    total = 0\n    for item in items:\n' + body + '    return total\n\n\n' +
            filler('B', 60) + '\n\ndef caller(values):\n    return target(values) * 2\n')


def python_snapshot(extra=()):
    files = [changed('pkg/calc.py', module(), module(True))] + list(extra)
    return snapshot(files)


def anchor_line(view, path='pkg/calc.py'):
    return next(line for side, name, line in view.changes if side == 'head' and name == path)


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.view = View.from_input(snapshot())
        self.head = candidate(self.view, 'head', 'pkg/quota.py', 100)

    def test_packet_adds_companion_patches_and_definitions_to_the_version_1_parts(self):
        _, _, packet = v.prepare(self.view, self.head)
        _, _, old = v1.prepare(self.view, self.head)
        self.assertEqual(tuple(packet), v.PACKET_KEYS)
        for key in ('candidate', 'pull_request', 'anchor_patch', 'head_source', 'test_patches'):
            self.assertEqual(packet[key], old[key], key)
        notes = next(f for f in self.view.value['files'] if f['path'] == 'docs/notes.md')
        self.assertEqual(packet['companion_patches'], [dict(path='docs/notes.md', base_path='docs/notes.md', diff=notes['patch'],
                                                            rows=packet['companion_patches'][0]['rows'])])
        self.assertIn('OTHER_FILE_MARKER', canonical(packet['companion_patches']).decode())

    def test_request_is_thinking_with_effort_low(self):
        body, reservation, packet = v.prepare(self.view, self.head)
        self.assertEqual((body['thinking'], body['reasoning_effort'], body['max_tokens']), (dict(type='enabled'), 'low', 16000))
        self.assertNotIn('tools', body)
        p.check_thinking(body)
        self.assertEqual(reservation, v.input_tokens(body) + 16000)
        self.assertLessEqual(v.input_tokens(body), v.INPUT_TOKENS)
        self.assertEqual(v.check(body), packet)

    def test_each_version_refuses_the_other_versions_request(self):
        new, _, _ = v.prepare(self.view, self.head)
        old, _, _ = v1.prepare(self.view, self.head)
        with self.assertRaisesRegex(ValueError, 'VERIFICATION_REQUEST_SHAPE'):
            v1.check(new)
        with self.assertRaisesRegex(ValueError, 'VERIFICATION_REQUEST_SHAPE'):
            v.check(old)
        with self.assertRaisesRegex(ValueError, 'VERIFICATION_REQUEST_SHAPE'):
            v.check(dict(new, reasoning_effort='high'))

    def test_prompt_states_the_intent_rule(self):
        for phrase in ('nothing in the packet presents that behavior as intended',
                       'A test that expects the behavior is evidence of intent',
                       'already handles the case', 'Prefer INSUFFICIENT to SUPPORTED'):
            self.assertIn(phrase, v.PROMPT)
        self.assertNotEqual(v.PROMPT, v1.PROMPT)

    def test_non_python_anchor_has_no_definitions(self):
        view = View.from_input(snapshot([changed('src/a.rs', 'fn a() {}\nfn b() {}\n', 'fn a() {}\nfn c() {}\n'),
                                         changed('pkg/quota.py', source(200), source(200, changes=(100,)))]))
        _, _, packet = v.prepare(view, candidate(view, 'head', 'src/a.rs', 2))
        self.assertEqual(packet['definitions'], dict(path='src/a.rs', omission=v.NOT_PYTHON))
        self.assertEqual([c['path'] for c in packet['companion_patches']], ['pkg/quota.py'])

    def test_unparseable_python_head_has_no_definitions(self):
        view = View.from_input(snapshot([changed('pkg/bad.py', 'def f(:\n    pass\n', 'def f(:\n    return 1\n')]))
        _, _, packet = v.prepare(view, candidate(view, 'head', 'pkg/bad.py', 2))
        self.assertEqual(packet['definitions'], dict(path='pkg/bad.py', omission=v.PARSE_UNAVAILABLE))


class DefinitionTests(unittest.TestCase):
    def setUp(self):
        self.view = View.from_input(python_snapshot())
        self.line = anchor_line(self.view)
        self.c = candidate(self.view, 'head', 'pkg/calc.py', self.line)

    def test_callers_then_callees_of_the_anchor_function(self):
        spans = v.definition_spans(module(True), self.line)
        self.assertEqual([(s['role'], s['name']) for s in spans], [('caller', 'caller'), ('callee', 'helper')])
        self.assertEqual(spans[1]['start'], 1)

    def test_definition_rows_are_head_lines_outside_the_source_window(self):
        _, _, packet = v.prepare(self.view, self.c)
        rows = self.view.rows['head', 'pkg/calc.py']
        shown = {r[1] for r in packet['head_source']['rows']}
        lines = [r[1] for r in packet['definitions']['rows']]
        self.assertTrue(lines and not set(lines) & shown)
        self.assertIn(1, lines)
        self.assertIn(next(i for i, t in enumerate(rows, 1) if t.startswith('def caller')), lines)
        for identifier, line, text in packet['definitions']['rows']:
            self.assertEqual((identifier, text), (f's{line}', rows[line - 1]))

    def test_spans_are_capped_in_length(self):
        long = 'def helper(x):\n' + ''.join(f'    x += {i}\n' for i in range(200)) + '    return x\n\n\n'
        before = long + filler('A', 60) + '\n\ndef target(items):\n    return helper(items)\n'
        after = before.replace('return helper(items)', 'return helper(items) + 1')
        view = View.from_input(snapshot([changed('pkg/calc.py', before, after)]))
        spans = v.definition_spans(after, anchor_line(view))
        self.assertEqual([(s['name'], s['start'], s['end']) for s in spans], [('helper', 1, v.DEFINITION_LINES)])

    def test_every_packet_row_maps_to_frozen_source(self):
        _, _, packet = v.prepare(self.view, self.c)
        table = v.citable(packet)
        self.assertTrue(any(i.startswith('s') and i not in {r[0] for r in packet['head_source']['rows']} for i in table))
        for identifier, (side, path, line, text) in table.items():
            self.assertEqual(self.view.citation(side, path, line)['quote'], text, identifier)

    def test_a_definition_row_that_disagrees_with_the_source_window_is_drift(self):
        _, _, packet = v.prepare(self.view, self.c)
        packet = copy.deepcopy(packet)
        first = packet['head_source']['rows'][0]
        packet['definitions']['rows'].append([first[0], first[1], 'forged'])
        with self.assertRaisesRegex(ValueError, 'VERIFICATION_PACKET_DRIFT'):
            v.citable(packet)


class FitTests(unittest.TestCase):
    def test_parts_that_do_not_fit_are_named_or_dropped_in_order(self):
        big = changed('docs/big.md', filler('old', 1500), filler('new', 1500))
        view = View.from_input(python_snapshot([big, changed('tests/test_calc.py', source(20), source(20, changes=(5,)))]))
        c = candidate(view, 'head', 'pkg/calc.py', anchor_line(view))
        body, _, packet = v.prepare(view, c)
        self.assertLessEqual(v.input_tokens(body), v.INPUT_TOKENS)
        self.assertEqual(packet['companion_patches'], [dict(path='docs/big.md', omission=v.BOUND,
                                                            content_sha256=digest(big['patch'].encode()))])
        self.assertEqual(['diff' in t for t in packet['test_patches']], [True])
        with patch.object(v, 'INPUT_TOKENS', v.input_tokens(v.body_of(v._assemble(
                view, c, *v1.anchor_of(view, c), False, {'tests/test_calc.py'}, set(), [], None))) + 5):
            _, _, tight = v.prepare(view, c)
        self.assertEqual(tight['definitions']['spans'], [])
        self.assertEqual(tight['definitions']['rows'], [])

    def test_a_packet_that_cannot_fit_is_refused(self):
        view = View.from_input(python_snapshot())
        with patch.object(v, 'INPUT_TOKENS', 300), self.assertRaisesRegex(ValueError, v.BOUND):
            v.prepare(view, candidate(view, 'head', 'pkg/calc.py', anchor_line(view)))


class VerdictTests(unittest.TestCase):
    def setUp(self):
        self.view = View.from_input(python_snapshot([changed('docs/spec.md', 'old rule\n', 'new rule\n')]))
        self.c = candidate(self.view, 'head', 'pkg/calc.py', anchor_line(self.view))
        self.request, _, self.packet = v.prepare(self.view, self.c)

    def test_definition_and_companion_rows_are_citable(self):
        definition = self.packet['definitions']['rows'][0][0]
        spec = self.packet['companion_patches'][0]['rows'][-1][0]
        result = v.decode_verdict(self.view, self.c, self.request, verdict('REFUTED', [definition, spec]))
        self.assertEqual((result['verdict'], result['version'], result['downgrade']), ('REFUTED', v.VERSION, None))
        self.assertEqual([c['path'] for c in result['citations']], ['pkg/calc.py', 'docs/spec.md'])

    def test_uncitable_or_missing_citations_make_it_insufficient(self):
        for ids, code in ((['s9999'], 'CITATION_NOT_IN_PACKET'), ([], 'CITATION_REQUIRED')):
            result = v.decode_verdict(self.view, self.c, self.request, verdict('SUPPORTED', ids))
            self.assertEqual((result['verdict'], result['downgrade']), ('INSUFFICIENT', code))

    def test_truncated_or_malformed_responses_raise(self):
        with self.assertRaisesRegex(ValueError, 'VERIFICATION_PROVIDER_STATE'):
            v.decode_verdict(self.view, self.c, self.request, dict(verdict('SUPPORTED', ['s1']), finish_reason='length'))
        with self.assertRaises(ValueError):
            v.decode_verdict(self.view, self.c, self.request, receipt('not json'))

    def test_a_request_for_another_candidate_is_drift(self):
        other = dict(self.c, title='Another claim')
        with self.assertRaisesRegex(ValueError, 'VERIFICATION_REQUEST_DRIFT'):
            v.decode_verdict(self.view, other, self.request, verdict('SUPPORTED', ['s1']))

    def test_partition_is_version_2(self):
        result = v.partition([self.c, self.c], [v.decode_verdict(self.view, self.c, self.request, verdict('SUPPORTED', ['s1'])), None])
        self.assertEqual((result['version'], result['counts']['supported'], result['counts']['insufficient']), (v.VERSION, 1, 1))
        self.assertEqual(result['kept']['INSUFFICIENT'][0]['verification']['version'], v.VERSION)


class Version21Tests(unittest.TestCase):
    def setUp(self):
        self.view = View.from_input(python_snapshot())
        self.c = candidate(self.view, 'head', 'pkg/calc.py', anchor_line(self.view))

    def test_prompt_adds_only_the_limits_the_decoder_enforces(self):
        self.assertEqual(v.PROMPT_21.replace(
            '\nCite at most 16 distinct IDs, each ID once, and keep the reason under 1200 characters.', ''), v.PROMPT)
        self.assertIn('at most 16 distinct IDs', v.PROMPT_21)
        self.assertEqual((v1.MAX_CITATIONS, v1.REASON_CHARS), (16, 1200))

    def test_requests_differ_only_in_the_system_prompt(self):
        old, old_tokens, old_packet = v.prepare(self.view, self.c)
        new, new_tokens, new_packet = v.prepare(self.view, self.c, version=v.VERSION_21)
        self.assertEqual(new_packet, old_packet)
        self.assertEqual(new['messages'][0]['content'], v.PROMPT_21)
        self.assertEqual(dict(new, messages=new['messages'][1:]), dict(old, messages=old['messages'][1:]))
        self.assertGreater(new_tokens, old_tokens)
        self.assertEqual((v.version_of(old), v.version_of(new)), (v.VERSION, v.VERSION_21))

    def test_check_takes_any_known_version_or_exactly_the_one_named(self):
        old, _, _ = v.prepare(self.view, self.c)
        new, _, packet = v.prepare(self.view, self.c, version=v.VERSION_21)
        self.assertEqual(v.check(new), packet)
        self.assertEqual(v.check(new, v.VERSION_21), packet)
        with self.assertRaisesRegex(ValueError, 'VERIFICATION_REQUEST_SHAPE'):
            v.check(old, v.VERSION_21)
        with self.assertRaisesRegex(ValueError, 'VERIFICATION_VERSION'):
            v.prepare(self.view, self.c, version='mea-candidate-verification-v9')

    def test_verdicts_and_partitions_carry_the_request_version(self):
        new, _, _ = v.prepare(self.view, self.c, version=v.VERSION_21)
        result = v.decode_verdict(self.view, self.c, new, verdict('REFUTED', ['s1']))
        self.assertEqual((result['version'], result['verdict']), (v.VERSION_21, 'REFUTED'))
        with self.assertRaises(ValueError):  # the decoder's citation limit itself is unchanged
            v.decode_verdict(self.view, self.c, new, verdict('REFUTED', [f's{i}' for i in range(1, 18)]))
        part = v.partition([self.c, self.c], [result, None], v.VERSION_21)
        self.assertEqual(part['version'], v.VERSION_21)
        self.assertEqual(part['kept']['INSUFFICIENT'][0]['verification']['version'], v.VERSION_21)
