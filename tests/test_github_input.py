"""GitHub PR read-only capture from an offline fixture. No network."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
import unittest

from mea.contract import digest, validate
from mea.github_input import (MISSING, TRANSPORT, TRUNCATED, Response, RestFetcher, TransportError,
                              capture, parse_ref)
from mea import github_input
from mea.investigation.provider_view import CrossFileView
from mea.review import review
import github_fixtures as fx

REF = f'{fx.OWNER}/{fx.REPO}#{fx.NUMBER}'
BASE = f'/repos/{fx.OWNER}/{fx.REPO}'


def kinds(report):
    return sorted({(g['kind'], g['code'], g['path']) for g in report['gaps']})


class GithubInputTests(unittest.TestCase):
    def setUp(self):
        guard = patch('socket.socket.connect', side_effect=AssertionError('NO_NETWORK'))
        guard.start()
        self.addCleanup(guard.stop)

    def test_complete_capture_pins_head_and_merge_base_with_both_sides(self):
        fetcher = fx.Fetcher()
        report, snapshot = capture(REF, fetcher, now=0)
        self.assertEqual(report['status'], 'COMPLETE')
        self.assertEqual(report['gaps'], [])
        validate(snapshot)
        self.assertEqual(snapshot['revisions'], dict(base=fx.MERGE_BASE, head=fx.HEAD, target_base=fx.TARGET_BASE))
        self.assertEqual(snapshot['pr']['url'], f'https://github.com/{fx.OWNER}/{fx.REPO}/pull/{fx.NUMBER}')
        changed = snapshot['files'][0]
        self.assertEqual((changed['path'], changed['status']), ('shop/pricing.py', 'modified'))
        self.assertEqual(changed['sides']['base']['text'], fx.BASE_FILES['shop/pricing.py'])
        self.assertEqual(changed['sides']['head']['text'], fx.HEAD_FILES['shop/pricing.py'])
        context = {f['path']: f for f in snapshot['files'][1:]}
        self.assertIn('tests/test_pricing.py', context)
        self.assertIn('shop/cart.py', context)
        self.assertTrue(all(f['status'] == 'context' and f['patch'] is None for f in context.values()))
        reasons = {r['path']: r['reason'] for r in report['context_selection']['files']}
        self.assertTrue(reasons['tests/test_pricing.py'].startswith('TEST_NAME_MATCH'))
        self.assertTrue(reasons['shop/cart.py'].startswith('CODE_SEARCH_DEFAULT_BRANCH'))
        self.assertEqual(snapshot['scope'], dict(reported_changed_files=len(snapshot['files']), complete_listing=True, omitted_sources=[],
                                                 capture=dict(status='COMPLETE', context_selection='COLLECTED',
                                                              gap_counts={MISSING: 0, TRUNCATED: 0, TRANSPORT: 0}, gaps=[])))
        self.assertEqual(snapshot['provenance']['kind'], 'KNOWN_HISTORICAL_DEVELOPMENT')
        self.assertEqual(snapshot['provenance']['capture_sha256'], digest(report['requests']))
        self.assertEqual(report['input_sha256'], digest(snapshot))
        self.assertTrue(all(r['method'] == 'GET' for r in report['requests']))
        # Only collected snapshot paths at the pinned refs were read.
        self.assertTrue(all(f'ref={fx.HEAD}' in p or f'ref={fx.MERGE_BASE}' in p for p, _ in fetcher.calls if '/contents/' in p))
        view = CrossFileView.from_input(snapshot)
        self.assertIn(('head', 'shop/pricing.py', 3), view.changes)

    def test_pinned_historical_head_must_belong_to_the_pull(self):
        pinned = REF + '@' + fx.HEAD
        report, snapshot = capture(pinned, fx.Fetcher(), now=0)
        self.assertEqual(report['status'], 'COMPLETE')
        self.assertTrue(report['pinned_head'])
        report, snapshot = capture(REF + '@' + '9' * 40, fx.Fetcher(), now=0)
        self.assertIsNone(snapshot)
        self.assertEqual((report['status'], report['failure']['kind'], report['failure']['code']),
                         ('FAILED', MISSING, 'PINNED_HEAD_NOT_IN_PULL_COMMITS'))

    def test_missing_evidence_is_reported_as_missing(self):
        head = dict(fx.HEAD_FILES, **{'assets/logo.py': 'x = 1\n'})
        fetcher = fx.Fetcher(head=head, overrides={
            f'{BASE}/contents/assets/logo.py?ref={fx.HEAD}': fx.contents(b'\x89PNG\x00\x01'),
        })
        files = fetcher.files()
        for row in files:
            if row['filename'] == 'assets/logo.py':
                row['sha'] = fx.git_blob_sha(b'\x89PNG\x00\x01')
                del row['patch']
        fetcher.compare_files = files
        report, snapshot = capture(REF, fetcher, now=0)
        self.assertEqual(report['status'], 'INCOMPLETE')
        self.assertEqual(kinds(report), [(MISSING, 'BINARY_CONTENT', 'assets/logo.py')])
        self.assertIn(dict(path='assets/logo.py', side='head', reason='MISSING:BINARY_CONTENT'), snapshot['scope']['omitted_sources'])
        logo = next(f for f in snapshot['files'] if f['path'] == 'assets/logo.py')
        self.assertEqual((logo['sides'], logo['patch']), ({}, None))
        # A 404 at the pinned base is also missing evidence, not a transport failure.
        fetcher = fx.Fetcher(overrides={f'{BASE}/contents/shop/pricing.py?ref={fx.MERGE_BASE}': Response(404, b'{}')})
        report, snapshot = capture(REF, fetcher, now=0)
        self.assertIn((MISSING, 'CONTENT_HTTP_404', 'shop/pricing.py'), kinds(report))
        self.assertEqual(report['gap_counts'][TRANSPORT], 0)

    def test_truncated_evidence_is_reported_as_truncated(self):
        big = 'x = 1\n' * 21000
        head = dict(fx.HEAD_FILES, **{'shop/big.py': big})
        fetcher = fx.Fetcher(head=head)
        files = fetcher.files()
        for row in files:
            if row['filename'] == 'shop/pricing.py':
                del row['patch']  # GitHub omits patches of large diffs.
        fetcher.compare_files = files
        report, snapshot = capture(REF, fetcher, now=0)
        self.assertEqual(report['status'], 'INCOMPLETE')
        found = kinds(report)
        self.assertIn((TRUNCATED, 'SOURCE_BYTE_BOUND', 'shop/big.py'), found)
        self.assertIn((TRUNCATED, 'PROVIDER_PATCH_OMITTED', 'shop/pricing.py'), found)
        self.assertEqual(report['gap_counts'][MISSING], 0)
        self.assertEqual(report['gap_counts'][TRANSPORT], 0)
        pricing = next(f for f in snapshot['files'] if f['path'] == 'shop/pricing.py')
        self.assertIsNone(pricing['patch'])
        self.assertEqual(set(pricing['sides']), {'base', 'head'})
        # A capped provider file listing marks the listing incomplete and skips context.
        many = [dict(filename=f'pkg/m{i}.py', status='added', changes=1, patch='@@ -0,0 +1 @@\n+x = 1',
                     sha=fx.git_blob_sha(b'x = 1\n')) for i in range(300)]
        report, snapshot = capture(REF, fx.Fetcher(compare_files=many), now=0)
        self.assertIn((TRUNCATED, 'PROVIDER_FILE_LIST_LIMIT', None), kinds(report))
        self.assertIn((TRUNCATED, 'FILE_COUNT_BOUND', 'pkg/m24.py'), kinds(report))
        self.assertFalse(snapshot['scope']['complete_listing'])
        self.assertEqual(report['context_selection']['status'], 'SKIPPED_INCOMPLETE_CHANGE_LISTING')

    def test_transport_failures_are_reported_as_transport(self):
        fetcher = fx.Fetcher(overrides={f'{BASE}/contents/shop/pricing.py?ref={fx.HEAD}': TransportError('TimeoutError')})
        report, snapshot = capture(REF, fetcher, now=0)
        self.assertEqual(report['status'], 'INCOMPLETE')
        self.assertEqual(kinds(report), [(TRANSPORT, 'TimeoutError', 'shop/pricing.py')])
        self.assertIn(dict(path='shop/pricing.py', side='head', reason='TRANSPORT:TimeoutError'), snapshot['scope']['omitted_sources'])
        for status, code in [(503, 'HTTP_503'), (403, 'HTTP_403_FORBIDDEN_OR_RATE_LIMITED'), (429, 'HTTP_429_RATE_LIMITED')]:
            report, snapshot = capture(REF, fx.Fetcher(overrides={f'{BASE}/pulls/{fx.NUMBER}': Response(status, b'{}')}), now=0)
            self.assertIsNone(snapshot)
            self.assertEqual((report['status'], report['failure']['kind'], report['failure']['code']), ('FAILED', TRANSPORT, code))
        report, snapshot = capture(REF, fx.Fetcher(overrides={f'{BASE}/pulls/{fx.NUMBER}': TransportError('ConnectionResetError')}), now=0)
        self.assertEqual(report['failure']['kind'], TRANSPORT)
        report, snapshot = capture(REF, fx.Fetcher(overrides={f'{BASE}/pulls/{fx.NUMBER}': Response(200, b'not json')}), now=0)
        self.assertEqual((report['failure']['kind'], report['failure']['code']), (TRANSPORT, 'MALFORMED_JSON_RESPONSE'))
        # A missing PR is not a transport failure.
        report, snapshot = capture(REF, fx.Fetcher(overrides={f'{BASE}/pulls/{fx.NUMBER}': Response(404, b'{}')}), now=0)
        self.assertEqual((report['failure']['kind'], report['failure']['code']), (MISSING, 'PULL_HTTP_404'))

    def test_tree_failure_is_a_typed_snapshot_gap_and_failed_context_selection(self):
        tree = f'{BASE}/git/trees/{fx.HEAD}?recursive=1'
        report, snapshot = capture(REF, fx.Fetcher(overrides={tree: Response(503, b'{}')}), now=0)
        self.assertEqual(report['status'], 'INCOMPLETE')
        self.assertEqual(report['context_selection']['status'], 'FAILED')
        self.assertEqual(report['counts']['context_included'], 0)
        validate(snapshot)
        # No path, so omitted_sources stays empty; the typed capture status carries the gap.
        self.assertEqual(snapshot['scope']['omitted_sources'], [])
        self.assertTrue(snapshot['scope']['complete_listing'])
        self.assertEqual(snapshot['scope']['capture'], dict(
            status='INCOMPLETE', context_selection='FAILED', gap_counts={MISSING: 0, TRUNCATED: 0, TRANSPORT: 1},
            gaps=[dict(kind=TRANSPORT, code='HTTP_503', request='tree', path=None, side=None)]))
        view = CrossFileView.from_input(snapshot)
        result = view.execute('search', dict(query='total', revision=fx.HEAD))
        self.assertIn('SNAPSHOT_CAPTURE_INCOMPLETE:context_selection=FAILED', result['unknown'])
        page = view.execute('read_source', dict(path='shop/pricing.py', revision=fx.HEAD, start=1, end=2))
        self.assertIn('CAPTURE_GAP:TRANSPORT:HTTP_503:tree:-', page['unknown'])
        abstentions = review(snapshot)['abstentions']
        self.assertIn(dict(path=None, reason='TRANSPORT:HTTP_503', request='tree'), abstentions)
        self.assertEqual(next(a for a in abstentions if a['reason'] == 'CAPTURE_INCOMPLETE')['context_selection'], 'FAILED')
        # A 404 tree is MISSING, still a failed selection.
        report, snapshot = capture(REF, fx.Fetcher(overrides={tree: Response(404, b'{}')}), now=0)
        self.assertEqual(snapshot['scope']['capture']['gaps'][0]['kind'], MISSING)
        self.assertEqual(snapshot['scope']['capture']['context_selection'], 'FAILED')

    def test_code_search_failure_marks_context_selection_partial(self):
        class SearchDown(fx.Fetcher):
            def get(self, path, accept):
                if path.startswith('/search/code'):
                    self.calls.append((path, accept))
                    return Response(403, b'{"message":"rate limit"}')
                return super().get(path, accept)
        report, snapshot = capture(REF, SearchDown(), now=0)
        cap = snapshot['scope']['capture']
        self.assertEqual((cap['status'], cap['context_selection']), ('INCOMPLETE', 'PARTIAL'))
        self.assertEqual(cap['gaps'][0], dict(kind=TRANSPORT, code='HTTP_403_FORBIDDEN_OR_RATE_LIMITED', request='search', path=None, side='head'))
        self.assertEqual(report['context_selection']['status'], 'PARTIAL')
        self.assertNotIn('shop/cart.py', [f['path'] for f in snapshot['files']])  # Its only route was search.
        self.assertIn('tests/test_pricing.py', [f['path'] for f in snapshot['files']])
        malformed = fx.Fetcher(overrides={})
        original = malformed.get
        malformed.get = lambda path, accept: Response(200, b'{"items": "x"}') if path.startswith('/search/code') else original(path, accept)
        report, snapshot = capture(REF, malformed, now=0)
        self.assertEqual(snapshot['scope']['capture']['gaps'][0]['code'], 'MALFORMED_RESPONSE_STRUCTURE')

    def test_capture_status_contract(self):
        report, snapshot = capture(REF, fx.Fetcher(), now=0)
        bad = json.loads(json.dumps(snapshot))
        bad['scope']['capture']['gaps'] = [dict(kind=TRANSPORT, code='HTTP_503', request='tree', path=None, side=None)]
        with self.assertRaisesRegex(ValueError, 'CAPTURE_GAP_BOUND'):
            validate(bad)
        bad['scope']['capture'].update(status='INCOMPLETE', gap_counts={MISSING: 0, TRUNCATED: 0, TRANSPORT: 1})
        with self.assertRaisesRegex(ValueError, 'CAPTURE_CONTEXT_SELECTION'):
            validate(bad)  # COLLECTED cannot carry a tree gap.
        bad['scope']['capture']['gaps'][0]['kind'] = 'OTHER'
        with self.assertRaisesRegex(ValueError, 'CAPTURE_GAP'):
            validate(bad)
        legacy = json.loads(json.dumps(snapshot))
        del legacy['scope']['capture']
        validate(legacy)  # Snapshots without the field stay valid.

    def test_wrong_structure_responses_fail_as_transport(self):
        pull = f'{BASE}/pulls/{fx.NUMBER}'
        compare = f'{BASE}/compare/{fx.TARGET_BASE}...{fx.HEAD}'
        commits = f'{BASE}/pulls/{fx.NUMBER}/commits?per_page=100'
        no_head = fx.Fetcher().pull()
        del no_head['head']['sha']
        cases = [(REF, {pull: {}}, 'pull'), (REF, {pull: no_head}, 'pull'), (REF, {pull: dict(fx.Fetcher().pull(), head=dict(sha='abc'))}, 'pull'),
                 (REF, {compare: {}}, 'compare'), (REF, {compare: dict(merge_base_commit=dict(sha=fx.MERGE_BASE), files=[dict(filename=1)])}, 'compare'),
                 (REF + '@' + fx.HEAD, {commits: {'sha': fx.HEAD}}, 'pull_commits')]
        for reference, overrides, what in cases:
            report, snapshot = capture(reference, fx.Fetcher(overrides=overrides), now=0)
            self.assertIsNone(snapshot)
            self.assertEqual((report['status'], report['failure']['kind'], report['failure']['code'], report['failure']['request']),
                             ('FAILED', TRANSPORT, 'MALFORMED_RESPONSE_STRUCTURE', what))
            self.assertEqual(report['requests'][-1]['http_status'], 200)
        tree = f'{BASE}/git/trees/{fx.HEAD}?recursive=1'
        report, snapshot = capture(REF, fx.Fetcher(overrides={tree: {'tree': 'x'}}), now=0)
        self.assertEqual(snapshot['scope']['capture']['context_selection'], 'FAILED')
        self.assertEqual(snapshot['scope']['capture']['gaps'][0]['code'], 'MALFORMED_RESPONSE_STRUCTURE')

    # Round 2: required provider fields are never defaulted. For each list field there are three
    # controls: field absent (a gap), explicitly empty list (no match) and a normal list.
    @staticmethod
    def searching(value):
        class Search(fx.Fetcher):
            def get(self, path, accept):
                if path.startswith('/search/code'):
                    self.calls.append((path, accept))
                    return value if isinstance(value, Response) else Response(200, json.dumps(value).encode())
                return super().get(path, accept)
        return Search()

    def assert_context(self, snapshot, status, selection, gaps):
        cap = snapshot['scope']['capture']
        self.assertEqual((cap['status'], cap['context_selection']), (status, selection))
        self.assertEqual([(g['kind'], g['code'], g['request']) for g in cap['gaps']], gaps)

    def test_search_items_absent_empty_and_normal(self):
        paths = lambda snap: [f['path'] for f in snap['files']]
        report, snapshot = capture(REF, self.searching(dict(total_count=0, incomplete_results=False)), now=0)
        self.assert_context(snapshot, 'INCOMPLETE', 'PARTIAL', [(TRANSPORT, 'MALFORMED_RESPONSE_STRUCTURE', 'search')])
        self.assertEqual(report['context_selection']['status'], 'PARTIAL')
        self.assertNotIn('shop/cart.py', paths(snapshot))
        self.assertIn('SNAPSHOT_CAPTURE_INCOMPLETE:context_selection=PARTIAL',
                      CrossFileView.from_input(snapshot).execute('search', dict(query='total', revision=fx.HEAD))['unknown'])
        report, snapshot = capture(REF, self.searching(dict(total_count=0, incomplete_results=False, items=[])), now=0)
        self.assert_context(snapshot, 'COMPLETE', 'COLLECTED', [])
        self.assertNotIn('shop/cart.py', paths(snapshot))
        report, snapshot = capture(REF, self.searching(dict(total_count=1, incomplete_results=False, items=[dict(path='shop/cart.py')])), now=0)
        self.assert_context(snapshot, 'COMPLETE', 'COLLECTED', [])
        self.assertIn('shop/cart.py', paths(snapshot))

    def test_search_completeness_flag_and_definitive_errors(self):
        items = [dict(path='shop/cart.py')]
        report, snapshot = capture(REF, self.searching(dict(total_count=1, items=items)), now=0)
        self.assert_context(snapshot, 'INCOMPLETE', 'PARTIAL', [(TRANSPORT, 'MALFORMED_RESPONSE_STRUCTURE', 'search')])
        report, snapshot = capture(REF, self.searching(dict(total_count=9, incomplete_results=True, items=items)), now=0)
        self.assert_context(snapshot, 'INCOMPLETE', 'PARTIAL', [(TRUNCATED, 'PROVIDER_SEARCH_INCOMPLETE', 'search')])
        self.assertIn('shop/cart.py', [f['path'] for f in snapshot['files']])
        report, snapshot = capture(REF, self.searching(Response(422, b'{"message":"Validation Failed"}')), now=0)
        self.assert_context(snapshot, 'INCOMPLETE', 'PARTIAL', [(MISSING, 'SEARCH_HTTP_422', 'search')])

    def test_compare_files_absent_empty_and_normal(self):
        compare = f'{BASE}/compare/{fx.TARGET_BASE}...{fx.HEAD}'
        merge_base = dict(sha=fx.MERGE_BASE)
        report, snapshot = capture(REF, fx.Fetcher(overrides={compare: dict(merge_base_commit=merge_base)}), now=0)
        self.assertIsNone(snapshot)
        self.assertEqual((report['failure']['kind'], report['failure']['code'], report['failure']['request']),
                         (TRANSPORT, 'MALFORMED_RESPONSE_STRUCTURE', 'compare'))
        report, snapshot = capture(REF, fx.Fetcher(overrides={compare: dict(merge_base_commit=merge_base, files=[])}), now=0)
        self.assertIsNone(snapshot)
        self.assertEqual((report['failure']['kind'], report['failure']['code']), (MISSING, 'NO_COLLECTABLE_CHANGED_FILE'))
        report, snapshot = capture(REF, fx.Fetcher(), now=0)
        self.assertEqual((report['status'], report['counts']['changed_reported']), ('COMPLETE', 1))

    def test_compare_file_fields_are_required(self):
        base = fx.Fetcher().files()[0]
        for missing in ('changes', 'sha', 'status', 'filename'):
            with self.subTest(missing=missing):
                row = {k: v for k, v in base.items() if k != missing}
                report, snapshot = capture(REF, fx.Fetcher(compare_files=[row]), now=0)
                self.assertEqual((report['failure']['code'], report['failure']['request']), ('MALFORMED_RESPONSE_STRUCTURE', 'compare'))
        renamed = dict(base, status='renamed')
        report, snapshot = capture(REF, fx.Fetcher(compare_files=[renamed]), now=0)
        self.assertEqual(report['failure']['code'], 'MALFORMED_RESPONSE_STRUCTURE')  # previous_filename required for renames
        # Fields GitHub omits legitimately: patch (large or binary diff) and sha of a removed file.
        no_patch = {k: v for k, v in base.items() if k != 'patch'}
        report, snapshot = capture(REF, fx.Fetcher(compare_files=[no_patch]), now=0)
        self.assertIn((TRUNCATED, 'PROVIDER_PATCH_OMITTED', 'shop/pricing.py'), kinds(report))
        head = {k: v for k, v in fx.HEAD_FILES.items() if k != 'web/view.ts'}
        report, snapshot = capture(REF, fx.Fetcher(head=head), now=0)
        removed = next(f for f in snapshot['files'] if f['path'] == 'web/view.ts')
        self.assertEqual((removed['status'], list(removed['sides'])), ('removed', ['base']))

    def test_tree_entries_absent_empty_and_normal(self):
        tree = f'{BASE}/git/trees/{fx.HEAD}?recursive=1'
        report, snapshot = capture(REF, fx.Fetcher(overrides={tree: dict(truncated=False)}), now=0)
        self.assert_context(snapshot, 'INCOMPLETE', 'FAILED', [(TRANSPORT, 'MALFORMED_RESPONSE_STRUCTURE', 'tree')])
        self.assertEqual(report['counts']['context_included'], 0)
        report, snapshot = capture(REF, fx.Fetcher(overrides={tree: dict(truncated=False, tree=[])}), now=0)
        self.assert_context(snapshot, 'COMPLETE', 'COLLECTED', [])
        self.assertEqual(report['counts']['context_included'], 0)
        report, snapshot = capture(REF, fx.Fetcher(), now=0)
        self.assert_context(snapshot, 'COMPLETE', 'COLLECTED', [])
        self.assertGreater(report['counts']['context_included'], 0)

    def test_tree_flags_and_entry_fields_are_required(self):
        tree = f'{BASE}/git/trees/{fx.HEAD}?recursive=1'
        entries = fx.Fetcher().routes()[tree]['tree']
        report, snapshot = capture(REF, fx.Fetcher(overrides={tree: dict(tree=entries)}), now=0)
        self.assert_context(snapshot, 'INCOMPLETE', 'FAILED', [(TRANSPORT, 'MALFORMED_RESPONSE_STRUCTURE', 'tree')])
        report, snapshot = capture(REF, fx.Fetcher(overrides={tree: dict(tree=entries, truncated=True)}), now=0)
        self.assert_context(snapshot, 'INCOMPLETE', 'PARTIAL', [(TRUNCATED, 'PROVIDER_TREE_TRUNCATED', 'tree')])
        # sha is required by GitHub's git-tree schema; size is optional (M3 C2, see below).
        broken = [{k: v for k, v in e.items() if k != 'sha'} for e in entries]
        report, snapshot = capture(REF, fx.Fetcher(overrides={tree: dict(tree=broken, truncated=False)}), now=0)
        self.assert_context(snapshot, 'INCOMPLETE', 'FAILED', [(TRANSPORT, 'MALFORMED_RESPONSE_STRUCTURE', 'tree')])
        directories = entries + [dict(path='shop', type='tree', sha='9' * 40)]  # Directory entries have no size.
        report, snapshot = capture(REF, fx.Fetcher(overrides={tree: dict(tree=directories, truncated=False)}), now=0)
        self.assert_context(snapshot, 'COMPLETE', 'COLLECTED', [])

    def test_pinned_commit_list_absent_empty_and_normal(self):
        commits = f'{BASE}/pulls/{fx.NUMBER}/commits?per_page=100'
        pinned = REF + '@' + fx.HEAD
        for value, outcome in [({'commits': []}, (TRANSPORT, 'MALFORMED_RESPONSE_STRUCTURE')),
                               (Response(404, b'{}'), (MISSING, 'PULL_COMMITS_HTTP_404')),
                               ([], (MISSING, 'PINNED_HEAD_NOT_IN_PULL_COMMITS'))]:
            report, snapshot = capture(pinned, fx.Fetcher(overrides={commits: value}), now=0)
            self.assertIsNone(snapshot)
            self.assertEqual((report['failure']['kind'], report['failure']['code']), outcome)
        report, snapshot = capture(pinned, fx.Fetcher(), now=0)
        self.assertEqual(report['status'], 'COMPLETE')

    def test_pull_fields_are_required_except_documented_nulls(self):
        pull = f'{BASE}/pulls/{fx.NUMBER}'
        base = fx.Fetcher().pull()
        for missing in ('title', 'body', 'merged', 'merged_at', 'state'):
            with self.subTest(missing=missing):
                value = {k: v for k, v in base.items() if k != missing}
                report, snapshot = capture(REF, fx.Fetcher(overrides={pull: value}), now=0)
                self.assertEqual((report['failure']['kind'], report['failure']['code']), (TRANSPORT, 'MALFORMED_RESPONSE_STRUCTURE'))
        report, snapshot = capture(REF, fx.Fetcher(overrides={pull: dict(base, body=None)}), now=0)
        self.assertEqual(snapshot['pr']['body'], '')  # GitHub sends null for an empty description.
        report, snapshot = capture(REF, fx.Fetcher(overrides={pull: dict(base, merged=False, merged_at=None)}), now=0)
        self.assertFalse(snapshot['provenance']['merged_at_capture'])
        report, snapshot = capture(REF, fx.Fetcher(overrides={pull: dict(base, merged='yes')}), now=0)
        self.assertEqual(report['failure']['code'], 'MALFORMED_RESPONSE_STRUCTURE')

    def test_contents_fields_are_required(self):
        target = f'{BASE}/contents/shop/pricing.py?ref={fx.HEAD}'
        good = fx.contents(fx.HEAD_FILES['shop/pricing.py'])
        for missing in ('sha', 'size', 'encoding', 'content', 'type'):
            with self.subTest(missing=missing):
                value = {k: v for k, v in good.items() if k != missing}
                report, snapshot = capture(REF, fx.Fetcher(overrides={target: value}), now=0)
                self.assertEqual(kinds(report), [(TRANSPORT, 'MALFORMED_RESPONSE_STRUCTURE', 'shop/pricing.py')])
                self.assertNotIn('head', snapshot['files'][0]['sides'])
        report, snapshot = capture(REF, fx.Fetcher(overrides={target: [dict(path='shop/pricing.py/x')]}), now=0)
        self.assertEqual(kinds(report), [(MISSING, 'NOT_A_REGULAR_FILE', 'shop/pricing.py')])  # A directory listing.
        report, snapshot = capture(REF, fx.Fetcher(overrides={target: dict(good, encoding='none', content='')}), now=0)
        self.assertEqual(kinds(report), [(TRUNCATED, 'PROVIDER_CONTENT_OMITTED', 'shop/pricing.py')])
        report, snapshot = capture(REF, fx.Fetcher(overrides={target: dict(good, sha='0' * 40)}), now=0)
        self.assertEqual(kinds(report), [(MISSING, 'BLOB_SHA_MISMATCH', 'shop/pricing.py')])

    def test_cli_writes_the_failed_capture_before_a_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as out, \
                patch('mea.github_input.RestFetcher', lambda *a, **k: fx.Fetcher(overrides={f'{BASE}/pulls/{fx.NUMBER}': {}})), \
                patch('mea.github_input.token', lambda: None), redirect_stdout(io.StringIO()):
            self.assertEqual(github_input.main([REF, '--out-dir', out]), 3)
            report = json.loads((Path(out) / 'capture.json').read_text())
            self.assertFalse((Path(out) / 'snapshot.json').exists())
        self.assertEqual((report['status'], report['exit_status'], report['failure']['code']), ('FAILED', 3, 'MALFORMED_RESPONSE_STRUCTURE'))
        self.assertEqual([r['path'] for r in report['requests']], [f'{BASE}/pulls/{fx.NUMBER}'])

    # M3 step 1: C1/C2 from the M2 round-2 review and CodeRabbit's seven comments on f941812. Each
    # case has a contract-violating input, a legal boundary input and the normal input.
    def test_c1_compare_sha_key_absent_null_and_present(self):
        row = fx.Fetcher().files()[0]
        absent = {k: v for k, v in row.items() if k != 'sha'}
        report, snapshot = capture(REF, fx.Fetcher(compare_files=[absent]), now=0)
        self.assertIsNone(snapshot)
        self.assertEqual((report['failure']['kind'], report['failure']['code']), (TRANSPORT, 'MALFORMED_RESPONSE_STRUCTURE'))
        fetcher = fx.Fetcher(compare_files=[dict(row, sha=None)])
        report, snapshot = capture(REF, fetcher, now=0)
        self.assertEqual(report['status'], 'COMPLETE')
        self.assertEqual(report['compare_sha_null'], ['shop/pricing.py'])
        head = snapshot['files'][0]['sides']['head']
        self.assertEqual((head['text'], head['blob_sha']), (fx.HEAD_FILES['shop/pricing.py'], row['sha']))
        self.assertIn((f'{BASE}/contents/shop/pricing.py?ref={fx.HEAD}', github_input.JSON), fetcher.calls)
        # With a null compare sha the contents are still checked against their own Git blob.
        lying = dict(fx.contents(fx.HEAD_FILES['shop/pricing.py']), sha='0' * 40)
        report, snapshot = capture(REF, fx.Fetcher(compare_files=[dict(row, sha=None)], overrides={
            f'{BASE}/contents/shop/pricing.py?ref={fx.HEAD}': lying}), now=0)
        self.assertEqual(kinds(report), [(MISSING, 'BLOB_SHA_MISMATCH', 'shop/pricing.py')])
        report, snapshot = capture(REF, fx.Fetcher(), now=0)
        self.assertEqual((report['status'], report['compare_sha_null']), ('COMPLETE', []))

    def test_c2_tree_blob_size_invalid_absent_and_present(self):
        tree = f'{BASE}/git/trees/{fx.HEAD}?recursive=1'
        entries = fx.Fetcher().routes()[tree]['tree']
        wrong = [dict(e, size=str(e['size'])) for e in entries]
        report, snapshot = capture(REF, fx.Fetcher(overrides={tree: dict(tree=wrong, truncated=False)}), now=0)
        self.assert_context(snapshot, 'INCOMPLETE', 'FAILED', [(TRANSPORT, 'MALFORMED_RESPONSE_STRUCTURE', 'tree')])
        unknown = [{k: v for k, v in e.items() if k != 'size'} for e in entries]
        report, snapshot = capture(REF, fx.Fetcher(overrides={tree: dict(tree=unknown, truncated=False)}), now=0)
        self.assert_context(snapshot, 'COMPLETE', 'COLLECTED', [])
        picked = report['context_selection']['files']
        self.assertTrue(picked and all(f['tree_size'] is None for f in picked))
        # Unknown size, oversized contents: the byte bound applies when the contents are read.
        big = 'x = 1\n' * 21000
        head = dict(fx.HEAD_FILES, **{'tests/test_pricing.py': big})
        sized = fx.Fetcher(head=head)
        sized.compare_files = fx.Fetcher().files()
        rows = [{k: v for k, v in e.items() if k != 'size'} for e in sized.routes()[tree]['tree']]
        sized.overrides = {tree: dict(tree=rows, truncated=False)}
        report, snapshot = capture(REF, sized, now=0)
        self.assertIn((TRUNCATED, 'SOURCE_BYTE_BOUND', 'tests/test_pricing.py'), kinds(report))
        self.assertNotIn('tests/test_pricing.py', [f['path'] for f in snapshot['files']])
        report, snapshot = capture(REF, fx.Fetcher(), now=0)
        self.assert_context(snapshot, 'COMPLETE', 'COLLECTED', [])
        self.assertTrue(all(isinstance(f['tree_size'], int) for f in report['context_selection']['files']))

    def test_pinned_commit_pages_beyond_100_and_the_250_ceiling(self):
        page = lambda n: f'{BASE}/pulls/{fx.NUMBER}/commits?per_page=100' + ('' if n == 1 else f'&page={n}')
        fill = lambda start, count: [dict(sha=f'{i:040x}') for i in range(start, start + count)]
        pinned = REF + '@' + fx.HEAD
        # Violation: 250 listed commits without the pin cannot verify it; fewer means it is absent.
        full = fx.Fetcher(overrides={page(1): fill(10, 100), page(2): fill(110, 100), page(3): fill(210, 50)})
        report, snapshot = capture(pinned, full, now=0)
        self.assertIsNone(snapshot)
        self.assertEqual((report['failure']['kind'], report['failure']['code'], report['failure']['listed']),
                         (TRUNCATED, 'PULL_COMMITS_LIST_LIMIT', 250))
        report, snapshot = capture(pinned, fx.Fetcher(overrides={page(1): fill(10, 100), page(2): fill(110, 30)}), now=0)
        self.assertEqual((report['failure']['kind'], report['failure']['code']), (MISSING, 'PINNED_HEAD_NOT_IN_PULL_COMMITS'))
        self.assertEqual(sum('/commits' in r['path'] for r in report['requests']), 2)
        # Boundary: commit 101 is on the second page.
        fetcher = fx.Fetcher(overrides={page(1): fill(10, 100), page(2): [dict(sha=fx.HEAD)]})
        report, snapshot = capture(pinned, fetcher, now=0)
        self.assertEqual(report['status'], 'COMPLETE')
        self.assertEqual([p for p, _ in fetcher.calls if '/commits' in p], [page(1), page(2)])
        fetcher = fx.Fetcher()
        report, snapshot = capture(pinned, fetcher, now=0)
        self.assertEqual(report['status'], 'COMPLETE')
        self.assertEqual([p for p, _ in fetcher.calls if '/commits' in p], [page(1)])

    def recheck(self, final):
        fetcher = fx.Fetcher(state='open', merged=False)
        pulls = iter([fetcher.pull(), dict(fetcher.pull(), **final)])
        original = fetcher.get

        def get(path, accept):
            if path == f'{BASE}/pulls/{fx.NUMBER}':
                fetcher.calls.append((path, accept))
                return Response(200, json.dumps(next(pulls)).encode())
            return original(path, accept)
        fetcher.get = get
        return capture(REF, fetcher, now=0)

    def test_provenance_uses_the_final_pull_state(self):
        report, snapshot = self.recheck(dict(state='closed', merged=True, merged_at='2026-11-02T00:00:00Z'))
        prov = snapshot['provenance']
        self.assertEqual((prov['kind'], prov['state_at_capture'], prov['merged_at_capture'], prov['pending_review_version']),
                         ('KNOWN_HISTORICAL_DEVELOPMENT', 'closed', True, False))
        self.assertEqual((report['pr_state'], report['merged']), ('closed', True))
        self.assertEqual(snapshot['revisions']['head'], fx.HEAD)  # Source revisions stay as captured.
        report, snapshot = self.recheck(dict(state='closed', merged=False))
        self.assertEqual((snapshot['provenance']['kind'], snapshot['provenance']['merged_at_capture']),
                         ('KNOWN_HISTORICAL_DEVELOPMENT', False))
        report, snapshot = self.recheck({})
        self.assertEqual((snapshot['provenance']['kind'], snapshot['provenance']['state_at_capture']), ('FROZEN_PENDING_REVIEW', 'open'))

    def run_cli(self, out, fetcher):
        with patch('mea.github_input.RestFetcher', lambda *a, **k: fetcher), patch('mea.github_input.token', lambda: None), \
                redirect_stdout(io.StringIO()):
            return github_input.main([REF, '--out-dir', out])

    def test_failed_recapture_removes_an_earlier_snapshot(self):
        failing = lambda: fx.Fetcher(overrides={f'{BASE}/pulls/{fx.NUMBER}': Response(503, b'{}')})
        with tempfile.TemporaryDirectory() as out:
            snap = Path(out) / 'snapshot.json'
            self.assertEqual(self.run_cli(out, fx.Fetcher()), 0)
            first = snap.read_bytes()
            self.assertEqual(self.run_cli(out, fx.Fetcher()), 0)  # Normal: a recapture rewrites it.
            self.assertEqual(snap.read_bytes(), first)
            self.assertEqual(self.run_cli(out, failing()), 3)     # Violation: the earlier snapshot is gone.
            self.assertFalse(snap.exists())
            self.assertEqual(json.loads((Path(out) / 'capture.json').read_text())['status'], 'FAILED')
            self.assertEqual(self.run_cli(out, failing()), 3)     # Boundary: nothing to remove.
            self.assertFalse(snap.exists())

    def test_empty_sha_strings_are_rejected(self):
        row = fx.Fetcher().files()[0]
        report, snapshot = capture(REF, fx.Fetcher(compare_files=[dict(row, sha='')]), now=0)
        self.assertEqual((report['failure']['kind'], report['failure']['code']), (TRANSPORT, 'MALFORMED_RESPONSE_STRUCTURE'))
        tree = f'{BASE}/git/trees/{fx.HEAD}?recursive=1'
        entries = fx.Fetcher().routes()[tree]['tree']
        report, snapshot = capture(REF, fx.Fetcher(overrides={tree: dict(tree=[dict(e, sha='') for e in entries], truncated=False)}), now=0)
        self.assert_context(snapshot, 'INCOMPLETE', 'FAILED', [(TRANSPORT, 'MALFORMED_RESPONSE_STRUCTURE', 'tree')])
        head = {k: v for k, v in fx.HEAD_FILES.items() if k != 'web/view.ts'}
        removed = fx.Fetcher(head=head)
        removed.compare_files = [dict(r, sha='') if r['status'] == 'removed' else r for r in removed.files()]
        report, snapshot = capture(REF, removed, now=0)
        self.assertEqual(report['failure']['code'], 'MALFORMED_RESPONSE_STRUCTURE')
        # Boundary: a removed file may carry a null sha; normal: a full SHA.
        removed.compare_files = [dict(r, sha=None) if r['status'] == 'removed' else r for r in removed.files()]
        report, snapshot = capture(REF, removed, now=0)
        self.assertEqual(report['status'], 'COMPLETE')
        report, snapshot = capture(REF, fx.Fetcher(), now=0)
        self.assertEqual(report['status'], 'COMPLETE')

    def test_invalid_unicode_follows_the_failed_capture_path(self):
        pull = f'{BASE}/pulls/{fx.NUMBER}'
        body = json.dumps(fx.Fetcher().pull()).replace('"Add discount"', '"Add \\ud800 discount"').encode()
        report, snapshot = capture(REF, fx.Fetcher(overrides={pull: Response(200, body)}), now=0)
        self.assertIsNone(snapshot)
        self.assertEqual((report['failure']['kind'], report['failure']['code'], report['failure']['request']),
                         (TRANSPORT, 'INVALID_UNICODE_IN_RESPONSE', 'pull'))
        with tempfile.TemporaryDirectory() as out:
            self.assertEqual(self.run_cli(out, fx.Fetcher(overrides={pull: Response(200, body)})), 3)
            self.assertEqual(json.loads((Path(out) / 'capture.json').read_text())['failure']['code'], 'INVALID_UNICODE_IN_RESPONSE')
        # Boundary: an escaped surrogate pair is a valid astral character.
        pair = json.dumps(fx.Fetcher().pull()).replace('"Add discount"', '"Add \\ud83d\\ude00 discount"').encode()
        report, snapshot = capture(REF, fx.Fetcher(overrides={pull: Response(200, pair)}), now=0)
        self.assertEqual(snapshot['pr']['title'], 'Add \U0001F600 discount')
        report, snapshot = capture(REF, fx.Fetcher(), now=0)
        self.assertEqual(snapshot['pr']['title'], 'Add discount')

    def test_patch_inconsistent_with_pinned_source_is_dropped(self):
        fetcher = fx.Fetcher()
        files = fetcher.files()
        files[0]['patch'] = '@@ -1,1 +1,1 @@\n-def other():\n+def total(items, discount=None):'
        fetcher.compare_files = files
        report, snapshot = capture(REF, fetcher, now=0)
        self.assertIn((MISSING, 'PATCH_INCONSISTENT:PATCH_SOURCE_MISMATCH', 'shop/pricing.py'), kinds(report))
        self.assertIsNone(snapshot['files'][0]['patch'])

    def test_open_pull_head_moving_during_capture_fails(self):
        fetcher = fx.Fetcher(state='open', merged=False)
        pulls = iter([fetcher.pull(), dict(fetcher.pull(), head=dict(sha='5' * 40))])
        original = fetcher.get

        def get(path, accept):
            if path == f'{BASE}/pulls/{fx.NUMBER}':
                fetcher.calls.append((path, accept))
                return Response(200, json.dumps(next(pulls)).encode())
            return original(path, accept)
        fetcher.get = get
        report, snapshot = capture(REF, fetcher, now=0)
        self.assertIsNone(snapshot)
        self.assertEqual(report['failure']['code'], 'HEAD_MOVED_DURING_CAPTURE')
        report, snapshot = capture(REF, fx.Fetcher(state='open', merged=False), now=0)
        self.assertEqual(snapshot['provenance']['kind'], 'FROZEN_PENDING_REVIEW')

    def test_reference_grammar(self):
        self.assertEqual(parse_ref('a/b#3'), ('a', 'b', 3, None))
        for bad in ['a/b', 'a/b#0', 'a b/c#1', 'a/b#1@abc', '../x#1']:
            with self.assertRaises(ValueError):
                parse_ref(bad)

    def test_rest_fetcher_sends_authenticated_get_and_bounds_response(self):
        sent = []

        class Conn:
            def __init__(self, body=b'{}', error=None):
                self.body, self.error = body, error
            def request(self, method, path, headers):
                sent.append((method, path, headers))
                if self.error:
                    raise self.error
            def getresponse(self):
                body = self.body
                class R:
                    status = 200
                    def read(self, n):
                        return body[:n]
                    def getheaders(self):
                        return [('Content-Type', 'application/json')]
                return R()
            def close(self):
                pass
        fetcher = RestFetcher('ghs-test-token', connect=lambda: Conn())
        self.assertEqual(fetcher.get('/repos/a/b/pulls/1').status, 200)
        method, path, headers = sent[0]
        self.assertEqual((method, path, headers['Authorization']), ('GET', '/repos/a/b/pulls/1', 'Bearer ghs-test-token'))
        with self.assertRaises(TransportError) as raised:
            RestFetcher(connect=lambda: Conn(error=TimeoutError())).get('/x')
        self.assertEqual(raised.exception.code, 'TimeoutError')
        with patch('mea.github_input.RESPONSE_BYTES', 4), self.assertRaises(TransportError) as raised:
            RestFetcher(connect=lambda: Conn(body=b'0123456789')).get('/x')
        self.assertEqual(raised.exception.code, 'RESPONSE_BYTE_BOUND')
        report, _ = capture(REF, fx.Fetcher(), now=0)
        self.assertNotIn('token', json.dumps(report).lower())


if __name__ == '__main__':
    unittest.main()
