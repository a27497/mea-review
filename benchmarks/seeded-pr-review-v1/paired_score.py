"""Arm-blind scoring, frozen receipts, and paired resampling for Phase 10."""

import argparse
import math
import os
import random
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from statistics import median

from freeze import digest, read, write
from paired import ARMS, load_inputs
from replay import REVIEW_RULES, anchor, judge, micro, row_score, scrub, use_judgments

SEED = 20261120
RESAMPLES = 10000


def assemble(phase, source, cohort):
    snapshots = load_inputs(source, cohort)
    metadata = {r['case']: r for r in read(source / 'freeze.json')}
    audit = read(phase / f'{cohort}-leak-audit.json')
    if audit['status'] != 'PASS':
        raise ValueError('LEAK_AUDIT_REQUIRED')
    auditor = {(r['case'], r['arm']): r for r in audit['rows']}
    entries, score_cases, packet_cases = [], [], []
    out = phase / f'{cohort}-score'
    for key, snapshot in snapshots.items():
        profiles = {}
        gold = read(source / 'gold' / f'{key}.json')
        options = {}
        write(out/'blind/support'/f'{key}.json', dict(revisions=snapshot['revisions'], files=snapshot['files']))
        for arm in ARMS:
            relative = f'{cohort}-live/{key}/{arm}/result.json'
            result = read(phase / relative)
            candidates = result['candidates']
            for i, row in enumerate(candidates):
                status = anchor(snapshot, row)
                if status == 'VALID' and row['detail'].get('evidence_valid') is False:
                    status = 'INVALID_EVIDENCE'
                row['detail']['anchor_status'] = status
                item = judge.candidate_text(scrub(row))
                item.update(revision=row['detail'].get('revision'), anchor_status=status)
                hashed = digest(item)
                candidate_id = judge.opaque(SEED, key, hashed)
                entries.append(dict(slug=key, arm=arm, position=i, blind_id=candidate_id))
                comparable = item['side'] in ('base', 'head') and type(item['line']) is int
                quote = judge.anchor_quote(snapshot, item['path'], item['side'], item['line']) if comparable else None
                options[candidate_id] = dict(candidate_id=candidate_id, anchor_quote=quote, **item)
            transfer = auditor[key, arm]
            profiles[arm] = dict(status=result['status'], reason=result['reason'], secs=result['elapsed_seconds'],
                                 posts=result['posts'], receipt=relative, receipt_sha256=digest(read(phase / relative)),
                                 requests=result['requests'], visible_diff_lines=result['diff_lines']
                                 if transfer['complete_diff'] else 0,
                                 raw_candidate_count=result.get('raw_candidates', len(candidates)),
                                 candidate_validation_rejections=len(result['rejected']), review_candidates=candidates,
                                 context_gaps=sum(bool(tool['page']['unknown'] or tool['page']['source_truncated']
                                                       or tool['page']['host_page_limited']) for tool in transfer['tools']))
        choices = sorted(options.values(), key=lambda r: r['candidate_id'])
        random.Random(f'{SEED}:{key}:options').shuffle(choices)
        diff, omitted = judge.bounded_diff(snapshot, limit=200_000)
        if omitted:
            raise ValueError('BLIND_DIFF_INCOMPLETE')
        packet_cases.append(dict(slug=key, diff=diff, revisions=snapshot['revisions'],
                                 gold=[{k: g[k] for k in ('id','anchors','summary','bodies')} for g in gold],
                                 candidates=choices))
        score_cases.append(dict(case=key, repository=metadata[key]['repository'], gold=gold,
                                injected=bool(gold), arms=profiles))
    random.Random(f'{SEED}:cases').shuffle(packet_cases)
    packet = dict(schema='frozen-blind-review-v2', instructions=REVIEW_RULES, prs=packet_cases)
    mapping = dict(packet_sha256=digest(packet), entries=entries)
    write(out/'blind/review-packet.json', packet)
    (out/'blind/PACKET-SHA256.txt').write_text(digest(packet)+'\n')
    write(out/'restricted/arm-mapping.json', mapping)
    write(out/'restricted/score-input.json', dict(cases=score_cases, packet_sha256=digest(packet),
          mapping_sha256=digest(mapping), audit_sha256=digest(audit)))


def paired_interval(pairs, a, b, seed):
    if not pairs:
        return None
    strata = defaultdict(list)
    for p in pairs:
        strata[p['repository']].append(p)
    rng = random.Random(seed)

    def difference(rows):
        left = micro([r['arms'][a]['counts'] for r in rows])['f1']
        right = micro([r['arms'][b]['counts'] for r in rows])['f1']
        return left-right if left is not None and right is not None else None

    diffs = []
    for _ in range(RESAMPLES):
        collected = [rng.choice(group) for group in strata.values() for _ in group]
        gap = difference(collected)
        if gap is not None:
            diffs.append(gap)
    diffs.sort()
    return dict(point=difference(pairs), replicates=len(diffs), unscorable=RESAMPLES-len(diffs),
                lower=diffs[math.floor(.025*len(diffs))] if diffs else None,
                upper=diffs[min(len(diffs)-1,math.floor(.975*len(diffs)))] if diffs else None)


def summarize(rows):
    successful = [r for r in rows if r['status'] == 'COMPLETED']
    seconds = sorted(r['secs'] for r in rows if r['status'] != 'NOT_RUN')
    return dict(planned=len(rows), completed=len(successful), completed_zero=sum(not r['raw_candidate_count'] for r in successful),
                completion_rate=len(successful)/len(rows) if rows else None,
                statuses={status:sum(r['status']==status for r in rows) for status in ('COMPLETED','FAILED','LIMITED','NOT_RUN')},
                true_extra=sum(r['extras'] for r in rows), unresolved=sum(r['unresolved'] for r in rows),
                raw=sum(r['raw_candidates'] for r in rows), valid_anchors=sum(r['validated_anchors'] for r in rows),
                validation_rejections=sum(r['candidate_validation_rejections'] for r in rows),
                duplicates=sum(r['duplicates'] for r in rows), posts=sum(r['posts'] for r in rows),
                confirmed_false=sum(r['confirmed_false'] for r in rows),
                planned_micro=micro([r['counts'] for r in rows]), completed_micro=micro([r['counts'] for r in successful]),
                p50_seconds=median(seconds) if seconds else None,
                p95_seconds=seconds[math.ceil(.95*len(seconds))-1] if seconds else None,
                peak_nano=sum(e.get('peak_nano',0) for r in rows for e in r['requests']),
                scheduled_nano=sum(e.get('scheduled_nano',0) for r in rows for e in r['requests']),
                unresolved_nano=sum(e.get('unresolved_nano',0) for r in rows for e in r['requests']),
                prompt_tokens=sum(e.get('usage',{}).get('prompt_tokens',0) for r in rows for e in r['requests']),
                completion_tokens=sum(e.get('usage',{}).get('completion_tokens',0) for r in rows for e in r['requests']))


def replay(phase, cohort, target, judgments):
    out = phase / f'{cohort}-score'
    packet = read(out/'blind/review-packet.json')
    mapping = read(out/'restricted/arm-mapping.json')
    frozen = read(out/'restricted/score-input.json')
    if (digest(packet) != mapping['packet_sha256'] or digest(packet) != frozen['packet_sha256'] or
            digest(mapping) != frozen['mapping_sha256'] or
            digest(read(phase/f'{cohort}-leak-audit.json')) != frozen['audit_sha256']):
        raise ValueError('BLIND_RECEIPT_BINDING')
    decisions, agreement = use_judgments(packet, judgments)
    entries = {(e['slug'],e['arm'],e['position']):e['blind_id'] for e in mapping['entries']}
    pairs = []
    for case in frozen['cases']:
        scored = {}
        for arm, data in case['arms'].items():
            if digest(read(phase/data['receipt'])) != data['receipt_sha256']:
                raise ValueError('FROZEN_SCORE_RECEIPT')
            row = row_score(case, arm, data, entries, decisions)
            row.update({k:v for k,v in data.items() if k != 'review_candidates'})
            scored[arm] = row
        pairs.append(dict(case=case['case'], repository=case['repository'], arms=scored, injected=case['injected'],
                          all_completed=all(r['status']=='COMPLETED' for r in scored.values()),
                          gold=[dict(id=g['id'],cross_file=g.get('cross_file'),hits=[
                              a for a,r in scored.items() if g['id'] in r['match_ids']]) for g in case['gold']]))
    complete = [p for p in pairs if p['all_completed']]
    controls = [p for p in pairs if not p['injected']]
    summaries = {arm: summarize([p['arms'][arm] for p in pairs]) for arm in ARMS}
    repos = sorted({p['repository'] for p in pairs})
    arms_order = ('agent', 'direct', 'static')
    status = 'ADJUDICATED' if agreement and not any(r['unresolved'] for r in summaries.values()) else 'PROVISIONAL'
    write(target, dict(status=status, score_input_sha256=digest(frozen), review_agreement=agreement, summary=summaries,
        pairs=len(pairs), complete_pairs=len(complete), per_pr=pairs,
        complete_pair_scores={a:summarize([p['arms'][a] for p in complete]) for a in ARMS},
        audit_sha256=frozen['audit_sha256'], bootstrap_method=dict(strata='repository', seed=SEED, resamples=RESAMPLES, measure='micro F1 difference'),
        paired_intervals={f'{a}_minus_{b}':dict(full=paired_interval(pairs,a,b,SEED),
             complete=paired_interval(complete,a,b,SEED)) for a,b in combinations(arms_order,2)},
        per_repository={repo:{a:summarize([p['arms'][a] for p in pairs if p['repository']==repo]) for a in ARMS} for repo in repos},
        controls={a:dict(prs=len(controls), completed=sum(p['arms'][a]['status']=='COMPLETED' for p in controls),
            prs_with_candidates=sum(p['arms'][a]['raw_candidates']>0 for p in controls),
            raw=sum(p['arms'][a]['raw_candidates'] for p in controls),
            false_positive_confirmed=sum(p['arms'][a]['confirmed_false'] for p in controls)) for a in ARMS},
        strata_gold={str(cross):dict(gold=sum(g['cross_file'] is cross for p in pairs for g in p['gold']),
            hits={a:sum(g['cross_file'] is cross and a in g['hits'] for p in pairs for g in p['gold'])
                  for a in ARMS}) for cross in (True,False,None)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', type=Path)
    parser.add_argument('source', type=Path)
    parser.add_argument('cohort', choices=('dev','holdout'))
    parser.add_argument('--assemble', action='store_true')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--review', type=Path, action='append', default=[])
    args = parser.parse_args()
    os.umask(0o077)
    path = args.phase.resolve(strict=True)
    if args.assemble:
        assemble(path, args.source.resolve(strict=True), args.cohort)
    if args.output:
        replay(path, args.cohort, args.output, args.review)
