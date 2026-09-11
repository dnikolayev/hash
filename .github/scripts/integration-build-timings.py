#!/usr/bin/env python3
r"""Compare saved GitHub Actions timing evidence without third-party dependencies.

Save each attempt separately (substitute REPO, RUN and ATTEMPT):
  gh api repos/REPO/actions/runs/RUN/attempts/ATTEMPT > run.json
  gh api --paginate --slurp \
    'repos/REPO/actions/runs/RUN/attempts/ATTEMPT/jobs?per_page=100' > jobs.json

Pass a JSON manifest containing {"runs": [{"variant": "baseline",
"cache_condition": "cold", "scope": "complete-test", "run": "run.json",
"jobs": "jobs.json"}]}.
Paths are relative to the manifest. Keep failed attempts in the manifest;
optional exclusion_reason explains an intentional exclusion. Retain cache
receipts and test counts in cache_evidence and test_results, and the checked-out
source/workflow revisions in source_sha and workflow_sha. These annotations
are supplied evidence, not inferred from successful or replayed step logs.

Use scope "integration-only" for subset workflows; unlike scopes are never
combined. Optional phase_steps maps phase names to exact API step names.
Preparation and transfer are separate; older runs may not expose these phases.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
from statistics import median


PHASE_STEPS = {
    "graph preparation": ["Prepare graph build"],
    "graph cache transfer": ["Restore graph build", "Save graph build"],
    "background startup and readiness": ["Start background tasks"],
    "test execution": ["Run tests"],
}


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed


def elapsed(start, end):
    seconds = (timestamp(end) - timestamp(start)).total_seconds()
    if seconds < 0:
        raise ValueError("negative elapsed time")
    return seconds


def load_jobs(value):
    """Accept a REST jobs object or gh --paginate --slurp page array."""
    pages = value if isinstance(value, list) else [value]
    jobs = [job for page in pages for job in page["jobs"]]
    if not pages or any(page["total_count"] != len(jobs) for page in pages):
        raise ValueError("incomplete jobs pagination")
    if len({job["id"] for job in jobs}) != len(jobs):
        raise ValueError("duplicate jobs")
    return jobs


def measure(entry, directory, phase_steps):
    run = json.loads((directory / entry["run"]).read_text())
    jobs = load_jobs(json.loads((directory / entry["jobs"]).read_text()))
    result = {
        key: entry[key]
        for key in (
            "variant", "cache_condition", "source_sha", "workflow_sha",
            "cache_evidence", "test_results", "runner_details",
        )
        if key in entry
    }
    result.update({
        "scope": entry.get("scope", "unspecified"),
        "run_id": run["id"], "attempt": run["run_attempt"],
        "head_sha": run["head_sha"], "workflow_path": run["path"],
        "url": run["html_url"], "conclusion": run["conclusion"],
        "exclusions": [], "metrics_seconds": {}, "jobs": [],
    })
    if entry.get("exclusion_reason"):
        result["exclusions"].append(entry["exclusion_reason"])
    if run["status"] != "completed" or run["conclusion"] != "success":
        result["exclusions"].append(f"run status: {run['status']}/{run['conclusion']}")
    names = [job["name"] for job in jobs]
    if len(set(names)) != len(names):
        raise ValueError("duplicate job names prevent unambiguous comparisons")
    started_jobs = []
    for job in jobs:
        if job["run_id"] != run["id"] or job["run_attempt"] != run["run_attempt"]:
            raise ValueError("job belongs to a different run or attempt")
        record = {key: job.get(key) for key in (
            "id", "name", "html_url", "conclusion", "labels", "runner_id",
            "runner_name", "runner_group_id", "runner_group_name",
            "created_at", "started_at", "completed_at",
        )}
        record["steps"] = job.get("steps", [])
        record["missing_phases"] = []
        result["jobs"].append(record)
        if job["conclusion"] == "skipped":
            continue
        if job["status"] != "completed" or job["conclusion"] != "success":
            result["exclusions"].append(
                f"job {job['name']}: {job['status']}/{job['conclusion']}"
            )
        if not job.get("started_at") or not job.get("completed_at"):
            result["exclusions"].append(f"job {job['name']}: missing timestamps")
            continue
        started_jobs.append(job)
        metrics = result["metrics_seconds"]
        metrics[f"{job['name']} / wall"] = elapsed(job["started_at"], job["completed_at"])
        if job.get("created_at"):
            metrics[f"{job['name']} / waiting (dependencies and queue)"] = elapsed(
                job["created_at"], job["started_at"]
            )
        for phase, step_names in phase_steps.items():
            steps = [step for step in job.get("steps", [])
                     if step["name"] in step_names and step.get("conclusion") != "skipped"]
            if not steps or any(
                step.get("conclusion") != "success"
                or not step.get("started_at") or not step.get("completed_at")
                for step in steps
            ):
                record["missing_phases"].append(phase)
                continue
            metrics[f"{job['name']} / {phase}"] = sum(
                elapsed(step["started_at"], step["completed_at"]) for step in steps
            )
    if started_jobs:
        metrics = result["metrics_seconds"]
        metrics["workflow / wall"] = elapsed(
            run["run_started_at"], max(job["completed_at"] for job in started_jobs)
        )
        metrics["workflow / initial wait"] = elapsed(
            run["run_started_at"], min(job["started_at"] for job in started_jobs)
        )
        metrics["workflow / aggregate jobs"] = sum(
            elapsed(job["started_at"], job["completed_at"]) for job in started_jobs
        )
    else:
        result["exclusions"].append("no completed job timing available")
    return result


def summarize(runs):
    groups = defaultdict(list)
    for run in runs:
        if not run["exclusions"] and run["variant"] in ("baseline", "candidate"):
            for metric, seconds in run["metrics_seconds"].items():
                groups[(run.get("scope", "unspecified"), run["cache_condition"], metric,
                        run["variant"])].append((seconds, run["url"]))
    summaries = []
    for scope, condition, metric in sorted({key[:3] for key in groups}):
        row = {"scope": scope, "cache_condition": condition, "metric": metric}
        for variant in ("baseline", "candidate"):
            samples = groups.get((scope, condition, metric, variant), [])
            values = [sample[0] for sample in samples]
            row[variant] = {
                "samples": len(values), "median_seconds": median(values) if values else None,
                "range_seconds": [min(values), max(values)] if values else None,
                "run_links": [sample[1] for sample in samples],
            }
        enough = all(row[variant]["samples"] >= 3 for variant in ("baseline", "candidate"))
        row["comparison_status"] = "descriptive comparison" if enough else "insufficient successful samples"
        before = row["baseline"]["median_seconds"]
        after = row["candidate"]["median_seconds"]
        row["seconds_saved"] = before - after if enough else None
        row["percent_saved"] = 100 * (before - after) / before if enough and before else None
        summaries.append(row)
    return summaries


def report(manifest, directory):
    runs = []
    seen = set()
    phases = manifest.get("phase_steps", PHASE_STEPS)
    for index, entry in enumerate(manifest["runs"]):
        try:
            if not entry.get("variant") or not entry.get("cache_condition"):
                raise ValueError("variant and cache_condition are required")
            run = measure(entry, directory, phases)
            identity = (run["run_id"], run["attempt"])
            if identity in seen:
                run["exclusions"].append("duplicate run attempt")
            seen.add(identity)
        except (KeyError, TypeError, ValueError, OSError) as error:
            run = {"manifest_index": index, "variant": entry.get("variant"),
                   "cache_condition": entry.get("cache_condition"),
                   "exclusions": [f"invalid evidence: {error}"], "metrics_seconds": {}}
        runs.append(run)
    return {
        "notes": [
            "All durations are seconds; divide aggregate jobs by 60 for job-minutes.",
            "Workflow wall is run_started_at to the last job completion, including dependencies and scheduling; controller finalization is not available from these timestamps.",
            "Initial wait precedes the first job. Per-job waiting includes dependencies and queueing; pure runner scheduling delay is unavailable without separate readiness evidence.",
            "Step times measure execution envelopes; test-result cache hits, actual test counts and cache state require retained logs or receipts.",
            "Missing phases are unavailable, never zero. Phase totals include only matching successful steps.",
            "Failed, incomplete, duplicate and explicitly excluded attempts remain listed but do not contribute to summaries.",
            "Savings require at least three successful samples per variant. Labels alone do not establish equivalent sources, runners, caches or test execution.",
            "Scope separates complete Test workflows from integration-only subsets; subset timings do not establish complete workflow savings.",
        ],
        "runs": runs,
        "comparisons": summarize(runs),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    output = report(json.loads(args.manifest.read_text()), args.manifest.parent)
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
