# ReviewBench offline replay

Run from the MEA Review root with Python 3.12+ and Node 24 on PATH.

~~~sh
python3 -I -B benchmarks/reviewbench/verify.py --out artifacts/reviewbench-offline-1
~~~

Choose a fresh output directory for each replay. The command uses the checked-in
samples and the pinned ReviewBench input reader. It needs no Git cache, npm
installation, provider or judge. It writes six arm records and six format
exports, evidence-visibility checks, synthetic scoring arithmetic, negative
checks and a C1 Main recomputation.

## Packaged sample replay

The subset is drawn from the frozen ReviewBench v2 source cohort at MEA
checkpoint e41bf60796c4ddae54566388636dd943ac623465. The two examples illustrate
a complete initial diff and an explicit omission. They are exposed
reproduction samples and are outside the upstream test split.

| Sample | Replay behavior | Source rights |
| --- | --- | --- |
| resplab/leap #48 | All seven changed paths appear in the initial diff | [Pinned MIT notice](samples/licenses/leap-MIT.txt) |
| torchfont/torchfont #133 | The initial diff omits tests/test_dataset.py. Agent Mock observes lines through a frozen read. | [Pinned MIT notice](samples/licenses/torchfont-MIT.txt) |

[Sample manifest](samples/manifest.json) binds each snapshot and view, both
revisions and licenses, the exact diff projection, and the upstream parser.
The bundled code and patches are data. The arm subprocess takes one
hash-verified snapshot and denies socket, subprocess and out-of-scope reads.
The benchmark imports neither the product broker nor a real transport.

Static runs the local rules on all collected changed sides. Direct Mock and
Agent Mock share the same five initial evidence fields and path omissions.
Direct Mock checks exact changed-line quotes within visible patches. Agent
Mock validates native calls, reads frozen source with the product tools and
ties each tool response to its call ID. Candidate correctness stays
unadjudicated. These runs exercise the contracts, including format validation
by the pinned upstream reader. The synthetic scoring example tests arithmetic
with fabricated adjudications separately.

## Historical C1 Main recomputation

C1 remains **PARTIAL** and independent quality scoring remains **BLOCKED**.
The packaged [numeric projection](c1/main-cells.json) carries 8 cases, 3
rounds and 4 arms, with anonymous case IDs. It excludes raw source journals,
model requests and responses, adjudications and ledgers. The verifier
recomputes the Main totals from the 96 arm cells and compares them to the
[archived summary projection](c1/expected-summary.json).

| Historical Main arm | Completed | Stopped | Validated candidates |
| --- | ---: | ---: | ---: |
| Static | 24/24 | 0 | 0 |
| Direct | 24/24 | 0 | 0 |
| Original Agent | 0/24 | 24 | 0 |
| Optimized Agent | 12/24 | 12 | 0 |

The recomputation includes completion and failure rates, nearest-rank task
latency, reported tokens, known HTTP posts and candidate counts. It is a
reduction of the archived Main records. Historical Pilot outcomes, raw
receipts, the separate coverage index and ledger reconciliation stay on the
server. Six Main PRs had shared eligible positive anchors against the
preregistered minimum of ten. This coverage count comes from the archived
index. Three rounds over the same PRs do not create 24 independent PRs.

Independent matcher or Judge adjudication is required for semantic quality.
C1 precision, recall, F1, confirmed FP and matched TP remain null. Completed
tasks include abstentions and empty findings, so completion counts do not
measure detection quality. See the [method](METHOD.md) for denominators.
