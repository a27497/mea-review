"""Prepare an arm-blind packet and replay strictly anchored pilot scores offline."""

import argparse
import copy
import json
import math
import os
import random
import re
from pathlib import Path
from statistics import median

from freeze import digest, read, write
from pilot import ARMS, CASES, arms, load_inputs
import judge
import score

BLIND_SEED = 20261120
REVIEW_RULES = (
    'Judge the root cause, trigger and observed behavior. Split a compound candidate into atomic issues.',
    'Use MATCH only for the same issue with a coordinate in the supplied gold range and the frozen revision.',
    'For each other atomic issue give FALSE_POSITIVE or UNDETERMINED and novel_valid true, false or null.',
    'Use the same issue_key for repeated descriptions of the same underlying issue at the same anchor.',
    'Use only this packet and the companion frozen source. Do not consult the Internet or other reviewers.'
)


def anchor(snapshot, candidate):
    side, path, line = (candidate[k] for k in ('side', 'path', 'line'))
    rev = candidate['detail'].get('revision')
    if rev is None:
        return 'MISSING_REVISION'
    if rev != snapshot['revisions'].get(side):
        return 'REVISION_MISMATCH'
    if (side, path, line) not in set(arms.changed_rows(snapshot)):
        return 'OUTSIDE_CHANGED_LINES'
    return 'VALID'


def raw_direct(cell, snapshot):
    receipt = read(cell / '01.output.json')
    if not receipt.get('message', {}).get('content'):
        return []
    try:
        values = json.loads(receipt['message']['content'])['candidates']
    except (ValueError, KeyError, TypeError):
        return []
    rows = []
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get('change'), dict):
            continue
        change = item['change']
        side, path, line = (change.get(k) for k in ('side', 'path', 'line'))
        impact = item.get('impact', item.get('conditional_impact'))
        if (side not in ('base', 'head') or not isinstance(path, str) or type(line) is not int or
                not all(isinstance(text, str) and text.strip() for text in (item.get('title'), item.get('trigger'), impact))):
            continue
        candidate = arms.common(path, side, line, item['title'], item['trigger'], impact, [],
                                dict(revision=change.get('revision')))
        candidate['detail']['anchor_status'] = anchor(snapshot, candidate)
        rows.append(candidate)
    return rows


def candidate_counts(cell, arm, result):
    if arm != 'agent' or not (cell / 'journal.json').exists():
        return 0, 0
    task = read(cell / 'journal.json')
    rejected = sum(e['kind'] in ('decision_rejected', 'response_rejected') for e in task['events'])
    raw = 0
    for r in task['requests']:
        for call in (r.get('receipt') or {}).get('message', {}).get('tool_calls', []):
            if call['function']['name'] == 'finish_investigation':
                try:
                    raw += len(json.loads(call['function']['arguments'])['candidates'])
                except (ValueError, KeyError, TypeError):
                    pass
    return max(raw, len(result['candidates']) + len(result['rejected'])), rejected


def scrub(candidate):
    candidate = copy.deepcopy(candidate)
    brands = re.compile(r'\b(?:MEA Agent|MEA Review|DeepSeek(?:-V[\w.-]+)?|Direct LLM|CodeRabbit)\b', re.I)
    for key in ('title', 'trigger', 'impact'):
        candidate[key] = brands.sub('[review system]', candidate[key])
    return candidate


def assemble(root):
    audit = read(root / 'live-leak-audit.json')
    if audit['status'] != 'PASS':
        raise ValueError('SOURCE_AUDIT_REQUIRED')
    indexed_audit = {(a['case'], a['arm']): a for a in audit['rows']}
    run_dir = root / 'live'
    freeze_rows = {r['case']: r for r in read(root / 'freeze.json')}
    pairs, cases, receipts = [], [], {}
    for case, snapshot in load_inputs(root).items():
        gold = read(root / 'gold' / f'{case}.json')
        write(root / 'blind/support' / f'{case}.json', dict(revisions=snapshot['revisions'], files=snapshot['files']))
        review = {}
        for arm in ARMS:
            cell = run_dir / case / arm
            result = read(cell / 'result.json')
            receipts[str((cell / 'result.json').relative_to(root))] = digest(result)
            raw_agent, protocol_rejections = candidate_counts(cell, arm, result)
            raw = result['candidates']
            audit_row = indexed_audit.get((case, arm), {})
            if arm == 'direct' and result['status'] != 'COMPLETED' and (cell / '01.output.json').exists():
                raw = raw_direct(cell, snapshot)
                protocol_rejections = 1
            for candidate in raw:
                if arm == 'static':
                    candidate['detail']['revision'] = snapshot['revisions'].get(candidate['side'])
                candidate['detail']['anchor_status'] = anchor(snapshot, candidate)
            review[arm] = dict(status=result['status'], reason=result['reason'],
                               secs=result['elapsed_seconds'], cash=result['cash'],
                               requests=result['requests'], posts=result.get('http_posts_attempted', 0),
                               visible_diff_lines=result['diff_lines'] if arm == 'static' or audit_row.get('requests') else 0,
                               raw_candidate_count=raw_agent if arm == 'agent' else len(raw),
                               candidate_validation_rejections=len(result['rejected']),
                               protocol_rejections=protocol_rejections, review_candidates=raw,
                               usage=result.get('usage'), context_gaps=len(audit_row.get('context_gaps', [])))
        pairs.append(dict(slug=case, snapshot=snapshot, gold=gold,
                          outputs={(arm, 1): list(map(scrub, review[arm]['review_candidates'])) for arm in ARMS}))
        cases.append(dict(case=case, gold=gold, injected=freeze_rows[case]['injected'], arms=review))
    packet, mapping = judge.build_packet(pairs, BLIND_SEED)
    packet.update(schema='frozen-blind-review-v1', instructions=REVIEW_RULES)
    for pr in packet['prs']:
        if pr['diff_omitted_files']:
            raise ValueError('BLIND_DIFF_TRUNCATED')
        case = next(row for row in cases if row['case'] == pr['slug'])
        for g, source in zip(pr['gold'], case['gold'], strict=True):
            g['anchors'] = source['anchors']
        for item in pr['candidates']:
            anchors = set()
            for m in mapping['entries']:
                if m['blind_id'] == item['candidate_id']:
                    candidate = case['arms'][m['arm']]['review_candidates'][m['position']]
                    anchors.add((candidate['detail'].get('revision'), candidate['detail']['anchor_status']))
            item['supplied_anchors'] = sorted([dict(revision=rev, status=status) for rev, status in anchors],
                                             key=lambda r: (r['revision'] or '', r['status']))
    random.Random(f'{BLIND_SEED}:case-order').shuffle(packet['prs'])
    mapping['packet_sha256'] = digest(packet)
    mapping['receipts_sha256'] = digest(receipts)
    mapping['schema'] = 'frozen-blind-mapping-v1'
    write(root / 'blind/review-packet.json', packet)
    write(root / 'restricted/blind-mapping.json', mapping)
    write(root / 'restricted/score-input.json', dict(cases=cases, receipt_digests=receipts, packet_sha256=digest(packet),
          mapping_sha256=digest(mapping), audit_sha256=digest(audit)))


def match_anchor(candidate, gold):
    side, path, line, detail = (candidate[k] for k in ('side', 'path', 'line', 'detail'))
    return detail['anchor_status'] == 'VALID' and any(
        side == a['side'] and path == a['path'] and a['start'] <= line <= a['end'] and
        detail['revision'] == a['revision'] for a in gold['anchors'])


def use_judgments(packet, paths):
    if len(paths) != 2:
        return {}, None
    a, b = map(read, paths)
    expected = {c['candidate_id'] for pr in packet['prs'] for c in pr['candidates']}
    if (a['reviewer'] == b['reviewer'] or not a['reviewer'] or not b['reviewer'] or any(
            v['packet_sha256'] != digest(packet) or set(v['decisions']) != expected
            for v in (a, b))):
        raise ValueError('INDEPENDENT_REVIEW_BINDING')
    consensus = {}
    for cid in sorted(expected):
        labels = []
        for review in (a, b):
            issues = review['decisions'][cid]
            if not isinstance(issues, list) or not issues:
                raise ValueError('ATOMIC_ISSUE_SET')
            normalized = [{k: i[k] for k in ('verdict', 'gold_id', 'novel_valid', 'issue_key')} for i in issues]
            if (len({i['issue_key'] for i in normalized}) != len(normalized) or
                    any(i['verdict'] not in score.VERDICTS or not isinstance(i['issue_key'], str) or
                        not i['issue_key'] or i['novel_valid'] not in (True, False, None) or
                        (i['gold_id'] is not None) != (i['verdict'] == 'MATCH') for i in normalized)):
                raise ValueError('INVALID_HUMAN_VERDICT')
            labels.append(sorted(normalized, key=lambda i: i['issue_key']))
        if labels[0] == labels[1]:
            consensus[cid] = labels[0]
    return consensus, dict(reviewers=2, candidate_agreement=len(consensus), population=len(expected),
                           disagreements=len(expected) - len(consensus),
                           agreement_ratio=len(consensus) / len(expected) if expected else None)


def row_score(case, arm, result, entries, consensus):
    raw = result['review_candidates']
    labels, exploratory = [], []
    duplicates, extras, unresolved, valid, confirmed_false = 0, 0, 0, 0, 0
    gold = {g['id']: g for g in case['gold']}
    seen = set()
    for pos, candidate in enumerate(raw):
        cid = entries[(case['case'], arm, pos)]
        anchor_valid = candidate['detail']['anchor_status'] == 'VALID'
        valid += anchor_valid
        verdicts = consensus.get(cid)
        if verdicts is None:
            unresolved += 1
            verdicts = [dict(verdict='UNDETERMINED' if anchor_valid else 'FALSE_POSITIVE',
                             gold_id=None, novel_valid=None, issue_key=cid)]
        for decision in verdicts:
            verdict, matched = decision['verdict'], decision['gold_id']
            if verdict == 'MATCH' and (matched not in gold or not match_anchor(candidate, gold[matched])):
                verdict, matched = 'FALSE_POSITIVE', None
            key = matched if verdict == 'MATCH' else (decision['issue_key'], candidate['side'],
                                                       candidate['path'], candidate['line'])
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            extras += decision['novel_valid'] is True and verdict != 'MATCH'
            confirmed_false += cid in consensus and decision['novel_valid'] is False and verdict != 'MATCH'
            exploratory.append(dict(verdict=verdict, gold_id=matched))
            if result['status'] == 'COMPLETED':
                labels.append(exploratory[-1])
    gold_ids = list(gold)
    counts = score.pr_counts(gold_ids, labels)
    sensitivity = score.pr_counts(gold_ids, exploratory)
    return dict(counts=counts, raw_visibility_sensitivity=sensitivity, raw_candidates=result['raw_candidate_count'],
                validated_anchors=valid, duplicates=duplicates, extras=extras, unresolved=unresolved,
                confirmed_false=confirmed_false, match_ids=[c['gold_id'] for c in labels if c['verdict'] == 'MATCH'],
                exploratory_match_ids=[c['gold_id'] for c in exploratory if c['verdict'] == 'MATCH'])


def micro(rows):
    counts = score.pooled(rows)
    tp, fp, fn = counts['matched_tp'], counts['unmatched_fp'], counts['gold_tp'] - counts['gold_tp_covered']
    return dict(tp=tp, fp=fp, fn=fn, precision=tp / (tp+fp) if tp+fp else None,
                recall=tp / (tp+fn) if tp+fn else None,
                f1=(2*tp) / (2*tp+fp+fn) if 2*tp+fp+fn else None,
                undetermined_in_precision_denominator=counts['undetermined'])


def replay(root, output, reviews):
    frozen = read(root / 'restricted/score-input.json')
    packet = read(root / 'blind/review-packet.json')
    mapping = read(root / 'restricted/blind-mapping.json')
    if (digest(packet) != frozen['packet_sha256'] or digest(packet) != mapping['packet_sha256'] or
            digest(mapping) != frozen['mapping_sha256'] or digest(frozen['receipt_digests']) != mapping['receipts_sha256'] or
            digest(read(root / 'live-leak-audit.json')) != frozen['audit_sha256']):
        raise ValueError('REPLAY_BINDING')
    for path, sha in frozen['receipt_digests'].items():
        if digest(read(root / path)) != sha:
            raise ValueError('RECEIPT_REPLACED')
    consensus, agreement = use_judgments(packet, reviews)
    entries = {(r['slug'], r['arm'], r['position']): r['blind_id'] for r in mapping['entries']}
    pairs, arm_tables = [], {arm: [] for arm in ARMS}
    for case in frozen['cases']:
        by_arm = {}
        for arm, result in case['arms'].items():
            row = row_score(case, arm, result, entries, consensus)
            row.update({k: v for k, v in result.items() if k != 'review_candidates'})
            arm_tables[arm].append(row)
            by_arm[arm] = row
        pairs.append(dict(case=case['case'], injected=case['injected'], arms=by_arm,
                         triple_completed=all(r['status'] == 'COMPLETED' for r in by_arm.values()),
                         gold_hits={g['id']: [arm for arm, r in by_arm.items() if g['id'] in r['match_ids']]
                                    for g in case['gold']}))
    summaries = {}
    for arm, rows in arm_tables.items():
        completed = [r for r in rows if r['status'] == 'COMPLETED']
        controls = [p['arms'][arm] for p in pairs if not p['injected']]
        timings = sorted(r['secs'] for r in rows if r['status'] != 'NOT_RUN')
        summaries[arm] = dict(
            planned=len(rows), completed=len(completed), scorable=len(completed),
            completed_zero=sum(r['raw_candidates'] == 0 for r in completed),
            failed=sum(r['status'] in ('STOPPED', 'FAILED') for r in rows),
            limited=sum(r['status'] == 'LIMITED' for r in rows), not_run=sum(r['status'] == 'NOT_RUN' for r in rows),
            completion_rate=len(completed)/len(rows), posts=sum(r['posts'] for r in rows),
            p50_seconds=median(timings) if timings else None,
            p95_seconds=timings[math.ceil(.95*len(timings))-1] if timings else None,
            scheduled_nano=sum(r['cash']['scheduled_nano'] for r in rows),
            peak_nano=sum(r['cash']['peak_nano'] for r in rows),
            unresolved_nano=sum(r['cash']['unresolved_nano'] for r in rows),
            planned_micro=micro([r['counts'] for r in rows]),
            scored_micro=micro([r['counts'] for r in completed]),
            raw_visibility_sensitivity=micro([r['raw_visibility_sensitivity'] for r in rows]),
            unresolved=sum(r['unresolved'] for r in rows),
            control_reviewed_prs=sum(r['status'] == 'COMPLETED' for r in controls),
            control_raw_candidates=sum(r['raw_candidates'] for r in controls),
            control_raw_with_candidates=sum(r['raw_candidates'] > 0 for r in controls))
    write(output, dict(
        status='PROVISIONAL' if agreement is None or any(r['unresolved'] for r in summaries.values()) else 'ADJUDICATED',
        score_input_sha256=digest(frozen), review_agreement=agreement,
        complete_triples=sum(p['triple_completed'] for p in pairs),
        arm_summaries=summaries, per_pr=pairs))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('private', type=Path)
    parser.add_argument('--assemble', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--review', action='append', type=Path, default=[])
    args = parser.parse_args()
    os.umask(0o077)
    root = args.private.resolve(strict=True)
    if args.assemble:
        assemble(root)
    if args.output:
        replay(root, args.output, args.review)
    if not (args.output or args.assemble):
        parser.error('Choose --assemble or --output.')


if __name__ == '__main__':
    main()
