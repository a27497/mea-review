# MEA Review

MEA Review opens frozen pull request snapshots on a local workbench. Static
rules identify a bounded set of changes for human review. Each citation shows
the original side, revision and line. The deterministic Mock investigation
follows changed literal Python None returns into collected callers and tests.

## Start the workbench

Use Python 3.12 or later on a Unix-like system. The server uses the Python
standard library, SQLite and local browser assets.

~~~sh
python3 -B -m mea serve
~~~

Open the printed loopback URL to enter Review v3. Choose **Demo guide** and
try the included scenarios:

1. **Completed** runs the self-owned v4 investigation. Open **Review** to
   inspect checked scope, follow a source citation and export the task.
2. **Human review** opens the self-owned Mock workspace. Select the candidate,
   inspect the changed line and caller, edit and save the draft, then preview
   its exact text, revision and anchor. Acknowledge the preview, confirm, and
   separately select **Send to Mock**. If the outcome is uncertain, use
   **Reconcile read-only** to check for a recorded match.
3. **Re-review** runs a manual comparison of two frozen versions. Start with
   v1 → v2 to see the renamed file and moved line. The v1 comment stays on its
   original commit. Later pairs illustrate narrow resolution, a new candidate,
   an outdated location and a case requiring recheck.

On a phone use **Files**, **Diff** or **Compare**, and **Review**. A citation
opens its frozen line and provides a **Back to review** action.

## Import your own frozen snapshot

Choose **Import & task controls** from the workspace menu or open /classic.
The importer accepts UTF-8 JSON in the included mea-snapshot-v1 shape. It
inspects source as data and marks patch-only evidence. Use a built-in snapshot
or import one, then open findings or export a static report. You can also
create a deterministic offline task from the open snapshot. Completed
investigations can be reopened in Review v3. Mock comment sending is limited
to the registered synthetic fixture. Other snapshots support local draft and
preview only.

Tasks and review state persist by default under artifacts/investigation-tasks.
Use --task-dir followed by a directory path to start an isolated session.
To reopen a task, restart with the same directory and select it in **Saved
workspaces** or in the classic task controls. Pending deterministic work can
resume within its original deadline and budgets. A saved uncertain Mock send
can be checked by read-only reconciliation.

## Rehearse the controlled DeepSeek protocol

Normal startup leaves provider execution disabled. The local rehearsal uses
the DeepSeek Flash native tool-call format with a simulated transport.

Start the workbench and the Mock worker in separate terminals with the same
local database directory:

~~~sh
python3 -B -m mea serve --task-dir artifacts/provider-rehearsal
python3 -B -m mea.investigation.provider_worker --mock --store artifacts/provider-rehearsal
~~~

In **Import & task controls**, choose an owned adapter case and review its
snapshot. Expand **DeepSeek protocol rehearsal**, acknowledge the simulation
and select **Create Mock provider task**. The two snapshots illustrate a
conditional finding and a guarded counterexample. The task JSON contains
the request hashes, observed tool results and simulated reservation states.

The Mock scope binds the two frozen input digests, policy, model identifier,
prompt and tool schema. It allows at most two distinct tasks and ten
request intents per database. The worker reserves request, token and simulated
cost capacity in one SQLite transaction before launch. A second worker cannot
use the same store. Reopening a task does not create a new request.

A prepared request interrupted before launch is recorded as NOT_SENT. An
interrupted SENDING request is UNCERTAIN. Both retain their reservation.
After restart the worker may process a saved response or deterministic
feedback. It cannot reactivate a previous task or resend an uncertain request.

All costs in this rehearsal are simulated. The workbench HTTP server never
admits a real provider task. Real runs use the separate command-line lane
below.

## Capture a GitHub pull request (read-only)

~~~sh
python3 -B -m mea.github_input owner/repo#123 --out-dir PRIVATE_DIR
python3 -B -m mea.github_input owner/repo#123@HEAD_SHA --out-dir PRIVATE_DIR
~~~

The reader makes GET requests only, with the token from GH_TOKEN, GITHUB_TOKEN
or `gh auth token`. It pins the head SHA (the current head or a named commit of
the PR) and takes the merge base from the compare API. Each changed file gets
its full text at both sides, checked against the Git blob SHA. Up to eight
unchanged context files are added with status `context`. They are chosen from
test file names, GitHub code search for changed definitions and imports of the
changed files, and are always read at the pinned head.

`capture.json` lists every request and keeps three gap kinds apart. MISSING
means the evidence is absent or unusable, for example 404, binary or not UTF-8.
TRUNCATED means a bound cut existing evidence, such as an omitted provider
patch or an oversized source. TRANSPORT means no definitive answer, for example
a network error, 401/403/429 or 5xx. `snapshot.json` is a mea-snapshot-v1 input
and lists each gap in `omitted_sources`. Real PR snapshots belong in private
storage, not in this repository.

## Run the controlled real DeepSeek lane

~~~sh
python3 -B -m mea.investigation.live_run --scope SCOPE.json \
  --snapshot PRIVATE_DIR/snapshot.json --store PRIVATE_STORE \
  --out PRIVATE_DIR/run.json --confirm-real-provider
~~~

This is a billed call to the DeepSeek API at api.deepseek.com, model
`deepseek-flash`, with thinking disabled. The run reuses the native tool-call
protocol, Coordinator and Broker from the Mock rehearsal. The request intent,
SENDING marker, reservation and uncertainty rules stay the same, and each
request makes one POST with no retry. The key comes from
~/.config/mea/deepseek.env (DEEPSEEK_API_KEY, owner-only file). Receipts
redact it.

The scope file names the grant id, the exact snapshot input digests, task and
request caps, task budget, tool profile, first-turn profile, approval text and
a USD budget. The live ledger prices reported usage at the official list price
(https://api-docs.deepseek.com/quick_start/pricing, read 2026-11-17) and
refuses a request whose peak-rate reservation exceeds USD 10. It is an
estimate, not reconciled with the provider invoice.

The `cross-file-tools-v1` profile adds `related_files` (import edges between
collected files, each citing its import line), JavaScript/TypeScript lexical
`find_symbol` and `references`, JS/TS test reads and 80-line source pages
bounded by the bytes the model sees. Its last request offers only
`finish_investigation`. Candidates still validate only against cited lines that
exist in the selected snapshot and were returned to the model. Tasks without
the profile keep the previous tool schema and pages.

## Verify

~~~sh
npm test
npm ci --ignore-scripts
MEA_BROWSER=/path/to/chromium npm run test:e2e
MEA_BROWSER=/path/to/chromium npm run test:review-v3
~~~

A local Chromium installation or a Playwright-provisioned Chromium is required
for browser verification. The browser test starts its own server on an
available loopback port, uses a fresh task database and writes test evidence
under artifacts. MEA_E2E_OUT and MEA_V3_E2E_OUT select output directories
for the classic and Review v3 browser checks.

## Replay ReviewBench offline

Run the packaged two-sample comparison and reproduce C1 Main telemetry with
Python 3.12+ and Node 24. Use a fresh evidence directory.

~~~sh
python3 -I -B benchmarks/reviewbench/verify.py --out artifacts/reviewbench-offline-1
~~~

The benchmark runs Static, Direct Mock and Agent Mock. The historical C1
Main recomputation preserves PARTIAL and independent quality BLOCKED.
[Replay guide and method](benchmarks/reviewbench/README.md)

## Optional request profiles

The local Mock investigation API supports a calibrated token estimate and a
bounded first-turn view of the pull request title and description. Developers
can opt in when calling `mea.investigation.real.create` with
`estimator=context_budget.CALIBRATED` and `first_turn=real_protocol.FIRST_TURN`.
The estimator reserves simulated tokens before a request. The description remains
untrusted input. The default request and the existing offline ReviewBench replay
retain their original behavior.

## Offline candidate verification

`mea.investigation.verification` and `verification_v2` prepare a bounded
request from a frozen snapshot and validate a supplied response. Version 2
adds companion patches and Python definitions. The verifier checks cited rows
against the frozen source. Its output keeps correctness unadjudicated and
preserves refuted or insufficient candidates for inspection.

The tests use synthetic snapshots and supplied responses. These modules do
not launch a verification worker from the workbench.

The [ReviewBench v3 adjudication guide](benchmarks/reviewbench/v3/README.md)
covers reading an existing blind packet, recording decisions locally and
reconciling the sealed decisions.

## Scope and provenance

The tool and report engines come from MEA product checkpoint
344acf0c8bd59157e81d03aa771547e2b330986e. Review v3 and the Mock review
service come from checkpoint 5e8d91e5fd7678f93cda4f44632157395bfa2bdf. The native protocol, frozen paging
and Mock rehearsal adapt checkpoint 3b9c08d81735d8b55a48bc6f6a47d0f7d74ce3b4.
All examples are self-owned synthetic fixtures. See the
[source manifest](SOURCE-MANIFEST.tsv) and [third-party notices](THIRD_PARTY_NOTICES.md).

Static findings, Mock candidates and real-model candidates require human
judgment. Source hashes bind evidence to the supplied snapshot. Read-only
GitHub capture and the command-line real DeepSeek lane are included. Source
authenticity, target execution, real AI in the workbench and GitHub writeback
are outside this candidate.
The project is licensed under the [MIT License](LICENSE). Publication approval is pending.
