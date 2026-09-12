# Graph build reuse benchmark

This directory records twelve complete `Test` workflows: three cold/warm pairs for a cache-disabled baseline and three for the graph-build reuse candidate. Only the runs below enter the medians, ranges, and deltas. Diagnostic, failed, and superseded runs are excluded.

## End-to-end results

| Condition | Metric | Baseline median and range | Candidate median and range | Candidate difference |
| --- | --- | --- | --- | --- |
| Cold | Complete workflow | 1,882 s (31.4 min); 1,738–1,885 s | 2,003 s (33.4 min); 1,937–2,007 s | 121 s slower (6.4%) |
| Warm | Complete workflow | 1,585 s (26.4 min); 1,388–1,808 s | 1,511 s (25.2 min); 1,241–1,518 s | 74 s faster (4.7%) |
| Cold | Aggregate jobs | 25,865 s (431.1 min); 25,289–26,355 s | 26,544 s (442.4 min); 26,431–26,611 s | 679 s slower (2.6%) |
| Warm | Aggregate jobs | 26,036 s (433.9 min); 24,849–26,467 s | 23,770 s (396.2 min); 23,725–24,075 s | 2,266 s faster (8.7%) |

Workflow wall runs from GitHub's `run_started_at` timestamp through the final job completion. It is a CI waiting-time proxy, not exact push-to-notification time. Aggregate job time is the sum of execution time across non-skipped jobs and represents CI consumption rather than user waiting.

On the cold path, the candidate still built the executable and also prepared and stored the payload; its observed workflow median was 121 seconds slower. On warm runs, all five affected jobs accepted exact Actions cache hits and skipped `cargo build --bin hash-graph --all-features`. Every selected integration retained the same behavior counts: 86 benchmark cases, 49 setup and 181 test HTTP requests, 130 graph integration tests, 110 passed/9 skipped plus 48 passed backend tests, and 32 passed/8 skipped Playwright tests.

## Candidate cache evidence

Each cold run started with an empty graph-cache namespace: five misses produced five valid payloads, three saves, two shared-key recheck hits, and three stored entries. Its paired warm run saw the same three entries before and after, accepted five exact hits, and issued no build, recheck, or save. Compressed storage was 974,778,783 bytes median (974,771,666–974,780,568), about 929.6 MiB. The implementation adds no custom TTL; normal GitHub Actions retention and eviction apply.

The phase totals below are per affected job across 15 job-samples per condition. `build` is Cargo workload, while the other rows expose helper and transfer cost. The native CPU probe itself stayed subsecond in the median; repeated source/resolution fingerprinting is included in `prepare` and is already reflected in the end-to-end result.

| Condition | Phase | Median total seconds | Range | Median events (range) |
| --- | --- | ---: | ---: | ---: |
| Cold | `cpu-probe` | 0.600 | 0.357–1.158 | 6 (5–6) |
| Cold | `prepare` | 142.537 | 78.340–287.313 | 5 (4–5) |
| Cold | `restore` | 13.737 | 3.490–60.647 | 4 (3–4) |
| Cold | `build` | 434.764 | 346.529–474.753 | 1 (1–1) |
| Cold | `store` | 35.224 | 30.003–53.294 | 1 (1–1) |
| Cold | `lock` | 25.801 | 18.568–149.910 | 4 (3–4) |
| Warm | `cpu-probe` | 0.460 | 0.323–1.475 | 5 (4–5) |
| Warm | `prepare` | 146.038 | 114.142–296.944 | 5 (4–5) |
| Warm | `restore` | 7.820 | 5.822–27.672 | 4 (3–4) |
| Warm | `build` | 0.000 | 0.000–0.000 | 0 (0–0) |
| Warm | `store` | 0.000 | 0.000–0.000 | 0 (0–0) |
| Warm | `lock` | 25.832 | 19.403–148.594 | 4 (3–4) |

## Measurement boundaries

- Both variants disabled remote Turbo caching and sccache and used the same Quay-hosted MinIO client image at the source image's pinned digest because the source registry was unavailable.
- Baseline "warm" means a same-tree second run with graph caching still disabled. Candidate warm means five helper-accepted Actions hits.
- Baseline source `6c9b16b02a5393ae982cec33784e151c3aeed519` and measured candidate `5e087b2ad15d8fe887699f5e2540220e0279d341` are different commits. Their seven changed paths are confined to the cache helper, its tests/documentation, the Test workflow, and evidence-analyzer files; application and dependency files do not differ. Candidate workflows contain one extra focused cache-helper check job: 78 jobs (77 successful, one skipped) versus 77 (76 successful, one skipped).
- The final proposed commit `3278c458d1a8ec4492f1e3549ce244d376f778dd` was not benchmarked directly. Its five implementation files, including modes, are identical to the measured candidate. A supplemental name-level path audit found no overlap between upstream changes after `67f60d5446ed3224609161f938e1b36bc9d62f89` and the final graph Cargo closure, fixed cache inputs, helper directory, or Test workflow; timing remains pinned to the measured source.
- The five affected jobs ran on Linux X64 GitHub-hosted runners on image `20260907.300.1`. The implementation falls back to the ordinary Cargo build outside its supported reuse path.
- The collector found zero graph-cache reservation warnings. Successful logs still include unrelated Node runtime deprecation messages and an expected `hash.invalid` negative-path error, so this is not a warning-free claim.
- [Correctness notes](evidence/correctness.md) and the [machine-readable receipt](evidence/correctness.json) describe separate predecessor-source invalidation/recovery checks. They are excluded from performance measurements.

## Runs

| Variant | Sample | Condition | Workflow | Wall seconds | Aggregate job seconds |
| --- | ---: | --- | --- | ---: | ---: |
| Baseline | 1 | Cold | [34641904793](https://github.com/dnikolayev/hash/actions/runs/34641904793) | 1,882 | 26,355 |
| Baseline | 1 | Warm | [34645133106](https://github.com/dnikolayev/hash/actions/runs/34645133106) | 1,808 | 26,467 |
| Baseline | 2 | Cold | [34660262273](https://github.com/dnikolayev/hash/actions/runs/34660262273) | 1,738 | 25,865 |
| Baseline | 2 | Warm | [34662145145](https://github.com/dnikolayev/hash/actions/runs/34662145145) | 1,585 | 26,036 |
| Baseline | 3 | Cold | [34673120179](https://github.com/dnikolayev/hash/actions/runs/34673120179) | 1,885 | 25,289 |
| Baseline | 3 | Warm | [34674881183](https://github.com/dnikolayev/hash/actions/runs/34674881183) | 1,388 | 24,849 |
| Candidate | 1 | Cold | [34663777159](https://github.com/dnikolayev/hash/actions/runs/34663777159) | 1,937 | 26,544 |
| Candidate | 1 | Warm | [34665584512](https://github.com/dnikolayev/hash/actions/runs/34665584512) | 1,511 | 23,725 |
| Candidate | 2 | Cold | [34666896025](https://github.com/dnikolayev/hash/actions/runs/34666896025) | 2,007 | 26,431 |
| Candidate | 2 | Warm | [34668599115](https://github.com/dnikolayev/hash/actions/runs/34668599115) | 1,518 | 24,075 |
| Candidate | 3 | Cold | [34670121057](https://github.com/dnikolayev/hash/actions/runs/34670121057) | 2,003 | 26,611 |
| Candidate | 3 | Warm | [34671885632](https://github.com/dnikolayev/hash/actions/runs/34671885632) | 1,241 | 23,770 |

## Reproduce the report

From the repository root:

```sh
python3 -B .github/benchmarks/graph-build-reuse/evidence/repro/integration-build-timings.py \
  .github/benchmarks/graph-build-reuse/evidence/manifest.json
```

The output must match `evidence/timing-report.json` byte-for-byte. `evidence/selected-results.json` retains the accepted matrix, source and generator identities, exact cache states, phase totals, storage, and final-file equivalence. `evidence/parity.json`, `evidence/provenance.json`, and `evidence/inventory.json` bind the projected inputs and hashes.
