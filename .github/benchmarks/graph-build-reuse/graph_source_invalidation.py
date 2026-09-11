#!/usr/bin/env python3
"""Fork-only source invalidation proof; excludes this run from performance samples."""
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

SOURCES = {"direct": "apps/hash-graph/src/main.rs",
           "transitive": "libs/@local/graph/types/src/lib.rs"}
BACKUPS = {**SOURCES, "unrelated": ".github/README-CI.md"}
ADDITION = b"\nconst _: () = ();\n"


def parse_events(output):
    output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    events = []
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and {"phase", "status", "key", "seconds"} <= event.keys():
            events.append({key: event[key] for key in ("phase", "status", "key", "seconds")})
    return output, events


def main():
    if not __debug__:
        raise SystemExit("source-proof-requires-python-assertions")
    assert len(sys.argv) == 2 and sys.argv[1] in {"prepare", "recover"}
    mode = sys.argv[1]
    root = Path.cwd()
    script = root / ".github/actions/graph-build-cache/graph_build_cache.py"
    helper = runpy.run_path(str(script))
    state = Path(os.environ["RUNNER_TEMP"]) / f"graph-source-invalidation-{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}"
    payload, binary = root / "target/graph-build-cache", root / "target/debug/hash-graph"
    receipt = root / "var/logs/graph-invalidation-proof.json"
    data = {"result": "failed", "run_id": os.environ["GITHUB_RUN_ID"],
            "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"], "sources": BACKUPS, "calls": []}
    owned = False
    if mode == "recover":
        if not state.exists():
            print(json.dumps({"result": "not-started"}))
            return
        data = json.loads((state / "state.json").read_text())
        assert data["run_id"] == os.environ["GITHUB_RUN_ID"] and data["run_attempt"] == os.environ["GITHUB_RUN_ATTEMPT"]
        owned = True
        data["result"] = "failed"

    def compile_graph(label):
        with tempfile.TemporaryFile(mode="w+") as output:
            result = subprocess.run([sys.executable, str(script), "compile"],
                                    stdout=output, stderr=subprocess.STDOUT, check=False)
            output.seek(0)
            text, events = parse_events(output.read())
        data["calls"].append({"label": label, "returncode": result.returncode, "events": events})
        return result.returncode, text, events

    def require_hit(label, key):
        code, _, events = compile_graph(label)
        assert code == 0 and sum((event["phase"], event["status"], event["key"]) == ("restore", "hit", key) for event in events) == 1
        assert not any(event["phase"] in {"build", "store", "reject"} for event in events)

    try:
        assert os.environ.get("HASH_GRAPH_BUILD_CACHE") == "1"
        if mode == "prepare":
            key = os.environ["PROOF_PREPARED_KEY"]
            assert os.environ["PROOF_ACTIONS_CACHE_HIT"] == "true"
            assert re.fullmatch(r"graph-build-v1-[0-9a-f]{64}", key)
            assert not binary.exists() and not binary.is_symlink()
            workspace = tomllib.loads((root / "Cargo.toml").read_text())["workspace"]["dependencies"]
            for name, path in (("hash-graph-store", "libs/@local/graph/store/rust"),
                               ("hash-graph-types", "libs/@local/graph/types")):
                assert workspace[name]["path"] == path
            for manifest, dependency in (("apps/hash-graph/Cargo.toml", "hash-graph-store"),
                                          ("libs/@local/graph/store/rust/Cargo.toml", "hash-graph-types")):
                edge = tomllib.loads((root / manifest).read_text())["dependencies"][dependency]
                assert edge.get("workspace") is True and not edge.get("optional", False)
            state.mkdir()  # Refuse to overwrite any pre-existing state.
            owned = True
            data.update({"baseline_key": key, "original_sources": {}, "actions_cache_hit": True,
                         "workflow_sha": os.environ["GITHUB_WORKFLOW_SHA"],
                         "checkout_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()})
            for label, relative in BACKUPS.items():
                assert not (root / relative).is_symlink()
                original = (root / relative).read_bytes()
                (state / f"{label}.original").write_bytes(original)
                data["original_sources"][label] = hashlib.sha256(original).hexdigest()
            with helper["build_lock"](root):
                data["baseline_binary_sha256"] = helper["verified_payload_digest"](payload, key)
                assert data["baseline_binary_sha256"] is not None
                (state / "payload").mkdir()
                shutil.copy2(payload / "hash-graph", state / "payload/hash-graph")
                (state / "payload/manifest.json").write_bytes((payload / "manifest.json").read_bytes())
                assert helper["verified_payload_digest"](state / "payload", key) == data["baseline_binary_sha256"]
            data["armed"] = True
            (state / "state.json").write_text(json.dumps(data) + "\n")
            require_hit("fresh-restoration", key)
            unrelated = root / BACKUPS["unrelated"]
            original = (state / "unrelated.original").read_bytes()
            try:
                unrelated.write_bytes(original + b"\n<!-- Graph cache source-boundary verification. -->\n")
                assert helper["fingerprint"](root, dict(os.environ))[0] == key
                require_hit("unrelated-documentation", key)
                data["unrelated_documentation"] = {
                    "path": BACKUPS["unrelated"], "key": key,
                    "original_sha256": data["original_sources"]["unrelated"],
                    "changed_sha256": helper["file_digest"](unrelated),
                }
            finally:
                unrelated.write_bytes(original)
                assert unrelated.read_bytes() == original
            data["unrelated_documentation"]["restored"] = True
            for label, relative in SOURCES.items():
                source, original = root / relative, (state / f"{label}.original").read_bytes()
                marker = "GRAPH_CACHE_INVALIDATION_PROOF_" + label.upper()
                try:
                    source.write_bytes(original + f'\ncompile_error!("{marker}");\n'.encode())
                    code, output, events = compile_graph(label)
                    ready = [event["key"] for event in events if (event["phase"], event["status"]) == ("prepare", "ready")]
                    assert len(ready) == 1 and ready[0] != key
                    phases = [(event["phase"], event["status"], event["key"]) for event in events]
                    assert code == 101 and re.search(r"^error: " + marker + r"\s*$", output, re.MULTILINE)
                    assert phases.count(("restore", "miss", ready[0])) == phases.count(("build", "failed", ready[0])) == 1
                    assert not any(event["phase"] in {"store", "reject"} or (event["phase"], event["status"]) == ("restore", "hit") for event in events)
                    assert not binary.exists() and not binary.is_symlink()
                    assert (payload / "manifest.json").read_bytes() == (state / "payload/manifest.json").read_bytes()
                    assert helper["verified_payload_digest"](payload, key) == data["baseline_binary_sha256"]
                    data["calls"][-1]["rust_error_marker_observed"] = marker
                finally:
                    source.write_bytes(original)
                    assert source.read_bytes() == original
                require_hit(label + "-recovery", key)
            source = root / SOURCES["transitive"]
            source.write_bytes((state / "transitive.original").read_bytes() + ADDITION)
            code, output, events = compile_graph("positive-transitive")
            ready = [event["key"] for event in events if (event["phase"], event["status"]) == ("prepare", "ready")]
            assert len(ready) == 1 and ready[0] != key and code == 0
            phases = [(event["phase"], event["status"], event["key"]) for event in events]
            assert all(phases.count((phase, status, ready[0])) == 1 for phase, status in (("restore", "miss"), ("build", "success"), ("store", "ready")))
            assert not any(event["phase"] == "reject" or (event["phase"], event["status"]) == ("restore", "hit") for event in events)
            assert re.search(r"^\s*Compiling hash-graph-types v\d", output, re.MULTILINE)
            data["calls"][-1]["transitive_crate_compilation_observed"] = True
            data.update({"positive_key": ready[0], "positive_source_addition": ADDITION.decode(),
                         "positive_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                         "positive_binary_sha256": helper["verified_payload_digest"](payload, ready[0])})
            assert data["positive_binary_sha256"] is not None and helper["executable"](binary)
            assert helper["file_digest"](binary) == data["positive_binary_sha256"]
            data["result"] = "positive-build-passed-awaiting-integration"
        else:
            key = data["baseline_key"]
            source = root / SOURCES["transitive"]
            positive_source = source.is_file() and source.read_bytes() == (state / "transitive.original").read_bytes() + ADDITION
            for label, relative in BACKUPS.items():
                original = (state / f"{label}.original").read_bytes()
                assert hashlib.sha256(original).hexdigest() == data["original_sources"][label]
                (root / relative).write_bytes(original)
                assert (root / relative).read_bytes() == original
            data["original_sources_restored"] = True
            with helper["build_lock"](root):
                positive_payload = bool(data.get("positive_key")) and helper["verified_payload_digest"](payload, data["positive_key"]) == data["positive_binary_sha256"]
                assert helper["verified_payload_digest"](state / "payload", key) == data["baseline_binary_sha256"]
                helper["write_payload"](payload, state / "payload/hash-graph", key)
                assert (payload / "manifest.json").read_bytes() == (state / "payload/manifest.json").read_bytes()
                binary.unlink(missing_ok=True)
                assert helper["fingerprint"](root, dict(os.environ))[0] == key
            require_hit("final-original-recovery", key)
            data.update({"positive_source_present_after_behavior": positive_source,
                         "positive_payload_present_after_behavior": positive_payload,
                         "startup_outcome": os.environ["PROOF_STARTUP_OUTCOME"],
                         "test_outcome": os.environ["PROOF_TEST_OUTCOME"]})
            shutil.rmtree(state)
            assert positive_source and positive_payload and data["startup_outcome"] == data["test_outcome"] == "success"
            assert data["unrelated_documentation"]["restored"] is True
            data["result"] = "passed"
    finally:
        if owned and mode == "prepare" and not data.get("armed"):
            shutil.rmtree(state)  # Source mutation starts only after verified backups.
        if owned and state.is_dir():
            (state / "state.json").write_text(json.dumps(data) + "\n")
        data["temporary_backup_removed"] = not state.exists()
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps(data, indent=2) + "\n")
        print(json.dumps(data))


if __name__ == "__main__":
    main()
