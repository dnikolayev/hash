import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import collect

spec = importlib.util.spec_from_file_location("timings", Path(__file__).with_name("integration-build-timings.py"))
timings = importlib.util.module_from_spec(spec)
spec.loader.exec_module(timings)
KEY = "graph-build-v1-" + "a" * 64


def at(seconds):
    return f"2026-01-01T00:{seconds // 60:02d}:{seconds % 60:02d}Z"


def job(identifier, name, end):
    return {"id": identifier, "run_id": 1, "run_attempt": 1, "name": name,
            "status": "completed", "conclusion": "success", "runner_name": "example-runner",
            "created_at": at(0), "started_at": at(10), "completed_at": at(end),
            "steps": [{"name": "Run tests", "conclusion": "success", "started_at": at(20), "completed_at": at(40)}]}


def task_summaries():
    def execution(start, end):
        return {"startTime": 1767225600000 + start * 1000, "endTime": 1767225600000 + end * 1000, "exitCode": 0}
    return [{"id": name + "-summary", "version": "1", "turboVersion": "2.10.12",
             "execution": {"command": command, **execution(start, end)},
             "tasks": [{"taskId": task, "command": "example", "execution": execution(start, end),
                        "turbo_cache_status": "MISS", "task_cache_enabled": False}]}
            for name, command, task, start, end in [
                ("compile", "turbo run compile", "@apps/hash-graph#compile", 11, 19),
                ("test", "turbo run test:integration --filter=@tests/example", "@tests/example#test:integration", 20, 45)]]



def log(hit=True, build=False, replay=False, probe=False):
    def line(second, text):
        return f"2026-01-01T00:00:{second:06.3f}Z {text}"

    def phase(name, status, key=KEY):
        return json.dumps({"phase": name, "status": status, "key": key, "seconds": .25})

    rows = [line(10.1, phase("prepare", "ready")),
            line(10.2, ("Cache restored from key: " if hit else "Cache not found for input keys: ") + "sample-" + KEY),
            line(18, "##[group]@apps/hash-graph:compile"),
            line(18.1, "cache hit, replaying logs abc" if replay else "cache bypass, force executing abc")]
    if probe:
        rows.append(line(18.2, phase("cpu-probe", "ready", "")))
    rows += [line(18.3, phase("prepare", "ready")), line(18.4, phase("restore", "miss" if build else "hit"))]
    if build:
        rows.append(line(18.5, phase("build", "success")))
    rows += [line(18.8, "##[endgroup]"), line(19, "Summary: /synthetic/.turbo/runs/compile-summary.json"),
             line(40, "##[group]@apps/hash-graph:compile"), line(40.1, "cache bypass, force executing abc"),
             line(40.2, phase("prepare", "ready")), line(40.3, phase("restore", "hit")), line(40.4, "##[endgroup]")]
    return "\n".join(rows)


class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.job = job(1, "Browser", 100)
        self.job["steps"] += [{"name": name, "conclusion": "success", "started_at": at(10), "completed_at": at(11)}
                              for name in ("Prepare graph build", "Restore graph build")]
        self.raw = {"GRAPH_PREPARED_KEY": KEY, "GRAPH_CACHE_HIT": "true", "GRAPH_UPLOAD_VALID": ""}

    def project(self, text, raw=None, summaries=None):
        return collect.project(raw or self.raw, self.job, summaries or task_summaries(), text, timings)

    def test_current_hit_and_probe_are_derived_without_counting_later_invocations(self):
        receipt = self.project(log(probe=True))
        self.assertTrue(receipt["helper_restore_accepted"])
        self.assertEqual(receipt["initial_compile_summary_id"], "compile-summary")
        self.assertEqual(receipt["initial_compile_cpu_probes"]["count"], 1)
        self.assertEqual(receipt["initial_compile_cpu_probes"]["seconds"], .25)
        self.assertEqual(receipt["verified_context_cpu_probe_count"], 1)
        self.assertEqual(receipt["initial_compile_fingerprint_seconds"], .25)
        self.assertEqual(receipt["collection_issues"], [])

    def test_first_miss_or_rejection_cannot_be_upgraded_by_later_local_hit(self):
        miss = {**self.raw, "GRAPH_CACHE_HIT": ""}
        self.assertFalse(self.project(log(hit=False, build=True), raw=miss)["helper_restore_accepted"])
        self.assertFalse(self.project(log(build=True))["helper_restore_accepted"])
        earlier = '2026-01-01T00:00:11.000Z ' + json.dumps({"phase": "build", "status": "success", "key": KEY, "seconds": .25})
        self.assertFalse(self.project(earlier + "\n" + log())["helper_restore_accepted"])

    def test_replayed_missing_mismatched_or_unbound_evidence_is_not_accepted(self):
        cases = [log(replay=True), log().replace(KEY, "graph-build-v1-" + "b" * 64),
                 log().replace("compile-summary.json", "other-summary.json"),
                 log().replace('"status": "hit"', '"status": "unknown"')]
        for text in cases:
            with self.subTest(text=text):
                result = self.project(text)
                self.assertNotIn("helper_restore_accepted", result)
                self.assertTrue(result["collection_issues"])
        summaries = copy.deepcopy(task_summaries())
        summaries[0]["tasks"][0]["turbo_cache_status"] = "HIT"
        self.assertNotIn("helper_restore_accepted", self.project(log(), summaries=summaries))

    def test_raw_claim_is_replaced_and_absent_probe_is_unavailable(self):
        result = self.project(log(hit=False, build=True), raw={**self.raw, "GRAPH_CACHE_HIT": "", "helper_restore_accepted": True})
        self.assertFalse(result["helper_restore_accepted"])
        self.assertIsNone(result["job_total_native_probe_seconds"])
        self.assertNotIn("initial_compile_cpu_probes", result)

    def test_manifest_collection_preserves_metrics_and_rejects_wrong_artifact_attempt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = {
                "run.json": {"id": 1, "run_attempt": 1, "head_sha": "b" * 40, "path": ".github/workflows/test.yml",
                             "html_url": "https://example.com/run/1", "status": "completed", "conclusion": "success", "run_started_at": at(0)},
                "jobs.json": {"total_count": 1, "jobs": [self.job]},
                "cache.json": self.raw, "tasks.json": task_summaries(),
                "environment.json": {"checkout_sha": "a" * 40, "workflow_sha": "b" * 40, "run_id": "1", "run_attempt": "1", "runner_name": "example-runner"},
                "manifest.json": {"runs": [{"variant": "candidate", "cache_condition": "warm", "scope": "complete-test",
                                            "source_sha": "a" * 40, "workflow_sha": "b" * 40, "run": "run.json", "jobs": "jobs.json",
                                            "cache_evidence": {"Browser": "cache.json"}, "task_timings": {"Browser": "tasks.json"},
                                            "runner_details": {"Browser": "environment.json"}}]},
            }
            for name, value in files.items():
                (root / name).write_text(json.dumps(value))
            (root / "job-1.log").write_text(log())
            report = collect.collect(root / "manifest.json", root / "valid", timings)
            self.assertEqual(report["runs"][0]["graph_cache_state"], "accepted-cache-hit")
            files["environment.json"]["run_attempt"] = "2"
            (root / "environment.json").write_text(json.dumps(files["environment.json"]))
            report = collect.collect(root / "manifest.json", root / "wrong-attempt", timings)
            self.assertEqual(report["runs"][0]["graph_cache_state"], "unknown")
            self.assertEqual(report["runs"][0]["metrics_seconds"]["workflow / wall"], 100)
            receipt = json.loads(next((root / "wrong-attempt").glob("run-*.json")).read_text())
            self.assertIn("artifact attempt identity mismatch", receipt["collection_issues"][0])


if __name__ == "__main__":
    unittest.main()
