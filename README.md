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

## Scope and provenance

The tool and report engines come from MEA product checkpoint
344acf0c8bd59157e81d03aa771547e2b330986e. Review v3 and the Mock review
service come from checkpoint 5e8d91e5fd7678f93cda4f44632157395bfa2bdf.
All examples are self-owned synthetic fixtures. See the
[source manifest](SOURCE-MANIFEST.tsv) and [third-party notices](THIRD_PARTY_NOTICES.md).

Static findings and Mock candidates require human judgment. Source hashes
bind evidence to the supplied snapshot. Source authenticity, target execution,
live capture, real AI and GitHub writeback are outside this local candidate.
Project licensing and publication approval are pending.
