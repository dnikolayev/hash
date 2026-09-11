#!/usr/bin/env python3
"""Record one graph build's active Cargo units and compiler arguments."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import tomllib


HEX_STRING = re.compile(r'"((?:\\x[0-9a-f]{2})*)"')
COMPILER = re.compile(
    r"^(?:[A-Za-z0-9_]+-)*(?:rustc|gcc|g\+\+|cc|c\+\+|clang|clang\+\+|"
    r"cc1|cc1plus|collect2|as|ld|ld\.bfd|ld\.gold|ld\.lld|lld|rust-lld|"
    r"ar|gcc-ar|llvm-ar)(?:-\d+(?:\.\d+)*)?$"
)
NATIVE_COMPILER = re.compile(
    r"^(?:[A-Za-z0-9_]+-)*(?:gcc|g\+\+|cc|c\+\+|clang|clang\+\+)(?:-\d+(?:\.\d+)*)?$"
)


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def decode_exec(line):
    """Decode successful strace -xx exec calls; reject incomplete arguments."""
    if not line.startswith(("execve(", "execveat(")):
        assert "unfinished" not in line and "resumed>" not in line, "split-exec-record"
        return None
    assert "unfinished" not in line, "split-exec-record"
    if not re.search(r"\)\s+= 0$", line):
        assert re.search(r"\)\s+= -1 \w+", line), "unreadable-exec-result"
        return None
    match = re.search(r"(\[.*\]), (?:0x[0-9a-f]+|NULL) /\* \d+ vars? \*/", line)
    assert match, "unreadable-exec-arguments"
    vector = match[1]
    assert not HEX_STRING.sub("", vector).strip(" ,[]"), "truncated-exec-arguments"
    strings = HEX_STRING.findall(line[:match.start()])
    assert len(strings) == 1, "unreadable-exec-path"
    decode = lambda value: bytes.fromhex(value.replace("\\x", "")).decode("utf-8")
    executable = decode(strings[0])
    assert executable, "unresolved-execveat-path"
    return executable, [decode(value) for value in HEX_STRING.findall(vector)]


def main():
    root = Path.cwd().resolve()
    logs = root / "var/logs"
    logs.mkdir(parents=True, exist_ok=True)
    receipt = {"scope": "graph-native-compiler-diagnostic", "status": "incomplete"}
    try:
        helper = runpy.run_path(str(root / ".github/actions/graph-build-cache/graph_build_cache.py"))
        original = dict(os.environ)
        prepared = json.loads((root / "target/graph-build-inputs.json").read_text())
        assert original["HASH_GRAPH_BUILD_CACHE"] == "1", "cache-mode"
        assert original["DIAGNOSTIC_ACTIONS_CACHE_HIT"] != "true", "requires-actions-miss"
        assert not (root / "target/debug/hash-graph").exists(), "requires-absent-live-binary"
        key, env, inputs = helper["fingerprint"](root, original)
        assert prepared == {"key": key, "inputs": inputs}, "prepared-inputs-changed"
        assert helper["verified_payload_digest"](root / "target/graph-build-cache", key) is None, "requires-payload-miss"
        receipt.update({"prepared": prepared, "actions_cache_hit": False, "initial_live_binary": False})
        cpu_lines = sorted({line for line in Path("/proc/cpuinfo").read_text().splitlines()
                            if line.split(":", 1)[0].strip() in
                            {"model name", "flags", "Features", "CPU implementer", "CPU part"}})
        machine = {"platform": platform.platform(), "machine": platform.machine(),
                   "image": original.get("ImageVersion", ""), "root": str(root),
                   "os": sha(Path("/etc/os-release")),
                   "cpu": helper["digest"]("\n".join(cpu_lines).encode())}
        assert helper["digest"](helper["encoded"](machine)) == inputs["machine"], "machine-reconstruction-mismatch"
        receipt["machine"] = {**{name: value for name, value in machine.items() if name != "root"},
                              "root_sha256": helper["digest"](str(root).encode()), "cpu_lines": cpu_lines}

        aliases = sorted({
            str(root): "$WORKSPACE",
            original["RUNNER_TEMP"]: "$RUNNER_TEMP",
            env.get("CARGO_HOME", str(Path(env["HOME"]) / ".cargo")): "$CARGO_HOME",
            env.get("RUSTUP_HOME", str(Path(env["HOME"]) / ".rustup")): "$RUSTUP_HOME",
            "/opt/hostedtoolcache": "$TOOL_CACHE",
            env["HOME"]: "$HOME",
            "/tmp": "$TMP",
        }.items(), key=lambda item: -len(item[0]))

        def public(value, context=None):
            if isinstance(value, dict):
                return {name: public(item, "rustc-argv" if name == "argv"
                                    and Path(str(value.get("executable", ""))).name == "rustc" else None)
                        for name, item in value.items()}
            if isinstance(value, list):
                return [public(item, "extern" if context == "rustc-argv" and index > 0
                               and value[index - 1] == "--extern" else None)
                        for index, item in enumerate(value)]
            if isinstance(value, str):
                for path, alias in aliases:
                    value = value.replace(path, alias)
                scan = value
                if context == "extern":
                    library = re.fullmatch(r"(?:[A-Za-z_][A-Za-z0-9_]*:)*[A-Za-z_][A-Za-z0-9_]*=(\$WORKSPACE/target/[^\r\n]+\.(?:rlib|rmeta|so))", value)
                    if library:
                        scan = library[1]
                assert not re.search(r"https?://[^/\s]*@|(?:password|secret|authorization|token)=", scan, re.I), "unexpected-argument-value"
            return value

        metadata = json.loads(helper["run"](["cargo", "metadata", "--format-version=1", "--all-features"], root, env))
        assert helper["digest"](helper["encoded"](metadata)) == inputs["resolution"], "metadata-changed"
        graph = json.loads(helper["run"](helper["COMMAND"] + ["--unit-graph", "-Z", "unstable-options"], root, env))
        assert graph["version"] == 1 and len(graph["roots"]) == 1, "unexpected-unit-graph"
        units = graph["units"]
        graph_root = units[graph["roots"][0]]
        assert graph_root["target"]["name"] == "hash-graph" and graph_root["target"]["kind"] == ["bin"], "unexpected-graph-root"
        active, pending = set(), list(graph["roots"])
        while pending:
            index = pending.pop()
            if index not in active:
                active.add(index)
                pending.extend(dependency["index"] for dependency in units[index]["dependencies"])
        package_ids = {units[index]["pkg_id"] for index in active}
        packages = {package["id"]: package for package in metadata["packages"]}
        assert package_ids <= packages.keys(), "unmatched-unit-package"
        lock = tomllib.loads((root / "Cargo.lock").read_text())
        inventory = []
        for package_id in sorted(package_ids):
            package = packages[package_id]
            manifest = Path(package["manifest_path"])
            locked = [entry for entry in lock["package"] if entry["name"] == package["name"]
                      and entry["version"] == package["version"] and entry.get("source") == package["source"]]
            assert len(locked) == 1, "unmatched-lock-package"
            checksum_file = manifest.parent / ".cargo-checksum.json"
            active_sources = sorted({units[index]["target"]["src_path"] for index in active
                                     if units[index]["pkg_id"] == package_id})
            inventory.append({
                "id": package_id, "name": package["name"], "version": package["version"],
                "source": package["source"], "manifest": str(manifest), "manifest_sha256": sha(manifest),
                "lock_checksums": [entry.get("checksum") for entry in locked],
                "source_checksum_manifest_sha256": sha(checksum_file) if checksum_file.is_file() else None,
                "active_target_sources": [{"path": path, "sha256": sha(Path(path))} for path in active_sources],
            })
        projected = {"version": graph["version"], "roots": graph["roots"], "units": [
            {"index": index, **{name: units[index][name] for name in
                               ("pkg_id", "target", "profile", "platform", "mode", "features", "dependencies")}}
            for index in sorted(active)
        ], "packages": inventory}
        receipt["unit_graph_sha256"] = helper["digest"](helper["encoded"](graph))
        (logs / "graph-native-units.json").write_text(json.dumps(public(projected), indent=2) + "\n")
        assert helper["fingerprint"](root, original)[0] == key, "inventory-changed-inputs"

        tracer = shutil.which("strace")
        assert tracer, "strace-unavailable"
        receipt["strace_version"] = subprocess.check_output([tracer, "-V"], text=True).splitlines()[0]
        with tempfile.TemporaryDirectory(prefix="graph-native-diagnostic-", dir=original["RUNNER_TEMP"]) as directory:
            temporary = Path(directory)
            options = [tracer, "-ff", "-qq", "-xx", "-s", "65535", "-e", "trace=execve,execveat"]
            preflight = subprocess.run(options + ["-o", str(temporary / "preflight"), "--", "/usr/bin/true"],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            assert preflight.returncode == 0, "strace-preflight-failed"
            assert any(decode_exec(line) for path in temporary.glob("preflight.*") for line in path.read_text().splitlines()), "strace-preflight-empty"
            compile_log = temporary / "compile.log"
            with compile_log.open("wb") as output:
                result = subprocess.run(options + ["-o", str(temporary / "exec"), "--", sys.executable,
                    str(root / ".github/actions/graph-build-cache/graph_build_cache.py"), "compile"],
                    cwd=root, env=original, stdout=output, stderr=subprocess.STDOUT)
            phases, compiling = [], []
            for line in compile_log.read_text(errors="replace").splitlines():
                if line.startswith("{"):
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if "phase" in event:
                        phases.append({name: event[name] for name in ("phase", "status", "key", "reason") if name in event})
                matched = re.match(r"\s*Compiling ([A-Za-z0-9_-]+) v([^\s]+)", line)
                if matched:
                    compiling.append({"name": matched[1], "version": matched[2]})
            receipt.update({"compile_exit_code": result.returncode, "phases": phases, "compiling": compiling})
            records, successful_execs, executable_hashes, unrecorded, decode_failures = [], 0, {}, {}, []
            try:
                for trace_index, trace in enumerate(sorted(temporary.glob("exec.*"))):
                    for line_number, line in enumerate(trace.read_text().splitlines(), 1):
                        try:
                            decoded = decode_exec(line)
                        except (AssertionError, UnicodeError, ValueError) as error:
                            decode_failures.append({"trace_index": trace_index, "line_number": line_number,
                                                    "reason": str(error) if isinstance(error, AssertionError) else type(error).__name__})
                            continue
                        if decoded is None:
                            continue
                        successful_execs += 1
                        executable, argv = decoded
                        if not COMPILER.fullmatch(Path(executable).name):
                            name = Path(executable).name
                            unrecorded[name] = unrecorded.get(name, 0) + 1
                            continue
                        if executable not in executable_hashes:
                            executable_hashes[executable] = sha(Path(executable).resolve())
                        unresolved = [index for index, argument in enumerate(argv[1:], 1)
                                      if argument.startswith("@") or (argument.startswith("-Wl,")
                                      and any(part.startswith("@") for part in argument.split(",")[1:]))]
                        records.append({"executable": executable, "executable_sha256": executable_hashes[executable],
                                        "argv": argv, "unresolved_response_argument_indices": unresolved})
            finally:
                unresolved_count = sum(len(record["unresolved_response_argument_indices"]) for record in records)
                receipt.update({"successful_execs": successful_execs, "compiler_execs": len(records),
                                "native_compile_execs": sum(bool(NATIVE_COMPILER.fullmatch(Path(record["executable"]).name))
                                                            and "-c" in record["argv"] for record in records)})
                receipt["coverage"] = {"successful_exec_records": "partial" if decode_failures or sys.exc_info()[0] else "decoded",
                                       "decode_failures": decode_failures, "unresolved_response_arguments": unresolved_count,
                                       "compiler_response_files": "unresolved" if unresolved_count else "none",
                                       "selection": "known compiler executable names", "unrecorded_exec_names": unrecorded}
                (logs / "graph-native-compiler-argv.json").write_text(json.dumps(public(records), indent=2) + "\n")
            assert not decode_failures, "incomplete-exec-decoding"
            assert not unresolved_count, "compiler-response-file-unresolved"
            assert result.returncode == 0, "graph-compile-failed"
            for phase, status in [("restore", "miss"), ("build", "success"), ("store", "ready")]:
                assert any(event.get("phase") == phase and event.get("status") == status and event.get("key") == key for event in phases), "missing-compile-phase"
            assert any("--crate-name" in record["argv"] and record["argv"][record["argv"].index("--crate-name") + 1] == "hash_graph"
                       and "bin" in record["argv"] for record in records), "missing-graph-rustc"
            assert receipt["native_compile_execs"] > 0, "missing-native-compiler"
            assert helper["fingerprint"](root, original)[0] == key, "compile-changed-inputs"
            receipt["binary_sha256"] = helper["verified_payload_digest"](root / "target/graph-build-cache", key)
            assert receipt["binary_sha256"], "invalid-produced-payload"
            receipt["status"] = "compiler-evidence-complete"
    except Exception as error:
        receipt["failure"] = str(error) if isinstance(error, AssertionError) else type(error).__name__
        return 1
    finally:
        (logs / "graph-native-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps({name: receipt[name] for name in ("scope", "status", "failure") if name in receipt}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
