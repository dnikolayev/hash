#!/usr/bin/env python3
"""Focused timing checks using synthetic REST responses."""

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location(
    "timings", Path(__file__).with_name("integration-build-timings.py")
)
TIMINGS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(TIMINGS)


def at(seconds):
    return f"2026-01-01T00:{seconds // 60:02d}:{seconds % 60:02d}Z"


def job(identifier, name, end, conclusion="success"):
    return {
        "id": identifier, "run_id": 1, "run_attempt": 1, "name": name,
        "status": "completed", "conclusion": conclusion,
        "created_at": at(0), "started_at": at(10), "completed_at": at(end),
        "steps": [{"name": "Run tests", "conclusion": "success",
                   "started_at": at(20), "completed_at": at(40)}],
    }


def task_summaries():
    def execution(start, end):
        return {"startTime": 1767225600000 + start, "endTime": 1767225600000 + end,
                "exitCode": 0}

    return [
        {"id": "compile-summary", "version": "1", "turboVersion": "2.10.12",
         "execution": {"command": "turbo run compile", **execution(11000, 19000)},
         "tasks": [
             {"taskId": "@apps/hash-graph#compile", "command": "cargo build --bin hash-graph",
              "execution": execution(13250, 17500), "turbo_cache_status": "MISS",
              "task_cache_enabled": False},
             {"taskId": "@example/library#compile", "command": "tsc",
              "execution": execution(11000, 12000), "turbo_cache_status": "HIT",
              "task_cache_enabled": True},
         ]},
        {"id": "test-summary", "version": "1", "turboVersion": "2.10.12",
         "execution": {"command": "turbo run test:integration --filter=@tests/example",
                       **execution(20000, 45000)},
         "tasks": [
             {"taskId": "@apps/hash-graph#compile", "command": "cargo build --bin hash-graph",
              "execution": execution(20000, 21000), "turbo_cache_status": "MISS",
              "task_cache_enabled": False},
             {"taskId": "@tests/example#test:integration", "command": "playwright test",
              "execution": execution(22125, 40375), "turbo_cache_status": "MISS",
              "task_cache_enabled": False},
         ]},
    ]


class TimingEvidenceTests(unittest.TestCase):
    def test_parallel_wall_is_not_sum_and_missing_phases_are_not_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            run = {"id": 1, "run_attempt": 1, "head_sha": "a" * 40,
                   "path": ".github/workflows/test.yml", "html_url": "https://example.com/run/1",
                   "status": "completed", "conclusion": "success", "run_started_at": at(0)}
            (directory / "run.json").write_text(json.dumps(run))
            pages = [{"total_count": 2, "jobs": [job(1, "Browser", 100)]},
                     {"total_count": 2, "jobs": [job(2, "Backend", 90)]}]
            (directory / "jobs.json").write_text(json.dumps(pages))
            entry = {"variant": "baseline", "cache_condition": "cold",
                     "run": "run.json", "jobs": "jobs.json"}
            output = TIMINGS.report({"runs": [entry, entry]}, directory)
            metrics = output["runs"][0]["metrics_seconds"]
            self.assertEqual(metrics["workflow / wall"], 100)
            self.assertEqual(metrics["workflow / aggregate jobs"], 170)
            self.assertEqual(metrics["workflow / initial wait"], 10)
            self.assertEqual(metrics["Browser / test execution"], 20)
            self.assertNotIn("Browser / graph preparation", metrics)
            self.assertIn("graph preparation", output["runs"][0]["jobs"][0]["missing_phases"])
            self.assertEqual(output["runs"][1]["exclusions"], ["duplicate run attempt"])
            self.assertTrue(all(row["seconds_saved"] is None for row in output["comparisons"]))
            entry["task_timings"] = {"Browser": "task-timings.json"}
            summaries = task_summaries()
            (directory / "task-timings.json").write_text(json.dumps(summaries))
            projected = TIMINGS.report({"runs": [entry]}, directory)["runs"][0]
            self.assertEqual(projected["task_timings"], entry["task_timings"])
            self.assertEqual(projected["metrics_seconds"]["Browser / initial graph compile task"], 4.25)
            self.assertEqual(projected["metrics_seconds"]["Browser / selected integration test task"], 18.25)
            self.assertEqual(projected["metrics_seconds"]["Browser / test execution"], 20)
            summaries[1]["tasks"][1]["turbo_cache_status"] = "HIT"
            (directory / "task-timings.json").write_text(json.dumps(summaries))
            cached = TIMINGS.report({"runs": [entry]}, directory)["runs"][0]
            self.assertNotIn("Browser / selected integration test task", cached["metrics_seconds"])
            self.assertIn("cached", cached["jobs"][0]["task_timing_issues"][0])
            self.assertEqual(cached["metrics_seconds"]["Browser / test execution"], 20)
            pages[1]["jobs"][0]["conclusion"] = "failure"
            (directory / "jobs.json").write_text(json.dumps(pages))
            failed = TIMINGS.report({"runs": [entry]}, directory)
            self.assertEqual(failed["comparisons"], [])
            self.assertIn("job Backend: completed/failure", failed["runs"][0]["exclusions"])
            pages[1]["jobs"][0]["run_attempt"] = 2
            (directory / "jobs.json").write_text(json.dumps(pages))
            invalid = TIMINGS.report({"runs": [entry]}, directory)
            self.assertIn("different run or attempt", invalid["runs"][0]["exclusions"][0])

    def test_task_summaries_reject_missing_cached_duplicate_or_invalid_execution(self):
        valid = task_summaries()
        cases = []
        for field, value, message in (
            ("startTime", None, "epoch milliseconds"),
            ("endTime", 1767225610000, "negative"),
            ("endTime", 1767225620000, "outside invocation"),
            ("exitCode", None, "incomplete"),
        ):
            summaries = copy.deepcopy(valid)
            summaries[0]["tasks"][0]["execution"][field] = value
            cases.append((message, summaries))
        for field, value, message in (
            ("turbo_cache_status", "HIT", "cached"),
            ("task_cache_enabled", True, "disable Turbo caching"),
            ("taskId", "@example/other#compile", "missing matched task"),
        ):
            summaries = copy.deepcopy(valid)
            summaries[0]["tasks"][0][field] = value
            cases.append((message, summaries))
        for field, value in (("version", "2"), ("turboVersion", "2.11.0")):
            summaries = copy.deepcopy(valid)
            summaries[0][field] = value
            cases.append(("unsupported", summaries))
        cases.append(("missing invocation", valid[:1]))
        cases.append(("duplicate task summary", valid + [valid[0]]))
        cases.append(("duplicate invocation", valid + [{**valid[0], "id": "another-compile"}]))
        summaries = copy.deepcopy(valid)
        summaries[0]["tasks"].append(summaries[0]["tasks"][0])
        cases.append(("duplicate task ids", summaries))
        for message, summaries in cases:
            with self.subTest(reason=message), self.assertRaisesRegex(ValueError, message):
                TIMINGS.measure_tasks(summaries, job(1, "Browser", 100))

    def test_incomplete_pages_duplicate_jobs_and_wrong_attempt_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "incomplete"):
            TIMINGS.load_jobs({"total_count": 2, "jobs": [job(1, "Browser", 100)]})
        with self.assertRaisesRegex(ValueError, "duplicate"):
            TIMINGS.load_jobs({"total_count": 2, "jobs": [job(1, "Browser", 100)] * 2})
        with self.assertRaisesRegex(ValueError, "negative"):
            TIMINGS.elapsed(at(20), at(10))

    def test_three_samples_medians_ranges_and_failed_attempt_exclusion(self):
        runs = []
        for variant, values in (("baseline", [120, 100, 110]), ("candidate", [80, 100, 90])):
            runs.extend({"variant": variant, "cache_condition": "warm", "exclusions": [],
                         "metrics_seconds": {"workflow / wall": value}, "url": str(index)}
                        for index, value in enumerate(values))
        runs.append({**runs[0], "exclusions": ["cancelled"],
                     "metrics_seconds": {"workflow / wall": 900}})
        row, = TIMINGS.summarize(runs)
        self.assertEqual(row["baseline"]["samples"], 3)
        self.assertEqual(row["baseline"]["median_seconds"], 110)
        self.assertEqual(row["candidate"]["range_seconds"], [80, 100])
        self.assertEqual(row["seconds_saved"], 20)
        self.assertAlmostEqual(row["percent_saved"], 2000 / 110)
        for run in runs:
            run["scope"] = "complete-test" if run["variant"] == "baseline" else "integration-only"
        separate = TIMINGS.summarize(runs)
        self.assertEqual(len(separate), 2)
        self.assertTrue(all(row["seconds_saved"] is None for row in separate))


if __name__ == "__main__":
    unittest.main()
