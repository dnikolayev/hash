#!/usr/bin/env python3
"""Focused timing checks using synthetic REST responses."""

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
            pages[1]["jobs"][0]["conclusion"] = "failure"
            (directory / "jobs.json").write_text(json.dumps(pages))
            failed = TIMINGS.report({"runs": [entry]}, directory)
            self.assertEqual(failed["comparisons"], [])
            self.assertIn("job Backend: completed/failure", failed["runs"][0]["exclusions"])
            pages[1]["jobs"][0]["run_attempt"] = 2
            (directory / "jobs.json").write_text(json.dumps(pages))
            invalid = TIMINGS.report({"runs": [entry]}, directory)
            self.assertIn("different run or attempt", invalid["runs"][0]["exclusions"][0])

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
