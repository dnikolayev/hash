#!/usr/bin/env python3
"""Hermetic graph-output reuse checks; never compile Rust or launch services."""

from __future__ import annotations

import contextlib
import errno
import io
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import graph_build_cache as cache


def binary(path: Path, content: bytes = b"compiled") -> None:
    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    header[18:20] = (62).to_bytes(2, "little")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + content)
    path.chmod(0o755)


class GraphBuildCache(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.env = {"HASH_GRAPH_BUILD_CACHE": "1", "HOME": str(self.root / "home"), "PATH": "/usr/bin"}
        self.isolated = {"HOME": self.env["HOME"], "PATH": "/usr/bin", "LANG": "C.UTF-8"}
        self.files = ["Cargo.toml", "Cargo.lock", ".cargo/config.toml", "rust-toolchain.toml",
                      "apps/hash-graph/package.json", "apps/hash-graph/Cargo.toml", "apps/hash-graph/src/main.rs",
                      "libs/dependency/Cargo.toml", "libs/dependency/src/lib.rs",
                      "libs/dependency/migrations/up.sql", "web/source.ts"]
        for name in self.files:
            self.write(name, b"input\n")
        self.write(".cargo/config.toml", b"[target.'cfg(target_arch = \"x86_64\")']\nrustflags = [\"-Ctarget-cpu=x86-64-v3\"]\n")
        self.write("apps/hash-graph/package.json", json.dumps({"scripts": {"compile": " ".join(cache.COMMAND)}}).encode())
        self.metadata = {"target_directory": str(self.root / "target"), "resolve": {"nodes": ["locked-dependency"]},
                         "packages": [{"source": None, "manifest_path": str(self.root / name)}
                                      for name in ("apps/hash-graph/Cargo.toml", "libs/dependency/Cargo.toml")]}
        self.tracked = b"\0".join(name.encode() for name in self.files) + b"\0"
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(cache.platform, "machine", return_value="x86_64"))
        self.stack.enter_context(patch.object(cache, "compiler_environment", return_value=(self.isolated, "tools-v1")))
        self.stack.enter_context(patch.object(cache, "machine_digest", side_effect=lambda root, env: cache.digest((str(root) + env.get("ImageVersion", "")).encode())))
        self.stack.enter_context(patch.object(cache.subprocess, "check_output", return_value=self.tracked))
        self.metadata_command = self.stack.enter_context(patch.object(cache, "run", side_effect=lambda *args: json.dumps(self.metadata).encode()))
        self.stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
        self.output = self.root / "target/debug/hash-graph"
        self.payload = self.root / "target/graph-build-cache"

    def write(self, name: str, value: bytes) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        return path

    def key(self) -> str:
        return cache.fingerprint(self.root, self.env)[0]

    def compile(self, code: int = 0, mutation=None, produce: bool = True, content: bytes = b"compiled"):
        def build(command, *, cwd, env):
            self.assertEqual(command, cache.COMMAND)
            self.assertEqual(cwd, self.root / "apps/hash-graph")
            if mutation:
                mutation()
            if not code and produce:
                binary(self.output, content)
            return subprocess.CompletedProcess(command, code)
        return patch.object(cache.subprocess, "run", side_effect=build)

    def test_exact_hit_restores_only_binary_without_compiling(self) -> None:
        with self.compile() as build:
            self.assertEqual(cache.compile_graph(self.root, self.env), 0)
            self.assertEqual(build.call_count, 1)
        self.assertEqual(sorted(path.name for path in self.payload.iterdir()), ["hash-graph", "manifest.json"])
        self.output.unlink()
        with self.compile() as build:
            self.assertEqual(cache.compile_graph(self.root, self.env), 0)
            build.assert_not_called()
        self.assertEqual(self.output.read_bytes(), (self.payload / "hash-graph").read_bytes())
        self.assertTrue(self.output.samefile(self.payload / "hash-graph"))
        inode = self.output.stat().st_ino
        with self.compile() as build, patch.object(cache, "file_digest", wraps=cache.file_digest) as hashes:
            self.assertEqual(cache.compile_graph(self.root, self.env), 0)
            build.assert_not_called()
        binary_hashes = [call.args[0] for call in hashes.call_args_list
                         if call.args[0] in {self.output, self.payload / "hash-graph"}]
        self.assertEqual(binary_hashes, [self.payload / "hash-graph"])
        self.assertEqual(inode, self.output.stat().st_ino)

    @unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(), "requires POSIX processes")
    def test_concurrent_compilers_and_fallback_share_one_lock(self) -> None:
        context = multiprocessing.get_context("fork")
        fingerprint = cache.fingerprint
        for fallback in (False, True):
            with self.subTest(fallback=fallback):
                shutil.rmtree(self.payload, ignore_errors=True)
                entered = context.Event()
                release = context.Event()
                second_started = context.Event()
                overlapping_build = context.Event()
                builds = context.Value("i", 0)

                def inspect(root, env):
                    if env.get("UNSUPPORTED"):
                        raise cache.NoReuse("synthetic-unsupported")
                    return fingerprint(root, env)

                def build(command, *, cwd, env):
                    with builds.get_lock():
                        builds.value += 1
                        number = builds.value
                    if number > 1 and not release.is_set():
                        overlapping_build.set()
                    entered.set()
                    if not release.wait(10):
                        raise AssertionError("compiler was not released")
                    binary(self.output)
                    return subprocess.CompletedProcess(command, 0)

                def worker(second):
                    env = dict(self.env)
                    if second:
                        second_started.set()
                        if fallback:
                            env["UNSUPPORTED"] = "1"
                    if cache.compile_graph(self.root, env) != 0:
                        raise AssertionError("compiler failed")

                processes = [context.Process(target=worker, args=(second,)) for second in (False, True)]
                try:
                    with patch.object(cache, "fingerprint", side_effect=inspect), patch.object(cache.subprocess, "run", side_effect=build):
                        processes[0].start()
                        self.assertTrue(entered.wait(5))
                        processes[1].start()
                        self.assertTrue(second_started.wait(5))
                        self.assertFalse(overlapping_build.wait(0.2))
                        release.set()
                        for process in processes:
                            process.join(10)
                            self.assertEqual(process.exitcode, 0)
                    self.assertEqual(builds.value, 2 if fallback else 1)
                    self.assertFalse(overlapping_build.is_set())
                finally:
                    release.set()
                    for process in processes:
                        if process.is_alive():
                            process.terminate()
                        if process.pid is not None:
                            process.join(5)

    def test_changed_inputs_and_cargo_replacement_preserve_old_payload_inode(self) -> None:
        with self.compile():
            self.assertEqual(cache.compile_graph(self.root, self.env), 0)
        old_key = self.key()
        old_binary = self.payload / "hash-graph"
        old_content = old_binary.read_bytes()
        retained = self.root / "retained-graph"
        os.link(old_binary, retained)
        self.write("apps/hash-graph/src/main.rs", b"changed input")
        with self.compile(content=b"changed executable"):
            self.assertEqual(cache.compile_graph(self.root, self.env), 0)
        self.assertEqual(retained.read_bytes(), old_content)
        self.assertNotEqual(old_key, self.key())
        self.assertTrue(self.output.samefile(old_binary))
        self.assertEqual(old_binary.read_bytes(), self.output.read_bytes())

        # Cargo links its final output from deps, removing old paths before relinking.
        dependency_output = self.root / "target/debug/deps/hash-graph-example"
        dependency_output.parent.mkdir(parents=True, exist_ok=True)
        os.link(self.output, dependency_output)
        before = old_binary.read_bytes()
        key = self.key()
        with cache.build_lock(self.root):
            dependency_output.unlink()
            binary(dependency_output, b"normal Cargo replacement")
            self.output.unlink()
            os.link(dependency_output, self.output)
        self.assertEqual(old_binary.read_bytes(), before)
        self.assertIsNotNone(cache.verified_payload_digest(self.payload, key))

    def test_mutation_through_shared_runtime_inode_rejects_reuse(self) -> None:
        for mutation in (lambda: self.output.write_bytes(b"corrupt"), lambda: self.output.chmod(0o644)):
            with self.compile():
                self.assertEqual(cache.compile_graph(self.root, self.env), 0)
            self.assertTrue(self.output.samefile(self.payload / "hash-graph"))
            mutation()
            self.assertIsNone(cache.verified_payload_digest(self.payload, self.key()))
            with self.compile() as build:
                self.assertEqual(cache.compile_graph(self.root, self.env), 0)
                build.assert_called_once()

    def test_copy_fallback_and_atomic_staging_cleanup(self) -> None:
        with patch.object(cache.os, "link", side_effect=OSError(errno.EXDEV, "cross-device link")):
            with self.compile():
                self.assertEqual(cache.compile_graph(self.root, self.env), 0)
            self.assertFalse(self.output.samefile(self.payload / "hash-graph"))
            self.output.unlink()
            with self.compile() as build:
                self.assertEqual(cache.compile_graph(self.root, self.env), 0)
                build.assert_not_called()
            self.assertEqual(self.output.read_bytes(), (self.payload / "hash-graph").read_bytes())
        before = self.output.read_bytes()
        with patch.object(cache.os, "replace", side_effect=OSError("synthetic replacement failure")):
            with self.assertRaises(OSError):
                cache.link_or_copy_binary(self.payload / "hash-graph", self.output)
        self.assertEqual(self.output.read_bytes(), before)
        self.assertEqual(list((self.root / "target").rglob(".hash-graph-*")), [])
        self.assertEqual(list((self.root / "target").rglob(".manifest-*")), [])

    def test_tar_payload_restores_without_external_hardlink(self) -> None:
        with self.compile():
            self.assertEqual(cache.compile_graph(self.root, self.env), 0)
        archive = self.root / "payload.tar"
        key = self.key()
        subprocess.run(["tar", "--posix", "-cf", str(archive), "-C", str(self.root),
                        "target/graph-build-cache"], check=True)
        with tarfile.open(archive) as contents:
            self.assertTrue(contents.getmember("target/graph-build-cache/hash-graph").isfile())
            self.assertNotIn("target/graph-build-cache.lock", contents.getnames())
        self.output.unlink()
        shutil.rmtree(self.payload)
        subprocess.run(["tar", "-xf", str(archive), "-C", str(self.root)], check=True)
        self.assertEqual((self.payload / "hash-graph").stat().st_nlink, 1)
        self.assertIsNotNone(cache.verified_payload_digest(self.payload, key))
        with self.compile() as build:
            self.assertEqual(cache.compile_graph(self.root, self.env), 0)
            build.assert_not_called()

    def test_source_changes_include_transitive_non_rust_generated_and_deletions(self) -> None:
        before = self.key()
        for name in self.files:
            if name == ".cargo/config.toml":
                continue
            with self.subTest(name=name):
                path = self.root / name
                old = path.read_bytes()
                path.write_bytes(old + b"changed")
                self.assertNotEqual(before, self.key())
                path.write_bytes(old)
                path.unlink()
                self.assertNotEqual(before, self.key())
                path.write_bytes(old)
        generated = self.write("libs/dependency/generated/types.rs", b"generated")
        self.assertNotEqual(before, self.key())
        generated.unlink()
        self.assertEqual(before, self.key())
        self.write("libs/dependency/target/build/artifact", b"output")
        self.assertEqual(before, self.key())

    def test_key_binds_resolution_environment_tools_platform_and_absolute_path(self) -> None:
        before = self.key()
        self.metadata["resolve"] = {"nodes": ["new-dependency"]}
        self.assertNotEqual(before, self.key())
        self.metadata["resolve"] = {"nodes": ["locked-dependency"]}
        self.isolated["CARGO_INCREMENTAL"] = "0"
        self.assertNotEqual(before, self.key())
        del self.isolated["CARGO_INCREMENTAL"]
        with patch.object(cache, "compiler_environment", return_value=(self.isolated, "tools-v2")):
            self.assertNotEqual(before, self.key())
        self.env["ImageVersion"] = "new-image"
        self.assertNotEqual(before, self.key())
        del self.env["ImageVersion"]
        self.assertEqual(before, self.key())
        with patch.object(cache, "machine_digest", return_value="another-root"):
            self.assertNotEqual(before, self.key())

    def test_declared_wasm_output_does_not_drift_but_tracked_and_generated_inputs_do(self) -> None:
        manifest = self.write("libs/@blockprotocol/type-system/rust/Cargo.toml", b"input")
        self.metadata["packages"].append({"source": None, "manifest_path": str(manifest)})
        before = self.key()
        output_name = "libs/@blockprotocol/type-system/rust/pkg/type-system_bg.wasm"
        output = self.write(output_name, b"wasm output")
        self.assertEqual(before, self.key())
        output.write_bytes(b"rebuilt wasm output")
        self.assertEqual(before, self.key())
        generated = self.write("libs/@blockprotocol/type-system/rust/generated/types.rs", b"generated input")
        self.assertNotEqual(before, self.key())
        generated.unlink()
        self.assertEqual(before, self.key())
        other_pkg = self.write("libs/dependency/pkg/input.rs", b"another package input")
        self.assertNotEqual(before, self.key())
        other_pkg.unlink()
        with patch.object(cache.subprocess, "check_output", return_value=self.tracked + output_name.encode() + b"\0"):
            tracked_key = self.key()
            output.write_bytes(b"changed tracked input")
            self.assertNotEqual(tracked_key, self.key())

    def test_corrupt_missing_wrong_arch_nonexecutable_and_symlink_payloads_miss(self) -> None:
        mutations = [lambda: (self.payload / "hash-graph").write_bytes(b"corrupt"),
                     lambda: (self.payload / "manifest.json").write_text("[]"),
                     lambda: (self.payload / "manifest.json").unlink(),
                     lambda: (self.payload / "hash-graph").chmod(0o644),
                     lambda: (self.payload / "hash-graph").unlink()]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                shutil.rmtree(self.payload, ignore_errors=True)
                binary(self.output)
                cache.write_payload(self.payload, self.output, self.key())
                mutation()
                with self.compile() as build:
                    self.assertEqual(cache.compile_graph(self.root, self.env), 0)
                    build.assert_called_once()
        binary(self.output)
        cache.write_payload(self.payload, self.output, self.key())
        cached = self.payload / "hash-graph"
        cached.unlink()
        cached.symlink_to(self.output)
        with self.compile() as build:
            self.assertEqual(cache.compile_graph(self.root, self.env), 0)
            build.assert_called_once()
        self.assertTrue(cached.is_symlink())
        with patch.object(cache.platform, "machine", return_value="aarch64"):
            self.assertFalse(cache.executable(self.output))

    def test_changed_inputs_and_failed_or_invalid_build_do_not_publish(self) -> None:
        with self.compile(mutation=lambda: self.write("Cargo.lock", b"changed during build")):
            self.assertEqual(cache.compile_graph(self.root, self.env), 0)
        self.assertFalse(self.payload.exists())
        with self.compile(code=7):
            self.assertEqual(cache.compile_graph(self.root, self.env), 7)
        self.assertFalse(self.payload.exists())
        with self.compile(produce=False):
            self.assertEqual(cache.compile_graph(self.root, self.env), 1)
        self.assertFalse(self.payload.exists())

    def test_baseline_uses_identical_environment_without_hit_or_store(self) -> None:
        binary(self.output)
        cache.write_payload(self.payload, self.output, self.key())
        manifest = (self.payload / "manifest.json").read_bytes()
        self.env["HASH_GRAPH_BUILD_CACHE"] = "baseline"
        with self.compile() as build:
            self.assertEqual(cache.compile_graph(self.root, self.env), 0)
            build.assert_called_once()
            self.assertIs(build.call_args.kwargs["env"], self.isolated)
        self.assertEqual((self.payload / "manifest.json").read_bytes(), manifest)

    def test_disabled_or_unsupported_uses_original_command_and_environment(self) -> None:
        for mode in (None, "0"):
            self.env.pop("HASH_GRAPH_BUILD_CACHE", None)
            if mode:
                self.env["HASH_GRAPH_BUILD_CACHE"] = mode
            with self.compile() as build:
                self.assertEqual(cache.compile_graph(self.root, self.env), 0)
                self.assertIs(build.call_args.kwargs["env"], self.env)
        self.env["HASH_GRAPH_BUILD_CACHE"] = "1"
        with patch.object(cache, "fingerprint", side_effect=cache.NoReuse), self.compile() as build:
            self.assertEqual(cache.compile_graph(self.root, self.env), 0)
            self.assertIs(build.call_args.kwargs["env"], self.env)

    def test_unsupported_config_and_external_sources_reject_reuse(self) -> None:
        for value in (b"[build]\ntarget-dir = '/custom'\n", b"[env]\nCUSTOM='value'\n",
                      b"[target.example]\nlinker='/custom/ld'\n"):
            self.write(".cargo/config.toml", value)
            with self.assertRaises(cache.NoReuse):
                self.key()
        self.write(".cargo/config.toml", b"")
        source = self.root / "libs/dependency/src/lib.rs"
        source.unlink()
        source.symlink_to("/outside/source.rs")
        with self.assertRaises(cache.NoReuse):
            self.key()
        source.unlink()
        self.metadata["packages"][0]["manifest_path"] = "/outside/Cargo.toml"
        with self.assertRaises(cache.NoReuse):
            self.key()

    def test_in_repository_symlinks_bind_target_text_and_contents(self) -> None:
        source = self.root / "libs/dependency/src/lib.rs"
        before = self.key()
        source.unlink()
        source.symlink_to(self.root / "Cargo.lock")
        linked = self.key()
        self.assertNotEqual(before, linked)
        self.write("Cargo.lock", b"changed linked content")
        self.assertNotEqual(linked, self.key())

    def test_prepare_patches_only_supported_original_and_restores_on_failure(self) -> None:
        package = self.root / "apps/hash-graph/package.json"
        before = package.read_bytes()
        with patch.object(cache, "fingerprint", side_effect=cache.NoReuse):
            self.assertEqual(cache.prepare(self.root, self.env), "")
        self.assertEqual(before, package.read_bytes())
        key = cache.prepare(self.root, self.env)
        self.assertEqual(json.loads(package.read_text())["scripts"]["compile"], cache.WRAPPER)
        self.assertEqual(key, self.key())
        self.assertEqual(key, json.loads((self.root / "target/graph-build-inputs.json").read_text())["key"])
        self.assertEqual(cache.prepare(self.root, self.env), key)
        with patch.object(cache, "fingerprint", side_effect=cache.NoReuse):
            self.assertEqual(cache.prepare(self.root, self.env), "")
        self.assertEqual(json.loads(package.read_text())["scripts"]["compile"], " ".join(cache.COMMAND))
        package.write_text('{"scripts":{"compile":"custom command"}}')
        before = package.read_bytes()
        self.assertEqual(cache.prepare(self.root, self.env), "")
        self.assertEqual(before, package.read_bytes())
        package.write_text("invalid")
        self.assertEqual(cache.prepare(self.root, self.env), "")
        self.assertEqual(package.read_text(), "invalid")

    def test_prepare_lock_failure_restores_original_command_and_disables_reuse(self) -> None:
        package = self.root / "apps/hash-graph/package.json"
        self.assertTrue(cache.prepare(self.root, self.env))
        self.assertEqual(json.loads(package.read_text())["scripts"]["compile"], cache.WRAPPER)
        with patch.object(cache, "build_lock", side_effect=OSError("synthetic unsupported lock")), patch.object(cache, "fingerprint") as inspect:
            self.assertEqual(cache.prepare(self.root, self.env), "")
            inspect.assert_not_called()
        self.assertEqual(json.loads(package.read_text())["scripts"]["compile"], " ".join(cache.COMMAND))

    def test_manifest_contains_only_digests_and_no_environment_values(self) -> None:
        self.isolated["CARGO_REGISTRIES_EXAMPLE_TOKEN"] = "synthetic-sensitive-value"
        _, _, inputs = cache.fingerprint(self.root, self.env)
        self.assertNotIn("synthetic-sensitive-value", json.dumps(inputs))
        self.assertNotIn(str(self.root), json.dumps(inputs))


class CompilerEnvironment(unittest.TestCase):
    def test_real_environment_function_resolves_shims_before_isolating(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            installed = root / "installed/bin"
            installed.mkdir(parents=True)
            for name in ("rustc", "cargo", "protoc", "cc", "c++", "ld", "ar", "as", "pkg-config", "cmake", "make", "custom-wrapper", "custom-cc"):
                tool = installed / name
                tool.write_bytes(b"synthetic tool")
                tool.chmod(0o755)
            shims = root / "shims"
            shims.mkdir()
            (shims / "protoc").write_bytes(b"tool-manager shim")
            (shims / "protoc").chmod(0o755)
            original = {"HOME": str(root / "home"), "PATH": f"{shims}:{installed}",
                        "PROTOC": str(installed / "protoc"), "RUSTUP_TOOLCHAIN": "nightly-synthetic",
                        "RUSTC_WRAPPER": str(installed / "custom-wrapper"), "CC": str(installed / "custom-cc"),
                        "SCCACHE_SERVER_PORT": "4226", "MISE_TRUSTED_CONFIG_PATHS": "/synthetic",
                        "GITHUB_TOKEN": "synthetic-sensitive-value"}
            calls = []

            def query(args, cwd, env):
                calls.append((args, env))
                return str(root / "installed").encode() if "sysroot" in args else b"synthetic version"

            with patch.object(cache.platform, "system", return_value="Linux"), patch.object(cache.platform, "machine", return_value="x86_64"), patch.object(cache, "run", side_effect=query):
                env, tools = cache.compiler_environment(root, original)
                self.assertIs(calls[0][1], original)
                self.assertTrue(all(call_env is env for _, call_env in calls[1:]))
                self.assertEqual([args for args, _ in calls],
                                 [["rustc", "--print", "sysroot"], ["rustc", "-vV"], ["cargo", "--version"]])
                self.assertEqual(env["SCCACHE_SERVER_PORT"], "4226")
                self.assertEqual(env["PROTOC"], str(installed / "protoc"))
                self.assertNotIn(str(shims), env["PATH"])
                self.assertNotIn("GITHUB_TOKEN", env)
                self.assertNotIn("MISE_TRUSTED_CONFIG_PATHS", env)
                self.assertNotIn("synthetic-sensitive-value", tools)
                (installed / "cc").write_bytes(b"changed tool")
                self.assertNotEqual(tools, cache.compiler_environment(root, original)[1])
                _, before_wrapper_change = cache.compiler_environment(root, original)
                (installed / "custom-wrapper").write_bytes(b"changed wrapper")
                self.assertNotEqual(before_wrapper_change, cache.compiler_environment(root, original)[1])
                self.assertTrue(all(args[0] in {"rustc", "cargo"} for args, _ in calls))

    def test_diagnostics_use_reason_codes_without_exception_payloads(self) -> None:
        for args, expected in ((["rustc", "--print", "sysroot"], "sysroot-query-failed"),
                               (["cargo", "metadata"], "metadata-command-failed"),
                               (["protoc", "--version"], "tool-version-failed")):
            error = subprocess.CalledProcessError(1, ["synthetic-sensitive-value"], stderr=b"synthetic-sensitive-value")
            with self.subTest(args=args), patch.object(cache.subprocess, "run", side_effect=error):
                with self.assertRaises(cache.NoReuse) as caught:
                    cache.run(args, Path("/synthetic"), {})
                self.assertEqual(cache.failure_reason(caught.exception), expected)
        for error in (OSError("synthetic-sensitive-value"), KeyError("synthetic-sensitive-value"), ValueError("synthetic-sensitive-value")):
            self.assertNotIn("synthetic-sensitive-value", cache.failure_reason(error))

    def test_custom_flags_targets_and_profiles_fall_back(self) -> None:
        original = {"HOME": "/home/example", "PATH": "/usr/bin"}
        with patch.object(cache.platform, "system", return_value="Linux"), patch.object(cache.platform, "machine", return_value="x86_64"):
            for name in ("RUSTFLAGS", "CFLAGS", "CARGO_TARGET_DIR", "CARGO_BUILD_TARGET",
                         "CARGO_PROFILE_DEV_OPT_LEVEL", "CARGO_ENCODED_RUSTFLAGS",
                         "OPENSSL_DIR", "PKG_CONFIG_PATH", "CPATH", "LD_LIBRARY_PATH", "SCCACHE_RECACHE", "SCCACHE_DISABLE"):
                with self.subTest(name=name), self.assertRaises(cache.NoReuse):
                    cache.compiler_environment(Path("/example"), {**original, name: "custom"})

    def test_sccache_port_is_a_build_input_and_unrelated_variables_are_not(self) -> None:
        self.assertTrue(cache.BUILD_ENV.match("SCCACHE_SERVER_PORT"))
        self.assertFalse(cache.BUILD_ENV.match("GITHUB_TOKEN"))
        self.assertFalse(cache.BUILD_ENV.match("HASH_GRAPH_LOG_FOLDER"))


if __name__ == "__main__":
    unittest.main()
