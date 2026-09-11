# Graph integration build reuse

The Test workflow can restore the graph executable built by
`cargo build --bin hash-graph --all-features`. The existing Turbo compile task
remains uncached. After pruning and installing dependencies, `prepare` replaces
only that checkout's compile script with the guarded compiler invocation.
The tracked application scripts and compilation outside this CI path are unchanged.

A hit restores `target/debug/hash-graph` atomically. The cache contains only the
executable and its input key and SHA-256 digest. It contains no migrations,
databases, readiness results, test results, or frontend bundles. Existing service
startup, migration, healthcheck and integration commands still execute.

Within a runner, storage and restoration use atomic hardlinks with a copy fallback.
Each hit verifies the payload checksum; when the live executable shares that inode,
it does not need a second checksum pass. A local lock outside the cached payload
serializes compiler invocations, including unsupported-environment fallbacks, and
upload validation. Cargo removes its prior output before relinking, so rebuilding
does not overwrite a retained payload inode. Cache upload remains after successful
startup healthchecks and before the integration command can request another build.

## Input and trust boundaries

The key includes the effective pruned tracked files, missing-file markers, local
Cargo package files including generated inputs, resolved Cargo metadata and
lockfile, Cargo configuration, compiler/tool identities, the compiler environment,
runner image/platform, and canonical checkout path. The path matters because the
graph embeds `CARGO_MANIFEST_DIR` when locating runtime environment files.
The untracked wasm-pack output in the type-system crate is excluded because native
graph compilation does not consume it; tracked files there remain inputs.

Cargo metadata resolves the pruned workspace before hashing. The helper rechecks
inputs after compilation and refuses to store an output if they changed.
Restoration requires the exact key, a regular executable with the expected ELF
architecture, and the matching checksum. Invalid or incomplete payloads fall back
to compilation. Unsupported configurations use the original Cargo command.

Compilation in the supported CI path uses an explicit environment and stable
compiler-tool paths. Relevant Cargo, Rust, native compiler and protoc settings are
retained; unrelated runner and service variables are not supplied to compilation.
Custom output targets, profiles, flags, external include/library paths and Cargo
configuration selectors outside the supported model bypass reuse. The helper
stores environment hashes, never environment values.

GitHub Actions caches use exact keys without restore prefixes. Default-branch and
pull-request cache visibility remains governed by GitHub's cache scope; this
workflow does not use privileged pull-request execution or consume caches from a
separate untrusted workflow. A fork uses its own repository's cache.

The initial policy is conservative: changes to unrelated tracked files, different
pruned workspaces or runner identities may miss the cache. This trades hit rate
for a readily inspectable input boundary. No workflow saving is assumed from a
binary hit; preparation and transfer costs are part of the measurement.

## Focused checks

```sh
python3 .github/actions/graph-build-cache/graph_build_cache_test.py
python3 .github/scripts/integration_build_timings_test.py
```

The colocated unit checks use synthetic source trees and compiler outputs. They
cover direct/transitive/generated/non-Rust inputs, dependency and tool changes,
unsupported environments, malformed or absent payloads, failed builds, and
restoration permissions, concurrent compilation, shared-inode corruption, copy
fallback and archive restoration. They do not substitute for integration tests on
a fresh runner.

## Performance validation

Measure the complete change against the original Cargo command with reuse disabled
(`HASH_GRAPH_BUILD_CACHE=0`). The candidate uses `1` and pays all preparation,
validation, transfer and storage costs. The diagnostic `baseline` mode uses the
candidate's preparation and compiler environment without output reuse; it does
not measure the net change from existing CI. Phase events use a monotonic clock
and are emitted by the current invocation, independently of Turbo's cached logs.

Save the run and all job pages for every attempt, including failures. The timing
comparison accepts a JSON manifest and reports sample counts, medians, ranges,
per-job and workflow wall time, transfer/startup/test phases, and aggregate job
seconds:

```sh
python3 .github/scripts/integration-build-timings.py manifest.json > comparison.json
```

The optional `task_timings` manifest field maps exact job names to compact native
Turbo run summaries. It separates initial graph compilation and the selected
integration test task from their enclosing workflow steps. Only complete current
executions count; missing or cached task evidence is reported as unavailable.

The script documents the manifest and retrieval commands. Complete Test workflows
and integration-only subsets must have different scope labels. Fork runs without
upstream Turbo/sccache access cannot establish identical upstream performance.
At least three successful cold and warm samples per variant, fresh-runner restore
proof, changed-input rebuilds and equivalent executed test counts are required
before claiming an improvement.
