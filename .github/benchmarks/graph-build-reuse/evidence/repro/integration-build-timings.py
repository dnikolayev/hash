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
Optional task_timings maps exact job names to projected Turbo summary JSON files.
Each file is an array of version-1, Turbo-2.10.12 summaries containing id,
execution (command, startTime, endTime, exitCode), and tasks (taskId, execution,
turbo_cache_status, task_cache_enabled). Only current MISS task envelopes count;
unusable supplemental evidence retains its reason without replacing API times.
Optional graph_cache_receipts maps exact graph-consuming job names to JSON
receipts with GRAPH_PREPARED_KEY and GRAPH_CACHE_HIT. Successful preparation and
restore steps are required. Every non-skipped Prepare graph build is included,
even if its receipt is missing. Candidate cache labels alone never prove a hit.
An optional helper_restore_accepted boolean requires independently collected,
current matching-key helper restoration before any build, not a later local hit.
Only an Actions hit plus that acceptance qualifies as warm; Actions-only hits,
misses, mixed workflows and unknown evidence remain separate. Baseline cache
state is not-applicable. Comparisons match the supplied planned condition and
stratify candidates by observed state; missing cache proof preserves API times.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import json
from pathlib import Path
import re
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


def task_interval(execution):
    start, end = execution["startTime"], execution["endTime"]
    if any(type(value) is not int or value < 0 for value in (start, end)):
        raise ValueError("task timestamps must be nonnegative epoch milliseconds")
    if end < start:
        raise ValueError("negative task elapsed time")
    if type(execution["exitCode"]) is not int or execution["exitCode"] != 0:
        raise ValueError("task execution is incomplete or unsuccessful")
    return start, end


def measure_tasks(summaries, job):
    """Read only actual task execution, never replayed logs or stored timeSaved."""
    if not isinstance(summaries, list) or not summaries:
        raise ValueError("missing task summaries")
    metrics, seen = {}, set()
    test_prefix = "turbo run test:integration --filter="
    for summary in summaries:
        if summary["version"] != "1" or summary["turboVersion"] != "2.10.12":
            raise ValueError("unsupported task summary or Turbo version")
        if not isinstance(summary["id"], str) or not summary["id"] or summary["id"] in seen:
            raise ValueError("missing or duplicate task summary id")
        seen.add(summary["id"])
        execution = summary["execution"]
        command = execution["command"]
        if not isinstance(command, str):
            raise ValueError("missing task summary command")
        if command == "turbo run compile":
            phase, task_id = "initial graph compile task", "@apps/hash-graph#compile"
        elif command.startswith(test_prefix):
            package = command.removeprefix(test_prefix)
            if not package or any(character.isspace() for character in package):
                raise ValueError("ambiguous integration test command")
            phase, task_id = "selected integration test task", f"{package}#test:integration"
        else:
            continue
        if phase in metrics:
            raise ValueError(f"duplicate invocation for {phase}")
        start, end = task_interval(execution)
        # Actions timestamps have second precision; allow that boundary precision.
        job_start = timestamp(job["started_at"]).timestamp() * 1000
        job_end = timestamp(job["completed_at"]).timestamp() * 1000
        if start < job_start - 1000 or end > job_end + 1000:
            raise ValueError("task summary falls outside job timestamps")
        tasks = summary["tasks"]
        if not isinstance(tasks, list) or len({task["taskId"] for task in tasks}) != len(tasks):
            raise ValueError("invalid or duplicate task ids")
        matched = [task for task in tasks if task["taskId"] == task_id]
        if len(matched) != 1:
            raise ValueError(f"missing matched task {task_id}")
        task = matched[0]
        task_start, task_end = task_interval(task["execution"])
        if not start <= task_start <= task_end <= end:
            raise ValueError(f"task timestamps fall outside invocation: {task_id}")
        if task["turbo_cache_status"] != "MISS":
            raise ValueError(f"task is cached or cache status is unknown: {task_id}")
        if task_id == "@apps/hash-graph#compile" and task["task_cache_enabled"] is not False:
            raise ValueError("graph task must disable Turbo caching")
        metrics[phase] = (task_end - task_start) / 1000
    expected = {"initial graph compile task", "selected integration test task"}
    if expected - metrics.keys():
        raise ValueError(f"missing invocation: {', '.join(sorted(expected - metrics.keys()))}")
    return metrics


def graph_cache_states(entry, jobs, directory):
    """Require both an Actions hit and separately collected helper acceptance."""
    if entry["variant"] != "candidate":
        return "not-applicable", {}, []
    files = entry.get("graph_cache_receipts", {})
    issues = []
    if not isinstance(files, dict):
        files = {}
        issues.append("graph_cache_receipts must map exact job names to receipt paths")
    names = {job["name"] for job in jobs}
    if files.keys() - names:
        issues.append("graph_cache_receipts contains unknown job names")
    states = {}
    for job in jobs:
        preparation = [step for step in job.get("steps", [])
                       if step["name"] == "Prepare graph build"
                       and step.get("conclusion") != "skipped"]
        if not preparation and job["name"] not in files:
            continue
        states[job["name"]] = "unknown"
        try:
            restore = [step for step in job.get("steps", [])
                       if step["name"] == "Restore graph build"]
            if (len(preparation) != 1 or preparation[0].get("conclusion") != "success"
                    or len(restore) != 1 or restore[0].get("conclusion") != "success"):
                raise ValueError("preparation and restore must both have succeeded")
            path = files[job["name"]]
            if list(files.values()).count(path) != 1:
                raise ValueError("receipt path is shared by multiple jobs")
            receipt = json.loads((directory / path).read_text())
            if not re.fullmatch(r"graph-build-v[0-9]+-[0-9a-f]{64}", receipt["GRAPH_PREPARED_KEY"]):
                raise ValueError("missing or invalid prepared key")
            hit = receipt["GRAPH_CACHE_HIT"]
            if hit not in ("true", "false", ""):
                raise ValueError("unknown Actions cache-hit value")
            accepted = receipt.get("helper_restore_accepted")
            if accepted is not None and type(accepted) is not bool:
                raise ValueError("helper_restore_accepted must be a boolean")
            if accepted is True and hit != "true":
                raise ValueError("helper acceptance contradicts Actions cache miss")
            if hit != "true":
                states[job["name"]] = "actions-cache-miss"
            elif accepted is True:
                states[job["name"]] = "accepted-cache-hit"
            else:
                states[job["name"]] = "restore-rejected" if accepted is False else "actions-cache-hit"
        except (KeyError, TypeError, ValueError, OSError) as error:
            issues.append(f"job {job['name']}: invalid graph cache evidence: {error}")
    observed = set(states.values())
    if not observed or "unknown" in observed or issues:
        state = "unknown"
    else:
        if len(observed) == 1:
            state = next(iter(observed))
        elif "restore-rejected" in observed:
            state = "mixed-restore-rejected"
        else:
            state = "mixed-unverified" if "actions-cache-hit" in observed else "mixed"
    return state, states, issues


def measure(entry, directory, phase_steps):
    run = json.loads((directory / entry["run"]).read_text())
    jobs = load_jobs(json.loads((directory / entry["jobs"]).read_text()))
    result = {
        key: entry[key]
        for key in (
            "variant", "cache_condition", "source_sha", "workflow_sha",
            "cache_evidence", "test_results", "runner_details", "task_timings", "graph_cache_receipts",
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
    task_files = entry.get("task_timings", {})
    if not isinstance(task_files, dict) or task_files.keys() - set(names):
        raise ValueError("task_timings must map exact job names to summary paths")
    state, cache_states, issues = graph_cache_states(entry, jobs, directory)
    result.update(graph_cache_state=state, graph_cache_job_states=cache_states, graph_cache_issues=issues)
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
        record["graph_cache_state"] = cache_states.get(job["name"], "not-applicable")
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
        if job["name"] in task_files:
            record["task_timing_issues"] = []
            try:
                summaries = json.loads((directory / task_files[job["name"]]).read_text())
                task_metrics = measure_tasks(summaries, job)
                metrics.update({f"{job['name']} / {phase}": seconds
                                for phase, seconds in task_metrics.items()})
            except (KeyError, TypeError, ValueError, OSError) as error:
                record["task_timing_issues"].append(f"invalid task timing evidence: {error}")
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
                state = "not-applicable"
                if run["variant"] == "candidate":
                    state = run.get("graph_cache_state", "unknown")
                    for name, job_state in run.get("graph_cache_job_states", {}).items():
                        if metric.startswith(name + " / "):
                            state = job_state
                            break
                groups[(run.get("scope", "unspecified"), run["cache_condition"], metric,
                        run["variant"], state)].append((seconds, run["url"]))
    summaries = []
    for scope, condition, metric in sorted({key[:3] for key in groups}):
        states = {key[4] for key in groups if key[:4] == (scope, condition, metric, "candidate")} or {"unknown"}
        for state in sorted(states):
            observed = {"accepted-cache-hit": "warm", "actions-cache-hit": "actions-hit-unverified",
                        "actions-cache-miss": "cold" if condition == "cold" else "primed-miss"}.get(state, state)
            row = {"scope": scope, "planned_cache_condition": condition, "cache_condition": observed,
                   "candidate_graph_cache_state": state, "metric": metric}
            for variant in ("baseline", "candidate"):
                sample_state = "not-applicable" if variant == "baseline" else state
                samples = groups.get((scope, condition, metric, variant, sample_state), [])
                values = [sample[0] for sample in samples]
                row[variant] = {
                    "graph_cache_state": sample_state,
                    "samples": len(values), "median_seconds": median(values) if values else None,
                    "range_seconds": [min(values), max(values)] if values else None,
                    "run_links": [sample[1] for sample in samples],
                }
            enough = all(row[variant]["samples"] >= 3 for variant in ("baseline", "candidate"))
            verified = state in {"accepted-cache-hit", "actions-cache-miss", "mixed"}
            row["comparison_status"] = ("unverified graph cache evidence" if not verified else
                                        "descriptive comparison" if enough else "insufficient successful samples")
            before = row["baseline"]["median_seconds"]
            after = row["candidate"]["median_seconds"]
            row["seconds_saved"] = before - after if enough and verified else None
            row["percent_saved"] = 100 * (before - after) / before if enough and verified and before else None
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
            "Optional Turbo task times are current MISS execution envelopes, including task bookkeeping; they exclude prerequisites. Invalid task evidence is listed per job and omitted from task metrics; API envelopes remain available.",
            "Failed, incomplete, duplicate and explicitly excluded attempts remain listed but do not contribute to summaries.",
            "Savings require at least three successful samples per variant. Labels alone do not establish equivalent sources, runners, caches or test execution.",
            "Run cache_condition is the planned condition. Comparison cache_condition is observed for the candidate; baseline graph caching is not applicable. Warm requires Actions hit plus current helper acceptance in every affected job for workflow metrics, or in the specific job for its metrics.",
            "Actions-only hits, unknown receipts and rejected restores retain descriptive measurements but cannot produce savings. Mixed accepted-hit/miss workflows remain a separate comparison; their accepted-hit jobs can contribute to per-job warm comparisons.",
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
