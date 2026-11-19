# ReviewBench auxiliary novel-defect review

After the holdout scores froze, two Codex reviewers independently assessed a shuffled candidate packet with the arm labels hidden. Each reviewer received a distinct half. Entries came from candidates that the benchmark judge marked false positive against the gold list but plausible as issues outside it. The Agent sample included all 20 eligible candidates. The Direct sample included 20 of its 72 eligible candidates, drawn with fixed seed `mea-m3-closeout-novel-review-20261118`. [Frozen benchmark and sampling protocol](../benchmarks/reviewbench/v4/RESULTS.md#novel-defect-review-packet)

The frozen results record `NOT_RUN` as of packet preparation. The reviewers completed this auxiliary assessment after that record was written.

| Arm | Reviewed | Confirmed issues | Rejected | Unable to decide |
| --- | ---: | ---: | ---: | ---: |
| Agent | 20 | 18 | 2 | 0 |
| Direct | 20 | 15 | 5 | 0 |

The small, selected sample provides an auxiliary estimate for this subset of candidates. The audit did not test statistical significance or re-score the holdout. The frozen results and the stopping decision stand. The private review packet and judgments remain with the benchmark records.
