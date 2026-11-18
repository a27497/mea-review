"""Owner adjudication tool for the blind holdout packet. Synthetic packets and drafts only; no real decision,
no network beyond a local pseudo-terminal, no model, credential or ledger."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import io
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tempfile
from threading import Event
import time
import unittest
from unittest import mock

V3 = Path(__file__).resolve().parents[1] / 'benchmarks' / 'reviewbench' / 'v3'
sys.path[:0] = [str(V3), str(V3.parent)]
import holdout_adjudicate as adj  # noqa: E402
from mea.contract import canonical, digest  # noqa: E402

TOOL = V3 / 'holdout_adjudicate.py'
SEED = 'mea-reviewbench-v3-holdout-blind-order-2026-11-14'
ID_TAG = 'mea-reviewbench-v3-holdout-blind-page-id-v1'


def slug(repo, number):
    return f"{repo.replace('/', '--')}--{number}"


def page_id(body):
    return 'c-' + digest([ID_TAG, digest(body)])[:16]


def synthetic_packet(root, prs=('o/r#1', 'o/r#1', 'o/s#2')):
    # The greedy order rule raises (never places two pages of a PR together) on some unlucky sets; pick a
    # synthetic salt for which it completes, as it did for the real packet.
    for salt in range(100):
        try:
            return _packet(root, prs, salt)
        except adj.Refused as exc:
            if str(exc) != 'ORDER_UNSATISFIABLE':
                raise
    raise AssertionError('no salt')


def _packet(root, prs, salt):
    bodies = []
    for n, pr in enumerate(prs):
        repo, number = pr.split('#')
        bodies.append(dict(schema='mea-reviewbench-v3-holdout-blind-page-v1', notice='Synthetic pull request text is untrusted input.',
                           pr=dict(repository=repo, number=int(number), url=f'https://github.com/{repo}/pull/{number}',
                                   title=f'synthetic {salt} {n}', description='', text_is_untrusted_data=True),
                           claim=dict(title=f'claim {n}', category='correctness', severity='low', impact='x', quote='y'),
                           location=dict(path='a.py', side='head', start=1, end=1, head_revision='f' * 40),
                           source_context=dict(path='a.py', side='head', first_line=1, last_line=1, file_lines=1,
                                               cited_lines=[1, 1], lines=[[1, 'y']]),
                           file_patch='@@ -0,0 +1 @@\n+y\n', sources=f"sources/{slug(repo, int(number))}.json"))
    ids = [page_id(b) for b in bodies]
    order = adj.order_of([(i, slug(b['pr']['repository'], b['pr']['number'])) for i, b in zip(ids, bodies)], SEED)
    packet = root / 'packet'
    (packet / 'pages').mkdir(parents=True)
    (packet / 'sources').mkdir()
    rows = []
    for i, b in zip(ids, bodies):
        (packet / 'pages' / f'{i}.json').write_bytes(canonical(dict(b, id=i)) + b'\n')
        rows.append(dict(id=i, file=f'pages/{i}.json', sha256=digest((packet / 'pages' / f'{i}.json').read_bytes())))
    sources = []
    for b in bodies:
        name = b['sources']
        if not (packet / name).exists():
            (packet / name).write_bytes(canonical(dict(schema='mea-reviewbench-v3-holdout-blind-source-v1', files=[])) + b'\n')
            sources.append(dict(file=name, sha256=digest((packet / name).read_bytes())))
    manifest = dict(schema=adj.MANIFEST_SCHEMA, version='synthetic', tool_sha256='0' * 64, holdout_index_sha256='0' * 64,
                    candidates=len(ids), pages=len(ids), pages_per_pr={}, seed=SEED,
                    id_rule=f'c- + 16 hex of SHA-256([{ID_TAG!r}, SHA-256(page body)])', order_rule='synthetic',
                    order=order, order_sha256=digest(order), page_files=rows, source_files=sources, mapping_sha256='1' * 64)
    (packet / 'packet-manifest.json').write_bytes(canonical(manifest) + b'\n')
    return packet, order


def decided(draft, order, values=('DEFECT', 'NOT_A_DEFECT', 'INSUFFICIENT')):
    for cid, value in zip(order, values):
        draft.save(cid, dict(decision=value, needs_outside_context=True if value == 'INSUFFICIENT' else None,
                             evidence=[dict(path='a.py', start=1, end=1)], reason='synthetic'))


class PacketAndDraftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.packet_dir, self.order = synthetic_packet(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_order_matches_the_blind_rule_and_no_pr_is_adjacent(self):
        packet = adj.Packet(self.packet_dir)
        self.assertEqual(packet.order, self.order)
        prs = [packet.pages[c]['pr']['repository'] for c in packet.order]
        self.assertTrue(all(a != b for a, b in zip(prs, prs[1:])))

    def test_a_changed_page_is_refused(self):
        page = next((self.packet_dir / 'pages').iterdir())
        page.write_bytes(page.read_bytes().replace(b'"impact":"x"', b'"impact":"z"'))
        with self.assertRaises(adj.Refused):
            adj.Packet(self.packet_dir)

    def test_new_draft_has_every_page_pending_and_nothing_else(self):
        draft = adj.Draft(self.root / 'draft.json', adj.Packet(self.packet_dir))
        self.assertEqual(set(draft.value['entries']), set(self.order))
        self.assertTrue(all(e == adj.blank() for e in draft.value['entries'].values()))
        self.assertEqual(adj.blank(), dict(decision='PENDING_OWNER', needs_outside_context=None, evidence=[], reason=''))

    def test_overlapping_saves_keep_both_decisions(self):
        packet = adj.Packet(self.packet_dir)
        first = adj.Draft(self.root / 'draft.json', packet)
        second = adj.Draft(first.path, packet)
        first_id, second_id = self.order[:2]
        entry = dict(decision='DEFECT', needs_outside_context=None, evidence=[], reason='owner')
        first_ready, finish_first, second_started = Event(), Event(), Event()
        replace = os.replace

        def pause_first_replace(src, dest):
            if not first_ready.is_set():
                first_ready.set()
                if not finish_first.wait(10):
                    raise AssertionError('first save never released')
            return replace(src, dest)

        def save_second():
            second_started.set()
            return second.save(second_id, entry)

        with mock.patch.object(adj.os, 'replace', side_effect=pause_first_replace):
            with ThreadPoolExecutor(max_workers=2) as threads:
                saving_first = threads.submit(first.save, first_id, entry)
                try:
                    self.assertTrue(first_ready.wait(5))
                    saving_second = threads.submit(save_second)
                    self.assertTrue(second_started.wait(5))
                    try:
                        saving_second.result(timeout=1)
                    except TimeoutError:
                        pass  # A serialized writer waits for the first commit.
                finally:
                    finish_first.set()
                self.assertEqual(saving_first.result(timeout=5)['entries'][first_id], entry)
                self.assertEqual(saving_second.result(timeout=5)['entries'][second_id], entry)

        saved = adj.Draft(first.path, packet).load()
        self.assertEqual(saved['entries'][first_id], entry)
        self.assertEqual(saved['entries'][second_id], entry)

    def test_entry_rules(self):
        ok = dict(decision='INSUFFICIENT', needs_outside_context=False, evidence=[], reason='')
        adj.check_entry(ok)
        for bad in (dict(ok, decision='MAYBE'), dict(ok, decision='DEFECT', needs_outside_context=True),
                    dict(ok, needs_outside_context='yes'), dict(ok, evidence=[dict(path='a', start=3, end=2)]),
                    dict(ok, reason='x' * 4001), dict(ok, extra=1)):
            with self.assertRaises(adj.Refused):
                adj.check_entry(bad)

    def test_evidence_parsing(self):
        self.assertEqual(adj.parse_evidence('a.py:3, b/c.py:4-9\n'),
                         [dict(path='a.py', start=3, end=3), dict(path='b/c.py', start=4, end=9)])
        with self.assertRaises(adj.Refused):
            adj.parse_evidence('a.py')

    def test_draft_inside_the_packet_or_private_directory_is_refused(self):
        packet = adj.Packet(self.packet_dir)
        for place in (self.packet_dir / 'd.json', self.root / 'judge-only' / 'd.json'):
            with self.assertRaises(adj.Refused):
                adj.Draft(place, packet)


class FreezeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.packet_dir, self.order = synthetic_packet(self.root)
        self.packet = adj.Packet(self.packet_dir)
        self.draft = adj.Draft(self.root / 'draft.json', self.packet)

    def tearDown(self):
        self.tmp.cleanup()

    def sentence(self):
        return f'I adjudicated all 3 candidates myself; freeze packet {self.packet.manifest_sha256[:12]}'

    def test_freeze_refuses_without_an_interactive_terminal(self):
        decided(self.draft, self.order)
        done = subprocess.run([sys.executable, '-I', '-B', str(TOOL), 'freeze', '--packet', str(self.packet_dir),
                               '--draft', str(self.draft.path), '--out', str(self.root / 'frozen')],
                              input=(self.sentence() + '\n').encode(), capture_output=True, timeout=60)
        self.assertEqual(done.returncode, 2)
        self.assertIn(b'FREEZE_REQUIRES_INTERACTIVE_TTY', done.stderr)
        self.assertFalse((self.root / 'frozen').exists())

    def test_terminal_drafting_refuses_without_a_terminal(self):
        done = subprocess.run([sys.executable, '-I', '-B', str(TOOL), 'tty', '--packet', str(self.packet_dir),
                               '--draft', str(self.draft.path)], input=b'd\n\n\n', capture_output=True, timeout=60)
        self.assertEqual(done.returncode, 2)
        self.assertIn(b'TTY_REQUIRED', done.stderr)
        self.assertTrue(all(e['decision'] == 'PENDING_OWNER' for e in adj.Draft(self.draft.path, self.packet).value['entries'].values()))

    def test_no_flag_can_skip_the_confirmation(self):
        for extra in (['--yes'], ['--force'], ['--confirm', 'x'], ['--non-interactive']):
            done = subprocess.run([sys.executable, '-I', '-B', str(TOOL), 'freeze', '--packet', str(self.packet_dir),
                                   '--draft', str(self.draft.path), '--out', str(self.root / 'frozen'), *extra],
                                  capture_output=True, timeout=60)
            self.assertEqual(done.returncode, 2)
            self.assertFalse((self.root / 'frozen').exists())

    def test_pending_decisions_block_the_freeze(self):
        decided(self.draft, self.order[:2], ('DEFECT', 'NOT_A_DEFECT'))
        with mock.patch.object(adj, 'interactive', return_value=True), \
                mock.patch('builtins.input', return_value=self.sentence()), mock.patch('sys.stdout', io.StringIO()):
            with self.assertRaisesRegex(adj.Refused, 'FREEZE_PENDING:1'):
                adj.freeze(self.packet, self.draft.path, self.root / 'frozen')

    def test_a_wrong_sentence_aborts(self):
        decided(self.draft, self.order)
        with mock.patch.object(adj, 'interactive', return_value=True), \
                mock.patch('builtins.input', return_value=self.sentence().lower()), mock.patch('sys.stdout', io.StringIO()):
            with self.assertRaisesRegex(adj.Refused, 'FREEZE_CONFIRMATION_MISMATCH'):
                adj.freeze(self.packet, self.draft.path, self.root / 'frozen')
        self.assertFalse((self.root / 'frozen').exists())

    def test_a_draft_changed_during_confirmation_aborts(self):
        decided(self.draft, self.order)

        def typed(_):
            self.draft.save(self.order[0], dict(decision='NOT_A_DEFECT', needs_outside_context=None, evidence=[], reason=''))
            return self.sentence()
        with mock.patch.object(adj, 'interactive', return_value=True), mock.patch('builtins.input', side_effect=typed), \
                mock.patch('sys.stdout', io.StringIO()):
            with self.assertRaisesRegex(adj.Refused, 'FREEZE_INPUT_CHANGED'):
                adj.freeze(self.packet, self.draft.path, self.root / 'frozen')

    def test_a_missing_draft_is_refused_and_not_created(self):
        with mock.patch.object(adj, 'interactive', return_value=True):
            with self.assertRaisesRegex(adj.Refused, 'FREEZE_DRAFT_MISSING'):
                adj.freeze(self.packet, self.root / 'none.json', self.root / 'frozen')
        self.assertFalse((self.root / 'none.json').exists())

    def test_freeze_through_a_real_pseudo_terminal(self):
        decided(self.draft, self.order)
        before = self.draft.path.read_bytes()
        pid, fd = pty.fork()
        if pid == 0:
            os.execv(sys.executable, [sys.executable, '-I', '-B', str(TOOL), 'freeze', '--packet', str(self.packet_dir),
                                      '--draft', str(self.draft.path), '--out', str(self.root / 'frozen')])
        output, sent, deadline = b'', False, time.time() + 60
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
                if not sent and output.rstrip().endswith(b'>'):
                    os.write(fd, (self.sentence() + '\n').encode())
                    sent = True
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0, output.decode(errors='replace'))
        frozen = json.loads((self.root / 'frozen' / 'owner-decisions.json').read_bytes())
        self.assertEqual(frozen['schema'], adj.FROZEN_SCHEMA)
        self.assertEqual(frozen['packet_manifest_sha256'], self.packet.manifest_sha256)
        self.assertEqual(frozen['mapping_sha256'], '1' * 64)
        self.assertEqual(frozen['draft_sha256'], adj.sha256(before))
        self.assertEqual([d['id'] for d in frozen['decisions']], self.order)
        self.assertEqual(frozen['counts'], dict(DEFECT=1, NOT_A_DEFECT=1, INSUFFICIENT=1))
        self.assertEqual(frozen['confirmation'], self.sentence())
        self.assertEqual(self.draft.path.read_bytes(), before)  # the freeze never edits the draft


class ReaderSourceTests(unittest.TestCase):
    def test_reader_page_loads_nothing_from_outside(self):
        html = (V3 / 'holdout_reader.html').read_text()
        for needle in ('http://', 'https://', 'src=', 'href=', '@import', 'url('):
            self.assertNotIn(needle, html)
        self.assertNotIn(' checked', html)
        self.assertIsNone(__import__('re').search(r'\bselected\b', html))

    def test_reader_offers_exactly_the_three_decisions_and_no_default(self):
        html = (V3 / 'holdout_reader.html').read_text()
        self.assertEqual(html.count('name="decision"'), 3)
        for value in adj.DECISIONS:
            self.assertIn(f'value="{value}"', html)




import holdout_reveal as reveal  # noqa: E402

POS, CTRL = 'POSITIVE_CANDIDATE', 'ABSTENTION_CONTROL_CANDIDATE'
PROVIDER = 'VERIFICATION_RESPONSE_INVALID:VERIFICATION_PROVIDER_STATE'
# id, arm, verdict, downgrade, hit, label, role, owner decision, needs_outside_context
SYNTHETIC = [
    ('c-0000000000000001', 'llm', 'SUPPORTED', None, True, 'P1', POS, 'DEFECT', None),
    ('c-0000000000000002', 'llm', 'SUPPORTED', None, False, 'P1', POS, 'NOT_A_DEFECT', None),
    ('c-0000000000000003', 'llm', 'SUPPORTED', None, False, 'P2', POS, 'INSUFFICIENT', True),
    ('c-0000000000000004', 'llm', 'REFUTED', None, True, 'P2', POS, 'DEFECT', None),
    ('c-0000000000000005', 'llm', 'REFUTED', None, True, 'P3', POS, 'NOT_A_DEFECT', None),
    ('c-0000000000000006', 'llm', 'REFUTED', None, False, 'A1', CTRL, 'NOT_A_DEFECT', None),
    ('c-0000000000000007', 'llm', 'INSUFFICIENT', None, False, 'A1', CTRL, 'DEFECT', None),
    ('c-0000000000000008', 'llm', 'INSUFFICIENT', PROVIDER, True, 'P3', POS, 'INSUFFICIENT', False),
    ('c-0000000000000009', 'llm', 'INSUFFICIENT', reveal.NOT_VERIFIED, False, 'P3', POS, 'DEFECT', None),
    ('c-000000000000000a', 'static', None, None, True, 'P4', POS, 'NOT_A_DEFECT', None),
    ('c-000000000000000b', 'static', None, None, False, 'A2', CTRL, 'DEFECT', None),
]


class RevealTests(unittest.TestCase):
    """Synthetic decisions covering every branch; never real owner decisions."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        entries = [dict(id=i, arm=a, verdict=v, downgrade=g, hit=h, label=lab, role=role, repository='o/' + lab,
                        number=1, pr_id='rb-' + lab) for i, a, v, g, h, lab, role, _, _ in SYNTHETIC]
        self.mapping = self.root / 'mapping.json'
        self.mapping.write_bytes(reveal.canonical(dict(schema=reveal.MAPPING_SCHEMA, entries=entries)) + b'\n')
        order = [row[0] for row in SYNTHETIC]
        self.manifest = self.root / 'packet-manifest.json'
        self.manifest.write_bytes(reveal.canonical(dict(order=order, mapping_sha256=reveal.sha256(self.mapping.read_bytes()))) + b'\n')
        self.frozen_value = dict(schema=reveal.FROZEN_SCHEMA, packet_manifest_sha256=reveal.sha256(self.manifest.read_bytes()),
                                 mapping_sha256=reveal.sha256(self.mapping.read_bytes()), candidates=len(order),
                                 decisions=[dict(id=i, decision=d, needs_outside_context=o, evidence=[], reason='')
                                            for i, *_, d, o in SYNTHETIC])
        self.frozen = self.root / 'owner-decisions.json'
        self.frozen.write_bytes(reveal.canonical(self.frozen_value) + b'\n')

    def tearDown(self):
        self.tmp.cleanup()

    def run_reveal(self):
        frozen, mapping, _ = reveal.load(self.frozen, self.mapping, self.manifest)
        return reveal.reveal(frozen, mapping)[0]

    def test_all_and_group_counts(self):
        s = self.run_reveal()
        t = lambda n, d, x, i, o, h, dh, share, prec: dict(n=n, DEFECT=d, NOT_A_DEFECT=x, INSUFFICIENT=i, needs_outside_context=o,
                                                          hits=h, defect_and_hit=dh, defect_share=share, precision_decided=prec)
        self.assertEqual(s['all'], t(11, 5, 4, 2, 1, 5, 2, 0.454545, 0.555556))
        self.assertEqual(s['groups'], dict(
            static=t(2, 1, 1, 0, 0, 1, 0, 0.5, 0.5),
            supported=t(3, 1, 1, 1, 1, 1, 1, 0.333333, 0.5),
            refuted=t(3, 1, 2, 0, 0, 2, 1, 0.333333, 0.333333),
            insufficient_verified=t(2, 1, 0, 1, 0, 1, 0, 0.5, 1.0),
            not_verified=t(1, 1, 0, 0, 0, 0, 0, 1.0, 1.0)))
        self.assertEqual(s['insufficient_verified_by_downgrade'], {'MODEL_JUDGEMENT': 1, PROVIDER: 1})

    def test_anchor_and_verification_reconciliation(self):
        s = self.run_reveal()
        self.assertEqual(s['anchors'], dict(defect_total=5, defect_and_hit=2, defect_hit_share=0.4, hit_total=5,
                                            hit_and_defect=2, hit_and_not_a_defect=2, hit_and_insufficient=1))
        self.assertEqual(s['verification'], dict(
            supported=dict(n=3, judged_defect=1, judged_not_a_defect=1, judged_insufficient=1,
                           false_positive_rate=0.333333, false_positive_rate_decided=0.5),
            refuted=dict(n=3, judged_defect_false_negatives=1, judged_not_a_defect=2, judged_insufficient=0,
                         false_negative_share=0.333333),
            insufficient_verified_judged_defect=1, not_verified_judged_defect=1))

    def test_roles_and_labels(self):
        s = self.run_reveal()
        self.assertEqual({k: (v['n'], v['DEFECT'], v['NOT_A_DEFECT'], v['INSUFFICIENT'], v['hits'], v['defect_and_hit'])
                          for k, v in s['roles'].items()}, {POS: (8, 3, 3, 2, 5, 2), CTRL: (3, 2, 1, 0, 0, 0)})
        p3 = s['per_label']['P3']
        self.assertEqual((p3['n'], p3['DEFECT'], p3['NOT_A_DEFECT'], p3['INSUFFICIENT'], p3['hits']), (3, 1, 1, 1, 2))

    def test_empty_denominators_give_null(self):
        self.assertIsNone(reveal.ratio(0, 0))
        self.assertEqual(reveal.tally([])['precision_decided'], None)

    def test_missing_frozen_decisions_exit_with_an_error_and_write_nothing(self):
        self.frozen.unlink()
        done = subprocess.run([sys.executable, '-I', '-B', str(V3 / 'holdout_reveal.py'), '--frozen', str(self.frozen),
                               '--mapping', str(self.mapping), '--manifest', str(self.manifest), '--out', str(self.root / 'out')],
                              capture_output=True, timeout=60)
        self.assertEqual(done.returncode, 2)
        self.assertIn(b'NO_FROZEN_DECISIONS', done.stderr)
        self.assertEqual(done.stdout, b'')
        self.assertFalse((self.root / 'out').exists())

    def test_bindings_and_completeness_are_enforced(self):
        for change, code in ((dict(packet_manifest_sha256='0' * 64), 'PACKET_MANIFEST_BINDING'),
                             (dict(mapping_sha256='0' * 64), 'MAPPING_BINDING'),
                             (dict(decisions=self.frozen_value['decisions'][:-1]), 'PAGE_SET'),
                             (dict(decisions=[dict(d, decision='PENDING_OWNER') for d in self.frozen_value['decisions']]),
                              'DECISIONS_INCOMPLETE')):
            self.frozen.write_bytes(reveal.canonical(dict(self.frozen_value, **change)) + b'\n')
            with self.assertRaisesRegex(reveal.RevealError, code):
                reveal.load(self.frozen, self.mapping, self.manifest)

    def test_cli_writes_a_new_directory(self):
        done = subprocess.run([sys.executable, '-I', '-B', str(V3 / 'holdout_reveal.py'), '--frozen', str(self.frozen),
                               '--mapping', str(self.mapping), '--manifest', str(self.manifest), '--out', str(self.root / 'out')],
                              capture_output=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        summary = json.loads((self.root / 'out' / 'reveal-summary.json').read_bytes())
        self.assertEqual(summary['all']['DEFECT'], 5)
        again = subprocess.run([sys.executable, '-I', '-B', str(V3 / 'holdout_reveal.py'), '--frozen', str(self.frozen),
                                '--mapping', str(self.mapping), '--manifest', str(self.manifest), '--out', str(self.root / 'out')],
                               capture_output=True, timeout=60)
        self.assertEqual(again.returncode, 2)


if __name__ == '__main__':
    unittest.main()
