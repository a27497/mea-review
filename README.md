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

Open the printed loopback URL. Choose the synthetic constant assertion example
or upload its file from [fixtures/demo](fixtures/demo). Select a finding, open
the citation at the frozen line, and export the report as JSON or Markdown.
The importer accepts UTF-8 JSON in the included mea-snapshot-v1 shape. Source
text is inspected as data. Patch-only evidence is labeled and linked to the
patch rather than a full source file.

Open the none-return example and choose **Create offline task from open
frozen PR**. The completed task shows observed tool results and a conditional
candidate. The counterexample contains a caller guard and refutes that
hypothesis. Task exports contain the original frozen view and observed
evidence.

Tasks persist by default under artifacts/investigation-tasks. To use a
different directory, start the server with --task-dir followed by its path.
After a restart, start the server with the same directory, then select the
task under **Saved tasks** and choose **Reopen task**. Pending deterministic
work can resume within its original deadline and budgets. Reopening a
completed task reads its saved result.

## Verify

~~~sh
npm test
npm ci --ignore-scripts
MEA_BROWSER=/path/to/chromium npm run test:e2e
~~~

A local Chromium installation or a Playwright-provisioned Chromium is required
for browser verification. The browser test starts its own server on an
available loopback port, uses a fresh task database and writes test evidence
under artifacts. MEA_E2E_OUT can select a fresh output directory.

## Scope and provenance

The two investigation fixtures are self-owned synthetic examples. The constant
assertion fixture is a new synthetic example. The tool and report engines are
extracted from the local MEA product checkpoint
344acf0c8bd59157e81d03aa771547e2b330986e. The
[source manifest](SOURCE-MANIFEST.tsv) lists each included file.

Static findings and Mock candidates require human judgment. Source hashes
bind evidence to the supplied snapshot. Source authenticity, target execution,
live capture, real AI and GitHub writeback are outside this local candidate.
Project licensing and publication approval are pending.
