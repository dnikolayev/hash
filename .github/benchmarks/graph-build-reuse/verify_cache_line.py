#!/usr/bin/env python3
"""Compare the graph cache probe with its upstream detector on each allowed CPU."""
import hashlib
import json
import os
from pathlib import Path
import platform
import runpy
import shutil
import subprocess
import tarfile
import tempfile
import tomllib


PACKAGES = {
    "yep-cache-line-size": ("0.9.3", "59dea97e5518f64cbb9468f2cd1ac59c9d0186eeb5ee6198d4bb8adfa1465499"),
    "raw-cpuid": ("11.6.0", "498cd0dc59d73224351ee52a95fee0f1a617a2eae0e7d9d720cc622c73a54186"),
    "bitflags": ("2.13.0", "b4388bee8683e3d04af747c73422af53102d2bd24d9eadb6cbc100baef4b43f8"),
}
REFERENCE = r'''
use yep_cache_line_size::{get_cache_line_size, CacheInfoError, CacheLevel, CacheType};
fn main() {
    match get_cache_line_size(CacheLevel::L1, CacheType::Data) {
        Ok(bytes) => println!("{{\"status\":\"ok\",\"bytes\":{bytes}}}"),
        Err(error) => {
            let status = match error {
                CacheInfoError::Unsupported => "unsupported",
                CacheInfoError::NotPresent => "not-present",
                CacheInfoError::InvalidValue => "invalid-value",
            };
            println!("{{\"status\":\"{status}\"}}");
        }
    }
}
'''


class VerificationError(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise VerificationError(reason)


def main():
    root = Path.cwd()
    receipt = {"schema": 1, "scope": "cache-line-detector-verification",
               "source_sha": os.environ["BENCHMARK_SOURCE_SHA"],
               "workflow_sha": os.environ["BENCHMARK_WORKFLOW_SHA"],
               "run_id": os.environ["GITHUB_RUN_ID"],
               "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
               "probe": os.environ["DIAGNOSTIC_PROBE"],
               "platform": platform.platform(), "machine": platform.machine(),
               "image_version": os.environ.get("ImageVersion", ""),
               "stages": [], "per_cpu": [], "passed": False}
    temporary = None
    helper = prepared = cpus = None

    def command(stage, argv, cwd, env, pin=None, timeout=120):
        entry = {"stage": stage}
        receipt["stages"].append(entry)
        try:
            result = subprocess.run(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=timeout, preexec_fn=pin)
            entry["returncode"] = result.returncode
            require(result.returncode == 0, "command-failed")
            return result.stdout
        except Exception as error:
            entry["error_type"] = type(error).__name__
            raise

    try:
        helper = runpy.run_path(str(root / ".github/actions/graph-build-cache/graph_build_cache.py"))
        prepared = json.loads((root / "target/graph-build-inputs.json").read_text())
        receipt.update({"prepared_key": prepared["key"], "inputs": prepared["inputs"]})
        require(prepared["key"] == os.environ["DIAGNOSTIC_PREPARED_KEY"], "prepared-key-mismatch")
        key, env, inputs = helper["fingerprint"](root, dict(os.environ))
        require(key == prepared["key"] and inputs == prepared["inputs"], "initial-input-drift")
        receipt["initial_inputs_match"] = True
        expected = inputs["cpu_cache_line"]
        require(type(expected) is int and 0 < expected <= 131_072, "invalid-prepared-cache-line")
        cpus = os.sched_getaffinity(0)
        require(bool(cpus), "empty-affinity")
        receipt["allowed_cpus"] = sorted(cpus)
        cpu_lines = sorted({line for line in Path("/proc/cpuinfo").read_text().splitlines()
                            if line.split(":", 1)[0].strip() in
                            {"model name", "flags", "Features", "CPU implementer", "CPU part"}})
        receipt["cpu_lines"] = cpu_lines
        receipt["cpu_lines_sha256"] = hashlib.sha256("\n".join(cpu_lines).encode()).hexdigest()
        tool_directory = Path(env["PATH"].split(os.pathsep)[0])
        rustc, cargo = str(tool_directory / "rustc"), str(tool_directory / "cargo")
        probe_flags = ["--edition=2021"]
        if platform.machine() == "x86_64":
            probe_flags.append("-Ctarget-cpu=x86-64")
        receipt["probe_compiler_flags"] = probe_flags
        source = root / ".github/actions/graph-build-cache/cache_line_probe.rs"
        receipt["probe_source_sha256"] = helper["file_digest"](source)
        receipt["reference_source_sha256"] = hashlib.sha256(REFERENCE.encode()).hexdigest()
        original_lock = root / "Cargo.lock"
        receipt["original_lock_sha256"] = helper["file_digest"](original_lock)
        locked = tomllib.loads(original_lock.read_text())["package"]
        for name, (version, checksum) in PACKAGES.items():
            require(any(p["name"] == name and p["version"] == version
                        and p.get("checksum") == checksum for p in locked), "graph-lock-pin-mismatch")

        with tempfile.TemporaryDirectory(prefix="graph-cache-line-verification-",
                                         dir=os.environ["RUNNER_TEMP"]) as directory:
            temporary = Path(directory)
            shutil.copyfile(original_lock, temporary / "Cargo.lock")
            (temporary / "Cargo.toml").write_text(
                '[package]\nname = "cache-line-reference"\nversion = "0.0.0"\nedition = "2024"\n'
                '[workspace]\n[dependencies]\n'
                'yep-cache-line-size = "=0.9.3"\nraw-cpuid = "=11.6.0"\n'
                'bitflags = { version = "=2.13.0", features = ["std"] }\n')
            (temporary / "src").mkdir()
            (temporary / "src/main.rs").write_text(REFERENCE)
            metadata = json.loads(command("reference-metadata", [cargo, "metadata", "--offline",
                "--format-version=1"], temporary, env))
            require(Path(metadata["workspace_root"]).resolve() == temporary.resolve()
                    and Path(metadata["target_directory"]).resolve() == temporary / "target",
                    "reference-output-outside-temporary-directory")
            receipt["reference_packages"] = []
            for name, (version, checksum) in PACKAGES.items():
                matches = [p for p in metadata["packages"] if p["name"] == name and p["version"] == version]
                require(len(matches) == 1, "reference-resolution-mismatch")
                features = next(n["features"] for n in metadata["resolve"]["nodes"]
                                if n["id"] == matches[0]["id"])
                require(features == (["std"] if name == "bitflags" else []), "reference-features-mismatch")
                package = Path(matches[0]["manifest_path"]).parent
                archive = package.parents[2] / "cache" / package.parent.name / f"{name}-{version}.crate"
                require(helper["file_digest"](archive) == checksum, "archive-checksum-mismatch")
                files = 0
                with tarfile.open(archive, "r:gz") as members:
                    for member in members:
                        if member.isfile():
                            relative = Path(member.name).relative_to(f"{name}-{version}")
                            require(".." not in relative.parts, "invalid-archive-path")
                            require((package / relative).read_bytes() == members.extractfile(member).read(),
                                    "registry-source-mismatch")
                            files += 1
                receipt["reference_packages"].append(
                    {"name": name, "version": version, "features": features,
                     "checksum": checksum, "verified_files": files})
            command("probe-test-compile", [rustc, *probe_flags, "--test", str(source), "-o", str(temporary / "tests")],
                    temporary, env, timeout=30)
            command("probe-tests", [str(temporary / "tests")], temporary, env, timeout=30)
            receipt["probe_tests_passed"] = True
            command("probe-compile", [rustc, *probe_flags, str(source), "-o", str(temporary / "probe")],
                    temporary, env, timeout=30)
            command("reference-compile", [cargo, "build", "--offline"], temporary, env)
            binaries = {"probe": temporary / "probe", "reference": temporary / "target/debug/cache-line-reference"}
            receipt["binary_sha256"] = {name: helper["file_digest"](path) for name, path in binaries.items()}
            for cpu in sorted(cpus):
                def pin_child():
                    os.sched_setaffinity(0, {cpu})
                    require(os.sched_getaffinity(0) == {cpu}, "child-affinity-mismatch")
                entry = {"cpu": cpu}
                receipt["per_cpu"].append(entry)
                for name, binary in binaries.items():
                    entry[name] = json.loads(command(f"{name}-cpu-{cpu}", [str(binary)], temporary,
                                                     env, pin=pin_child, timeout=2))
                require(entry["probe"] == entry["reference"] == {"status": "ok", "bytes": expected},
                        "detector-mismatch")
        receipt["detectors_match_prepared"] = True
    except Exception as error:
        receipt["error_type"] = type(error).__name__
        if isinstance(error, VerificationError):
            receipt["reason"] = str(error)
    finally:
        receipt["temporary_files_removed"] = temporary is None or not temporary.exists()
        try:
            receipt["parent_affinity_unchanged"] = cpus is not None and os.sched_getaffinity(0) == cpus
        except OSError:
            receipt["parent_affinity_unchanged"] = False
        if helper is not None and prepared is not None:
            try:
                key, _, inputs = helper["fingerprint"](root, dict(os.environ))
                receipt["final_inputs_match"] = key == prepared["key"] and inputs == prepared["inputs"]
            except Exception as error:
                receipt["final_fingerprint_error_type"] = type(error).__name__
        receipt["passed"] = "error_type" not in receipt and all(receipt.get(field) is True for field in (
            "initial_inputs_match", "probe_tests_passed", "detectors_match_prepared",
            "temporary_files_removed", "parent_affinity_unchanged", "final_inputs_match"))
        path = root / "var/logs/graph-cache-line-verification.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt))
    return 0 if receipt["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
