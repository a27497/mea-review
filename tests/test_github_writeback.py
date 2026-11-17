"""M4 live writeback lane against a loopback fake GitHub server. Synthetic data, no real network."""
import ast
import copy
import difflib
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mea.contract import digest, git_blob_sha
from mea.github_writeback import LiveGitHub, Transport, commentable_lines, import_task, main as cli
from mea.human_review import demo, rereview
from mea.human_review.publisher import Publisher
from mea.human_review.remote import binding
from mea.human_review.service import Service
from mea.human_review.store import get
from mea.investigation.tasks import Store
from fake_github import FakeGitHub, TOKEN

REPO, NUMBER = 'mea-owned/m4-sandbox', 3
B, H1, H2, H3 = 'b' * 40, '1' * 40, '2' * 40, '3' * 40
CALLER = 'from quota import quota\n\n\ndef available(used):\n    return quota() - used\n'
BASE = 'def quota():\n    return 10\n'
V1 = 'def quota():\n    return None\n'
# Increment that replaces the commented line (2) and re-introduces a None return in its place.
V2_REPLACE = 'def quota():\n    if True:\n        return None\n'
# Increment that only inserts above the commented line.
V2_SHIFT = '# quota helpers\ndef quota():\n    return None\n'


def diff(before, after):
    return ''.join(list(difflib.unified_diff(before.splitlines(True), after.splitlines(True)))[2:])


def side(text):
    raw = text.encode()
    return dict(text=text, sha256=digest(raw), blob_sha=git_blob_sha(raw))


def snapshot(base_rev, head_rev, before, after, ident):
    files = [dict(path='quota.py', base_path='quota.py', status='modified', patch=diff(before, after), sides=dict(base=side(before), head=side(after))),
             dict(path='app.py', base_path='app.py', status='context', patch=None, sides=dict(base=side(CALLER), head=side(CALLER)))]
    return dict(schema='mea-snapshot-v1', id=ident,
                pr=dict(url=f'https://github.com/{REPO}/pull/{NUMBER}', repository=REPO, number=NUMBER, title='Synthetic quota change', body='Synthetic.'),
                revisions=dict(base=base_rev, head=head_rev, target_base=B),
                provenance=dict(kind='FROZEN_PENDING_REVIEW', captured_at='2026-11-17T00:00:00Z', state_at_capture='open', merged_at_capture=False,
                                pending_review_version=True, capture_sha256='0' * 64, source_authentication='NOT_INDEPENDENTLY_VERIFIED'),
                scope=dict(reported_changed_files=2, complete_listing=True, omitted_sources=[]), files=files)


def offline_task(store, value):
    # Same frozen-tool loop as the owned demo, applied to a synthetic snapshot.
    with patch.object(demo, 'load', return_value=value):
        return demo.run(store, value['id'])


class WritebackTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = Store(self.root / 'review')
        self.fake = FakeGitHub(REPO, NUMBER, H1, B, {'quota.py': diff(BASE, V1)})
        self.addCleanup(self.fake.close)
        self.t1 = offline_task(self.store, snapshot(B, H1, BASE, V1, 'm4-v1'))
        self.assertEqual(self.t1['reason'], 'HYPOTHESIS_SUPPORTED')
        self.s = self.service()
        self.r = self.s.create(self.t1['id'])
        self.d = self.s.draft(self.r['id'], self.t1['id'], self.t1['candidates'][0]['id'])

    def service(self, allow=(REPO,), timeout=2, store=None):
        return Service(store or self.store, LiveGitHub(Transport(TOKEN, connect=self.fake.connect(timeout)), allow))

    def approve(self, s=None, draft=None, reference='coordinator msg test'):
        s = s or self.s
        p = s.preview((draft or self.d)['id'])
        s.confirm((draft or self.d)['id'], p, True, dict(reference=reference, binding_sha256=binding(p),
                                                          acknowledged_other_comment_ids=[c['id'] for c in p['other_comments_at_anchor']]))
        return p

    def posts(self):
        return len(self.fake.posts)

    def test_preview_binds_pr_head_file_line_side_and_exact_text(self):
        p = self.s.preview(self.d['id'])
        self.assertEqual(self.r['mode'], 'LIVE_GITHUB_REVIEW')
        self.assertEqual(p['mode'], 'LIVE_GITHUB')
        self.assertEqual(p['target'], dict(repository=REPO, number=NUMBER))
        self.assertEqual({k: p['payload'][k] for k in ['commit_id', 'path', 'line', 'side']}, dict(commit_id=H1, path='quota.py', line=2, side='RIGHT'))
        self.assertTrue(p['live_publish'])
        self.assertEqual([m for m, _ in self.fake.requests], ['GET'] * len(self.fake.requests))

    def test_confirmation_requires_recorded_approval_of_the_exact_binding(self):
        p = self.s.preview(self.d['id'])
        with self.assertRaises(ValueError):
            self.s.confirm(self.d['id'], p, True)
        with self.assertRaisesRegex(ValueError, 'HUMAN_APPROVAL_BINDING_MISMATCH'):
            self.s.confirm(self.d['id'], p, True, dict(reference='msg', binding_sha256='0' * 64, acknowledged_other_comment_ids=[]))
        with self.assertRaisesRegex(ValueError, 'HUMAN_APPROVAL_REFERENCE_REQUIRED'):
            self.s.confirm(self.d['id'], p, True, dict(reference=' ', binding_sha256=binding(p), acknowledged_other_comment_ids=[]))
        for change in [dict(body='other text'), dict(line=1), dict(commit_id='f' * 40), dict(path='app.py'), dict(side='LEFT')]:
            stale = copy.deepcopy(p)
            stale['payload'].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'CONFIRMATION_SNAPSHOT_CHANGED'):
                self.s.confirm(self.d['id'], stale, True, dict(reference='msg', binding_sha256=binding(stale), acknowledged_other_comment_ids=[]))
        self.assertEqual(self.posts(), 0)

    def test_send_refused_when_any_bound_item_differs_from_confirmation(self):
        p = self.approve()
        self.fake.push(H2, {'quota.py': diff(BASE, V1)})  # head moved after confirmation
        sent = None
        with self.assertRaisesRegex(ValueError, 'PR_HEAD_DRIFT'):
            sent = Publisher(self.s).send(self.d['id'], p['confirmation_digest'], 'LIVE')
        self.assertIsNone(sent)
        with self.store.connection() as db:
            d = get(db, self.d['id'])
        self.assertIsNone(d['confirmation'])
        self.assertIsNone(d['approval'])
        self.fake.push(H1, {'quota.py': diff(BASE, V1)})
        p = self.approve()
        self.s.edit(self.d['id'], 'Edited after approval.', self.d['anchor'])
        with self.assertRaisesRegex(ValueError, 'HUMAN_CONFIRMATION_REQUIRED'):
            Publisher(self.s).send(self.d['id'], p['confirmation_digest'], 'LIVE')
        with self.assertRaisesRegex(ValueError, 'LIVE_PUBLISH_DISABLED'):
            Publisher(self.s).send(self.d['id'], p['confirmation_digest'], 'MOCK')
        self.assertEqual(self.posts(), 0)

    def test_success_is_recorded_with_redacted_receipt_and_is_idempotent(self):
        p = self.approve()
        pub = Publisher(self.s)
        first = pub.send(self.d['id'], p['confirmation_digest'], 'LIVE')
        self.assertEqual(first['status'], 'PUBLISHED')
        self.assertEqual(first['remote_comment']['html_url'], f'https://github.com/{REPO}/pull/{NUMBER}#discussion_r9001')
        self.assertEqual(self.fake.posts, [p['payload']])
        receipt = first['receipts'][-1]
        self.assertEqual((receipt['method'], receipt['http_status']), ('POST', 201))
        self.assertTrue(receipt['github_request_id'].startswith('FAKE:'))
        again = pub.send(self.d['id'], p['confirmation_digest'], 'LIVE')
        self.assertEqual((again['id'], again['status']), (first['id'], 'PUBLISHED'))
        self.assertEqual(self.posts(), 1)
        # A second store (no local slot) still refuses the same anchor from the live listing.
        other = Store(self.root / 'other')
        t = offline_task(other, snapshot(B, H1, BASE, V1, 'm4-v1'))
        s2 = self.service(store=other)
        r2 = s2.create(t['id'])
        d2 = s2.draft(r2['id'], t['id'], t['candidates'][0]['id'])
        with self.assertRaisesRegex(ValueError, 'DUPLICATE_REMOTE_ANCHOR'):
            s2.preview(d2['id'])
        self.assertEqual(self.posts(), 1)

    def test_token_never_persisted_or_logged(self):
        p = self.approve()
        self.fake.fault = 'HTTP_502_AFTER_EFFECT'
        Publisher(self.s).send(self.d['id'], p['confirmation_digest'], 'LIVE')
        blobs = b''.join(f.read_bytes() for f in self.root.rglob('*') if f.is_file())
        self.assertNotIn(TOKEN.encode(), blobs)
        self.assertNotIn(TOKEN, repr(self.s.remote.transport) + json.dumps(self.s.remote.transport.log))
        self.assertEqual(self.fake.auth_seen, {'Bearer ' + TOKEN})

    def test_5xx_after_effect_is_uncertain_and_reconciled_read_only(self):
        p = self.approve()
        self.fake.fault = 'HTTP_502_AFTER_EFFECT'
        pub = Publisher(self.s)
        r = pub.send(self.d['id'], p['confirmation_digest'], 'LIVE')
        self.assertEqual(r['status'], 'UNCERTAIN')
        self.assertEqual(pub.send(self.d['id'], p['confirmation_digest'], 'LIVE')['status'], 'UNCERTAIN')
        self.assertEqual(self.posts(), 1)
        before = len(self.fake.requests)
        done = pub.reconcile(r['id'])
        self.assertEqual(done['status'], 'PUBLISHED_RECONCILED')
        self.assertEqual(done['remote_comment']['id'], 9001)
        self.assertTrue(all(m == 'GET' for m, _ in self.fake.requests[before:]))
        self.assertEqual(self.posts(), 1)

    def test_timeout_after_effect_is_uncertain_then_reconciled(self):
        s = self.service(timeout=1)
        p = self.approve(s)
        self.fake.fault = 'TIMEOUT_AFTER_EFFECT'
        pub = Publisher(s)
        r = pub.send(self.d['id'], p['confirmation_digest'], 'LIVE')
        self.assertEqual(r['status'], 'UNCERTAIN')
        self.assertIn('TRANSPORT', r['events'][-1]['summary'])
        self.assertEqual(pub.reconcile(r['id'])['status'], 'PUBLISHED_RECONCILED')
        self.assertEqual(self.posts(), 1)
        self.assertEqual(len(self.fake.comments), 1)

    def test_uncertain_without_effect_needs_reconciled_absence_and_human_release(self):
        p = self.approve()
        self.fake.fault = 'HTTP_500_NO_EFFECT'
        pub = Publisher(self.s)
        r = pub.send(self.d['id'], p['confirmation_digest'], 'LIVE')
        self.assertEqual(r['status'], 'UNCERTAIN')
        with self.assertRaisesRegex(ValueError, 'RECONCILED_ABSENCE_REQUIRED'):
            pub.release_after_reconcile(r['id'], True)
        self.fake.fault = 'LIST_500'
        with self.assertRaises(ValueError):
            pub.reconcile(r['id'])
        self.fake.fault = None
        checked = pub.reconcile(r['id'])
        self.assertEqual((checked['status'], checked['reconciled_absent']), ('UNCERTAIN', 1))
        with self.assertRaises(ValueError):
            pub.release_after_reconcile(r['id'], False)
        d = pub.release_after_reconcile(r['id'], True)
        self.assertEqual((d['status'], d['confirmation'], d['approval']), ('DRAFT', None, None))
        self.assertEqual(self.posts(), 1)
        p = self.approve()
        sent = pub.send(self.d['id'], p['confirmation_digest'], 'LIVE')
        self.assertEqual(sent['status'], 'PUBLISHED')
        self.assertNotEqual(sent['id'], r['id'])
        self.assertEqual((self.posts(), len(self.fake.comments)), (2, 1))

    def test_definitive_rejection_is_not_sent_and_unusable_receipt_is_uncertain(self):
        p = self.approve()
        self.fake.fault = 'HTTP_422'
        pub = Publisher(self.s)
        r = pub.send(self.d['id'], p['confirmation_digest'], 'LIVE')
        self.assertEqual(r['status'], 'NOT_SENT')
        self.assertEqual(r['receipts'][-1]['http_status'], 422)
        self.assertEqual(self.fake.comments, [])
        d = pub.abandon_not_sent(r['id'], True)
        p = self.approve()
        self.fake.fault = 'BAD_RECEIPT'
        r = pub.send(self.d['id'], p['confirmation_digest'], 'LIVE')
        self.assertEqual(r['status'], 'UNCERTAIN')
        self.assertEqual(pub.reconcile(r['id'])['status'], 'PUBLISHED_RECONCILED')

    def test_remote_change_between_admission_and_post_is_not_sent_without_post(self):
        p = self.approve()
        pub = Publisher(self.s, fault=lambda stage, _: self.fake.push(H2, self.fake.patches) if stage == 'after_sending' else None)
        r = pub.send(self.d['id'], p['confirmation_digest'], 'LIVE')
        self.assertEqual(r['status'], 'NOT_SENT')
        self.assertIn('REMOTE_CHANGED_AFTER_ADMISSION', r['events'][-1]['summary'])
        self.assertEqual(self.posts(), 0)

    def test_other_reviewer_at_same_anchor_does_not_block_but_own_comment_does(self):
        bot = dict(id=77, html_url=f'https://github.com/{REPO}/pull/{NUMBER}#discussion_r77', user=dict(login='review-bot[bot]'), body='bot',
                   path='quota.py', side='RIGHT', commit_id=H1, original_commit_id=H1, line=2, original_line=2, position=1, in_reply_to_id=None)
        self.fake.comments.append(bot)
        p = self.s.preview(self.d['id'])
        self.assertEqual(p['other_comments_at_anchor'], [dict(id=77, login='review-bot[bot]', html_url=bot['html_url'], excerpt='bot', label='Already raised by review-bot[bot]')])
        with self.assertRaisesRegex(ValueError, 'OTHER_COMMENTS_AT_ANCHOR_NOT_ACKNOWLEDGED'):
            self.s.confirm(self.d['id'], p, True, dict(reference='msg', binding_sha256=binding(p), acknowledged_other_comment_ids=[]))
        p = self.approve()
        with self.store.connection() as db:
            self.assertEqual(get(db, self.d['id'])['approval']['acknowledged_other_comment_ids'], [77])
        self.assertEqual(Publisher(self.s).send(self.d['id'], p['confirmation_digest'], 'LIVE')['status'], 'PUBLISHED')
        self.assertEqual(self.posts(), 1)

    def test_new_other_comment_after_confirmation_requires_reconfirmation(self):
        p = self.approve()
        self.fake.comments.append(dict(id=78, html_url='u', user=dict(login='review-bot[bot]'), body='late', path='quota.py', side='RIGHT',
                                       commit_id=H1, original_commit_id=H1, line=2, original_line=2, position=1, in_reply_to_id=None))
        with self.assertRaisesRegex(ValueError, 'CONFIRMATION_INVALIDATED_BY_CHANGE'):
            Publisher(self.s).send(self.d['id'], p['confirmation_digest'], 'LIVE')
        self.assertEqual(self.posts(), 0)

    def test_declined_draft_cannot_be_confirmed_or_sent(self):
        p = self.s.preview(self.d['id'])
        d = self.s.decline(self.d['id'], 'coordinator msg declined')
        self.assertEqual((d['status'], d['declined']['reference']), ('DECLINED_BY_HUMAN', 'coordinator msg declined'))
        with self.assertRaisesRegex(ValueError, 'DRAFT_DECLINED_BY_HUMAN'):
            self.s.confirm(self.d['id'], p, True, dict(reference='msg', binding_sha256=binding(p), acknowledged_other_comment_ids=[]))
        with self.assertRaisesRegex(ValueError, 'HUMAN_CONFIRMATION_REQUIRED'):
            Publisher(self.s).send(self.d['id'], p['confirmation_digest'], 'LIVE')
        self.assertEqual(self.posts(), 0)

    def test_target_outside_allowlist_cannot_be_confirmed_or_sent(self):
        s = self.service(allow=('someone/else',))
        p = s.preview(self.d['id'])
        self.assertEqual(p['mode'], 'LOCAL_PREVIEW_ONLY')
        with self.assertRaisesRegex(ValueError, 'LIVE_PUBLISH_DISABLED_NO_TARGET'):
            s.confirm(self.d['id'], p, True, dict(reference='msg', binding_sha256=binding(p), acknowledged_other_comment_ids=[]))
        with self.assertRaisesRegex(ValueError, 'LIVE_TARGET_NOT_ALLOWLISTED'):
            s.remote.read(dict(repository=REPO, number=NUMBER))
        self.assertEqual(self.fake.requests, [])
        with self.assertRaises(ValueError):
            LiveGitHub(Transport(TOKEN), [])

    def test_workbench_service_cannot_confirm_a_live_draft(self):
        mock = Service(self.store)
        p = mock.preview(self.d['id'])
        self.assertEqual(p['mode'], 'LOCAL_PREVIEW_ONLY')
        with self.assertRaises(ValueError):
            mock.confirm(self.d['id'], p, True)
        self.assertEqual(self.fake.requests, [])

    def sent(self):
        p = self.approve()
        return Publisher(self.s).send(self.d['id'], p['confirmation_digest'], 'LIVE')

    def test_rereview_marks_modified_anchor_outdated_and_refuses_relocation(self):
        first = self.sent()
        t2 = offline_task(self.store, snapshot(H1, H2, V1, V2_REPLACE, 'm4-v2'))
        self.assertEqual(t2['reason'], 'HYPOTHESIS_SUPPORTED')
        self.fake.push(H2, {'quota.py': diff(BASE, V2_REPLACE)}, outdated_ids={first['remote_comment']['id']})
        observed = self.s.remote.read(dict(repository=REPO, number=NUMBER))
        posts = self.posts()
        r = self.s.rereview(self.r['id'], t2['id'], observed)
        check = r['rereviews'][-1]
        self.assertEqual(check['relation'], 'INCREMENTAL_LINEAR')
        row = check['comments'][0]
        self.assertEqual((row['status'], row['new'], row['republish']), ('OUTDATED', None, 'NEVER_AUTOMATIC'))
        self.assertEqual(row['replaced_head_lines'], dict(path='quota.py', start=2, end=3))
        self.assertTrue(row['github']['outdated'])
        self.assertEqual([c['status'] for c in check['candidates']], ['AT_OUTDATED_COMMENT_LOCATION'])
        with self.assertRaisesRegex(ValueError, 'REREVIEW_AT_OUTDATED_COMMENT_LOCATION'):
            self.s.draft(self.r['id'], t2['id'], t2['candidates'][0]['id'])
        self.assertEqual(self.posts(), posts)
        again = self.s.rereview(self.r['id'], t2['id'], observed)
        self.assertEqual(len(again['rereviews']), 1)
        self.assertEqual(again['rereviews'][0]['comments'][0]['status'], 'OUTDATED')
        self.assertEqual(again['events'][-1]['kind'], 'remote_reobserved')
        # The sent comment remains on its original commit and is not resent.
        self.assertEqual(again['sends'][0]['payload']['commit_id'], H1)

    def test_rereview_maps_unchanged_anchor_through_increment(self):
        first = self.sent()
        t2 = offline_task(self.store, snapshot(H1, H2, V1, V2_SHIFT, 'm4-v2-shift'))
        self.fake.push(H2, {'quota.py': diff(BASE, V2_SHIFT)}, moved={first['remote_comment']['id']: 3})
        r = self.s.rereview(self.r['id'], t2['id'], self.s.remote.read(dict(repository=REPO, number=NUMBER)))
        row = r['rereviews'][-1]['comments'][0]
        self.assertEqual((row['status'], row['new']), ('CURRENT', dict(commit_id=H2, path='quota.py', line=3, side='RIGHT')))
        self.assertEqual(row['github']['line'], 3)

    def test_rereview_of_non_incremental_version_needs_recheck(self):
        self.sent()
        t2 = offline_task(self.store, snapshot(B, H3, BASE, V2_REPLACE, 'm4-v3'))
        r = self.s.rereview(self.r['id'], t2['id'])
        check = r['rereviews'][-1]
        self.assertEqual(check['relation'], 'NOT_INCREMENTAL')
        self.assertEqual(check['comments'][0]['status'], 'NEEDS_RECHECK')

    def test_old_version_draft_cannot_be_sent_after_increment(self):
        p = self.approve()
        t2 = offline_task(self.store, snapshot(H1, H2, V1, V2_SHIFT, 'm4-v2-shift'))
        self.s.rereview(self.r['id'], t2['id'])
        with self.assertRaises(ValueError):
            Publisher(self.s).send(self.d['id'], p['confirmation_digest'], 'LIVE')
        self.assertEqual(self.posts(), 0)

    def test_import_task_copies_and_revalidates(self):
        other = Store(self.root / 'provider')
        t = offline_task(other, snapshot(B, H1, BASE, V1, 'm4-import'))
        target = Store(self.root / 'imported')
        self.assertEqual(import_task(target, other, t['id'])['id'], t['id'])
        self.assertEqual(import_task(target, other, t['id'])['id'], t['id'])
        with self.assertRaises(ValueError):
            import_task(target, other, 'task-' + '0' * 64)

    def test_cli_refuses_writes_without_explicit_flags_before_reading_a_token(self):
        for argv in (['send', '--draft', self.d['id']], ['confirm', '--draft', self.d['id'], '--preview', 'x', '--approval-ref', 'm', '--binding', '0' * 64]):
            out = io.StringIO()
            with patch('mea.github_writeback.token', side_effect=AssertionError('TOKEN_READ')) as tok, redirect_stdout(out):
                code = cli(['--store', str(self.root / 'review'), '--allow-repository', REPO] + argv)
            self.assertEqual(code, 2)
            self.assertIn('BLOCKED', out.getvalue())
            tok.assert_not_called()
        self.assertEqual(self.posts(), 0)


    def test_cli_confirm_reads_the_preview_file_written_by_preview(self):
        out = self.root / 'preview.json'
        base = ['--store', str(self.root / 'review'), '--allow-repository', REPO]
        with patch('mea.github_writeback.token', return_value=TOKEN), \
                patch('mea.github_writeback.Transport', lambda secret: Transport(secret, connect=self.fake.connect())), redirect_stdout(io.StringIO()):
            self.assertEqual(cli(base + ['--out', str(out), 'preview', '--draft', self.d['id']]), 0)
            saved = json.loads(out.read_bytes())['result']
            self.assertEqual(cli(base + ['confirm', '--draft', self.d['id'], '--preview', str(out), '--approval-ref', 'msg',
                                         '--binding', saved['binding_sha256'], '--human-approved']), 0)
            self.assertEqual(cli(base + ['send', '--draft', self.d['id'], '--confirm-live-write']), 0)
        self.assertEqual(self.posts(), 1)
        self.assertNotIn(TOKEN.encode(), out.read_bytes())


    def bot(self, identifier, line=2):
        return dict(id=identifier, html_url=f'https://github.com/{REPO}/pull/{NUMBER}#discussion_r{identifier}', user=dict(login='review-bot[bot]'),
                    body='bot', path='quota.py', side='RIGHT', commit_id=H1, original_commit_id=H1, line=line, original_line=line, position=1, in_reply_to_id=None)

    def test_other_comment_arriving_between_final_check_and_post_is_not_sent(self):
        p = self.approve()
        pub = Publisher(self.s, fault=lambda stage, _: self.fake.comments.append(self.bot(79)) if stage == 'after_sending' else None)
        r = pub.send(self.d['id'], p['confirmation_digest'], 'LIVE')
        self.assertEqual(r['status'], 'NOT_SENT')
        self.assertIn('OTHER_COMMENTS_AT_ANCHOR_CHANGED', r['events'][-1]['summary'])
        self.assertEqual(self.posts(), 0)

    def test_unrelated_new_comment_does_not_block_post(self):
        p = self.approve()
        pub = Publisher(self.s, fault=lambda stage, _: self.fake.comments.append(self.bot(80, line=1)) if stage == 'after_sending' else None)
        self.assertEqual(pub.send(self.d['id'], p['confirmation_digest'], 'LIVE')['status'], 'PUBLISHED')

    def test_left_anchor_requires_frozen_base_to_be_the_github_merge_base(self):
        p = self.s.preview(self.d['id'])
        value = copy.deepcopy(p)
        value['payload']['side'] = 'LEFT'
        remote = self.s.remote.read(dict(repository=REPO, number=NUMBER))
        self.s.admit_remote(dict(value, base_revision=B), dict(remote, commentable={'quota.py': dict(LEFT=[2], RIGHT=[2])}))
        with self.assertRaisesRegex(ValueError, 'LEFT_SIDE_BASE_MISMATCH'):
            self.s.admit_remote(dict(value, base_revision=H1), remote)

    def test_reconcile_with_multiple_matches_after_absence_blocks_release(self):
        p = self.approve()
        self.fake.fault = 'HTTP_500_NO_EFFECT'
        pub = Publisher(self.s)
        r = pub.send(self.d['id'], p['confirmation_digest'], 'LIVE')
        self.assertEqual(pub.reconcile(r['id'])['last_reconcile'], 'ABSENT')
        mine = dict(self.bot(81), user=dict(login='sandbox-owner'), body=p['payload']['body'])
        self.fake.comments += [mine, dict(mine, id=82, html_url=mine['html_url'][:-2] + '82')]
        self.assertEqual(pub.reconcile(r['id'])['last_reconcile'], 'MULTIPLE')
        with self.assertRaisesRegex(ValueError, 'RECONCILED_ABSENCE_REQUIRED'):
            pub.release_after_reconcile(r['id'], True)

    def test_existing_draft_is_blocked_after_rereview_marks_its_location(self):
        first = self.sent()
        t2 = offline_task(self.store, snapshot(H1, H2, V1, V2_REPLACE, 'm4-v2'))
        self.s.increment(self.r['id'], t2['id'])
        d2 = self.s.draft(self.r['id'], t2['id'], t2['candidates'][0]['id'])
        self.fake.push(H2, {'quota.py': diff(BASE, V2_REPLACE)}, outdated_ids={first['remote_comment']['id']})
        self.s.preview(d2['id'])
        self.s.rereview(self.r['id'], t2['id'])
        with self.assertRaisesRegex(ValueError, 'REREVIEW_AT_OUTDATED_COMMENT_LOCATION'):
            self.s.preview(d2['id'])
        self.assertEqual(self.posts(), 1)

    def test_rereview_uses_the_requested_versions_predecessor_and_chains_mappings(self):
        first = self.sent()
        t2 = offline_task(self.store, snapshot(H1, H2, V1, V2_SHIFT, 'm4-chain-2'))
        t3 = offline_task(self.store, snapshot(H2, H3, V2_SHIFT, '# quota helpers\ndef quota():\n    return 0\n', 'm4-chain-3'))
        self.s.increment(self.r['id'], t2['id'])
        self.s.increment(self.r['id'], t3['id'])
        with self.assertRaisesRegex(ValueError, 'REREVIEW_PREDECESSOR_REQUIRED'):
            self.s.rereview(self.r['id'], self.t1['id'])
        r = self.s.rereview(self.r['id'], t2['id'])
        self.assertEqual(r['rereviews'][-1]['old_task_id'], self.t1['id'])
        self.assertEqual(r['rereviews'][-1]['comments'][0]['new']['line'], 3)
        r = self.s.rereview(self.r['id'], t3['id'])
        row = r['rereviews'][-1]['comments'][0]
        self.assertEqual((row['old']['commit_id'], row['old']['line'], row['status']), (H2, 3, 'OUTDATED'))
        self.assertEqual(first['payload']['commit_id'], H1)

    def test_import_refuses_unfinished_task_without_leaving_a_row(self):
        other = Store(self.root / 'provider-running')
        t = offline_task(other, snapshot(B, H1, BASE, V1, 'm4-running'))
        with other.transaction() as db:
            doc = json.loads(db.execute('SELECT doc FROM tasks WHERE id=?', (t['id'],)).fetchone()[0])
            doc['status'] = 'RUNNING'
            db.execute('UPDATE tasks SET doc=? WHERE id=?', (json.dumps(doc), t['id']))
        target = Store(self.root / 'imported-running')
        with self.assertRaisesRegex(ValueError, 'COMPLETED_REVIEW_REQUIRED'):
            import_task(target, other, t['id'])
        with target.connection() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM tasks WHERE id=?', (t['id'],)).fetchone())

    def test_cli_recovers_interrupted_intent_and_abandons_not_sent(self):
        p = self.approve()
        class Stop(BaseException):
            pass
        def crash(stage, _):
            if stage == 'after_intent':
                raise Stop()
        with self.assertRaises(Stop):
            Publisher(self.s, fault=crash).send(self.d['id'], p['confirmation_digest'], 'LIVE')
        base = ['--store', str(self.root / 'review'), '--allow-repository', REPO]
        out = self.root / 'status.json'
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli(base + ['--out', str(out), 'status', '--review', self.r['id']]), 0)
        send = json.loads(out.read_bytes())['sends'][0]
        self.assertEqual(send['status'], 'NOT_SENT')
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli(base + ['abandon-not-sent', '--intent', send['id']]), 2)
            self.assertEqual(cli(base + ['abandon-not-sent', '--intent', send['id'], '--human-approved']), 0)
        with self.store.connection() as db:
            self.assertEqual(get(db, self.d['id'])['status'], 'DRAFT')
        self.assertEqual(self.posts(), 0)


class AnchorMappingTests(unittest.TestCase):
    def test_line_mapping_through_hunks(self):
        patch_text = diff('a\nb\nc\nd\ne\nf\n', 'x\na\nb\nC\nd\nf\n')
        self.assertEqual(rereview.map_line(patch_text, 1), ('CURRENT', 2, None))
        self.assertEqual(rereview.map_line(patch_text, 2), ('CURRENT', 3, None))
        self.assertEqual(rereview.map_line(patch_text, 3)[0], 'OUTDATED')
        self.assertEqual(rereview.map_line(patch_text, 5)[0], 'OUTDATED')
        self.assertEqual(rereview.map_line(patch_text, 4), ('CURRENT', 5, None))
        self.assertEqual(rereview.map_line(patch_text, 6), ('CURRENT', 6, None))
        far = '\n'.join(str(i) for i in range(1, 30)) + '\n'
        moved = far.replace('3\n', '3\nnew\n', 1)
        self.assertEqual(rereview.map_line(diff(far, moved), 25), ('CURRENT', 26, None))
        self.assertEqual(rereview.map_line('@@ -5,0 +6,2 @@\n+x\n+y\n', 5), ('CURRENT', 5, None))
        self.assertEqual(rereview.map_line('@@ -5,0 +6,2 @@\n+x\n+y\n', 6), ('CURRENT', 8, None))
        self.assertEqual(rereview.map_line('@@ -2,2 +2,1 @@\n a\n-b\n', 3), ('OUTDATED', None, (3, 2)))
        self.assertEqual(rereview.map_line(patch_text, 3), ('OUTDATED', None, (4, 4)))

    def test_candidate_at_mapped_sent_line_is_a_duplicate(self):
        old = dict(view=dict(revisions=dict(head=H1), files=[]), candidates=[])
        new = dict(view=dict(revisions=dict(base=H1, head=H2), files=[]),
                   candidates=[dict(id='c1', change=dict(path='a.py', line=4, side='base', revision=H1)),
                               dict(id='c2', change=dict(path='a.py', line=9, side='head', revision=H2))])
        send = dict(id='send-1', status='PUBLISHED', payload=dict(commit_id=H1, path='a.py', line=4, side='LEFT', body='x'),
                    remote_comment=dict(id=1, html_url='u'))
        result = rereview.check(old, new, [send])
        self.assertEqual(result['comments'][0]['status'], 'CURRENT')
        self.assertEqual([c['status'] for c in result['candidates']], ['DUPLICATE_OF_SENT_COMMENT', 'NEW'])

    def view_check(self, files, complete=True):
        old = dict(view=dict(revisions=dict(head=H1), files=[]), candidates=[])
        new = dict(view=dict(revisions=dict(base=H1, head=H2), files=files, coverage=dict(complete_listing=complete)), candidates=[])
        send = dict(id='send-1', status='PUBLISHED', payload=dict(commit_id=H1, path='a.py', line=2, side='RIGHT', body='x'),
                    remote_comment=dict(id=1, html_url='u'))
        return rereview.check(old, new, [send])['comments'][0]

    def test_copies_incomplete_listings_and_missing_sources_are_not_guessed(self):
        copy_entry = dict(path='b.py', base_path='a.py', status='copied', patch='@@ -2 +2 @@\n-x\n+y\n', sides=dict(head=dict()))
        self.assertEqual(self.view_check([copy_entry])['status'], 'CURRENT')
        self.assertEqual(self.view_check([], complete=False)['status'], 'NEEDS_RECHECK')
        missing = dict(path='a.py', base_path='a.py', status='modified', patch='@@ -2 +2 @@\n-x\n+y\n', sides=dict(base=dict()))
        self.assertEqual(self.view_check([missing])['status'], 'NEEDS_RECHECK')
        removed = dict(path='a.py', base_path='a.py', status='removed', patch=None, sides=dict(base=dict()))
        self.assertEqual(self.view_check([removed])['status'], 'OUTDATED')

    def test_commentable_lines_cover_every_hunk_row(self):
        self.assertEqual(commentable_lines(diff(BASE, V2_REPLACE)), dict(RIGHT=[1, 2, 3], LEFT=[1, 2]))


class BoundaryTests(unittest.TestCase):
    def test_workbench_and_human_review_never_import_the_live_lane(self):
        root = Path(__file__).resolve().parents[1] / 'mea'
        for file in [root / 'server.py', *sorted((root / 'human_review').glob('*.py'))]:
            tree = ast.parse(file.read_text())
            names = [n.module or '' for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
            names += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
            self.assertFalse([n for n in names if 'github_writeback' in n], file)


if __name__ == '__main__':
    unittest.main()


class IncrementCaptureTests(unittest.TestCase):
    def setUp(self):
        guard = patch('socket.socket.connect', side_effect=AssertionError('NO_NETWORK'))
        guard.start()
        self.addCleanup(guard.stop)

    def fetcher(self, merge_base):
        import github_fixtures as fx
        early = dict(fx.HEAD_FILES)
        early['shop/pricing.py'] = 'def total(items, discount=None):\n    return sum(i.price for i in items) - discount\n'

        class Increment(fx.Fetcher):
            def routes(self):
                result = super().routes()
                base = f'/repos/{fx.OWNER}/{fx.REPO}'
                rows = [dict(filename='shop/pricing.py', status='modified', changes=1, sha=git_blob_sha(fx.HEAD_FILES['shop/pricing.py'].encode()),
                             patch=diff(early['shop/pricing.py'], fx.HEAD_FILES['shop/pricing.py']))]
                result[f'{base}/compare/{fx.EARLY_HEAD}...{fx.HEAD}'] = dict(merge_base_commit=dict(sha=merge_base), files=rows)
                for path, text in early.items():
                    result[f'{base}/contents/{path}?ref={fx.EARLY_HEAD}'] = fx.contents(text)
                return result
        return fx, Increment(state='open', merged=False)

    def test_since_captures_only_the_increment_against_the_previous_head(self):
        from mea.github_input import capture
        import github_fixtures
        fx, f = self.fetcher(github_fixtures.EARLY_HEAD)
        report, snap = capture(f'{fx.OWNER}/{fx.REPO}#{fx.NUMBER}', f, now=0, since=fx.EARLY_HEAD)
        self.assertIsNotNone(snap, report.get('failure'))
        self.assertEqual(snap['revisions'], dict(base=fx.EARLY_HEAD, head=fx.HEAD, target_base=fx.TARGET_BASE))
        self.assertEqual(report['since'], fx.EARLY_HEAD)
        changed = [x for x in snap['files'] if x['status'] != 'context']
        self.assertEqual([x['path'] for x in changed], ['shop/pricing.py'])
        self.assertIn('-since-', snap['id'])
        self.assertTrue(any(f'/compare/{fx.EARLY_HEAD}...{fx.HEAD}' in p for p, _ in f.calls))

    def test_since_that_is_not_an_ancestor_fails_as_force_push(self):
        from mea.github_input import capture
        fx, f = self.fetcher('9' * 40)
        report, snap = capture(f'{fx.OWNER}/{fx.REPO}#{fx.NUMBER}', f, now=0, since=fx.EARLY_HEAD)
        self.assertIsNone(snap)
        self.assertEqual(report['failure']['code'], 'SINCE_NOT_ANCESTOR_OF_HEAD')
        with self.assertRaises(ValueError):
            capture(f'{fx.OWNER}/{fx.REPO}#{fx.NUMBER}', f, since='abc')
