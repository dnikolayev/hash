#!/usr/bin/env python3
"""Fork-only proof that a tracked transitive edit forces a real graph rebuild."""

import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import tomllib


SOURCE_SHA = "2d8904b0bdfe7e65578294b921e0b6ef1bf2aaae"
BRANCH = "speedup/integration-build-benchmark-v9-final-candidate-1"
CACHE_PREFIX = "graph-build-benchmark-v9final-1-"
SOURCE = Path("libs/@local/graph/types/src/lib.rs")
ADDITION = b"\nconst _: () = ();\n"
KEY = re.compile(r"graph-build-v1-[0-9a-f]{64}")


def require(condition, reason):
    if not condition:
        raise RuntimeError(reason)


def parse_events(output):
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    events = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and {"phase", "status", "key", "seconds"} <= event.keys():
            events.append({name: event[name] for name in ("phase", "status", "key", "seconds")})
    return output, events


def main():
    require(len(sys.argv) == 2 and sys.argv[1] in {"prepare", "recover"}, "usage: prepare|recover")
    mode = sys.argv[1]
    root = Path.cwd()
    helper_path = root / ".github/actions/graph-build-cache/graph_build_cache.py"
    helper = runpy.run_path(str(helper_path))
    state = Path(os.environ["RUNNER_TEMP"]) / (
        f"graph-source-invalidation-v9-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}"
    )
    source = root / SOURCE
    payload = root / "target/graph-build-cache"
    binary = root / "target/debug/hash-graph"
    receipt = root / "var/logs/graph-invalidation-proof.json"
    data = {
        "schema": 1,
        "kind": "tracked-transitive-source-invalidation",
        "performance_excluded": True,
        "result": "failed",
        "run_id": os.environ["GITHUB_RUN_ID"],
        "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
        "calls": [],
    }
    owned = False
    if mode == "recover":
        if not state.is_dir():
            print(json.dumps({"result": "not-started"}))
            return
        data = json.loads((state / "state.json").read_text())
        require(
            data["run_id"] == os.environ["GITHUB_RUN_ID"]
            and data["run_attempt"] == os.environ["GITHUB_RUN_ATTEMPT"],
            "run-identity-mismatch",
        )
        owned = True

    def save_state():
        if owned and state.is_dir():
            (state / "state.json").write_text(json.dumps(data, sort_keys=True) + "\n")

    def invoke(label):
        with tempfile.TemporaryFile(mode="w+") as output:
            result = subprocess.run(
                [sys.executable, str(helper_path), "compile"],
                stdout=output,
                stderr=subprocess.STDOUT,
                check=False,
            )
            output.seek(0)
            text, events = parse_events(output.read())
        raw_log = root / "var/logs/graph-invalidation" / (label + ".log")
        raw_log.parent.mkdir(parents=True, exist_ok=True)
        raw_log.write_text(text)
        record = {"label": label, "returncode": result.returncode, "events": events,
                  "log": str(raw_log.relative_to(root)),
                  "log_sha256": hashlib.sha256(raw_log.read_bytes()).hexdigest(),
                  "log_bytes": raw_log.stat().st_size}
        data["calls"].append(record)
        return result.returncode, text, events, record

    def has(events, phase, status, key):
        return sum(
            (event["phase"], event["status"], event["key"]) == (phase, status, key)
            for event in events
        ) == 1

    def require_hit(label, key):
        code, _, events, _ = invoke(label)
        require(
            code == 0
            and has(events, "prepare", "ready", key)
            and has(events, "restore", "hit", key),
            label + "-did-not-restore-old-key",
        )
        require(
            not any(event["phase"] in {"build", "reuse", "publish", "store", "reject"} for event in events),
            label + "-unexpected-build-or-publication",
        )

    try:
        require(os.environ.get("HASH_GRAPH_BUILD_CACHE") == "1", "reuse-mode-required")
        if mode == "prepare":
            key = os.environ["PROOF_PREPARED_KEY"]
            actions_key = os.environ["PROOF_ACTIONS_KEY"]
            require(os.environ["PROOF_ACTIONS_CACHE_HIT"] == "true", "exact-actions-cache-hit-required")
            require(KEY.fullmatch(key) is not None, "prepared-key-missing")
            require(actions_key == CACHE_PREFIX + key, "actions-key-mismatch")
            require(os.environ.get("GITHUB_REF_NAME") == BRANCH, "unexpected-branch")
            checkout_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            require(checkout_sha == SOURCE_SHA, "unexpected-source-head")
            require(not state.exists(), "proof-state-already-exists")
            require(source.is_file() and not source.is_symlink(), "transitive-source-not-regular")
            subprocess.run(["git", "ls-files", "--error-unmatch", str(SOURCE)], cwd=root, check=True)
            original = source.read_bytes()
            require(subprocess.run(["git", "diff", "--quiet", "--", str(SOURCE)], cwd=root).returncode == 0, "source-checkout-not-clean-before-proof")
            require(not original.endswith(ADDITION), "proof-addition-already-present")

            workspace = tomllib.loads((root / "Cargo.toml").read_text())["workspace"]["dependencies"]
            app = tomllib.loads((root / "apps/hash-graph/Cargo.toml").read_text())
            store = tomllib.loads((root / "libs/@local/graph/store/rust/Cargo.toml").read_text())
            require(
                workspace["hash-graph-store"]["path"] == "libs/@local/graph/store/rust"
                and workspace["hash-graph-types"]["path"] == "libs/@local/graph/types"
                and app["dependencies"]["hash-graph-store"].get("workspace") is True
                and not app["dependencies"]["hash-graph-store"].get("optional", False)
                and store["dependencies"]["hash-graph-types"].get("workspace") is True
                and not store["dependencies"]["hash-graph-types"].get("optional", False),
                "transitive-dependency-chain-changed",
            )
            require(not binary.exists() and not binary.is_symlink(), "fresh-runner-live-binary-required")
            require(not (root / helper["READY_STATE"]).exists(), "fresh-runner-ready-state-required")

            state.mkdir()
            owned = True
            shutil.copy2(source, state / "source.original")
            data.update(
                {
                    "armed": False,
                    "source": str(SOURCE),
                    "source_sha256": hashlib.sha256(original).hexdigest(),
                    "source_checkout_clean_before": subprocess.run(
                        ["git", "diff", "--quiet", "--", str(SOURCE)], cwd=root
                    ).returncode
                    == 0,
                    "dependency_chain": ["hash-graph", "hash-graph-store", "hash-graph-types"],
                    "baseline_key": key,
                    "actions_key": actions_key,
                    "actions_cache_hit": True,
                    "checkout_sha": checkout_sha,
                    "workflow_sha": os.environ["GITHUB_WORKFLOW_SHA"],
                    "event_sha": os.environ["GITHUB_SHA"],
                    "branch": os.environ["GITHUB_REF_NAME"],
                }
            )
            with helper["build_lock"](root):
                baseline_hash = helper["verified_payload_digest"](payload, key)
                require(baseline_hash is not None, "restored-payload-invalid")
                manifest = (payload / "manifest.json").read_bytes()
                initial_payload_state = {
                    name: helper["path_state"](payload / name) for name in ("hash-graph", "manifest.json")
                }
                (state / "payload").mkdir()
                shutil.copy2(payload / "hash-graph", state / "payload/hash-graph")
                (state / "payload/manifest.json").write_bytes(manifest)
                require(
                    helper["verified_payload_digest"](state / "payload", key) == baseline_hash,
                    "payload-backup-invalid",
                )
            data.update(
                {
                    "baseline_binary_sha256": baseline_hash,
                    "baseline_manifest_sha256": hashlib.sha256(manifest).hexdigest(),
                    "initial_payload_state": initial_payload_state,
                    "armed": True,
                }
            )
            save_state()

            require_hit("fresh-old-key-restoration", key)
            ready = helper["ready_state"](root, dict(os.environ))
            require(
                ready is not None
                and ready["key"] == key
                and helper["file_digest"](binary) == baseline_hash,
                "old-live-build-invalid",
            )

            source.write_bytes(original + ADDITION)
            require(source.read_bytes() == original + ADDITION, "source-mutation-failed")
            require(
                subprocess.run(["git", "diff", "--quiet", "--", str(SOURCE)], cwd=root).returncode == 1,
                "tracked-source-edit-not-observed",
            )
            code, output, events, record = invoke("changed-transitive-source")
            changed_keys = [
                event["key"]
                for event in events
                if (event["phase"], event["status"]) == ("prepare", "ready")
            ]
            compile_line = re.search(r"^\s*Compiling hash-graph-types v\S+.*$", output, re.MULTILINE)
            require(len(changed_keys) == 1 and changed_keys[0] != key, "source-edit-did-not-change-key")
            changed_key = changed_keys[0]
            require(
                code == 0
                and has(events, "restore", "miss", changed_key)
                and has(events, "build", "success", changed_key)
                and has(events, "reuse", "ready", changed_key),
                "changed-source-did-not-rebuild",
            )
            require(
                not any(
                    event["phase"] in {"publish", "store", "reject"}
                    or (event["phase"], event["status"]) in {("restore", "hit"), ("restore", "live")}
                    for event in events
                ),
                "changed-source-used-or-published-old-payload",
            )
            require(compile_line is not None, "transitive-crate-compilation-not-observed")
            changed_ready = helper["ready_state"](root, dict(os.environ))
            require(changed_ready is not None and changed_ready["key"] == changed_key, "changed-ready-state-invalid")
            positive_hash = helper["file_digest"](binary)
            stable_indexes = (0, 1, 2, 3, 4, 5, 6, 7, 9)
            current_payload_state = {
                name: helper["path_state"](payload / name) for name in ("hash-graph", "manifest.json")
            }
            unchanged_witness = all(
                all(current_payload_state[name][index] == initial_payload_state[name][index] for index in stable_indexes)
                for name in current_payload_state
            ) and (payload / "manifest.json").read_bytes() == manifest
            require(unchanged_witness, "old-payload-changed-during-rebuild")
            record["transitive_compile_line"] = compile_line.group(0).strip()
            data.update(
                {
                    "addition": ADDITION.decode(),
                    "changed_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    "changed_key": changed_key,
                    "changed_binary_sha256": positive_hash,
                    "old_payload_rejected_as_miss": True,
                    "old_payload_unchanged_after_build": True,
                    "result": "changed-build-passed-awaiting-backend",
                }
            )
        else:
            if not data.get("armed"):
                # No tracked source is modified until verified backups are armed.
                shutil.rmtree(state)
                owned = False
                data["temporary_backup_removed"] = True
                data["result"] = "preparation-failed-before-source-edit"
                return
            original = (state / "source.original").read_bytes()
            key = data["baseline_key"]
            changed_key = data.get("changed_key")
            proof_complete = data.get("result") == "changed-build-passed-awaiting-backend"
            data["result"] = "failed"

            positive_source_present = source.is_file() and source.read_bytes() == original + ADDITION
            current_fingerprint = None
            try:
                current_fingerprint = helper["fingerprint"](root, dict(os.environ))[0]
            except (OSError, ValueError, KeyError, subprocess.SubprocessError, helper["NoReuse"]):
                pass
            current_ready = helper["ready_state"](root, dict(os.environ))
            changed_binary_preserved = (helper["executable"](binary) and helper["file_digest"](binary) == data.get("changed_binary_sha256"))
            old_payload_preserved = helper["verified_payload_digest"](payload, key) == data["baseline_binary_sha256"]
            data.update(
                {
                    "changed_source_present_after_backend": positive_source_present,
                    "changed_key_after_backend": current_fingerprint,
                    "ready_key_after_backend": current_ready["key"] if current_ready else None,
                    "old_payload_preserved_after_backend": old_payload_preserved,
                    "changed_binary_preserved_after_backend": changed_binary_preserved,
                    "startup_outcome": os.environ.get("PROOF_STARTUP_OUTCOME", ""),
                    "test_outcome": os.environ.get("PROOF_TEST_OUTCOME", ""),
                }
            )

            shutil.copy2(state / "source.original", source)
            require(source.read_bytes() == original, "source-recovery-failed")
            require(
                subprocess.run(["git", "diff", "--quiet", "--", str(SOURCE)], cwd=root).returncode == 0,
                "source-checkout-not-clean-after-recovery",
            )
            with helper["build_lock"](root):
                backup = state / "payload"
                require(
                    helper["verified_payload_digest"](backup, key) == data["baseline_binary_sha256"],
                    "payload-backup-invalid-at-recovery",
                )
                exact_payload = (
                    helper["verified_payload_digest"](payload, key) == data["baseline_binary_sha256"]
                    and (payload / "manifest.json").read_bytes() == (backup / "manifest.json").read_bytes()
                )
                if not exact_payload:
                    helper["write_payload"](payload, backup / "hash-graph", key)
                require(
                    helper["verified_payload_digest"](payload, key) == data["baseline_binary_sha256"]
                    and (payload / "manifest.json").read_bytes() == (backup / "manifest.json").read_bytes(),
                    "old-payload-recovery-failed",
                )
                helper["clear_ready"](root)
                binary.unlink(missing_ok=True)
                require(helper["fingerprint"](root, dict(os.environ))[0] == key, "old-key-recovery-failed")

            require_hit("final-old-key-restoration", key)
            final_ready = helper["ready_state"](root, dict(os.environ))
            require(
                final_ready is not None
                and final_ready["key"] == key
                and helper["file_digest"](binary) == data["baseline_binary_sha256"],
                "final-old-live-build-invalid",
            )
            data.update(
                {
                    "source_restored": True,
                    "old_payload_restored": True,
                    "old_key_restored": True,
                }
            )
            shutil.rmtree(state)
            owned = False
            require(proof_complete, "changed-build-proof-incomplete")
            require(positive_source_present, "changed-source-not-present-during-backend")
            require(current_fingerprint == changed_key, "changed-key-not-present-after-backend")
            require(current_ready is not None and current_ready["key"] == changed_key, "changed-ready-state-not-present-after-backend")
            require(changed_binary_preserved, "changed-binary-not-present-after-backend")
            require(old_payload_preserved, "old-payload-not-preserved-during-backend")
            require(
                data["startup_outcome"] == data["test_outcome"] == "success",
                "backend-behavior-did-not-pass",
            )
            data["temporary_backup_removed"] = True
            data["result"] = "passed"
    except BaseException as error:
        data["failure"] = type(error).__name__ + ":" + str(error)
        raise
    finally:
        if owned and state.is_dir():
            save_state()
        data.setdefault("temporary_backup_removed", not state.exists())
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        print(json.dumps(data, sort_keys=True))


if __name__ == "__main__":
    main()
