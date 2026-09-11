# Integration graph build benchmark

This fork-only harness measures the complete change against the original Cargo
compile command. Both variants check out the same candidate source and dependencies.
Baseline mode `0` leaves the application compile script and compiler environment
unchanged. Candidate mode `1` prepares and reuses the executable, including its
preparation, validation, transfer and storage overhead.

The diagnostic helper mode `baseline` uses an explicit compiler environment and
fingerprinting without reuse. Earlier runs using that mode are diagnostic evidence,
not the primary comparison against existing CI.

## Generate and run

Use a checkout containing the baseline and candidate commits. Set `SOURCE_CHECKOUT`,
`BASELINE_SHA` and `CANDIDATE_SHA` to that checkout and those exact commits. Run this
command from a separate benchmark checkout:

```sh
python3 .github/benchmarks/graph-build-reuse/generate.py \
  --checkout "$SOURCE_CHECKOUT" \
  --baseline-ref "$BASELINE_SHA" \
  --candidate-ref "$CANDIDATE_SHA" \
  --variant baseline \
  --campaign example \
  --sample 1 \
  --scope full \
  --self-test \
  --output .github/workflows/integration-build-benchmark.yml
```

Push each variant/sample to a separate branch matching
`speedup/integration-build-benchmark-*`. After a cold run completes, repeat it with
an empty commit on the **same branch**, keeping workflow bytes and source SHA fixed.
Source pins retain the same affected-package selection during this repetition.
Each candidate sample has its own exact cache namespace; a different branch cannot
substitute for its warm repetition because cache visibility is scoped by ref.

Run full trials serially to avoid competing workflows changing account queueing.
Alternate baseline/candidate order across samples. Obtain at least three successful
measurements per variant and cold/warm condition. Keep every failed attempt and
explain exclusions. A nominal warm run with an actual miss remains a miss; retain
mixed cache states per job and report the observed hit rate.

## Differences from normal CI

- `full` preserves the complete push-profile Test workflow's selection, unit and
  integration matrices, and final gates. The candidate includes its added helper
  and timing check job; the baseline does not pay that added cost.
- `smoke` runs only Playwright and backend integration. It has no setup, unit or
  final-gate jobs and cannot establish complete Test workflow savings.
- All checkouts pin the candidate SHA. The workflow triggers on benchmark pushes.
  Existing concurrency cancellation remains; never push another repetition while
  its preceding run is active.
- Both variants disable remote Turbo and sccache. No upstream credentials are
  supplied. Turbo uses `local:rw` on each fresh runner, `CARGO_INCREMENTAL=0`, and
  the CI nextest profile. Existing tool-install caches remain available; retain
  their actual states. These fork measurements do not establish identical
  performance with upstream remote caches.
- Secret acquisition inputs are empty. Notifications are omitted because the
  push event cannot use them. The original coverage-upload step remains without
  an upstream secret.
- Both variants record source, runner/tool versions and current task timing.
  Native Turbo `--summarize` is added identically to the initial compile and
  selected integration invocation. Startup, service retries, migrations,
  healthchecks, test selection, coverage and timeouts remain unchanged.
- Candidate cache preparation must produce a key when the graph package exists.
  Payload validation must match that key before upload. Exact keys have no
  fallback prefixes. Only the executable and checksum manifest are reused.

## Evidence

Retain each run/attempt response and all job API pages. Logs retain current helper
phase events measured with a monotonic clock. `var/logs` artifacts additionally
contain benchmark environment, graph input fingerprints, cache receipts and a
compact `turbo-task-timings.json` projection. The projection contains current task
timestamps, commands and cache status; it omits environment values and full native
summaries. Verify executed test counts and live readiness/migration results from
the corresponding reports/logs.

The implementation's `.github/scripts/integration-build-timings.py` documents API
retrieval and its manifest. Optional `task_timings` maps exact job names to their
projected summary files. It rejects incomplete, cached, duplicate or mismatched
task evidence rather than inventing zero-duration phases.

```sh
python3 .github/scripts/integration-build-timings.py manifest.json > comparison.json
```

Report preparation/transfer, startup/readiness, selected test execution, affected
job wall time, complete workflow wall time and aggregate job-minutes separately.
Per-job waiting includes dependencies and queueing; pure scheduling delay needs
separate readiness evidence. Do not add savings across parallel jobs and call
that workflow savings. Retain input-change rebuild proof separately from the
unchanged-input cold/warm comparisons.
