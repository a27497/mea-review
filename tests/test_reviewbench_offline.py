"""Contract regressions for the offline benchmark. No provider or judge."""
import copy
import importlib
from pathlib import Path
import sys
import unittest

from mea.contract import canonical

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / 'benchmarks/reviewbench'
sys.path.insert(0, str(BENCH))
c1 = importlib.import_module('c1')
runner = importlib.import_module('runner')
verify = importlib.import_module('verify')


class ReviewBenchOfflineTests(unittest.TestCase):
    def test_locked_snapshots_and_both_license_sides(self):
        manifest = verify.read(verify.SAMPLES / 'manifest.json')
        selected = verify.verify_inputs(manifest)
        self.assertEqual({row['repository'] for row, _, _ in selected},
                         {'resplab/leap', 'torchfont/torchfont'})
        self.assertEqual(sum(len(row['licenses']) for row, _, _ in selected), 4)
        bad = copy.deepcopy(manifest)
        bad['cases'][0]['licenses'].pop('base')
        with self.assertRaisesRegex(ValueError, 'SOURCE_NOTICE_BOTH_SIDES_REQUIRED'):
            verify.verify_inputs(bad)

    def test_direct_cannot_cite_a_wrong_quote(self):
        value = verify.verify_inputs(verify.read(verify.SAMPLES / 'manifest.json'))[0][2]
        direct, _ = runner.initial_requests(runner.View.from_input(value))
        visible = runner.visible_body(direct)
        raw = runner.decode(runner.direct_mock(visible))
        raw['findings'][0]['quote'] = 'wrong literal'
        _, accepted, rejected = runner.validate_direct(visible, canonical(raw))
        self.assertEqual(accepted, [])
        self.assertEqual(rejected[0]['reason'], 'DIRECT_CITATION_OR_DUPLICATE')

    def test_c1_grid_and_historical_blocked_quality(self):
        data = verify.read(BENCH / 'c1/main-cells.json')
        replay = c1.recompute(data)
        self.assertEqual(replay, verify.read(BENCH / 'c1/expected-summary.json'))
        self.assertEqual(replay['campaign_status'], 'PARTIAL')
        self.assertEqual(replay['quality']['status'], 'BLOCKED')
        self.assertTrue(all(value is None for value in replay['quality']['grounded'].values()))
        self.assertEqual(replay['main']['original_agent']['completed'], 0)
        self.assertEqual(replay['main']['optimized_agent']['completed'], 12)

        duplicate = copy.deepcopy(data)
        duplicate['cells'][1] = duplicate['cells'][0]
        with self.assertRaisesRegex(ValueError, 'C1_CELL_DUPLICATE_OR_UNKNOWN'):
            c1.recompute(duplicate)
        missing_arm = copy.deepcopy(data)
        missing_arm['cells'][0]['arms'].pop('static')
        with self.assertRaisesRegex(ValueError, 'C1_MISSING_ARM'):
            c1.recompute(missing_arm)

    def test_pending_arithmetic_is_not_zero_quality(self):
        evidence = verify.synthetic_score()
        self.assertEqual(evidence['mode'], 'SYNTHETIC_ARITHMETIC_ONLY')
        self.assertEqual(evidence['pending']['pending'], 1)
        self.assertEqual(evidence['per_pr']['grounded_f1'], 2 / 3)
        self.assertEqual(evidence['per_pr']['augmented_f1'], 0.5)
