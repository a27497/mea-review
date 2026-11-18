# ReviewBench v4: M3 three-arm comparison

Aggregate M3 holdout results: [RESULTS.md](RESULTS.md).

v4 compares three review arms on real public pull requests and records whether
an Agent with cross-file tools beats a single Direct LLM request. Static rules
give a floor. v1 to v3 and their frozen inputs stay unchanged.

The repository holds code, synthetic tests, the PLAN.json digest and aggregate
results. Real PR snapshots, maintainer comments, model responses, judge output
and the PLAN.json itself stay in private storage.

## Arms

All arms read the same evaluation snapshot. It is the captured snapshot
pinned at the commit the reviewer commented on, with the PR title and
description withheld (`arms.evaluation_snapshot`). Every runner checks the
snapshot digest against PLAN.json and refuses PR text.

| Arm | Input | Model | Output |
| --- | --- | --- | --- |
| Static | changed sides | none, `mea/review.py` rules | findings |
| Direct | diff, full head source of changed files, deleted base lines | `deepseek-flash`, one request, thinking off, temperature 0, no source tools | `finish_investigation` candidates |
| Agent | diff (30,000-byte first turn), M2 tool loop, up to 8 context files | `deepseek-flash`, product task budget | `finish_investigation` candidates |

Direct offers only `finish_investigation` with a named tool choice. Its
candidates pass the same `validate_candidates` rule as the Agent: a changed-line
anchor and evidence IDs that were shown to the model. Every candidate is reduced
to path, side, line, title, trigger and impact for judging.

## Judging and scoring

The truth unit is a deduplicated defect. Review comments the curator assigned
to one defect become one gold entry (`score.dedupe_gold`).

`judge.build_packet` pools every candidate of every arm and repetition of one
PR, merges exact duplicates, gives each a seeded opaque ID and shuffles them.
The judge sees the diff, the gold defects and candidate text only. The arm
mapping is a separate judge-only file. The judge returns MATCH (with a gold
ID), FALSE_POSITIVE or UNDETERMINED for every candidate. `judge.reveal`
refuses unless packet, mapping and decisions bind by SHA-256.

`score.pr_counts` reuses `benchmarks/reviewbench/scoring.py`. A gold defect
can be claimed once. Unmatched, FALSE_POSITIVE and UNDETERMINED candidates
count against precision. Each repetition gets micro P, R and F1 over all PRs;
an arm reports the mean and range over repetitions. `score.bootstrap_difference`
resamples PRs with replacement for the CI of mean Agent F1 minus mean Direct F1.
If that mean gap is below 10 points the result is negative and M3 stops.

`judge.agreement_packet` draws a seeded sample of at least 20% of the judged
candidates for an independent reviewer, without arm or judge verdict.
`judge.agreement` reports percent agreement and Cohen's kappa.

## Run

~~~sh
python3 -B benchmarks/reviewbench/v4/m3.py capture --refs PRIVATE/selection/refs.txt
python3 -B benchmarks/reviewbench/v4/m3.py run --plan PLAN.json --set dev --round r1 --arm static --rep 1
python3 -B benchmarks/reviewbench/v4/m3.py run --plan PLAN.json --set dev --round r1 --arm direct --rep 1 --confirm-real-provider
python3 -B benchmarks/reviewbench/v4/m3.py run --plan PLAN.json --set dev --round r1 --arm agent --rep 1 --confirm-real-provider
python3 -B benchmarks/reviewbench/v4/m3.py judge --plan PLAN.json --set dev --round r1 --confirm-real-provider
python3 -B benchmarks/reviewbench/v4/m3.py score --plan PLAN.json --set dev --round r1
~~~

Billed commands need `--confirm-real-provider` and make one POST per request
with no retry. `--workers N` (at most the PLAN's `max_in_flight_per_arm`, and
at most 4) runs up to N tasks of one arm at a time; arms run one after another.
Direct and judge use threads. Agent tasks are split into batches of at most 8,
each with its own scope and SQLite store, run in separate processes; tasks in
one store stay sequential. The cost ledger takes an exclusive file lock to
reserve each request's peak bound before it is sent, so concurrent workers
cannot pass its cap together. Recorded task times start when the worker
sends, so they exclude queueing; each result records the concurrency. A holdout run also needs a FROZEN PLAN.json, a coordinator
approval naming its SHA-256, the frozen code SHA and a clean `mea/` and
`benchmarks/` tree. Before a holdout run, the pre-registered sample rules
(`plan.check_samples`) are applied to PLAN.json with the exclusion list whose
SHA-256 PLAN.json records. A completed arm and repetition cannot run again. An
infrastructure rerun names `--only SLUGS` and `--reason`, which is logged. It
only replaces a missing output or a transport or provider stop; the earlier
output and receipt move to `superseded/<rerun>/`. An Agent rerun gets its own
scope id and store.
The cost ledger prices reported usage at the official list price, keeps a
peak-rate bound and refuses a request above USD 10 or above the PLAN cap.
