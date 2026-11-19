# MEA Review operating guide

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
a network error, 401/403/429, 5xx, or a 200 response whose JSON or required
fields are unusable. Required provider fields are never filled with defaults:
an absent `files`, `tree` or search `items` list is such a TRANSPORT gap, while
an explicitly empty list means no results. A compare entry's `sha` key must be present but may
be null; the head file is then read at the pinned head and checked against its own Git blob.
A tree blob may omit its optional `size`; the byte bound is checked when its contents are read.
Empty or malformed SHA strings and response strings that are not valid Unicode are TRANSPORT
gaps. A pinned head is looked up across up to 250 PR commits (GitHub's list limit); a pin not
found within a full 250-commit listing is TRUNCATED. When an open pull is re-read at the end of
capture, its final state sets the snapshot provenance. A failed capture still writes
`capture.json` with its requests and `exit_status`, removes any earlier `snapshot.json` in the
output directory, and the command exits 3.

`snapshot.json` is a mea-snapshot-v1 input. `omitted_sources` lists only gaps
tied to a file path and side. `scope.capture` carries the typed capture
status: COMPLETE or INCOMPLETE, the context selection result, per-kind gap
counts and every gap, including those without a path such as a failed tree or
code search request. Context selection is COLLECTED, PARTIAL (some tree,
search or context read failed), FAILED (no usable tree),
SKIPPED_INCOMPLETE_CHANGE_LISTING or NOT_REQUESTED. Tool results, the first
model request, the static report and Review v3 repeat an incomplete capture
status. Snapshots without `scope.capture` remain valid. Real PR snapshots
belong in private storage, not in this repository.

To review only what changed since an earlier head of the same PR, add
`--since EARLIER_HEAD_SHA`. The base side is then that commit instead of the
merge base. The capture fails with `SINCE_NOT_ANCESTOR_OF_HEAD` after a force
push or rebase, because no increment exists.

## Send confirmed line comments (attended, allowlisted)

~~~sh
W="python3 -B -m mea.github_writeback --store PRIVATE_STORE --allow-repository owner/repo"
$W import-task --from-store RUN_STORE --task-id TASK
$W open --task-id TASK
$W draft --review REVIEW --task-id TASK --candidate CANDIDATE
$W --out PRIVATE_DIR/preview.json preview --draft DRAFT
$W confirm --draft DRAFT --preview PRIVATE_DIR/preview.json \
  --approval-ref "who approved, where" --binding BINDING_SHA256 --human-approved
$W send --draft DRAFT --confirm-live-write
~~~

This lane writes real pull request review comments, only to repositories named
with `--allow-repository`. The token comes from GH_TOKEN, GITHUB_TOKEN or
`gh auth token` at run time and only appears in the Authorization header.
Receipts keep the HTTP status, GitHub request id and a response digest.

`preview` reads the repository, the login, the pull, its changed-file patches
and the review-comment listing with GET requests. It prints the exact payload
(commit, path, line, side, body) and a binding digest of the repository, PR,
head and payload. `confirm` needs the recorded human approval of that binding.
It refuses a different binding, a changed preview or a moved head. Comments by
other accounts, bots included, on the same line are listed as "Already raised
by ACCOUNT". The confirmer must acknowledge each one by id with
`--acknowledge-other-comment`. `decline` records a human decision not to send.

`send` re-reads the remote state and makes one POST with no retry. A draft has
at most one send intent, and an anchor already commented on by the same login
is refused. The last read before the POST lists the other accounts' comments
on the anchor again. A change made after that read is outside the check.

- **BLOCKED** is a failed preflight before intent creation. A changed
  preview at this point also invalidates the confirmation.
- **NOT_SENT** is a recorded intent with a definitive no-send outcome.
  Examples include changed anchor comments detected during the final
  admission or a GitHub rejection with status 400, 401, 403, 404 or 422.
- **UNCERTAIN** is a recorded intent whose outcome could not be verified
  after a transport error, 5xx, 429 or unusable 201 response.
- **PUBLISHED** is a recorded intent with a matching GitHub receipt.

A NOT_SENT intent keeps its slot until
`abandon-not-sent --intent ID --human-approved` releases it; that command
refuses every other state. Release works at the draft's 16-entry history bound
by dropping the oldest history entry. The next corrective edit may do the
same once. `reconcile --intent ID` looks for exactly one
matching comment in a complete listing and makes no POST. Only after a
complete listing shows no match can `release-after-reconcile --human-approved`
free an UNCERTAIN slot. After either release the draft needs a new preview and
confirmation.

A base-side (LEFT) anchor is admitted only when GitHub's merge base, read in
the same admission, equals the frozen base revision. The base side of a
`--since` snapshot whose base is an earlier head therefore cannot be commented
on, even when the snapshot is opened as the first review version.

After a new commit, capture the increment with `--since`, run the investigation
on it, import the task and run `rereview --review REVIEW --task-id NEW_TASK`.
It maps each sent comment's line through the increment patch. A modified or
deleted anchor line is OUTDATED, and an unchanged one is CURRENT at its mapped
line. GitHub's own `line` for the comment is recorded next to it. Outdated
comments stay on their original commit and are not re-posted. A new candidate
inside the lines that replaced an outdated comment, or on a sent comment's
mapped line, cannot be drafted. Those replacement lines are mapped through each
later increment; when they cannot be mapped (incomplete listing, missing patch
or head source), candidates in that file cannot be drafted in that or any later
increment. If an incomplete listing does not name that file, a rename may have
been omitted. The marker then keeps the last known path with lineage UNKNOWN,
and no head-side candidate can be drafted or previewed. Start by opening the
initial version, then run re-review directly on the first increment.
Subsequent increments require the previous increment's re-review result.
A refused re-review leaves the existing links and confirmations in place.

When a later listing includes the last known path, the old name may now
belong to a different file. The current frozen inputs cannot establish
continuity across the missing increment, so UNKNOWN persists in later
versions of that review.

- **Pre-M5 review workspaces.** Every saved re-review without a `lineage_rule`
  is treated as `UNKNOWN` even if the workspace has never published a
  comment. On later versions, the guard rejects head-side `draft` and
  `preview` with `REREVIEW_AT_OUTDATED_COMMENT_LOCATION` even after a complete
  re-review. The candidate may still display `NEW`. Start a new review
  workspace from the current frozen snapshot.

Repeating the re-review preserves the original mapping and can refresh the
read-only remote observation.

The local Mock guard infers an increment from linked versions. A first version
has no predecessor for that check. Live admission verifies the merge base
for every LEFT anchor, including the first version. If a full snapshot's
merge base happens to equal the previous version's head, live admission can
still accept the LEFT anchor.

The workbench shows confirmation, writeback and re-review state for these
reviews. It has no live remote and cannot confirm or send a live draft.

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
(https://api-docs.deepseek.com/quick_start/pricing) and
refuses a request whose peak-rate reservation exceeds USD 10. It is an
estimate, not reconciled with the provider invoice. Both limits apply to one
`--store` only: a new store starts at zero, so they are not an account-level
or phase-wide cap. Store-level refusals (a changed scope for an existing store,
a second worker, the task cap) print `BLOCKED: <code>` and exit 2.

The `cross-file-tools-v1` profile adds `related_files` (import edges between
collected files, each citing its import line), JavaScript/TypeScript lexical
`find_symbol` and `references`, JS/TS test reads and 80-line source pages
bounded by the bytes the model sees. Its last request offers only
`finish_investigation`. Each response is parsed against the tools its own
request declared. A response that calls an undeclared tool executes nothing and
stops the task as TOOL_NOT_ALLOWED. Candidates still validate only against cited lines that
exist in the selected snapshot and were returned to the model. Tasks without
the profile keep the previous tool schema and pages.

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

The [ReviewBench v3 adjudication guide](../benchmarks/reviewbench/v3/README.md)
covers reading an existing blind packet, recording decisions locally and
reconciling the sealed decisions.
