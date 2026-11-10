import copy
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from mea.contract import decode, digest, validate
from mea.review import review, verify_report
from mea.export import markdown
ROOT=Path(__file__).resolve().parents[1]

from investigation_fixtures import changed_file, demo, snapshot


def owned(before='def f():\n    return 1\n', after='def f():\n    assert True\n    return 1\n'):
    return snapshot([changed_file('example.py', before, after)])


class ReviewTests(unittest.TestCase):
    def test_owned_examples_offline_with_exact_citations_and_no_network(self):
        with patch.object(socket.socket,'connect',side_effect=AssertionError('Network forbidden')):
            for path in (ROOT/'fixtures/demo').glob('*.json'):
                with self.subTest(case=path.stem):
                    d=decode(path.read_bytes());before=copy.deepcopy(d);r=review(d)
                    self.assertEqual(d,before);self.assertTrue(verify_report(r,d));self.assertTrue(r['evidence'])
                    self.assertEqual(r['real_ai'],'NOT_RUN');self.assertEqual(r['mode'],'STATIC_RULES_NOT_AI')
                    for e in r['evidence']:
                        f=next(f for f in d['files'] if (f['base_path'] if e['side']=='base' else f['path'])==e['path'])
                        if e['binding']['kind']=='SOURCE':self.assertEqual(e['quote'],f['sides'][e['side']]['text'].splitlines()[e['start']-1])
                        else:self.assertEqual(e['quote'],f['patch'].splitlines()[e['binding']['patch_line']-1][1:])
    def test_true_assertion_is_a_finding(self):
        r=review(owned());self.assertEqual([f['rule'] for f in r['findings']],['ASSERT_TRUE'])
        self.assertEqual(r['evidence'][0]['side'],'head')
    def test_mutable_default_and_bare_except(self):
        after='def f(values=[]):\n    try:\n        return 1\n    except:\n        pass\n'
        self.assertEqual({r['rule'] for r in review(owned(after=after))['findings']},{'MUTABLE_DEFAULT','BARE_EXCEPT_PASS'})
    def test_preexisting_rule_on_unchanged_line_is_not_new_finding(self):
        d=owned('def f():\n    assert True\n    return 1\n','def f():\n    assert True\n    return 2\n')
        self.assertFalse(review(d)['findings'])
    def test_comments_strings_do_not_become_python_findings(self):
        self.assertFalse(review(owned(after='def f():\n    return "assert True" # assert True\n'))['findings'])
    def test_source_is_not_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            marker=Path(directory)/'marker'
            review(owned(after=f'from pathlib import Path\nPath({str(marker)!r}).touch()\n'))
            self.assertFalse(marker.exists())
    def test_unpinned_action_and_full_sha_control(self):
        file = changed_file('.github/workflows/test.yml', '', 'name: test\nuses: owner/action@v1\n')
        self.assertEqual(review(snapshot([file]))['findings'][0]['rule'], 'ACTION_UNPINNED')
        d=owned(after='name: test\nuses: owner/action@'+'a'*40+'\n');d['files'][0]['path']=d['files'][0]['base_path']='.github/workflows/test.yml'
        self.assertFalse(review(d)['findings'])
    def test_patch_only_is_not_full_head(self):
        d = owned()
        d['files'][0]['sides'].pop('head')
        r = review(d)
        self.assertIn('PATCH_ONLY',{e['binding']['kind'] for e in r['evidence']})
    def test_tampered_report_rejected_even_after_rehash(self):
        d=demo();r=review(d);r['evidence'][0]['quote']='changed';r['sha256']=digest({k:v for k,v in r.items() if k!='sha256'})
        with self.assertRaises(ValueError):verify_report(r,d)
    def test_tampered_source_and_blob_rejected(self):
        for key in ['text','blob_sha','sha256']:
            d=demo();d['files'][0]['sides']['head'][key]='tampered'
            with self.assertRaises(ValueError):review(d)
    def test_incomplete_patch_abstains(self):
        d=owned();d['files'][0]['patch']=d['files'][0]['patch'].splitlines()[0]+'\n+broken'
        r=review(d);self.assertFalse(r['findings']);self.assertFalse(r['evidence']);self.assertTrue(r['abstentions'])
    def test_source_patch_mismatch_abstains(self):
        d=owned();d['files'][0]['patch']=d['files'][0]['patch'].replace('assert True','assert False')
        self.assertIn('PATCH_SOURCE_MISMATCH',review(d)['abstentions'][0]['reason'])
    def test_duplicate_keys_nan_and_size(self):
        for raw in [b'{"x":1,"x":2}',b'{"x":NaN}',b' '*2_000_001]:
            with self.assertRaises(ValueError):decode(raw)
    def test_inert_path_restrictions_and_legacy_format(self):
        for name in ['../file','/tmp/file','.git/config','.env','foo.key']:
            d=demo();d['files'][0]['path']=name
            with self.assertRaises(ValueError):review(d)
        with self.assertRaises(ValueError):validate({'version':'legacy-paid-report'})
    def test_pending_version_cannot_claim_merged_snapshot(self):
        d=demo();d['provenance']['merged_at_capture']=True
        with self.assertRaises(ValueError):review(d)
    def test_markdown_fences_and_html(self):
        d=owned(after='def f():\n    assert True # `````<script>evil()</script>\n')
        d['pr']['title']='<img src=x onerror=alert(1)>'
        output=markdown(review(d));self.assertIn('``````text',output);self.assertIn('&lt;img',output)
    def test_no_provider_or_write_capability(self):
        r=review(demo());self.assertEqual(r['capabilities'],dict(writeback=False,autofix=False,automerge=False))
