"""Cross-file frozen tools: import edges, JS/TS symbols and reference validation. No network."""
import unittest

from mea.contract import validate
from mea.investigation import native, real_protocol as protocol
from mea.investigation.provider_view import CrossFileView, View
from mea.investigation.tools import View as LegacyView, import_edges
from mea.investigation.validation import validate_candidates
from investigation_fixtures import changed_file, snapshot, source


def context(path, text):
    return dict(path=path, base_path=path, status='context', patch=None, sides=dict(base=source(text), head=source(text)))


PRICING_BEFORE = 'def total(items):\n    return sum(items)\n'
PRICING_AFTER = 'def total(items, discount=None):\n    return sum(items) - discount\n'
CART = 'from shop.pricing import total\n\n\ndef checkout(items):\n    return total(items)\n'
TEST = 'from shop import pricing\n\n\ndef test_total():\n    assert pricing.total([1]) == 1\n'
FORMAT_BEFORE = 'export function money(n: number) {\n  return n.toFixed(2);\n}\n'
FORMAT_AFTER = 'export function money(n: number, unit?: string) {\n  return n.toFixed(2) + unit;\n}\n'
VIEW_TS = "import { money } from './format';\n\nexport const show = (n: number) => money(n);\n"
SPEC_TS = "import { money } from '../web/format';\n\ntest('money', () => expect(money(1)).toBe('1.00'));\n"


def value():
    files = [changed_file('shop/pricing.py', PRICING_BEFORE, PRICING_AFTER),
             changed_file('web/format.ts', FORMAT_BEFORE, FORMAT_AFTER),
             context('shop/cart.py', CART), context('tests/test_pricing.py', TEST),
             context('web/view.ts', VIEW_TS), context('spec/format.test.ts', SPEC_TS)]
    files[0]['status'] = files[1]['status'] = 'modified'
    return snapshot(files)


class CrossFileToolTests(unittest.TestCase):
    def setUp(self):
        self.input = value()
        validate(self.input)
        self.view = CrossFileView.from_input(self.input)
        self.head = self.input['revisions']['head']
        self.base = self.input['revisions']['base']

    def run_tool(self, name, **args):
        return self.view.execute(name, dict(args, revision=args.pop('revision', self.head)))

    def test_related_files_cites_real_import_lines_in_both_directions(self):
        result = self.run_tool('related_files', path='shop/pricing.py')
        rows = {(r['direction'], r['path'], r['line']) for r in result['data']}
        self.assertEqual(rows, {('imported_by', 'shop/cart.py', 1), ('imported_by', 'tests/test_pricing.py', 1)})
        for e in result['evidence']:
            self.assertEqual(e, self.view.citation(e['side'], e['path'], e['line']))
        self.assertEqual(result['tool_version'], 'frozen-tools-v3-cross-file')
        ts = self.run_tool('related_files', path='web/format.ts')
        self.assertEqual({(r['direction'], r['path']) for r in ts['data']},
                         {('imported_by', 'web/view.ts'), ('imported_by', 'spec/format.test.ts')})
        out = self.run_tool('related_files', path='web/view.ts')
        self.assertEqual([(r['direction'], r['target'], r['specifier']) for r in out['data']], [('imports', 'web/format.ts', './format')])
        base = self.run_tool('related_files', path='shop/pricing.py', revision=self.base)
        self.assertTrue(all(e['revision'] == self.base and e['side'] == 'base' for e in base['evidence']))
        missing = self.run_tool('related_files', path='shop/absent.py')
        self.assertEqual((missing['data'], missing['evidence']), ([], []))
        self.assertIn('SOURCE_NOT_COLLECTED', missing['unknown'])

    def test_js_ts_symbols_and_tests_are_lexical_and_cited(self):
        found = self.run_tool('find_symbol', symbol='money')
        self.assertEqual([(e['path'], e['line']) for e in found['evidence']], [('web/format.ts', 1)])
        self.assertIn('LEXICAL_JS_TS_MATCHES', found['unknown'])
        refs = self.run_tool('references', symbol='money')
        self.assertEqual({e['path'] for e in refs['evidence']}, {'web/format.ts', 'web/view.ts', 'spec/format.test.ts'})
        page = self.view.execute('read_test', dict(path='spec/format.test.ts', revision=self.head, start=1, end=3))
        self.assertEqual(page['read']['status'], 'COMPLETE')
        with self.assertRaisesRegex(ValueError, 'TEST_PATH_REQUIRED'):
            self.view.validate_call('read_test', dict(path='web/view.ts', revision=self.head, start=1, end=2))

    def test_default_views_keep_the_previous_tool_surface(self):
        for cls in (View, LegacyView):
            view = cls.from_input(self.input)
            with self.assertRaisesRegex(ValueError, 'TOOL_NOT_ALLOWED_OR_ARGUMENTS'):
                view.validate_call('related_files', dict(path='shop/pricing.py', revision=self.head))
            self.assertEqual(view.execute('find_symbol', dict(symbol='money', revision=self.head))['evidence'], [])
            with self.assertRaisesRegex(ValueError, 'TEST_PATH_REQUIRED'):
                view.validate_call('read_test', dict(path='spec/format.test.ts', revision=self.head, start=1, end=2))
        self.assertEqual(native.SCHEMA_SHA256, native.schema_sha256(None))
        self.assertNotEqual(native.schema_sha256(native.CROSS_FILE), native.SCHEMA_SHA256)
        self.assertNotIn('related_files', native.PARAMETERS)
        call = dict(id='c1', type='function', function=dict(name='related_files', arguments='{"path":"a.py","revision":"%s","summary":"s"}' % self.head))
        message = dict(role='assistant', content=None, tool_calls=[call])
        with self.assertRaisesRegex(ValueError, 'NATIVE_FUNCTION_NOT_ALLOWED'):
            native.parse_batch(message)
        self.assertEqual(native.parse_batch(message, (), native.parameters(native.CROSS_FILE))[0][0]['tool'], 'related_files')
        task = dict(identity=dict(tool_profile=native.CROSS_FILE))
        self.assertIn('cross-file tool profile', protocol.task_prompt(task))
        self.assertEqual(protocol.task_prompt(dict(identity={})), protocol.PROMPT)
        with self.assertRaisesRegex(ValueError, 'TOOL_PROFILE'):
            protocol.tool_profile(dict(identity=dict(tool_profile='unknown')))

    def test_candidate_citations_must_resolve_to_observed_snapshot_lines(self):
        diff = self.run_tool('read_diff', path='shop/pricing.py')
        related = self.run_tool('related_files', path='shop/pricing.py')
        caller = self.view.execute('read_source', dict(path='shop/cart.py', revision=self.head, start=4, end=5))
        results = [diff, related, caller]
        anchor = next(e for e in diff['evidence'] if e['line'] == 2)
        call = next(e for e in caller['evidence'] if e['line'] == 5)

        def candidate(ids, line=2, path='shop/pricing.py'):
            return dict(title='discount=None reaches subtraction', trigger='checkout calls total without discount',
                        change=dict(path=path, side='head', line=line, revision=self.head),
                        impact='TypeError when discount is omitted', evidence_ids=ids)
        accepted, rejected = validate_candidates(self.view, [candidate([anchor['id'], call['id']])], results, detailed=True)
        self.assertEqual(len(accepted), 1)
        self.assertEqual([c['path'] for c in accepted[0]['citations']], ['shop/pricing.py', 'shop/cart.py'])
        forged = dict(call, line=99, id='ev-' + '0' * 64)
        drifted = dict(call, quote='    return 0')
        cases = [
            candidate([anchor['id'], 'ev-' + '0' * 64]),             # never observed
            candidate([call['id']], line=5, path='shop/cart.py'),    # context file is not a changed line
            candidate([anchor['id']], line=1),                       # anchor not cited
        ]
        accepted, rejected = validate_candidates(self.view, cases, results, detailed=True)
        self.assertEqual(accepted, [])
        self.assertEqual([r['reason'] for r in rejected], ['REFERENCE_INVALID_OR_UNOBSERVED', 'DETERMINISTIC_VALIDATION_REJECTED', 'DETERMINISTIC_VALIDATION_REJECTED'])
        for fake in (forged, drifted):
            tampered = [diff, dict(caller, evidence=[fake])]
            accepted, rejected = validate_candidates(self.view, [candidate([anchor['id'], fake['id']])], tampered, detailed=True)
            # A line outside the file or a quote that differs from the frozen line never validates.
            self.assertEqual((accepted, len(rejected)), ([], 1))

    def test_import_resolution_is_bounded_to_known_paths(self):
        edges, unresolved = import_edges('pkg/a.py', 'import os\nfrom . import b\nfrom ..up import c\nfrom .sub.mod import x\n',
                                         {'pkg/b.py', 'pkg/sub/mod.py', 'up.py'})
        self.assertEqual([(l, t) for l, t, _ in edges], [(2, 'pkg/b.py'), (4, 'pkg/sub/mod.py')])
        self.assertEqual(unresolved, 2)
        edges, unresolved = import_edges('src/x.ts', "import a from 'react';\nconst b = require('../../outside');\nexport * from './y.js';\n",
                                         {'src/y.ts'})
        self.assertEqual([(l, t) for l, t, _ in edges], [(3, 'src/y.ts')])
        self.assertEqual(unresolved, 2)
        self.assertEqual(import_edges('a.py', 'def (:\n', {'b.py'}), (None, 0))


if __name__ == '__main__':
    unittest.main()
