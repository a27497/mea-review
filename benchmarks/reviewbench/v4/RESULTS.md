# ReviewBench v4 (M3) aggregate results

This file holds only aggregates. Snapshots, gold comments, model responses,
judge output, PLAN.json and the per-PR rows live in private storage.

- PLAN.json SHA-256: `4a59740259b15888fce68685307a100cd4c40103ae996b3d038ae63a9b86a3ba`
  (frozen code `31f6ebda981475373c592aecdb1baa702ff6a45d`)
- PLAN-ADDENDUM.json SHA-256: `8a15cb7704635ef0fbe54aab3c88af986b9fc48bb7820bcc77b485bbccf75bef`
  (two sensitivity analyses, declared before the first holdout run, report only)
- Holdout draft SHA-256 (holdout list fixed before any dev run):
  `6cd474efb1d6efe63e512d97f60af65f0f7b2c6dc5dcbce4facfeb9857b12182`

## Holdout: 35 PRs, 42 deduplicated gold defects, 3 repetitions

Capability: Static is Offline; Direct and Agent are Real model on Real repo
snapshots; the judge is a Real model (`deepseek-v4-pro`, blind). The Codex
agreement check is NOT_RUN.

| Arm | Precision mean (runs) | Recall mean (runs) | F1 mean (min to max) | Candidates per run |
| --- | --- | --- | --- | --- |
| Static | undefined (no candidates) | 0.000 | 0.000 | 0, 0, 0 |
| Direct | 0.137 (0.140, 0.136, 0.133) | 0.151 (0.167, 0.143, 0.143) | 0.143 (0.138 to 0.152) | 50, 44, 45 |
| Agent | 0.399 (0.417, 0.467, 0.313) | 0.135 (0.119, 0.167, 0.119) | 0.201 (0.172 to 0.246) | 12, 15, 16 |

Agent F1 minus Direct F1 is +0.058. The 95% bootstrap CI is −0.024 to +0.128
(10,000 PR resamples, seed 20261117), and 11.5% of resamples reach +0.10.
The gap is below 10 points, so the pre-registered rule applies:
**NEGATIVE_RESULT_STOP**.

Sensitivity analyses (from the addendum; not used for the stop rule):

- (a) 29 PRs whose capture was COMPLETE, with 35 gold defects: Direct F1
  0.138, Agent F1 0.199. The gap is +0.061, CI −0.034 to +0.143.
- (b) No gold defect is unreachable, so the reachable-gold metrics equal
  the primary metrics.

## Operations (per task, 105 tasks per arm)

| Arm | Outcomes | Mean / p50 / max seconds | Cost USD (official price, peak bound) |
| --- | --- | --- | --- |
| Static | 105 completed | 0.02 / 0.02 / 0.08 | 0 |
| Direct | 52 supported, 35 zero, 1 insufficient; 10 INVALID_MODEL_DECISION, 7 PROVIDER_OUTPUT_INCOMPLETE | 4.7 / 4.5 / 11.0 | 0.269 (0.539) |
| Agent | 18 supported, 6 partly rejected, 36 zero, 34 insufficient; 6 INVALID_MODEL_DECISION, 3 OUTPUT_BUDGET, 2 TOOL_NOT_ALLOWED | 9.6 / 9.3 / 15.8 | 0.751 (1.503) |

Task times run from send to result. They exclude queueing at concurrency 4.
The Agent made 448 requests and 662 tool calls: 460 `read_source`,
123 `search`, 33 `references`, 31 `read_test`, 13 `read_diff` and
2 `related_files`.

## Attribution

- Neither arm found 33 of the 42 gold defects in any repetition. Both found 5.
  Two were found only by the Agent and two only by Direct.
- The Agent's F1 lead comes from precision. It proposed 43 candidates in
  total against Direct's 139, with similar recall (0.135 against 0.151).
  Direct reports more false positives. The judge marked 72 of Direct's 119
  and 20 of the Agent's 26 false positives as real defects missing from the
  gold list. These are reported only and are not scored.
- Cross-file tools barely ran. 69% of tool calls paged through source.
  `related_files` was used twice in 105 tasks.
- 67% of Agent tasks ended with no candidate (zero or insufficient
  evidence) after spending their tool budget on reads.
- By language: Python (15 PRs) Direct 0.169 and Agent 0.239. JS/TS
  (20 PRs) Direct 0.126 and Agent 0.164.
