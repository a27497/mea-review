"""Reproduce the packaged ReviewBench contract and historical C1 Main summary."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import c1
import runner
from scoring import aggregate, counts, metrics
from mea.contract import canonical, decode, digest, git_blob_sha, validate
from mea.investigation.provider_view import View

SAMPLES = HERE / 'samples'
VENDOR = HERE / 'vendor/ReviewBench'


def require(condition, code):
    if not condition:
        raise ValueError(code)


def read(path):
    return decode(path.read_bytes())


def write(path, value):
    path.write_bytes(canonical(value) + b'\n')


def synthetic_score():
    gold = ['tp', 'tp', 'fp']
    matches = [[1, 0], [0], [2], [], [], []]
    pending = counts(gold, matches, {1: 'fp', 3: 'tp', 4: 'fp'})
    require(pending == dict(candidates=6, gold_tp=2, gold_tp_covered=2, matched=2, matched_tp=1,
                            matched_fp=1, novel_tp=1, unmatched_fp=2, pending=1)
            and metrics(pending)['augmented_precision'] is None, 'PENDING_SCORING_ARITHMETIC')
    adjudicated = counts(gold, matches, {1: 'fp', 3: 'tp', 4: 'fp', 5: 'fp'})
    # A duplicate after claim cannot claim the same gold TP twice.
    require(adjudicated == dict(candidates=6, gold_tp=2, gold_tp_covered=2, matched=2,
                                matched_tp=1, matched_fp=1, novel_tp=1, unmatched_fp=3, pending=0),
            'SCORING_COUNT_ARITHMETIC')
    scored = metrics(adjudicated)
    require(scored['grounded_precision'] == 0.5 and scored['grounded_recall'] == 1
            and scored['grounded_f1'] == 2/3 and scored['augmented_precision'] == 1/3
            and scored['augmented_recall'] == 1 and scored['augmented_f1'] == 0.5
            and scored['confirmed_invalid'] == 4, 'SCORING_RATE_ARITHMETIC')
    empty = counts(['tp'], [], {})
    require(metrics(empty)['grounded_recall'] == 0 and metrics(empty)['grounded_precision'] is None,
            'EMPTY_FINDING_ARITHMETIC')
    return dict(mode='SYNTHETIC_ARITHMETIC_ONLY', golden_labels=gold, correspondences=matches,
                pending=pending, complete=adjudicated, per_pr=scored,
                aggregate=aggregate([adjudicated, empty]))


def verify_inputs(manifest):
    require(manifest['schema'] == 'mea-reviewbench-release-samples-v1', 'SAMPLE_MANIFEST_SCHEMA')
    rows = manifest['cases']
    ids = {row['id'] for row in rows}
    require(len(rows) == len(ids) and ids and
            {p.name for p in (SAMPLES / 'inputs').iterdir()} == {i + '.json' for i in ids},
            'SAMPLE_INVENTORY')
    selected = []
    for row in rows:
        source = SAMPLES / 'inputs' / (row['id'] + '.json')
        require(row['snapshot'] == source.relative_to(SAMPLES).as_posix() and
                not source.is_symlink() and source.is_file(), 'PLAIN_SNAPSHOT_REQUIRED')
        raw = source.read_bytes()
        value = validate(decode(raw))
        require(digest(raw) == row['file_sha256'] and digest(value) == row['snapshot_sha256']
                and View.from_input(value).sha256 == row['view_sha256'] and value['id'] == row['id']
                and value['revisions'] == row['revisions']
                and (value['pr']['repository'], value['pr']['number']) == (row['repository'], row['number']),
                'SNAPSHOT_LOCK_DRIFT')
        require(set(row['licenses']) == {'base', 'head'}, 'SOURCE_NOTICE_BOTH_SIDES_REQUIRED')
        for side, license_ in row['licenses'].items():
            path = SAMPLES / license_['path']
            require(path.parent == SAMPLES / 'licenses' and path.is_file() and not path.is_symlink(),
                    'SOURCE_NOTICE_PATH')
            notice = path.read_bytes()
            require(notice.startswith(b'MIT License') and digest(notice) == license_['sha256']
                    and git_blob_sha(notice) == license_['git_blob_sha']
                    and license_['source'] == 'https://raw.githubusercontent.com/'
                        + row['repository'] + '/' + value['revisions'][side] + '/' + license_['upstream_path'],
                    'SOURCE_NOTICE_DRIFT')
        selected.append((row, source, value))
    return selected


def run_arm(arm, source, checksum):
    return subprocess.run([sys.executable, '-I', '-B', str(HERE / 'runner.py'), arm, str(source), checksum],
                          cwd=HERE, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'},
                          capture_output=True, timeout=30)


def verify_arms(row, value, outputs):
    view = View.from_input(value)
    direct, agent = runner.initial_requests(view)
    d, a = (json.loads(body['messages'][1]['content']) for body in (direct, agent))
    require(d == {k: a[k] for k in runner.EVIDENCE_KEYS}, 'SHARED_INITIAL_EVIDENCE')
    visible = [p['path'] for p in d['initial_diffs'] if 'diff' in p]
    omitted = [p['path'] for p in d['initial_diffs'] if 'diff' not in p]
    require((visible, omitted) == (row['initial_visible_paths'], row['initial_omitted_paths']),
            'INITIAL_PROJECTION_LOCK_MISMATCH')
    for output in outputs.values():
        require(output['model_requests'] == output['judge_requests'] == 0
                and output['snapshot_sha256'] == row['snapshot_sha256']
                and output['id'] == row['id'] and output['status'] == 'COMPLETED', 'ARM_OUTPUT_SCOPE')
    static = outputs['static']
    require(static['mode'] == 'STATIC_RULES_NOT_AI' and static['raw']['real_ai'] == 'NOT_RUN'
            and static['request_count'] == 0, 'STATIC_SCOPE')
    mock_direct = outputs['direct']
    require(mock_direct['mode'] == 'MOCK_CONTRACT_NOT_AI' and mock_direct['request_body'] == direct
            and mock_direct['request_count'] == 1 and mock_direct['tool_count'] == 0 and 'tools' not in direct,
            'DIRECT_SCOPE')
    mock_agent = outputs['agent']
    task = mock_agent['raw']
    require(mock_agent['mode'] == 'MOCK_PROVIDER_NOT_AI' and task['requests'][0]['request_body'] == agent
            and all(r['receipt']['http_posts_attempted'] == 0 for r in task['requests'])
            and all(c['correctness'] == 'UNADJUDICATED' for c in task['candidates'])
            and task['results'], 'AGENT_SCOPE')
    messages = task['requests'][-1]['request_body']['messages']
    require(any(msg['role'] == 'tool' for msg in messages), 'AGENT_NO_ACTUAL_FEEDBACK')
    observed = {(e['side'], e['path'], e['line']) for r in task['results'] for e in r['evidence']}
    return dict(id=value['id'], shared_initial_sha256=digest(d), visible_paths=visible, omitted_paths=omitted,
                direct_new_source_lines=0, agent_observed_source_lines=sorted(observed),
                agent_observed_omitted_paths=sorted({name for _, name, _ in observed if name in omitted}))


def negative_checks(value, source, out):
    checksum = digest(value)
    isolated = run_arm('isolation', source, checksum)
    require(isolated.returncode == 0 and decode(isolated.stdout)['blocked'] == [
        'OFFLINE_DATA_ACCESS_BLOCKED', 'OFFLINE_EXTERNAL_OPERATION_BLOCKED'], 'ISOLATION_PROBE')
    invalid = run_arm('invalid', source, checksum)
    require(invalid.returncode == 0, 'INVALID_PROBE_EXIT')
    result = decode(invalid.stdout)
    require(result['status'] == 'STOPPED' and result['reason'] == 'INVALID_MODEL_DECISION'
            and result['tool_count'] == 0, 'INVALID_NATIVE_DISPATCH')
    with TemporaryDirectory(dir=out) as folder:
        probe = Path(folder) / 'inputs'
        probe.mkdir()
        target = probe / 'probe.json'
        write(target, dict(value, pr=dict(value['pr'], title='Changed neutral title')))
        altered = run_arm('static', target, checksum)
        require(altered.returncode != 0 and b'RUNNER_INPUT_HASH_MISMATCH' in altered.stderr, 'TAMPER_NOT_REJECTED')
        injected = dict(value, golden={'sentinel': 'SYNTHETIC_LEAK_TEST'})
        write(target, injected)
        injected_result = run_arm('direct', target, digest(injected))
        require(injected_result.returncode != 0 and b'UNSUPPORTED_OR_INVALID_FIELDS' in injected_result.stderr,
                'GOLD_FIELD_NOT_REJECTED')
    return dict(denied=['judge-only file read', 'socket creation', 'changed snapshot digest',
                        'injected golden field'], invalid_native_tool_dispatches=result['tool_count'])


def official_format(out, expectations, manifest):
    node = shutil.which('node')
    require(node is not None, 'NODE_24_REQUIRED_FOR_OFFICIAL_FORMAT')
    files = ('LICENSE', 'scripts/lib/types.ts', 'scripts/eval/input-validation.ts')
    require(set(files) == set(manifest), 'OFFICIAL_READER_INVENTORY')
    for name in files:
        require(digest((VENDOR / name).read_bytes()) == manifest[name]['sha256'], 'OFFICIAL_READER_HASH')
    expected = out / 'official-format-input.json'
    write(expected, expectations)
    with TemporaryDirectory(dir=out) as folder:
        source = Path(folder)
        for name in files[1:]:
            dest = source / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((VENDOR / name).read_bytes())
        (source / 'scripts/lib/types.js').symlink_to('types.ts')
        result = subprocess.run([node, '--permission', '--allow-fs-read=' + str(HERE),
            '--allow-fs-read=' + str(out), str(HERE / 'official-format.mjs'),
            str(source / 'scripts/eval/input-validation.ts'), str(expected)], cwd=out,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, capture_output=True, timeout=30)
    if result.returncode != 0:
        (out / 'official-format.stderr').write_bytes(result.stderr)
        raise ValueError('OFFICIAL_INPUT_READER_REJECTED')
    report = decode(result.stdout)
    require(report['mode'] == 'OFFICIAL_INPUT_FORMAT_ONLY'
            and len(report['reports']) == len(expectations), 'OFFICIAL_FORMAT_COVERAGE')
    return report


def verify(out):
    manifest = read(SAMPLES / 'manifest.json')
    selected = verify_inputs(manifest)
    out.mkdir(parents=True, exist_ok=False)
    exports = out / 'mock-contract-exports-do-not-judge'
    exports.mkdir()
    visibility, expectations = [], []
    for row, source, value in selected:
        outputs = {}
        for arm in runner.ARMS:
            proc = run_arm(arm, source, row['snapshot_sha256'])
            name = row['id'] + '-' + arm + '.json'
            if proc.returncode:
                (out / (name + '.stderr')).write_bytes(proc.stderr)
                raise ValueError('ARM_EXIT_NONZERO_' + arm)
            result = outputs[arm] = decode(proc.stdout)
            write(out / name, result)
            exported = runner.export(dict(row, base=value['revisions']['base'], head=value['revisions']['head']),
                                     result['findings'], arm)
            path = exports / name
            write(path, exported)
            expectations.append(dict(file=path.relative_to(out).as_posix(), pr=exported['pr'],
                                     count=len(exported['findings'])))
        visibility.append(verify_arms(row, value, outputs))
    require(any(r['agent_observed_omitted_paths'] for r in visibility), 'NO_PAGING_VISIBILITY_EXERCISED')
    write(out / 'visibility.json', visibility)
    write(out / 'official-format.json', official_format(out, expectations, manifest['input_reader']))
    write(out / 'synthetic-arithmetic.json', synthetic_score())
    row, sample, value = selected[0]
    write(out / 'negative-checks.json', negative_checks(value, sample, out))
    historical = c1.recompute(read(HERE / 'c1/main-cells.json'))
    require(historical == read(HERE / 'c1/expected-summary.json'), 'C1_ARCHIVED_SUMMARY_MISMATCH')
    write(out / 'c1-main-recomputed.json', historical)
    report = dict(status='OFFLINE_CONTRACT_PASS', samples=len(selected), arm_runs=len(expectations),
                  official_input_files=len(expectations), model_requests=0, judge_requests=0,
                  offline_quality='NOT_MEASURED', c1_main_cells=24 * len(c1.ARMS),
                  c1_campaign_status=historical['campaign_status'], c1_quality_status=historical['quality']['status'])
    write(out / 'verification.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True, help='fresh evidence directory')
    args = parser.parse_args()
    print(json.dumps(verify(args.out.resolve()), sort_keys=True))
