# Phase 10 results

**Technical execution completed. Formal scoring is PROVISIONAL.** The frozen holdout produced 24 complete three-arm pairs and 72 terminal receipts. No cell failed, reached a limit, or remained unrun. [Freeze PR](https://github.com/a27497/mea-review/pull/16) · [Exact frozen head](https://github.com/a27497/mea-review/tree/33c07d7292f5ed7bc28d3ea6ede29780cc917d31)

| Arm | Completed | Raw candidates | Valid anchors | Rejected candidates | Provider requests | Input / output tokens | p50 / p95 seconds | Estimated USD |
| --- | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: |
| Agent | 24/24 | 58 | 47 | 11 | 48 | 151,084 / 14,220 | 3.564 / 5.743 | 0.053357520 |
| Direct | 24/24 | 69 | 69 | 0 | 24 | 42,960 / 9,232 | 1.968 / 4.509 | 0.023966400 |
| Static | 24/24 | 0 | 0 | 0 | 0 | 0 / 0 | 0.022 / 0.096 | 0 |

The Dev rerun completed all 12 cells across four triples after repairing terminal response handling, tool feedback bounds, and Direct JSON parsing. Historical pilot receipts and the repaired Dev rerun remain separate.

The holdout contains 32 seeded defects, including eight tagged cross-file. Four unchanged controls completed in each arm. Agent produced four raw candidates on one control, Direct seven on two controls, and Static zero. Their semantic validity remains unresolved. Per-repository and complete-cohort counts are in the machine-readable aggregate. [Aggregate data](RESULTS.json) · [Frozen protocol](PROTOCOL.md)

Under the frozen scoring policy, the provisional strict accounting treats unresolved candidates as unmatched. Agent records 0 TP, 47 unmatched and 32 FN. Direct records 0 TP, 69 unmatched and 32 FN. Static records 0 TP, 0 unmatched and 32 FN, with precision undefined. All three provisional F1 values are zero. The paired bootstrap replays those provisional labels. Independent human review coverage is 0/2, and 116 blind items remain unresolved. Comparison of defect detection quality requires those judgments.

The provider replied to all 72 holdout requests, with no retries or unsettled reservations. The holdout estimate is USD 0.077323920 and the cumulative Phase 10 estimate is USD 0.110683488 against the USD 2 cap. These are usage-based peak-rate estimates. The invoice remains unreconciled. [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing/)

The audit rebuilt 72 wire inputs and 48 tool results and detected zero gold leaks. Two independent offline scoring invocations produced byte-identical output, SHA-256 `8f734fce84ec4fdabea9dafe194969de0a1342a49b3478d8c4997a7cc2a5c82e`. Frozen manifest SHA-256 is `c7caa3c61a60acea9c657f20e5fd6eeeeb052347333f5dedd6fa73dcb40bd509`. [Frozen CI](https://github.com/a27497/mea-review/actions/runs/45135955573)

The experiment covers four repositories and one run per cell. Agent used bounded cross-file pages and local citation validation. Direct saw only the common diff and anchors. Eleven source sides exceeded the full-blob limit and retained their complete changed hunks. These constraints apply to interpretation of this cohort.
