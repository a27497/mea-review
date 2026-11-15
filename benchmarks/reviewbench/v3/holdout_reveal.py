"""Reveal and reconcile the owner's frozen holdout decisions. Offline; use only after the freeze.

Reads the frozen owner-decisions.json, the packet manifest and the judge-only mapping, and refuses
unless the three bind to each other (packet manifest digest, mapping digest, the same page IDs) and
every decision is DEFECT, NOT_A_DEFECT or INSUFFICIENT. With no frozen decision file it exits with
an error; it never reports counts for absent decisions.

Reported, all as counts with the stated denominators:

- per group (static findings; review candidates by verification result: SUPPORTED, REFUTED,
  INSUFFICIENT after verification, NOT_VERIFIED): the three owner decisions, needs_outside_context,
  defect_share = DEFECT / n and precision_decided = DEFECT / (DEFECT + NOT_A_DEFECT);
- mechanical anchors: owner DEFECTs that also hit an anchor, and hits the owner judged NOT_A_DEFECT
  or INSUFFICIENT;
- verification v2.1: SUPPORTED judged NOT_A_DEFECT (false positives, over all SUPPORTED and over
  the decided ones) and REFUTED judged DEFECT (false negatives), and the same for the other groups;
- roles: positive candidates and abstention controls separately, and every PR by its label.

Small numbers: a ratio is null when its denominator is 0.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

VERSION = 'mea-reviewbench-v3-holdout-reveal-v1'
FROZEN_SCHEMA = 'mea-reviewbench-v3-holdout-owner-decisions-v1'
MAPPING_SCHEMA = 'mea-reviewbench-v3-holdout-blind-mapping-v1'
DECISIONS = ('DEFECT', 'NOT_A_DEFECT', 'INSUFFICIENT')
NOT_VERIFIED = 'NOT_VERIFIED_CANDIDATE_CAP'
GROUPS = ('static', 'supported', 'refuted', 'insufficient_verified', 'not_verified')


class RevealError(Exception):
    pass


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def ratio(a, b):
    return None if b == 0 else round(a / b, 6)


def group_of(entry):
    if entry['arm'] == 'static':
        return 'static'
    if entry['downgrade'] == NOT_VERIFIED:
        return 'not_verified'
    return {'SUPPORTED': 'supported', 'REFUTED': 'refuted', 'INSUFFICIENT': 'insufficient_verified'}[entry['verdict']]


def tally(rows):
    d = {k: sum(r['decision'] == k for r in rows) for k in DECISIONS}
    return dict(n=len(rows), **d, needs_outside_context=sum(r['needs_outside_context'] is True for r in rows),
                hits=sum(r['hit'] for r in rows), defect_and_hit=sum(r['hit'] and r['decision'] == 'DEFECT' for r in rows),
                defect_share=ratio(d['DEFECT'], len(rows)), precision_decided=ratio(d['DEFECT'], d['DEFECT'] + d['NOT_A_DEFECT']))


def load(frozen_path, mapping_path, manifest_path):
    frozen_path = Path(frozen_path)
    if not frozen_path.is_file():
        raise RevealError('NO_FROZEN_DECISIONS: freeze the owner decisions first; nothing is reported without them')
    frozen_raw, mapping_raw, manifest_raw = (Path(p).read_bytes() for p in (frozen_path, mapping_path, manifest_path))
    frozen, mapping, manifest = (json.loads(r) for r in (frozen_raw, mapping_raw, manifest_raw))
    if frozen.get('schema') != FROZEN_SCHEMA or mapping.get('schema') != MAPPING_SCHEMA:
        raise RevealError('SCHEMA')
    if frozen['packet_manifest_sha256'] != sha256(manifest_raw):
        raise RevealError('PACKET_MANIFEST_BINDING')
    if not frozen['mapping_sha256'] == manifest['mapping_sha256'] == sha256(mapping_raw):
        raise RevealError('MAPPING_BINDING')
    ids = [d['id'] for d in frozen['decisions']]
    if ids != manifest['order'] or set(ids) != {e['id'] for e in mapping['entries']} or len(set(ids)) != len(ids):
        raise RevealError('PAGE_SET')
    if frozen['candidates'] != len(ids) or any(d['decision'] not in DECISIONS for d in frozen['decisions']):
        raise RevealError('DECISIONS_INCOMPLETE')
    return frozen, mapping, dict(frozen=sha256(frozen_raw), mapping=sha256(mapping_raw), manifest=sha256(manifest_raw))


def reveal(frozen, mapping):
    by_id = {e['id']: e for e in mapping['entries']}
    rows = []
    for d in frozen['decisions']:
        e = by_id[d['id']]
        rows.append(dict(id=d['id'], decision=d['decision'], needs_outside_context=d['needs_outside_context'],
                         evidence=d['evidence'], reason=d['reason'], group=group_of(e), arm=e['arm'], verdict=e['verdict'],
                         downgrade=e['downgrade'], hit=e['hit'], label=e['label'], role=e['role'],
                         repository=e['repository'], number=e['number'], pr_id=e['pr_id']))
    groups = {g: tally([r for r in rows if r['group'] == g]) for g in GROUPS}
    insufficient_by_downgrade = {}
    for r in rows:
        if r['group'] == 'insufficient_verified':
            key = r['downgrade'] or 'MODEL_JUDGEMENT'
            insufficient_by_downgrade[key] = insufficient_by_downgrade.get(key, 0) + 1
    hits = [r for r in rows if r['hit']]
    defects = [r for r in rows if r['decision'] == 'DEFECT']
    anchors = dict(defect_total=len(defects), defect_and_hit=sum(r['hit'] for r in defects),
                   defect_hit_share=ratio(sum(r['hit'] for r in defects), len(defects)), hit_total=len(hits),
                   hit_and_defect=sum(r['decision'] == 'DEFECT' for r in hits),
                   hit_and_not_a_defect=sum(r['decision'] == 'NOT_A_DEFECT' for r in hits),
                   hit_and_insufficient=sum(r['decision'] == 'INSUFFICIENT' for r in hits))
    s, f = groups['supported'], groups['refuted']
    verification = dict(
        supported=dict(n=s['n'], judged_defect=s['DEFECT'], judged_not_a_defect=s['NOT_A_DEFECT'],
                       judged_insufficient=s['INSUFFICIENT'], false_positive_rate=ratio(s['NOT_A_DEFECT'], s['n']),
                       false_positive_rate_decided=ratio(s['NOT_A_DEFECT'], s['DEFECT'] + s['NOT_A_DEFECT'])),
        refuted=dict(n=f['n'], judged_defect_false_negatives=f['DEFECT'], judged_not_a_defect=f['NOT_A_DEFECT'],
                     judged_insufficient=f['INSUFFICIENT'], false_negative_share=ratio(f['DEFECT'], f['n'])),
        insufficient_verified_judged_defect=groups['insufficient_verified']['DEFECT'],
        not_verified_judged_defect=groups['not_verified']['DEFECT'])
    roles = {}
    for r in rows:
        roles.setdefault(r['role'], []).append(r)
    labels = {}
    for r in rows:
        labels.setdefault(r['label'], []).append(r)
    per_label = {k: dict(repository=v[0]['repository'], number=v[0]['number'], role=v[0]['role'], **tally(v))
                 for k, v in sorted(labels.items())}
    summary = dict(schema=VERSION, candidates=len(rows), all=tally(rows), groups=groups,
                   insufficient_verified_by_downgrade=dict(sorted(insufficient_by_downgrade.items())),
                   anchors=anchors, verification=verification,
                   roles={k: tally(v) for k, v in sorted(roles.items())}, per_label=per_label,
                   meaning=('Owner decisions are one person\'s judgement on the frozen input; an anchor hit is a '
                            'later fix touching the cited line. Twelve PRs and few candidates: no ratio here is '
                            'statistically meaningful.'))
    return summary, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--frozen', type=Path, required=True, help='owner-decisions.json written by the freeze')
    parser.add_argument('--mapping', type=Path, required=True, help='judge-only/mapping.json')
    parser.add_argument('--manifest', type=Path, required=True, help='packet/packet-manifest.json')
    parser.add_argument('--out', type=Path, required=True, help='new directory for the reveal')
    args = parser.parse_args()
    try:
        if args.out.exists():
            raise RevealError('OUTPUT_EXISTS')
        frozen, mapping, digests = load(args.frozen, args.mapping, args.manifest)
        summary, rows = reveal(frozen, mapping)
    except RevealError as exc:
        print('Reveal refused:', exc, file=sys.stderr)
        return 2
    summary['inputs_sha256'] = digests
    args.out.mkdir(mode=0o700)
    for name, value in (('reveal-summary.json', summary), ('reveal-candidates.json', dict(schema=VERSION + '-candidates', rows=rows))):
        with (args.out / name).open('xb') as handle:
            handle.write(canonical(value) + b'\n')
        os.chmod(args.out / name, 0o444)
    print(json.dumps(dict(all=summary['all'], anchors=summary['anchors'], verification=summary['verification']), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
