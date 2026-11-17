"""M3 scoring over deduplicated gold defects. Pure arithmetic; no I/O, model or judge.

The unit of truth is a deduplicated defect: review comments the curator assigned to the same defect
key become one gold entry. A candidate judged MATCH claims its gold defect; each gold defect can be
claimed once (stable candidate order), so a second candidate for the same defect is unmatched.
Unmatched, FALSE_POSITIVE and UNDETERMINED candidates all count against precision (primary metric).

Primary metrics reuse benchmarks/reviewbench/scoring.py: with every unmatched candidate labelled fp,
its augmented precision is matched / candidates and its augmented recall is covered / gold.
"""
import random
import sys
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import scoring  # noqa: E402

VERDICTS = ('MATCH', 'FALSE_POSITIVE', 'UNDETERMINED')
THRESHOLD = 0.10


def dedupe_gold(slug, comments):
    """Curated comments -> gold defects in first-appearance order.

    Each comment: dict(comment_id, defect, path, line, body, summary). Comments with the same
    defect key are one defect; its summary is the first non-empty curator summary.
    """
    defects, index = [], {}
    for c in comments:
        key = c['defect']
        if not isinstance(key, str) or not key:
            raise ValueError('GOLD_DEFECT_KEY')
        if key not in index:
            index[key] = len(defects)
            defects.append(dict(id=f'{slug}:D{len(defects) + 1}', key=key, comment_ids=[], locations=[], bodies=[], summary=''))
        d = defects[index[key]]
        if c['comment_id'] in d['comment_ids']:
            raise ValueError('GOLD_DUPLICATE_COMMENT')
        d['comment_ids'].append(c['comment_id'])
        loc = dict(path=c['path'], line=c['line'])
        if loc not in d['locations']:
            d['locations'].append(loc)
        d['bodies'].append(c['body'])
        if not d['summary'] and c.get('summary'):
            d['summary'] = c['summary']
    if any(not d['summary'] for d in defects):
        raise ValueError('GOLD_SUMMARY_REQUIRED')
    return defects


def unmatched_indices(correspondences):
    claimed, unmatched = set(), []
    for i, indices in enumerate(correspondences):
        remaining = set(indices) - claimed
        if remaining:
            claimed.add(min(remaining))
        else:
            unmatched.append(i)
    return unmatched


def pr_counts(gold_ids, decisions):
    """Counts for one arm, run and PR. decisions: one dict(verdict, gold_id) per candidate, in output order."""
    position = {g: i for i, g in enumerate(gold_ids)}
    correspondences = []
    undetermined = 0
    for d in decisions:
        if d['verdict'] not in VERDICTS:
            raise ValueError('VERDICT')
        if d['verdict'] == 'MATCH':
            if d.get('gold_id') not in position:
                raise ValueError('MATCH_GOLD_UNKNOWN')
            correspondences.append([position[d['gold_id']]])
        else:
            if d.get('gold_id') is not None:
                raise ValueError('NON_MATCH_WITH_GOLD')
            correspondences.append([])
            undetermined += d['verdict'] == 'UNDETERMINED'
    labels = {i: 'fp' for i in unmatched_indices(correspondences)}
    row = scoring.counts(['tp'] * len(gold_ids), correspondences, labels)
    if row['pending']:
        raise AssertionError('PENDING_AFTER_LABELS')
    row['undetermined'] = undetermined
    return row


def metrics(row):
    m = scoring.metrics(row)
    decided = row['candidates'] - row['undetermined']
    return dict(precision=m['augmented_precision'], recall=m['augmented_recall'], f1=m['augmented_f1'],
                precision_decided=scoring.ratio(row['matched_tp'], decided),
                candidates=row['candidates'], matched=row['matched_tp'], gold=row['gold_tp'], covered=row['gold_tp_covered'],
                undetermined=row['undetermined'])


def pooled(rows):
    keys = ('candidates', 'gold_tp', 'gold_tp_covered', 'matched', 'matched_tp', 'matched_fp', 'novel_tp', 'unmatched_fp',
            'pending', 'undetermined')
    return {k: sum(r[k] for r in rows) for k in keys}


def run_metrics(table, prs):
    """table[pr] -> counts row. Micro metrics over the listed PRs (repeats allowed for bootstrap)."""
    return metrics(pooled([table[p] for p in prs]))


def zero_f1(value):
    return 0.0 if value is None else value


def arm_summary(runs, prs):
    """runs: list of table[pr] (one per repetition). Mean and range of micro P/R/F1 over repetitions."""
    per_run = [run_metrics(t, prs) for t in runs]
    out = {}
    for key in ('precision', 'recall', 'f1'):
        values = [zero_f1(r[key]) if key == 'f1' else r[key] for r in per_run]
        defined = [v for v in values if v is not None]
        out[key] = dict(mean=mean(defined) if defined else None, min=min(defined) if defined else None,
                        max=max(defined) if defined else None, runs=values)
    out['per_run'] = per_run
    return out


def mean_f1(runs, prs):
    return mean(zero_f1(run_metrics(t, prs)['f1']) for t in runs)


def bootstrap_difference(agent_runs, direct_runs, prs, *, resamples=10000, seed=0, alpha=0.05):
    """Percentile CI of mean-over-runs micro F1 (Agent - Direct), resampling PRs with replacement.

    The same PR resample is applied to both arms and every repetition.
    """
    prs = list(prs)
    if not prs:
        raise ValueError('EMPTY_BOOTSTRAP')
    rng = random.Random(seed)
    diffs = []
    for _ in range(resamples):
        sample = [prs[rng.randrange(len(prs))] for _ in prs]
        diffs.append(mean_f1(agent_runs, sample) - mean_f1(direct_runs, sample))
    diffs.sort()
    lo = diffs[int((alpha / 2) * resamples)]
    hi = diffs[min(resamples - 1, int((1 - alpha / 2) * resamples))]
    point = mean_f1(agent_runs, prs) - mean_f1(direct_runs, prs)
    return dict(point=point, low=lo, high=hi, resamples=resamples, seed=seed, alpha=alpha,
                share_at_or_above_threshold=sum(d >= THRESHOLD for d in diffs) / resamples)


def stop_decision(agent_mean_f1, direct_mean_f1, threshold=THRESHOLD):
    """M3 stop rule (ROADMAP section 7): Agent must beat Direct by >= 10 F1 points."""
    gap = agent_mean_f1 - direct_mean_f1
    return dict(gap=gap, threshold=threshold,
                outcome='NEGATIVE_RESULT_STOP' if gap < threshold else 'THRESHOLD_MET')
