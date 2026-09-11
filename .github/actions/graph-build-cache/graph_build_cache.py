#!/usr/bin/env python3
"""Opt-in reuse of the integration graph executable, never service or test state."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib


COMMAND = ["cargo", "build", "--bin", "hash-graph", "--all-features"]
WRAPPER = "python3 ../../.github/actions/graph-build-cache/graph_build_cache.py compile"
OUTPUT_DIRS = {"target", "node_modules", ".git", ".turbo", "__pycache__"}
# wasm-pack writes this JavaScript consumer output during service startup.
WASM_OUTPUT = Path("libs/@blockprotocol/type-system/rust/pkg")
BUILD_ENV = re.compile(
    r"^(CARGO|RUST|CC|CXX|AR|AS|LD|CFLAGS|CXXFLAGS|CPPFLAGS|LDFLAGS|"
    r"HOST_|TARGET_|PKG_CONFIG|OPENSSL|ZSTD|LIBZ|CMAKE|BINDGEN|LLVM|LIBCLANG|"
    r"PROTOC|LIBRARY_PATH|CPATH|C_INCLUDE_PATH|CPLUS_INCLUDE_PATH|SCCACHE_(SERVER_PORT|RECACHE|DISABLE)$)"
)
TOOL_ENV = re.compile(r"^(CC|CXX|AR|AS|LD)(_|$)|^RUSTC(?:_WRAPPER|_WORKSPACE_WRAPPER)?$|^RUSTDOC$|_LINKER$")
EXTERNAL_ENV = re.compile(r"^(OPENSSL|ZSTD|LIBZ|CMAKE|BINDGEN|LLVM|LIBCLANG|PKG_CONFIG|"
                          r"LD_LIBRARY_PATH|LIBRARY_PATH|CPATH|C_INCLUDE_PATH|CPLUS_INCLUDE_PATH|DEP_|SCCACHE_(RECACHE|DISABLE)$)")


class NoReuse(Exception):
    """The normal compiler remains the fallback for unsupported configurations."""


def failure_reason(error: Exception) -> str:
    # Only locally assigned reason codes are printable, never exception messages.
    if isinstance(error, NoReuse):
        return error.args[0] if error.args else "unsupported"
    if isinstance(error, OSError):
        return "filesystem-error"
    if isinstance(error, KeyError):
        return "missing-field"
    if isinstance(error, ValueError):
        return "invalid-data"
    return "subprocess-failed"


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def event(phase: str, status: str, started: float, key: str = "", reason: str = "") -> None:
    print(json.dumps({"phase": phase, "status": status, "key": key,
                      **({"reason": reason} if reason else {}),
                      "seconds": round(time.monotonic() - started, 3)}), file=sys.stderr)


def run(args: list[str], root: Path, env: dict[str, str]) -> bytes:
    try:
        return subprocess.run(args, cwd=root / "apps/hash-graph", env=env,
                              check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE).stdout
    except (OSError, subprocess.SubprocessError) as error:
        reason = ("metadata-command-failed" if "metadata" in args else
                  "sysroot-query-failed" if "sysroot" in args else "tool-version-failed")
        raise NoReuse(reason) from error


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def compiler_environment(root: Path, original: dict[str, str]) -> tuple[dict[str, str], str]:
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "aarch64"}:
        raise NoReuse("unsupported-platform")
    if any(original.get(name) for name in (
        "CARGO_TARGET_DIR", "CARGO_BUILD_TARGET_DIR", "CARGO_BUILD_TARGET",
        "CARGO_BUILD_BUILD_DIR", "CARGO_BUILD_PROFILE",
    )) or any(name.startswith("CARGO_PROFILE_") for name in original):
        raise NoReuse("custom-target-or-profile")
    # Custom flags can reference external include files, linkers or codegen backends.
    # Those inputs belong to Cargo's normal path until explicitly modeled here.
    if any(value and "FLAGS" in name and BUILD_ENV.match(name)
           and name != "CARGO_MAKEFLAGS" for name, value in original.items()):
        raise NoReuse("custom-build-flags")
    if any(value and EXTERNAL_ENV.match(name) for name, value in original.items()):
        raise NoReuse("external-input-or-cache-control-env")

    env = {name: value for name, value in original.items() if BUILD_ENV.match(name)}
    env.update({"HOME": original["HOME"], "PATH": original["PATH"],
                "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TMPDIR": "/tmp"})
    # Use real tool directories, not Yarn's per-invocation temporary PATH entries.
    # Tool-manager shims may need their setup environment to locate the pinned tool.
    # Discovery is read-only; compilation and version checks use the isolated environment.
    sysroot = Path(run(["rustc", "--print", "sysroot"], root, original).decode().strip())
    tools = {"rustc": sysroot / "bin/rustc", "cargo": sysroot / "bin/cargo"}
    for name in ("protoc", "cc", "c++", "ld", "ar", "as", "pkg-config", "cmake", "make"):
        resolved = shutil.which(original.get("PROTOC") or name, path=original["PATH"]) if name == "protoc" else shutil.which(name, path=original["PATH"])
        if resolved:
            tools[name] = Path(resolved).resolve()
        elif name in {"protoc", "cc", "c++", "ld"}:
            raise NoReuse("required-tool-missing")
    for name, value in env.items():
        if value and (TOOL_ENV.search(name) or name == "PROTOC"):
            words = shlex.split(value)
            # Wrapper commands with arguments need their own input model.
            if len(words) != 1:
                raise NoReuse("tool-env-command-arguments")
            resolved = shutil.which(words[0], path=original["PATH"])
            if not resolved:
                raise NoReuse("tool-env-command-missing")
            tools[name] = Path(resolved).resolve()
            env[name] = str(tools[name])
    env["PATH"] = os.pathsep.join(dict.fromkeys(
        [str(sysroot / "bin"), *(str(path.parent) for path in tools.values()),
         "/usr/local/bin", "/usr/bin", "/bin"]
    ))
    # Only fixed compiler commands are probed. Environment-selected tools are
    # identified by their bytes without executing them for cache inspection.
    versions = {
        "rustc": digest(run(["rustc", "-vV"], root, env)),
        "cargo": digest(run(["cargo", "--version"], root, env)),
    }
    identities = []
    for name, path in sorted(tools.items()):
        if not path.is_file():
            raise NoReuse("tool-file-missing")
        identities.append([name, str(path), file_digest(path), versions.get(name)])
    return env, digest(encoded(identities))


def cargo_configs(root: Path, env: dict[str, str]) -> list[Path]:
    paths = {Path(env.get("CARGO_HOME", str(Path(env["HOME"]) / ".cargo"))) / name
             for name in ("config", "config.toml")}
    for directory in [root / "apps/hash-graph", *(root / "apps/hash-graph").parents]:
        paths.update(directory / ".cargo" / name for name in ("config", "config.toml"))
    for path in paths:
        if path.is_file():
            config = tomllib.loads(path.read_text())
            if any(name in config for name in ("include", "env", "source", "patch")) or any(
                name in config.get("build", {}) for name in
                ("target", "target-dir", "build-dir", "rustflags", "rustc", "rustc-wrapper", "rustc-workspace-wrapper")
            ):
                raise NoReuse("custom-cargo-config")
            for target in config.get("target", {}).values():
                if set(target) - {"rustflags"} or target.get("rustflags", []) != ["-Ctarget-cpu=x86-64-v3"]:
                    raise NoReuse("custom-cargo-target-config")
    return sorted(paths)


def source_digest(root: Path, packages: list[str], configs: list[Path]) -> str:
    tracked = subprocess.check_output(["git", "ls-files", "--cached", "-z"], cwd=root)
    paths = {root / os.fsdecode(name) for name in tracked.split(b"\0") if name}
    def add_directory(directory: Path) -> None:
        for parent, dirs, files in os.walk(directory, followlinks=False):
            dirs[:] = sorted(name for name in dirs if name not in OUTPUT_DIRS
                             and Path(parent) / name != root / WASM_OUTPUT)
            paths.update(Path(parent) / name for name in dirs if (Path(parent) / name).is_symlink())
            paths.update(Path(parent) / name for name in files)

    # Retain absent tracked paths in the digest: pruning and deletion both matter.
    # Cargo can also read generated or ignored files inside local package directories.
    for manifest in packages:
        directory = Path(manifest).parent
        if not directory.is_relative_to(root):
            raise NoReuse("external-source-package")
        add_directory(directory)
    paths.update(configs)
    links = {}
    while True:
        pending = [path for path in paths if path.is_symlink() and path not in links]
        if not pending:
            break
        for path in pending:
            try:
                target = path.resolve()
            except RuntimeError as error:
                raise NoReuse("source-symlink-loop") from error
            if not target.is_relative_to(root):
                raise NoReuse("external-source-symlink")
            links[path] = [os.readlink(path), str(target.relative_to(root))]
            if target.is_dir():
                add_directory(target)
            else:
                paths.add(target)
    hasher = hashlib.sha256()
    for path in sorted(paths):
        name = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
        content = links[path] if path in links else file_digest(path) if path.is_file() else "absent"
        hasher.update(encoded([name, content]))
    return hasher.hexdigest()


def machine_digest(root: Path, original: dict[str, str]) -> str:
    machine = {"platform": platform.platform(), "machine": platform.machine(),
               "image": original.get("ImageVersion", ""), "root": str(root),
               "os": file_digest(Path("/etc/os-release"))}
    # /proc/cpuinfo includes sampled clocks; only model and capabilities are inputs.
    machine["cpu"] = digest("\n".join(sorted({line for line in Path("/proc/cpuinfo").read_text().splitlines()
                                              if line.split(":", 1)[0].strip() in
                                              {"model name", "flags", "Features", "CPU implementer", "CPU part"}})).encode())
    return digest(encoded(machine))


def fingerprint(root: Path, original: dict[str, str]) -> tuple[str, dict[str, str], dict]:
    env, tools = compiler_environment(root, original)
    configs = cargo_configs(root, env)
    # Resolve the pruned workspace before hashing: Cargo may update its copied lockfile.
    metadata = json.loads(run(["cargo", "metadata", "--format-version=1", "--all-features"],
                              root, env))
    if Path(metadata["target_directory"]).resolve() != root / "target":
        raise NoReuse("custom-metadata-target")
    packages = sorted(package["manifest_path"] for package in metadata["packages"]
                      if package["source"] is None)
    inputs = {"schema": 1, "command": COMMAND, "sources": source_digest(root, packages, configs),
              "resolution": digest(encoded(metadata)), "environment": digest(encoded(env)),
              "tools": tools, "machine": machine_digest(root, original)}
    key = "graph-build-v1-" + digest(encoded(inputs))
    return key, env, inputs


def regular(path: Path) -> bool:
    return path.exists() and not path.is_symlink() and stat.S_ISREG(path.stat().st_mode)


def executable(path: Path) -> bool:
    if not regular(path) or not path.stat().st_mode & 0o111:
        return False
    with path.open("rb") as stream:
        header = stream.read(20)
    machine = {"x86_64": 62, "aarch64": 183}.get(platform.machine())
    return (len(header) == 20 and header[:6] == b"\x7fELF\x02\x01"
            and int.from_bytes(header[18:20], "little") == machine)


def verified_payload_digest(payload: Path, key: str) -> str | None:
    try:
        if payload.is_symlink() or not regular(payload / "manifest.json"):
            return None
        manifest = json.loads((payload / "manifest.json").read_text())
        binary = payload / "hash-graph"
        if (set(manifest) == {"key", "sha256"} and manifest["key"] == key
                and executable(binary) and manifest["sha256"] == file_digest(binary)):
            return manifest["sha256"]
    except (OSError, ValueError, TypeError):
        return None
    return None


def write_payload(payload: Path, binary: Path, key: str) -> None:
    if payload.is_symlink() or any((payload / name).is_symlink()
                                    for name in ("hash-graph", "manifest.json")):
        raise NoReuse("payload-symlink")
    payload.mkdir(parents=True, exist_ok=True)
    copy_binary(binary, payload / "hash-graph")
    (payload / "manifest.json").write_text(json.dumps(
        {"key": key, "sha256": file_digest(payload / "hash-graph")}
    ) + "\n")


def copy_binary(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".hash-graph-", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def compile_graph(root: Path, original: dict[str, str]) -> int:
    mode = original.get("HASH_GRAPH_BUILD_CACHE")
    if mode not in {"1", "baseline"}:
        return subprocess.run(COMMAND, cwd=root / "apps/hash-graph", env=original).returncode
    started = time.monotonic()
    try:
        key, env, _ = fingerprint(root, original)
    except (NoReuse, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        event("prepare", "unsupported", started, reason=failure_reason(error))
        return subprocess.run(COMMAND, cwd=root / "apps/hash-graph", env=original).returncode
    event("prepare", "ready", started, key)
    payload = root / "target/graph-build-cache"
    binary = root / "target/debug/hash-graph"
    started = time.monotonic()
    payload_hash = verified_payload_digest(payload, key) if mode == "1" else None
    if payload_hash is not None:
        if not executable(binary) or file_digest(binary) != payload_hash:
            copy_binary(payload / "hash-graph", binary)
        event("restore", "hit", started, key)
        return 0
    if mode == "1":
        event("restore", "miss", started, key)
        # Force a rejected restored output to be recreated by Cargo.
        binary.unlink(missing_ok=True)
    started = time.monotonic()
    result = subprocess.run(COMMAND, cwd=root / "apps/hash-graph", env=env)
    event("build", "success" if result.returncode == 0 else "failed", started, key)
    if result.returncode or mode == "baseline":
        return result.returncode
    started = time.monotonic()
    if not executable(binary):
        event("reject", "invalid-output", started, key)
        return 1
    try:
        after, _, _ = fingerprint(root, original)
        if after == key:
            write_payload(payload, binary, key)
            event("store", "ready", started, key)
        else:
            event("reject", "inputs-or-output-changed", started, key)
    except (NoReuse, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        event("reject", "unsupported", started, key, failure_reason(error))
    return 0


def prepare(root: Path, original: dict[str, str]) -> str:
    started = time.monotonic()
    key = ""
    package = root / "apps/hash-graph/package.json"
    previous = None
    try:
        if original.get("HASH_GRAPH_BUILD_CACHE") not in {"1", "baseline"}:
            raise NoReuse("disabled")
        package_before = package.read_bytes()
        content = json.loads(package_before)
        if content["scripts"]["compile"] not in {" ".join(COMMAND), WRAPPER}:
            raise NoReuse("custom-compile-command")
        previous = package_before
        content["scripts"]["compile"] = WRAPPER
        package.write_text(json.dumps(content, indent=2) + "\n")
        key, _, inputs = fingerprint(root, original)
        target = root / "target"
        target.mkdir(exist_ok=True)
        (target / "graph-build-inputs.json").write_text(json.dumps(
            {"key": key, "inputs": inputs}, sort_keys=True
        ) + "\n")
        event("prepare", "ready", started, key)
    except (NoReuse, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        key = ""
        if previous is not None:
            content = json.loads(previous)
            if content["scripts"]["compile"] == WRAPPER:
                content["scripts"]["compile"] = " ".join(COMMAND)
                previous = (json.dumps(content, indent=2) + "\n").encode()
            package.write_bytes(previous)
        event("prepare", "unsupported", started, reason=failure_reason(error))
    return key


def main() -> int:
    root = Path(__file__).resolve().parents[3]
    if len(sys.argv) != 2 or sys.argv[1] not in {"prepare", "compile"}:
        raise SystemExit("Usage: graph_build_cache.py prepare|compile")
    original = dict(os.environ)
    if sys.argv[1] == "compile":
        return compile_graph(root, original)
    key = prepare(root, original)
    if original.get("GITHUB_OUTPUT"):
        with open(original["GITHUB_OUTPUT"], "a") as stream:
            stream.write(f"key={key}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
