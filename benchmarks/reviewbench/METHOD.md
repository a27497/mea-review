# Offline comparison and scoring method

## Evidence scope

The freeze binds the repository, PR number, base and head, input and view
hashes, changed paths, diff visibility and source notice bytes. During
execution, the frozen snapshot is the sole data input to an arm. Native
tools address literal lines on the frozen side. Uncollected paths and runtime
behavior remain unknown. The two included samples test visible and omitted
patch paths. They provide no population or accuracy estimate.

The Direct and Agent arms share revisions, objective, files, initial diffs
and coverage at turn one. Their prompts and tool access differ. Direct has
no source tools. Agent tools may add evidence, with each read stored separately
from the initial projection. Static has access to the supplied changed sides.
The format gate checks PR identity, HEAD-anchored exports and reader acceptance.
It does not run the upstream matcher, classifier or judge.

## Scoring arithmetic

Independent adjudication supplies a set of correspondences for every
candidate and TP or FP decisions for eligible unmatched candidates. Empty
correspondences are explicit. Stable source order resolves duplicate claims.
A matched gold entry can only be claimed once. Coverage counts each eligible
gold TP at most once.

| Metric | Calculation |
| --- | --- |
| Grounded precision | Matched TPs divided by matched candidates |
| Grounded recall | Covered eligible TPs divided by eligible gold TPs |
| Augmented precision | Matched and independently confirmed novel TPs divided by candidates in scope |
| Augmented recall | Covered eligible and novel TPs divided by eligible gold and novel TPs |
| F1 | Harmonic mean of precision and recall |

Undefined denominators remain null. Pending adjudications block augmented
metrics. Pooled counts yield micro results. Macro results average defined
per-PR values and retain the defined denominator. The included arithmetic
example uses invented labels and candidate correspondences.

## Historical C1

C1 compared four system configurations. Different prompts, native tool access
and request limits prevent attributing differences to tool use alone.
Main retains all 96 terminal cells, including empty, abstained and stopped
tasks. The historical telemetry uses nearest-rank p50 and p95 over non-null
task times. The candidate exports use validated HEAD anchors. Candidate
rejections, BASE-only results and stopped executions remain separate.

No independent semantic quality result is available for C1. The candidate
keeps PARTIAL and BLOCKED and checks that every quality metric stays null.
The portable reduction contains no matcher or judge dispatch.

The [pinned upstream methodology](https://github.com/review-bench/ReviewBench/blob/f223ce33558a6d211fd222b1f7ff2fd7b44403d4/docs/METHODOLOGY.md)
describes how upstream labels were derived and audited. Its label authorship
and matching scope differ from the synthetic arithmetic here.
