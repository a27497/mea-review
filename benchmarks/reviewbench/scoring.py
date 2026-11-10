"""ReviewBench overall arithmetic from independent adjudication, with no judge."""
from statistics import mean


def ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def f1(precision, recall):
    if precision is None or recall is None:
        return None
    return 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def counts(gold_labels, correspondences, unmatched_labels):
    """Gold in source order. One complete correspondence list per candidate.

    unmatched_labels maps candidate index to independent tp/fp, omitting
    pending decisions. Correspondences are produced outside MEA.
    """
    if any(label not in ('tp', 'fp') for label in gold_labels):
        raise ValueError('UNKNOWN_GOLD_LABEL')
    claimed, covered, matched, unmatched = set(), set(), {}, set()
    for candidate, indices in enumerate(correspondences):
        if indices is None or any(type(i) is not int or not 0 <= i < len(gold_labels) for i in indices):
            raise ValueError('INCOMPLETE_OR_INVALID_MATCH')
        covered.update(indices)
        remaining = set(indices) - claimed
        if remaining:
            selected = min(remaining)
            matched[candidate] = selected
            claimed.add(selected)
        else:
            unmatched.add(candidate)
    if (any(type(i) is not int for i in unmatched_labels)
            or unmatched_labels.keys() - unmatched
            or any(label not in ('tp', 'fp') for label in unmatched_labels.values())):
        raise ValueError('INVALID_UNMATCHED_ADJUDICATION')
    tp_match = sum(gold_labels[index] == 'tp' for index in matched.values())
    novel = sum(label == 'tp' for label in unmatched_labels.values())
    return dict(candidates=len(correspondences), gold_tp=gold_labels.count('tp'),
                gold_tp_covered=sum(gold_labels[i] == 'tp' for i in covered),
                matched=len(matched), matched_tp=tp_match, matched_fp=len(matched) - tp_match,
                novel_tp=novel, unmatched_fp=len(unmatched_labels) - novel,
                pending=len(unmatched) - len(unmatched_labels))


def metrics(row):
    grounded_precision = ratio(row['matched_tp'], row['matched'])
    grounded_recall = ratio(row['gold_tp_covered'], row['gold_tp'])
    augmented_precision = augmented_recall = None
    if not row['pending']:
        augmented_precision = ratio(row['matched_tp'] + row['novel_tp'], row['candidates'])
        augmented_recall = ratio(row['gold_tp_covered'] + row['novel_tp'], row['gold_tp'] + row['novel_tp'])
    return dict(grounded_precision=grounded_precision, grounded_recall=grounded_recall,
                grounded_f1=f1(grounded_precision, grounded_recall),
                augmented_precision=augmented_precision, augmented_recall=augmented_recall,
                augmented_f1=f1(augmented_precision, augmented_recall),
                confirmed_invalid=row['matched_fp'] + row['unmatched_fp'])


def aggregate(rows):
    if not rows:
        raise ValueError('EMPTY_SCORE_COHORT')
    per_pr = [metrics(row) for row in rows]
    pooled = {key: sum(row[key] for row in rows) for key in rows[0]}
    macro = {}
    for key in per_pr[0]:
        values = [row[key] for row in per_pr if row[key] is not None]
        macro[key] = dict(mean=mean(values) if values else None, defined_prs=len(values))
    return dict(pr_count=len(rows), counts=pooled, micro=metrics(pooled), macro=macro)
