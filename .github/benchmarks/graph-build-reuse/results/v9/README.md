# Graph executable reuse benchmark

This bundle records complete Test workflow attempts for the graph executable reuse change.
The complete campaign has twelve accepted attempts: three controls and three candidates with
cold tool caches in setup, plus three controls and three candidates with warm tool caches.
Each comparison uses its own controls with the same observed tool-cache state.

The measured revision is `2d8904b0bdfe7e65578294b921e0b6ef1bf2aaae`. The PR was
subsequently rebased to `379c67c1397fb5bbf1e3616df43b05c6bfb79857` on upstream
`47f3e2083642c7b223ca3dd5f1c5689bf68d17a6`. The implementation patch and all five
changed files are byte-identical across that rebase. The intervening upstream
commit only changes MinIO image references: the client mirror already used by
the benchmark and a server digest pin. These tables describe the measured
pre-rebase source; the rebased workflow was not measured separately.

## Results

Positive savings mean the candidate was faster; negative savings mean it was slower.

| Workflow/job | Cache condition | Samples | Before median (range) | After median (range) | Seconds saved | Percent saved | Run links |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Graph HTTP job | cold | 3 + 3 | 730.0 s (726.0–811.0) | 714.0 s (710.0–725.0) | 16.0 | 2.2% | [B 1](https://github.com/dnikolayev/hash/actions/runs/34690689946/attempts/4), [B 2](https://github.com/dnikolayev/hash/actions/runs/34696182490/attempts/1), [B 3](https://github.com/dnikolayev/hash/actions/runs/34700520543/attempts/1), [C 1](https://github.com/dnikolayev/hash/actions/runs/34693434058/attempts/1), [C 2](https://github.com/dnikolayev/hash/actions/runs/34697755835/attempts/1), [C 3](https://github.com/dnikolayev/hash/actions/runs/34701878368/attempts/1) |
| Backend integration job | cold | 3 + 3 | 1,240.0 s (1,167.0–1,318.0) | 1,300.0 s (1,170.0–1,339.0) | -60.0 | -4.8% | [B 1](https://github.com/dnikolayev/hash/actions/runs/34690689946/attempts/4), [B 2](https://github.com/dnikolayev/hash/actions/runs/34696182490/attempts/1), [B 3](https://github.com/dnikolayev/hash/actions/runs/34700520543/attempts/1), [C 1](https://github.com/dnikolayev/hash/actions/runs/34693434058/attempts/1), [C 2](https://github.com/dnikolayev/hash/actions/runs/34697755835/attempts/1), [C 3](https://github.com/dnikolayev/hash/actions/runs/34701878368/attempts/1) |
| Playwright job | cold | 3 + 3 | 1,468.0 s (1,451.0–1,878.0) | 1,738.0 s (1,695.0–1,790.0) | -270.0 | -18.4% | [B 1](https://github.com/dnikolayev/hash/actions/runs/34690689946/attempts/4), [B 2](https://github.com/dnikolayev/hash/actions/runs/34696182490/attempts/1), [B 3](https://github.com/dnikolayev/hash/actions/runs/34700520543/attempts/1), [C 1](https://github.com/dnikolayev/hash/actions/runs/34693434058/attempts/1), [C 2](https://github.com/dnikolayev/hash/actions/runs/34697755835/attempts/1), [C 3](https://github.com/dnikolayev/hash/actions/runs/34701878368/attempts/1) |
| All jobs (aggregate runner time) | cold | 3 + 3 | 26,181.0 s (26,157.0–26,327.0) | 26,779.0 s (26,180.0–26,978.0) | -598.0 | -2.3% | [B 1](https://github.com/dnikolayev/hash/actions/runs/34690689946/attempts/4), [B 2](https://github.com/dnikolayev/hash/actions/runs/34696182490/attempts/1), [B 3](https://github.com/dnikolayev/hash/actions/runs/34700520543/attempts/1), [C 1](https://github.com/dnikolayev/hash/actions/runs/34693434058/attempts/1), [C 2](https://github.com/dnikolayev/hash/actions/runs/34697755835/attempts/1), [C 3](https://github.com/dnikolayev/hash/actions/runs/34701878368/attempts/1) |
| Complete workflow (wall time) | cold | 3 + 3 | 1,583.0 s (1,555.0–1,969.0) | 1,847.0 s (1,791.0–1,892.0) | -264.0 | -16.7% | [B 1](https://github.com/dnikolayev/hash/actions/runs/34690689946/attempts/4), [B 2](https://github.com/dnikolayev/hash/actions/runs/34696182490/attempts/1), [B 3](https://github.com/dnikolayev/hash/actions/runs/34700520543/attempts/1), [C 1](https://github.com/dnikolayev/hash/actions/runs/34693434058/attempts/1), [C 2](https://github.com/dnikolayev/hash/actions/runs/34697755835/attempts/1), [C 3](https://github.com/dnikolayev/hash/actions/runs/34701878368/attempts/1) |
| Graph HTTP job | warm | 3 + 3 | 734.0 s (727.0–755.0) | 300.0 s (252.0–344.0) | 434.0 | 59.1% | [B 1](https://github.com/dnikolayev/hash/actions/runs/34690689946/attempts/2), [B 2](https://github.com/dnikolayev/hash/actions/runs/34696182490/attempts/2), [B 3](https://github.com/dnikolayev/hash/actions/runs/34700520543/attempts/2), [C 1](https://github.com/dnikolayev/hash/actions/runs/34695016374/attempts/1), [C 2](https://github.com/dnikolayev/hash/actions/runs/34699332402/attempts/1), [C 3](https://github.com/dnikolayev/hash/actions/runs/34703444733/attempts/2) |
| Backend integration job | warm | 3 + 3 | 1,299.0 s (1,278.0–1,308.0) | 817.0 s (775.0–857.0) | 482.0 | 37.1% | [B 1](https://github.com/dnikolayev/hash/actions/runs/34690689946/attempts/2), [B 2](https://github.com/dnikolayev/hash/actions/runs/34696182490/attempts/2), [B 3](https://github.com/dnikolayev/hash/actions/runs/34700520543/attempts/2), [C 1](https://github.com/dnikolayev/hash/actions/runs/34695016374/attempts/1), [C 2](https://github.com/dnikolayev/hash/actions/runs/34699332402/attempts/1), [C 3](https://github.com/dnikolayev/hash/actions/runs/34703444733/attempts/2) |
| Playwright job | warm | 3 + 3 | 1,833.0 s (1,827.0–1,907.0) | 1,349.0 s (1,340.0–1,386.0) | 484.0 | 26.4% | [B 1](https://github.com/dnikolayev/hash/actions/runs/34690689946/attempts/2), [B 2](https://github.com/dnikolayev/hash/actions/runs/34696182490/attempts/2), [B 3](https://github.com/dnikolayev/hash/actions/runs/34700520543/attempts/2), [C 1](https://github.com/dnikolayev/hash/actions/runs/34695016374/attempts/1), [C 2](https://github.com/dnikolayev/hash/actions/runs/34699332402/attempts/1), [C 3](https://github.com/dnikolayev/hash/actions/runs/34703444733/attempts/2) |
| All jobs (aggregate runner time) | warm | 3 + 3 | 26,973.0 s (26,575.0–27,097.0) | 25,235.0 s (25,074.0–25,434.0) | 1,738.0 | 6.4% | [B 1](https://github.com/dnikolayev/hash/actions/runs/34690689946/attempts/2), [B 2](https://github.com/dnikolayev/hash/actions/runs/34696182490/attempts/2), [B 3](https://github.com/dnikolayev/hash/actions/runs/34700520543/attempts/2), [C 1](https://github.com/dnikolayev/hash/actions/runs/34695016374/attempts/1), [C 2](https://github.com/dnikolayev/hash/actions/runs/34699332402/attempts/1), [C 3](https://github.com/dnikolayev/hash/actions/runs/34703444733/attempts/2) |
| Complete workflow (wall time) | warm | 3 + 3 | 1,890.0 s (1,886.0–1,965.0) | 1,411.0 s (1,399.0–1,439.0) | 479.0 | 25.3% | [B 1](https://github.com/dnikolayev/hash/actions/runs/34690689946/attempts/2), [B 2](https://github.com/dnikolayev/hash/actions/runs/34696182490/attempts/2), [B 3](https://github.com/dnikolayev/hash/actions/runs/34700520543/attempts/2), [C 1](https://github.com/dnikolayev/hash/actions/runs/34695016374/attempts/1), [C 2](https://github.com/dnikolayev/hash/actions/runs/34699332402/attempts/1), [C 3](https://github.com/dnikolayev/hash/actions/runs/34703444733/attempts/2) |

Playwright was the last job before the final success gate in all twelve attempts.
All six warm Playwright jobs used the same AMD EPYC 7763 CPU class. Their median
job time fell by 8m04s; the complete workflow median fell by 7m59s (25.3%).

The cold workflow median increased by 4m24s (16.7%), and aggregate job time
increased by 2.3%. Two cold controls used Xeon 8573C runners absent from the
candidate cohort; the only shared cold Playwright CPU class has one observation
per variant. These unadjusted results do not isolate the cache's cold overhead
or establish an exact upstream waiting-time change.

### Measured cache-establishment work

Only graph HTTP published a cache on cold runs. The consecutive upload-preparation
and save steps occupied 24, 25 and 14 seconds in samples 1, 2 and 3. HTTP, including
these steps and its tests, still finished 1,080, 1,013 and 982 seconds before
Playwright. Publication did not determine the observed workflow completion time.
The save step includes compression and cache-service work as well as transfer.
These are whole-second GitHub step envelopes, not millisecond-resolution estimates
of the change's total cost.

Backend and Playwright did not publish. Playwright still spent 27–32 seconds in
Prepare, including crate downloads reused by the later Cargo build, and 8.972–14.034
seconds in measured fingerprint, ready-stamp and live-reuse operations across its
invocations. Those operations replace some existing Cargo rechecks; lock waits
overlap other invocations. The logs do not isolate their net effect on cold waiting
time. The raw +264-second cold workflow median difference must therefore neither
be attributed entirely to the cache nor dismissed as having zero contribution
from this change.

## Build, startup and test phases

These are median seconds (range), with three samples in each column. The graph compile
task is part of background startup, so those two intervals overlap. Full job and workflow
times above include preparation, transfer, startup, tests and reporting.

| Job / phase | Cold control | Cold candidate | Warm control | Warm candidate |
| --- | ---: | ---: | ---: | ---: |
| Graph HTTP / graph compile task | 495.8 (470.6–553.8) | 456.9 (401.5–467.1) | 499.3 (492.0–511.1) | 5.5 (5.3–6.6) |
| Graph HTTP / startup and readiness | 503.0 (478.0–561.0) | 463.0 (406.0–474.0) | 507.0 (499.0–519.0) | 10.0 (10.0–11.0) |
| Graph HTTP / integration test task | 30.6 (28.3–32.1) | 29.4 (23.3–31.7) | 29.8 (29.6–29.9) | 24.8 (23.0–29.9) |
| Backend / graph compile task | 475.4 (470.7–528.4) | 469.1 (428.2–481.6) | 521.9 (512.8–527.8) | 7.1 (5.4–7.2) |
| Backend / startup and readiness | 884.0 (876.0–996.0) | 910.0 (822.0–937.0) | 964.0 (912.0–983.0) | 455.0 (348.0–461.0) |
| Backend / integration test task | 103.5 (100.9–117.6) | 111.0 (98.2–111.7) | 114.5 (90.1–116.8) | 111.5 (84.9–112.9) |
| Playwright / graph compile task | 425.8 (418.1–505.0) | 429.3 (423.7–456.8) | 501.4 (479.3–509.1) | 7.2 (7.2–7.2) |
| Playwright / startup and readiness | 953.0 (936.0–1,202.0) | 1,060.0 (988.0–1,109.0) | 1,210.0 (1,156.0–1,225.0) | 698.0 (693.0–708.0) |
| Playwright / integration test task | 221.2 (214.0–311.1) | 275.2 (196.8–298.8) | 316.6 (314.3–319.5) | 301.3 (300.4–302.3) |

The generated report.json also contains cache action costs: graph preparation, restore,
upload preparation and save. Skipped phases are omitted from the measurements;
missing data is never converted to zero.

## Method and limits

Both variants execute `2d8904b0bdfe7e65578294b921e0b6ef1bf2aaae`. The workflow template comes from
`8aedda02ba9162e77735234168281151d2d2584d`; that base is not the checkout used for the controls.
The controls retain the original Cargo command. Candidate runs enable reuse in graph HTTP,
backend integration and Playwright, with graph HTTP as the sole cache producer.
Each cold candidate starts with an empty task-specific cache namespace. Its warm repeat is an
empty child commit on the same branch and has the same Git tree and workflow bytes.

Ordinary mise and Corepack caches are matched separately from the graph executable cache.
In the cold cohort, setup misses and saves both tool caches before the matrix starts; every
matrix installation then hits both caches. In the warm cohort, setup and every matrix
installation hit both caches. Classification uses retained restore/save markers from every
Install tools step. This prevents ordinary tool downloads from being attributed to executable reuse.
Baseline cold/warm controls execute the same pinned workflow and commit in separate full attempts.
Three supplemental control attempts were added after the setup cache difference was identified;
all accepted attempts are retained. Cold controls require empty matching tool-cache scope,
and warm controls retain existing tool caches from a preceding setup.

The fork harness runs on benchmark-branch pushes and pins every checkout. It permits the
selected producer to save on those pushes; the proposed production workflow permits saves
only on main pushes. The harness also requires successful cache preparation, records machine
and cache receipts, and adds Turbo's --summarize timing output. Service, startup, healthcheck
and test command bodies otherwise remain unchanged. Upstream secrets and repository variables
are blank in both variants, apart from the ordinary scoped GITHUB_TOKEN; notifications are omitted.

All runs disable remote Turbo and sccache and use the same pinned MinIO client image from
its available official registry. Candidate runs include the additional helper-check job.
The source, harness commit, workflow digest, cache entries and test counts are recorded per attempt.

Workflow wall time runs from GitHub's `run_started_at` through final non-skipped job completion.
It includes dependencies and scheduling, but not exact push-to-notification latency.
Aggregate time sums the non-skipped job execution envelopes; parallel jobs contribute separately.
It is runner time, not CPU time. Created-to-started intervals combine dependencies and queueing.
Step envelopes and current Turbo task intervals are also retained; missing phases are unavailable,
never zero. Buffered log display timestamps are not treated as execution time.

A cache snapshot within GitHub's whole-second start timestamp requires retained local
pre-dispatch intent for that exact rerun and a successful dispatch response. The normalized
ordering fields and original-file digests are retained; ambiguous timestamps alone do not pass.

GitHub's requested runner class is Ubuntu 24.04/x64. Physical CPU models vary. Headline medians
and ranges are raw and unadjusted. CPU class IDs include image, platform, architecture, vCPU count
and CPU feature/model lines. Class-specific savings are calculated only with at least three
accepted samples per variant in exactly the same class. No full-workflow CPU adjustment is made.

Each restore is verified against its producer's recorded payload digest and exercised by current
integration tests. Independent fresh builds are not claimed to be bit-for-bit reproducible.

### Independent-build comparison

Two diagnostic workflows restored the original sample-1 and sample-2 producer
payloads without rebuilding, executing or republishing them:
[sample 1](https://github.com/dnikolayev/hash/actions/runs/34713359943/attempts/1)
and [sample 2](https://github.com/dnikolayev/hash/actions/runs/34713532050/attempts/1).
The [inspection script](https://github.com/dnikolayev/hash/blob/aab55c5242318e2c21ad8e1ac8e92a076a4e8579/.github/benchmarks/graph-build-reuse/inspect_graph_elf.py)
records ELF section sizes and hashes in compact artifacts:
[sample 1 artifact](https://github.com/dnikolayev/hash/actions/runs/34713359943/artifacts/10304101235)
and [sample 2 artifact](https://github.com/dnikolayev/hash/actions/runs/34713532050/artifacts/10304180826).

The same modeled inputs produced binaries differing by 832 bytes overall; the
second `.text` section is 64 bytes smaller. Allocated sections, including code,
also have different hashes. This is not just a debug-metadata difference. These
descriptions establish non-bit-reproducibility but identify neither a concrete
missing key input nor the semantic significance of the differences. Each restored
payload passed the current integration tests; those tests do not prove complete
semantic equivalence of independent builds.

A source audit also found compilation-clock macros in the active
`libmimalloc-sys 0.1.49` native build's version-reporting code. The helper does not
normalize that clock input. This explains an uncontrolled embedded timestamp,
but does not attribute the code-size differences. Deterministic bytes and the
semantic significance of all independent-build differences remain unproved.

Artifact SHA-256 values are `bd64aae505c61550564e3fd65881112b0dbd84d1c07c92349ff138d5983d0ff9`
and `a0c929564491ebde9eb6380041afd0242dacc095c3a673fe701878bbd8ca1418`, respectively.
These diagnostics are separate from the twelve performance attempts and are not
included in their timing statistics.

### Source-change validation

The separate [backend validation run](https://github.com/dnikolayev/hash/actions/runs/34714238378/attempts/1)
passed on the measured revision. It first restored the original producer payload,
then appended `const _: () = ();` to `libs/@local/graph/types/src/lib.rs`, a tracked
transitive dependency through `hash-graph-store`. The key changed, the old payload
was rejected for the changed source, and Cargo actually compiled `hash-graph-types`
and completed the graph build. Normal migrations, service readiness and backend
tests then passed: 110 tests with 9 skipped, plus 48 passed in the second suite.

The changed executable remained in use through backend testing. Recovery restored
the original source, key and checksum-bound executable without recompiling, and
removed the temporary backup. The original detached payload remained unchanged;
this validation did not publish a cache.

The [validation script](https://github.com/dnikolayev/hash/blob/75fff133f3166b3799440c5325e4c1c707dda52a/.github/benchmarks/graph-build-reuse/graph_source_invalidation.py)
and its [workflow](https://github.com/dnikolayev/hash/blob/75fff133f3166b3799440c5325e4c1c707dda52a/.github/workflows/integration-build-benchmark-v9-final-candidate-1.yml)
are pinned. The [artifact](https://github.com/dnikolayev/hash/actions/runs/34714238378/artifacts/10304752760)
contains `graph-invalidation-proof.json`, the three invocation logs and current
task summaries. Its SHA-256 is
`e4f3af9138ed4090331f20461767ce7b0ae57f0c35e15e990ac31f5c2fa83a12`.
The 450.623-second changed-source build belongs to this correctness check and is
excluded from performance medians. This validates source invalidation and recovery;
it does not explain the independent-build differences above.

## Attempt ledger

| Label | Attempt | Head | Status |
| --- | --- | --- | --- |
| baseline-1-cold-tools | [run 34690689946, attempt 4](https://github.com/dnikolayev/hash/actions/runs/34690689946/attempts/4) | `7bd81416aa2fd9239a00167a7d2e7f40a3c10bee` | accepted |
| candidate-1-cold | [run 34693434058, attempt 1](https://github.com/dnikolayev/hash/actions/runs/34693434058/attempts/1) | `bf4bfd2759a9ce0fd69b14c5643f3769fc3fd910` | accepted |
| baseline-1-warm-tools | [run 34690689946, attempt 2](https://github.com/dnikolayev/hash/actions/runs/34690689946/attempts/2) | `7bd81416aa2fd9239a00167a7d2e7f40a3c10bee` | accepted |
| candidate-1-warm | [run 34695016374, attempt 1](https://github.com/dnikolayev/hash/actions/runs/34695016374/attempts/1) | `cabc312ba8bae9b0875145ae4f7d39ce6ceb72f6` | accepted |
| baseline-2-cold-tools | [run 34696182490, attempt 1](https://github.com/dnikolayev/hash/actions/runs/34696182490/attempts/1) | `13e4d1fd0ab54cbb98ea4ea2d578389181967f93` | accepted |
| candidate-2-cold | [run 34697755835, attempt 1](https://github.com/dnikolayev/hash/actions/runs/34697755835/attempts/1) | `efaad6f98023805d6954687217b367de08c346db` | accepted |
| baseline-2-warm-tools | [run 34696182490, attempt 2](https://github.com/dnikolayev/hash/actions/runs/34696182490/attempts/2) | `13e4d1fd0ab54cbb98ea4ea2d578389181967f93` | accepted |
| candidate-2-warm | [run 34699332402, attempt 1](https://github.com/dnikolayev/hash/actions/runs/34699332402/attempts/1) | `8c1a2ff992d408b302cc799e62a22e372975329a` | accepted |
| baseline-3-cold-tools | [run 34700520543, attempt 1](https://github.com/dnikolayev/hash/actions/runs/34700520543/attempts/1) | `f84537d4d0b30f279dcb149c5e394f8227943652` | accepted |
| candidate-3-cold | [run 34701878368, attempt 1](https://github.com/dnikolayev/hash/actions/runs/34701878368/attempts/1) | `eee0b2d91197be94ba79a31a5acc4b759895e729` | accepted |
| baseline-3-warm-tools | [run 34700520543, attempt 2](https://github.com/dnikolayev/hash/actions/runs/34700520543/attempts/2) | `f84537d4d0b30f279dcb149c5e394f8227943652` | accepted |
| candidate-3-warm | [run 34703444733, attempt 2](https://github.com/dnikolayev/hash/actions/runs/34703444733/attempts/2) | `7005d7fa6c3859dc6b524fccf36971fa0932f196` | accepted |
| excluded | [run 34690689946, attempt 1](https://github.com/dnikolayev/hash/actions/runs/34690689946/attempts/1) | `7bd81416aa2fd9239a00167a7d2e7f40a3c10bee` | Excluded from performance medians: complete workflow conclusion was failure. The cache-disabled Playwright job timed out after 15 seconds waiting for the registration POST response in tests/account/mfa.spec.ts; 31 tests passed, 8 skipped, and 1 failed. Every other matrix job completed successfully. |
| excluded | [run 34703444733, attempt 1](https://github.com/dnikolayev/hash/actions/runs/34703444733/attempts/1) | `7005d7fa6c3859dc6b524fccf36971fa0932f196` | Playwright failed the enable-TOTP test because the backup-code dialog was not visible within 5 seconds (31 passed, 1 failed, 8 skipped). |
| excluded | [run 34690689946, attempt 3](https://github.com/dnikolayev/hash/actions/runs/34690689946/attempts/3) | `7bd81416aa2fd9239a00167a7d2e7f40a3c10bee` | Excluded from accepted performance samples: baseline-1 cold-tool control, run 34690689946 attempt 3, workflow head 7bd81416aa2fd9239a00167a7d2e7f40a3c10bee. The OpenAPI Generator 6.6.0 download returned HTTP 404 before the backend-utils unit tests could run. The workflow failed and its timing is not included in the performance comparison. |

## Historical opportunity

Projecting the final source closure across the last 400 first-parent commits
at the pinned base identifies 333 preserving commits and 67 invalidating commits.
This is a static path projection: historical dependency closures and per-job pruning are not replayed.

For main Test pushes from 2026-08-14 through 2026-09-12, the snapshot contains
227 runs, of which 162 succeeded. Of the 119
successful runs with an eligible graph consumer, the model classifies 30 as cold
and 89 as warm (74.8% warm opportunity).
Each sequence of commits sharing the projected inputs starts cold. A successful workflow
containing a target job supplies the first reusable build for that sequence.
Requiring the preceding producer to finish before a consumer starts gives the same counts in this sample.

| Selected consumers | Modeled cold runs | Modeled warm runs | Warm opportunity |
| --- | --- | --- | --- |
| HTTP + Backend | 3 | 2 | 40.0% |
| HTTP + Backend + Playwright | 23 | 29 | 55.8% |
| Backend | 0 | 1 | 100.0% |
| Backend + Playwright | 1 | 23 | 95.8% |
| Playwright | 3 | 34 | 91.9% |

These are source and matrix opportunities, not observed cache hits or measured upstream savings.
The matrix mix matters: the overall warm fraction cannot weight the complete-workflow benchmark
to establish an actual upstream gain. Runner images, tools, cache scope, eviction and upload failures
can cause additional misses. The model excludes unsuccessful runs and covers main pushes only.
The complete normalized commit/run inputs, API provenance and further limitations are in `campaign.json`.

The modeled ratio is about three warm opportunities per cold establishment
(`89 / 30`), rather than a measurement of hits in an existing executable cache.
Cumulative waiting-time benefit is the sum of savings across warm workflows minus
the additional waiting time across cold workflows. For example, four comparable
warm runs each saving the measured 479 seconds would save 31m56s in total before
subtracting one cold establishment's added waiting time. These are separate run
waits, which may overlap in calendar time; parallel job savings are not added.

Neither the historical upstream saving per warm run nor the causal cold cost is
established here. Upstream enables remote Turbo and sccache, workloads and job matrices
differ, and an unaffected job can become the critical path. Multiplying all 89
opportunities by the fork's 479-second full-workflow result would not establish an
actual or expected upstream saving.

## Reproduce the analysis

The analyzer uses Python's standard library and does not access the network.
Run these commands from the repository root:

```sh
cd .github/benchmarks/graph-build-reuse/results/v9
python3 reproduce.py --self-test
python3 -O reproduce.py --self-test
python3 reproduce.py > report.json
```

For a partial staging bundle, use `--allow-incomplete`; this suppresses all savings claims.
The analyzer derives job, phase and task intervals from the retained timestamps, checks parity
against the independently verified per-attempt metrics and checks the cache/source/behavior bindings.
The cold and warm comparisons use separate control attempts matched by observed tool-cache state.
The independent-build and source-change summaries above link to separate
diagnostic scripts and artifacts; the timing analyzer does not reproduce those
runtime checks.

`campaign.json` contains allowlisted projections and SHA-256/size provenance for the original
API responses, logs and artifacts. It does not contain full logs or environment dumps.
Hashes establish identity if the originals are reacquired; they do not independently prove that
the projections were extracted correctly. Run links permit inspection while GitHub retains the originals.

`generate.py` is the exact workflow generator used for the campaign. In a checkout containing
both source commits, regenerate a workflow with:

```sh
python3 generate.py --checkout . \
  --baseline-ref 8aedda02ba9162e77735234168281151d2d2584d \
  --candidate-ref 2d8904b0bdfe7e65578294b921e0b6ef1bf2aaae \
  --variant candidate --campaign v9final --sample 1 --scope full \
  --output candidate-1.yml
```

Repeat for variants `baseline` and `candidate` and samples `1`, `2`, `3`; compare each output's
SHA-256 with `bindings.workflow_sha256` in `campaign.json`. The generator needs Git and the
repository history; running the analyzer does not. This bundle does not dispatch CI or publish caches.
