"""M3 operator CLI: capture, run arms, blind judging, scoring and agreement packets.

Every billed call is attended and bounded by the declared sample list of one invocation: one POST
per request, no retries. Private inputs and outputs live under --private (never in Git).

  capture  --refs FILE                      read-only GitHub capture of owner/repo#n@pin lines
  run      --plan P --set S --arm A --rep K  run one arm over one set (holdout needs --approval)
  judge    --plan P --set S --runs DIR       build the blind packet and call the judge once per PR
  score    --plan P --set S --runs DIR       reveal, score, bootstrap and the stop rule
  agreement-packet / agreement               Codex consistency sample (holdout)
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for p in (str(ROOT), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

from mea.contract import canonical, decode, digest  # noqa: E402
import arms  # noqa: E402
import judge as judging  # noqa: E402
import plan as planning  # noqa: E402
import provider  # noqa: E402
import score as scoring  # noqa: E402

# Private M3 directory (samples, runs, ledger). Never inside the repository; set --private or MEA_M3_PRIVATE.
PRIVATE = Path(os.environ['MEA_M3_PRIVATE']) if os.environ.get('MEA_M3_PRIVATE') else None
NANO = 1_000_000_000


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical(value))


def read(path):
    return decode(Path(path).read_bytes(), limit=50_000_000)


def sample_dir(private, slug):
    return Path(private) / 'samples' / slug


def slug_of(repository, number):
    return f"{repository.replace('/', '--')}--{number}"


def git_state():
    head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(['git', 'status', '--porcelain', '--', 'mea', 'benchmarks'], cwd=ROOT, capture_output=True, text=True).stdout
    return head, bool(dirty.strip())


# ---- capture --------------------------------------------------------------------------------

def cmd_capture(args):
    from mea import github_input
    secret = github_input.token()
    out = []
    for line in Path(args.refs).read_text().split():
        reference = line.strip()
        owner, repo, number, pin = github_input.parse_ref(reference)
        slug = slug_of(f'{owner}/{repo}', number)
        target = sample_dir(args.private, slug)
        if (target / 'snapshot.json').exists() and not args.force:
            out.append(dict(slug=slug, status='EXISTING'))
            continue
        report, snapshot = github_input.capture(reference, github_input.RestFetcher(secret), search_pause=7)
        report['exit_status'] = 0 if snapshot else 3
        write(target / 'capture.json', report)
        if snapshot is None:
            (target / 'snapshot.json').unlink(missing_ok=True)
            out.append(dict(slug=slug, status='FAILED', failure=report['failure']))
            continue
        write(target / 'snapshot.json', snapshot)
        evaluation = arms.evaluation_snapshot(snapshot)
        write(target / 'eval-snapshot.json', evaluation)
        out.append(dict(slug=slug, status=report['status'], gaps=report['gap_counts'], counts=report['counts'],
                        input_sha256=digest(evaluation)))
        print(json.dumps(out[-1]), flush=True)
    write(Path(args.private) / 'selection' / f'capture-{int(time.time())}.json', out)


# ---- run ------------------------------------------------------------------------------------

def samples(plan, set_name, only=None):
    rows = plan['samples'][set_name]
    if only:
        rows = [s for s in rows if s['slug'] in only]
    return rows


def load_eval(private, sample):
    snapshot = read(sample_dir(private, sample['slug']) / 'eval-snapshot.json')
    if digest(snapshot) != sample['input_sha256']:
        raise ValueError('INPUT_SHA256_MISMATCH:' + sample['slug'])
    arms.assert_no_pr_text(snapshot)
    return snapshot


def run_static(private, rows, out):
    for s in rows:
        snapshot = load_eval(private, s)
        start = time.monotonic()
        result = arms.static_candidates(snapshot)
        result.update(slug=s['slug'], elapsed_seconds=round(time.monotonic() - start, 3), cost=dict(scheduled_nano=0, peak_nano=0))
        write(out / f"{s['slug']}.json", result)


MAX_WORKERS = 4  # Declared in PLAN.json: at most 4 tasks in flight per arm; arms never overlap.


def direct_one(private, s, out, ledger, secret, tags):
    snapshot = load_eval(private, s)
    body, context = arms.direct_request(snapshot)
    reserved = provider.reservation('deepseek-flash', len(canonical(body)), arms.DIRECT_OUTPUT_TOKENS)
    entry = ledger.admit(reserved, arm='direct', slug=s['slug'], **tags)
    start = time.monotonic()  # Per-task time: starts when this worker sends, so queueing is excluded.
    receipt = provider.direct_call(body, secret, arms.validate_direct_body)
    elapsed = round(time.monotonic() - start, 3)
    price = provider.cost('deepseek-flash', receipt.get('usage'))
    ledger.record(entry, at=time.time(), arm='direct', slug=s['slug'], model='deepseek-flash', usage=receipt.get('usage'),
                  scheduled_nano=price and price['scheduled'], peak_nano=price and price['peak'], reserved_nano=reserved,
                  http_status=receipt.get('http_status'), error=receipt.get('error'), **tags)
    result = arms.direct_result(receipt, context)
    result.update(slug=s['slug'], elapsed_seconds=elapsed, concurrency=tags['workers'],
                  cost=dict(scheduled_nano=price and price['scheduled'], peak_nano=price and price['peak']))
    write(out / f"{s['slug']}.json", result)
    write(out / 'raw' / f"{s['slug']}.receipt.json", dict(request_body_sha256=digest(body), receipt=receipt))
    return dict(slug=s['slug'], status=result['status'], reason=result['reason'], candidates=len(result['candidates']), seconds=elapsed)


def run_direct(private, rows, out, ledger, secret, tags):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=tags['workers']) as pool:
        for line in pool.map(lambda s: direct_one(private, s, out, ledger, secret, tags), rows):
            print(json.dumps(line), flush=True)


AGENT_TASK_RESERVATION = 512000 * 300 + 6 * 1600 * 1200  # nano-USD peak bound of one product-budget task


def agent_batches(rows, workers):
    """Contiguous batches of at most 8 tasks (one scope and store each), at least `workers` batches when possible."""
    size = max(1, min(8, -(-len(rows) // max(1, workers))))
    return [rows[i:i + size] for i in range(0, len(rows), size)]


def agent_batch(private, b, batch, out, ledger_path, ledger_cap, tags, config):
    """One scope and its own store, tasks in sequence. Runs in its own process, so no store or SQLite file is shared."""
    from mea.investigation import authorization, deepseek, live_run, real_protocol as protocol
    from mea.investigation.provider_worker import Broker
    from mea.investigation.real import Coordinator, create
    from mea.investigation.tasks import Store, TERMINAL
    secret = deepseek.read_key()
    ledger = provider.Ledger(ledger_path, ledger_cap)
    lines = []
    snaps = {s['slug']: load_eval(private, s) for s in batch}
    # An infrastructure rerun gets its own scope id, so its own store: it never reopens batch b1 of the
    # original run (stored task, PHASE3_TASK_CAP) or overwrites that batch's scope file.
    suffix = f"-x{tags['rerun']}" if tags.get('rerun') else ''
    scope = dict(id=f"mea-live-m3-{tags['set']}-{tags['round']}-r{tags['rep']}-b{b}{suffix}", inputs=[digest(v) for v in snaps.values()],
                 max_tasks=len(batch), max_requests=6 * len(batch), tool_profile=config['tool_profile'],
                 first_turn=config['first_turn'], approval=config['approval'], budget_usd=config['scope_budget_usd'],
                 task_budget=config['task_budget'])
    lane = authorization.live(scope)
    store_dir = Path(private) / 'stores' / scope['id']
    write(out / 'scopes' / f"{scope['id']}.json", scope)
    store = Store(store_dir)
    coordinator = Coordinator(store, lane)
    coordinator.boot()
    sender = deepseek.Sender(secret)
    broker = Broker(store, sender=sender, lane=lane)
    try:
        for s in batch:
            entry = ledger.admit(AGENT_TASK_RESERVATION, arm='agent', slug=s['slug'], **tags)
            start = time.monotonic()
            task = create(store, snaps[s['slug']], budget=lane.scope['task_budget'], lane=lane, confirmed=True,
                          first_turn=lane.scope['first_turn'], tool_profile=lane.scope['tool_profile'])
            sender.validate = lambda body, task=task: protocol.validate_task_request(body, task)
            if task['status'] not in TERMINAL:
                task = live_run.drive(store, coordinator, broker, task['id'])
            elapsed = round(time.monotonic() - start, 3)
            task = store.get(task['id'])
            spend = [r.get('shared_spending') or {} for r in task['requests']]
            scheduled = sum(x.get('scheduled_nano_usd') or 0 for x in spend)
            peak = sum(x.get('accounted_nano_usd') or 0 for x in spend)
            ledger.record(entry, at=time.time(), arm='agent', slug=s['slug'], model='deepseek-flash', usage=task['usage'],
                          scheduled_nano=scheduled, peak_nano=peak, reserved_nano=AGENT_TASK_RESERVATION,
                          requests=len(task['requests']), status=task['status'], reason=task['reason'], **tags)
            result = arms.agent_result(task)
            result.update(slug=s['slug'], elapsed_seconds=elapsed, task_id=task['id'], concurrency=tags['workers'], batch=scope['id'],
                          cost=dict(scheduled_nano=scheduled, peak_nano=peak))
            write(out / f"{s['slug']}.json", result)
            with store.connection() as db:
                summary = live_run.summary(task, lane.spending.summary(db))
            write(out / 'raw' / f"{s['slug']}.task.json", summary)
            lines.append(dict(slug=s['slug'], status=task['status'], reason=task['reason'], requests=len(task['requests']),
                              tools=result['tools'], candidates=len(result['candidates']), seconds=elapsed))
    finally:
        broker.close()
        coordinator.shutdown()
    return lines


def run_agent(private, rows, out, ledger, secret, tags, config):
    batches = agent_batches(rows, tags['workers'])
    if tags['workers'] == 1:
        for b, batch in enumerate(batches, 1):
            for line in agent_batch(private, b, batch, out, ledger.path, ledger.cap, tags, config):
                print(json.dumps(line), flush=True)
        return
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor
    with ProcessPoolExecutor(max_workers=tags['workers'], mp_context=multiprocessing.get_context('spawn')) as pool:
        futures = [pool.submit(agent_batch, private, b, batch, out, ledger.path, ledger.cap, tags, config)
                   for b, batch in enumerate(batches, 1)]
        for f in futures:
            for line in f.result():
                print(json.dumps(line), flush=True)


# Stops caused by transport or provider infrastructure, not by the model's answer. Only these (or a
# missing output, e.g. a killed process) may be rerun; any other first result is the sample.
INFRASTRUCTURE_STOPS = {'NO_RECEIPT', 'HTTP_ERROR', 'RESPONSE_BYTE_BOUND', 'CONNECTION_OR_RESPONSE_UNCERTAIN',
                        'PROVIDER_HTTP_ERROR', 'PROVIDER_WORKER_NOT_READY', 'RECOVERY_UNCERTAIN', 'TRANSPORT', 'UNCERTAIN'}


def infrastructure_stop(out, slug):
    path = Path(out) / f'{slug}.json'
    if not path.exists():
        return True
    result = read(path)
    return result.get('status') != 'COMPLETED' and result.get('reason') in INFRASTRUCTURE_STOPS


def supersede(out, slugs, rerun):
    """Move earlier outputs of rerun slugs to superseded/<rerun>/ so billed first attempts stay on disk."""
    out, moved = Path(out), []
    target = out / 'superseded' / rerun
    for slug in sorted(slugs):
        for path in [out / f'{slug}.json', *sorted((out / 'raw').glob(f'{slug}.*'))]:
            if path.exists():
                dest = target / path.relative_to(out)
                dest.parent.mkdir(parents=True, exist_ok=True)
                path.rename(dest)
                moved.append(str(dest.relative_to(out)))
    return moved


def check_preregistration(plan, exclusions_path):
    """Apply the pre-registered sample rules (exclusions, duplicates, cross-set repositories, bounds) before a holdout run."""
    exclusions_path = Path(exclusions_path)
    raw = exclusions_path.read_bytes() if exclusions_path.exists() else None
    # The same bytes are hashed and decoded, so the checked rules are the approved ones.
    if raw is None or hashlib.sha256(raw).hexdigest() != plan['exclusions']['sha256']:
        raise SystemExit('BLOCKED: the exclusion list must match the SHA-256 recorded in PLAN.json')
    try:
        planning.check_samples(plan['samples'], decode(raw, limit=50_000_000))
    except (ValueError, KeyError, TypeError) as exc:
        raise SystemExit('BLOCKED: pre-registration check failed: ' + str(exc)) from None


def cmd_run(args):
    plan, plan_sha = planning.load_plan(args.plan, args.plan_sha256)
    head, dirty = git_state()
    workers = args.workers
    limit = plan.get('concurrency', {}).get('max_in_flight_per_arm', 1)
    if not 1 <= workers <= min(limit, MAX_WORKERS):
        raise SystemExit(f'BLOCKED: --workers must be between 1 and {min(limit, MAX_WORKERS)} (PLAN concurrency)')
    tags = dict(set=args.set, rep=args.rep, round=args.round, plan_sha256=plan_sha, code_sha=head, workers=workers)
    out = Path(args.runs) / args.set / args.round / args.arm / f'rep{args.rep}'
    if args.set == 'holdout':
        planning.holdout_admission(plan, plan_sha, read(args.approval) if args.approval else None)
        check_preregistration(plan, args.exclusions or Path(args.private) / 'selection' / 'exclusions.json')
        if head != plan['frozen']['code_sha'] or dirty:
            raise SystemExit('BLOCKED: holdout runs need the frozen code SHA and a clean mea/benchmarks tree')
    if args.arm != 'static' and not args.confirm_real_provider:
        raise SystemExit('BLOCKED: --confirm-real-provider is required for billed DeepSeek calls')
    only = set(args.only.split(',')) if args.only else None
    if only:
        unknown = sorted(only - {s['slug'] for s in plan['samples'][args.set]})
        if unknown:
            raise SystemExit('BLOCKED: --only names slugs outside the set: ' + ','.join(unknown))
    with arm_lock(out):
        run_locked(args, plan, plan_sha, head, dirty, tags, out, only)


@contextmanager
def arm_lock(out):
    """One invocation per arm and repetition at a time; held until the results are in place."""
    import fcntl
    path = out.parent / f'.{out.name}.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit('BLOCKED: another run holds this arm and repetition') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def run_locked(args, plan, plan_sha, head, dirty, tags, out, only):
    # A directory with results or a recorded attempt has been dispatched before; even a first attempt
    # that wrote nothing may have created a store, so any further run is a rerun with its own scope.
    attempted = out.exists() and (any(out.glob('*.json')) or (out / 'attempts.jsonl').exists())
    target, rerun = out, None
    if attempted:
        if not (only and args.reason):
            raise SystemExit('BLOCKED: this arm/rep already ran; an infrastructure rerun needs --only SLUGS and --reason')
        if (out.parent.parent / 'judge' / 'packet.json').exists():
            raise SystemExit('BLOCKED: this round is already judged; rerun in a new round directory')
        kept = sorted(slug for slug in only if not infrastructure_stop(out, slug))
        if kept:
            raise SystemExit('BLOCKED: a rerun only replaces a missing output or an infrastructure stop; kept: ' + ','.join(kept))
        rerun = digest(dict(at=time.time(), only=sorted(only), reason=args.reason))[:8]
        tags['rerun'] = rerun
        target = out / 'staging' / rerun  # The active results stay in place until the replacement is complete.
    out.mkdir(parents=True, exist_ok=True)
    with (out / 'attempts.jsonl').open('a') as f:
        f.write(json.dumps(dict(at=time.time(), only=sorted(only) if only else None, reason=args.reason, rerun=rerun,
                                code_sha=head)) + '\n')
    rows = samples(plan, args.set, only)
    started = time.time()
    if args.arm == 'static':
        run_static(args.private, rows, target)
    else:
        from mea.investigation import deepseek
        secret = deepseek.read_key()
        ledger = provider.Ledger(Path(args.private) / 'ledger.jsonl', int(plan['budget']['ledger_cap_usd'] * NANO))
        if args.arm == 'direct':
            run_direct(args.private, rows, target, ledger, secret, tags)
        else:
            run_agent(args.private, rows, target, ledger, secret, tags, plan['arms']['agent'])
    if rerun:
        moved, promoted = promote(out, target, rerun)
        with (out / 'reruns.jsonl').open('a') as f:
            f.write(json.dumps(dict(at=time.time(), only=sorted(only), reason=args.reason, rerun=rerun, superseded=moved,
                                    promoted=promoted)) + '\n')
    elif not only:
        write(out / 'manifest.json', dict(plan_sha256=plan_sha, code_sha=head, dirty=dirty, set=args.set, arm=args.arm, rep=args.rep,
                                         round=args.round, workers=tags['workers'], started_at=started, finished_at=time.time(),
                                         samples=[s['slug'] for s in rows]))


def promote(out, stage, rerun):
    """Archive each replaced result under superseded/<rerun>/, then move its staged replacement in.

    Only slugs with a staged result are replaced; a slug whose rerun wrote nothing keeps its active result."""
    moved, promoted = [], []
    for staged in sorted(stage.glob('*.json')):
        slug = staged.stem
        moved += supersede(out, {slug}, rerun)
        for src in [staged, *sorted((stage / 'raw').glob(f'{slug}.*'))]:
            dest = out / src.relative_to(stage)
            dest.parent.mkdir(parents=True, exist_ok=True)
            src.rename(dest)
            promoted.append(str(dest.relative_to(out)))
    for src in sorted((stage / 'scopes').glob('*.json')):
        dest = out / 'scopes' / src.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dest)
    return moved, promoted


# ---- judge ----------------------------------------------------------------------------------

def outputs_for(plan, set_name, runs_dir, round_name, private):
    prs = []
    base = Path(runs_dir) / set_name / round_name
    for s in samples(plan, set_name):
        outputs = {}
        for arm in arms.ARMS:
            for rep_dir in sorted((base / arm).glob('rep*')):
                path = rep_dir / f"{s['slug']}.json"
                if not path.exists():
                    raise ValueError(f'MISSING_OUTPUT:{arm}:{rep_dir.name}:{s["slug"]}')
                outputs[(arm, int(rep_dir.name[3:]))] = read(path)['candidates']
        gold = read(sample_dir(private, s['slug']) / 'gold.json')['defects']
        prs.append(dict(slug=s['slug'], snapshot=load_eval(private, s), gold=gold, outputs=outputs))
    return prs


def cmd_judge(args):
    plan, plan_sha = planning.load_plan(args.plan, args.plan_sha256)
    base = Path(args.runs) / args.set / args.round
    jdir = base / 'judge'
    prs = outputs_for(plan, args.set, args.runs, args.round, args.private)
    packet, mapping = judging.build_packet(prs, plan['judge']['packet_seed'])
    if (jdir / 'packet.json').exists():
        if read(jdir / 'packet.json') != packet:
            raise SystemExit('BLOCKED: an existing packet differs; judge a new round directory')
    write(jdir / 'packet.json', packet)
    write(jdir / 'judge-only-mapping.json', mapping)
    decisions_path = jdir / 'decisions.json'
    doc = read(decisions_path) if decisions_path.exists() else judging.decisions_document(packet, {})
    if not args.confirm_real_provider:
        raise SystemExit('BLOCKED: --confirm-real-provider is required for billed judge calls')
    from mea.investigation import deepseek
    secret = deepseek.read_key()
    ledger = provider.Ledger(Path(args.private) / 'ledger.jsonl', int(plan['budget']['ledger_cap_usd'] * NANO))
    only = set(args.only.split(',')) if args.only else None
    workers = args.workers
    limit = plan.get('concurrency', {}).get('max_in_flight_per_arm', 1)
    if not 1 <= workers <= min(limit, MAX_WORKERS):
        raise SystemExit(f'BLOCKED: --workers must be between 1 and {min(limit, MAX_WORKERS)} (PLAN concurrency)')
    todo = []
    for pr in packet['prs']:
        if pr['slug'] in doc['decisions'] or (only and pr['slug'] not in only):
            continue
        if not pr['candidates']:
            doc['decisions'][pr['slug']] = {}
            continue
        todo.append(pr)

    def judge_one(pr):
        body = judging.judge_request(pr)
        reserved = provider.reservation(judging.JUDGE_MODEL, len(canonical(body)), judging.JUDGE_MAX_TOKENS)
        try:
            entry = ledger.admit(reserved, arm='judge', slug=pr['slug'], set=args.set, round=args.round)
        except ValueError as exc:
            # Request or ledger cap reached: nothing was sent. The loop keeps writing finished decisions.
            return pr, None, 'JUDGE_BLOCKED:' + str(exc), {}
        receipt = provider.judge_call(body, secret)
        price = provider.cost(judging.JUDGE_MODEL, receipt.get('usage'))
        ledger.record(entry, at=time.time(), arm='judge', slug=pr['slug'], model=judging.JUDGE_MODEL, usage=receipt.get('usage'),
                      scheduled_nano=price and price['scheduled'], peak_nano=price and price['peak'], reserved_nano=reserved,
                      http_status=receipt.get('http_status'), error=receipt.get('error'), set=args.set, round=args.round,
                      plan_sha256=plan_sha, workers=workers)
        write(jdir / 'raw' / f"{pr['slug']}.receipt.json", dict(request_body_sha256=digest(body), receipt=receipt, workers=workers))
        try:
            if receipt.get('error') or receipt.get('finish_reason') != 'stop':
                raise ValueError('JUDGE_RECEIPT:' + str(receipt.get('error') or receipt.get('finish_reason')))
            return pr, judging.parse_judgement(receipt.get('content'), pr), 'JUDGED', receipt
        except ValueError as exc:
            return pr, None, 'JUDGE_FAILED:' + str(exc), receipt

    from concurrent.futures import ThreadPoolExecutor
    blocked = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # Decisions are written by this thread only, after each judgement returns.
        for pr, parsed, status, receipt in pool.map(judge_one, todo):
            if parsed is not None:
                doc['decisions'][pr['slug']] = parsed
            if status.startswith('JUDGE_BLOCKED'):
                blocked.append(pr['slug'])
            write(decisions_path, doc)
            print(json.dumps(dict(slug=pr['slug'], candidates=len(pr['candidates']), status=status,
                                  seconds=receipt.get('elapsed_seconds'))), flush=True)
    write(decisions_path, doc)
    if blocked:
        raise SystemExit('BLOCKED: judge admission refused (decisions saved) for: ' + ','.join(blocked))


# ---- score ----------------------------------------------------------------------------------

def tables(plan, set_name, base, private):
    jdir = base / 'judge'
    packet, mapping, decisions = (read(jdir / n) for n in ('packet.json', 'judge-only-mapping.json', 'decisions.json'))
    # The judged packet must be the one the current outputs produce; a replaced output is judged first.
    rebuilt, _ = judging.build_packet(outputs_for(plan, set_name, base.parent.parent, base.name, private), plan['judge']['packet_seed'])
    if rebuilt != packet:
        raise SystemExit('BLOCKED: run outputs changed after judging; judge them before scoring')
    labels = judging.reveal(packet, mapping, decisions)
    out = {}
    for arm in arms.ARMS:
        for rep_dir in sorted((base / arm).glob('rep*')):
            rep = int(rep_dir.name[3:])
            table = {}
            for s in samples(plan, set_name):
                gold = [g['id'] for g in read(sample_dir(private, s['slug']) / 'gold.json')['defects']]
                table[s['slug']] = scoring.pr_counts(gold, labels.get((arm, rep), {}).get(s['slug'], []))
            out.setdefault(arm, []).append(table)
    return out, labels


def operations(plan, set_name, base):
    ops = {}
    for arm in arms.ARMS:
        rows = []
        for rep_dir in sorted((base / arm).glob('rep*')):
            for s in samples(plan, set_name):
                rows.append(read(rep_dir / f"{s['slug']}.json"))
        reasons = {}
        for r in rows:
            key = f"{r['status']}:{r['reason']}"
            reasons[key] = reasons.get(key, 0) + 1
        seconds = sorted(r['elapsed_seconds'] for r in rows)
        sched = sum(r['cost']['scheduled_nano'] or 0 for r in rows) / NANO
        peak = sum(r['cost']['peak_nano'] or 0 for r in rows) / NANO
        tools = {}
        for r in rows:
            for t in r.get('tools', []):
                tools[t] = tools.get(t, 0) + 1
        ops[arm] = dict(tasks=len(rows), outcomes=reasons, seconds_mean=sum(seconds) / len(seconds) if seconds else None,
                        seconds_p50=seconds[len(seconds) // 2] if seconds else None, seconds_max=seconds[-1] if seconds else None,
                        cost_usd_scheduled=sched, cost_usd_peak=peak,
                        cost_usd_per_pr_scheduled=sched / len(rows) if rows else None, tool_calls=tools,
                        candidates=sum(len(r['candidates']) for r in rows), rejected=sum(len(r['rejected']) for r in rows),
                        concurrency=sorted({r.get('concurrency', 1) for r in rows}),
                        seconds_note='per-task time measured from send to result; queueing excluded')
    return ops


def cmd_score(args):
    plan, plan_sha = planning.load_plan(args.plan, args.plan_sha256)
    base = Path(args.runs) / args.set / args.round
    table, labels = tables(plan, args.set, base, args.private)
    prs = [s['slug'] for s in samples(plan, args.set)]
    result = dict(schema='mea-m3-score-v1', plan_sha256=plan_sha, set=args.set, round=args.round, prs=len(prs),
                  gold_defects=sum(table['static'][0][p]['gold_tp'] for p in prs),
                  arms={arm: scoring.arm_summary(runs, prs) for arm, runs in table.items()},
                  operations=operations(plan, args.set, base))
    novel = {}
    for (arm, rep), per in labels.items():
        n = novel.setdefault(arm, dict(false_positive=0, novel_valid_true=0, novel_valid_false=0, novel_valid_null=0, undetermined=0))
        for rows in per.values():
            for d in rows:
                if d['verdict'] == 'FALSE_POSITIVE':
                    n['false_positive'] += 1
                    n['novel_valid_' + {True: 'true', False: 'false', None: 'null'}[d['novel_valid']]] += 1
                elif d['verdict'] == 'UNDETERMINED':
                    n['undetermined'] += 1
    result['false_positive_breakdown'] = novel
    if 'agent' in table and 'direct' in table:
        stats = plan['statistics']
        result['agent_minus_direct_f1'] = scoring.bootstrap_difference(
            table['agent'], table['direct'], prs, resamples=stats['bootstrap_resamples'], seed=stats['bootstrap_seed'])
        result['stop_rule'] = scoring.stop_decision(result['arms']['agent']['f1']['mean'], result['arms']['direct']['f1']['mean'])
    per_pr = {}
    for arm, runs in table.items():
        per_pr[arm] = {p: [scoring.metrics(t[p]) for t in runs] for p in prs}
    write(base / 'score.json', result)
    write(base / 'score-per-pr.json', per_pr)
    print(json.dumps({k: v for k, v in result.items() if k not in ('operations',)}, indent=1, default=str)[:6000])


# ---- agreement ------------------------------------------------------------------------------

def cmd_agreement_packet(args):
    plan, _ = planning.load_plan(args.plan, args.plan_sha256)
    jdir = Path(args.runs) / args.set / args.round / 'judge'
    packet, mapping, decisions = (read(jdir / n) for n in ('packet.json', 'judge-only-mapping.json', 'decisions.json'))
    review, key = judging.agreement_packet(packet, decisions, mapping, share=plan['agreement']['share'], seed=plan['agreement']['seed'])
    write(jdir / 'agreement' / 'review-packet.json', review)
    write(jdir / 'agreement' / 'judge-only-key.json', key)
    print(json.dumps(dict(population=review['population'], sampled=review['sampled'], review_sha256=digest(review))))


def cmd_agreement(args):
    jdir = Path(args.runs) / args.set / args.round / 'judge' / 'agreement'
    key = read(jdir / 'judge-only-key.json')
    reviewer = read(args.reviewer)
    result = judging.agreement(key, reviewer)
    write(jdir / 'agreement.json', result)
    print(json.dumps(result))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--private', type=Path, default=PRIVATE, help='private M3 directory (default: $MEA_M3_PRIVATE)')
    sub = parser.add_subparsers(dest='cmd', required=True)
    c = sub.add_parser('capture')
    c.add_argument('--refs', required=True)
    c.add_argument('--force', action='store_true')
    for name in ('run', 'judge', 'score', 'agreement-packet', 'agreement'):
        p = sub.add_parser(name)
        p.add_argument('--plan', required=name != 'agreement')
        p.add_argument('--plan-sha256')
        p.add_argument('--set', choices=planning.SETS, required=True)
        p.add_argument('--round', required=True)
        p.add_argument('--runs', type=Path, help='run directory (default: PRIVATE/runs)')
        if name in ('run', 'judge'):
            p.add_argument('--confirm-real-provider', action='store_true')
            p.add_argument('--only')
            p.add_argument('--workers', type=int, default=1)
        if name == 'run':
            p.add_argument('--arm', choices=arms.ARMS, required=True)
            p.add_argument('--rep', type=int, required=True)
            p.add_argument('--approval')
            p.add_argument('--reason')
            p.add_argument('--exclusions', type=Path, help='exclusion list bound by PLAN.json (default PRIVATE/selection/exclusions.json)')
        if name == 'agreement':
            p.add_argument('--reviewer', required=True)
    args = parser.parse_args(argv)
    # Only `agreement` with an explicit --runs works without the private directory.
    if args.private is None and not (args.cmd == 'agreement' and args.runs is not None):
        parser.error('--private (or MEA_M3_PRIVATE) is required for this command')
    if getattr(args, 'runs', 0) is None:
        args.runs = args.private / 'runs'
    return dict(capture=cmd_capture, run=cmd_run, judge=cmd_judge, score=cmd_score,
                **{'agreement-packet': cmd_agreement_packet, 'agreement': cmd_agreement})[args.cmd](args)


if __name__ == '__main__':
    sys.exit(main())
