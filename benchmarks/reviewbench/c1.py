"""Recompute C1 Main telemetry from a numeric projection, without raw runs or judging."""
from collections import Counter
from math import ceil, isfinite
import re

ARMS = ('static', 'direct', 'original_agent', 'optimized_agent')
TERMINAL = {'COMPLETED', 'STOPPED', 'CANCELLED'}
COUNTS = ('known_http_posts', 'reported_tokens', 'pending_head_candidates',
          'base_only_candidates', 'rejected_candidates', 'observed_changed_lines')
CASES = {f'case-{n}' for n in range(1, 9)}
ROUNDS = {1, 2, 3}


def distribution(values):
    ordered = sorted(n for n in values if n is not None)
    if not ordered:
        return dict(n=0, p50=None, p95=None)
    return dict(n=len(ordered), p50=ordered[ceil(len(ordered)*0.5)-1],
                p95=ordered[ceil(len(ordered)*0.95)-1])


def recompute(data):
    if (not isinstance(data, dict) or set(data) != {'schema', 'source_sha256', 'cells'}
            or data['schema'] != 'mea-reviewbench-c1-main-projection-v1'):
        raise ValueError('C1_PROJECTION_SCHEMA')
    hashes = data['source_sha256']
    if (not isinstance(hashes, dict) or set(hashes) != {'per-cell.json', 'summary.json'}
            or not all(isinstance(h, str) and re.fullmatch('[0-9a-f]{64}', h) for h in hashes.values())):
        raise ValueError('C1_SOURCE_IDENTITY')
    cells = data['cells']
    expected = {(case, round_) for case in CASES for round_ in ROUNDS}
    if not isinstance(cells, list) or len(cells) != len(expected):
        raise ValueError('C1_INCOMPLETE_GRID')
    groups = {arm: [] for arm in ARMS}
    for cell in cells:
        if not isinstance(cell, dict) or set(cell) != {'case', 'round', 'arms'}:
            raise ValueError('C1_CELL_SCHEMA')
        key = (cell['case'], cell['round'])
        if type(cell['round']) is not int or key not in expected:
            raise ValueError('C1_CELL_DUPLICATE_OR_UNKNOWN')
        expected.remove(key)
        arms = cell['arms']
        if not isinstance(arms, dict) or set(arms) != set(ARMS):
            raise ValueError('C1_MISSING_ARM')
        for arm, row in arms.items():
            if not isinstance(row, dict) or set(row) != {'status', 'seconds', *COUNTS}:
                raise ValueError('C1_CELL_FIELDS')
            elapsed = row['seconds']
            if row['status'] not in TERMINAL or (elapsed is not None and (
                    type(elapsed) not in (int, float) or not isfinite(elapsed) or elapsed < 0)):
                raise ValueError('C1_CELL_STATE')
            if any(type(row[k]) is not int or row[k] < 0 for k in COUNTS):
                raise ValueError('C1_CELL_COUNT')
            groups[arm].append(row)
    metrics = {}
    for arm, rows in groups.items():
        states = Counter(row['status'] for row in rows)
        metrics[arm] = dict(n=len(rows), completed=states['COMPLETED'],
            completed_rate=states['COMPLETED']/len(rows),
            failed_rate=(states['STOPPED'] + states['CANCELLED'])/len(rows),
            task_seconds=distribution(r['seconds'] for r in rows),
            **{key: sum(row[key] for row in rows) for key in COUNTS})
    # The archived v2 coverage index reports six shared-positive PRs. The
    # numeric Main projection carries neither that label index nor judgments.
    undefined = dict(precision=None, recall=None, f1=None)
    return dict(campaign_status='PARTIAL', main_distinct_prs=len(CASES), main_shared_positive_prs=6,
                quality=dict(status='BLOCKED', matched_tp=None, confirmed_fp=None,
                             grounded=undefined, augmented=dict(undefined)),
                judge_requests=0, source_sha256=hashes, main=metrics)
