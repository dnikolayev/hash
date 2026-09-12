#!/usr/bin/env python3
"""Validate and summarize the retained benchmark projections, without network access."""
from __future__ import annotations

import argparse
import collections
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import re
from statistics import median
import sys
import unittest

SOURCE = "2d8904b0bdfe7e65578294b921e0b6ef1bf2aaae"
BASE = "8aedda02ba9162e77735234168281151d2d2584d"
REPOSITORY = "dnikolayev/hash"
PACKAGES = ("@rust/hash-graph-http-tests", "@tests/hash-backend-integration", "@tests/hash-playwright")
SELECTED = tuple(f"Integration ({package})" for package in PACKAGES)
LEGACY_ORDER = tuple(label for sample in "123" for label in
                     (f"baseline-{sample}", f"candidate-{sample}-cold", f"candidate-{sample}-warm"))
ORDER = tuple(label for sample in "123" for label in
              (f"baseline-{sample}-cold-tools", f"candidate-{sample}-cold",
               f"baseline-{sample}-warm-tools", f"candidate-{sample}-warm"))
CACHE_STEPS = ("Prepare graph build", "Require graph build preparation", "Restore graph build",
               "Prepare graph build for upload", "Save graph build")
PHASES = {"graph preparation": "Prepare graph build", "graph restore": "Restore graph build",
          "graph upload preparation": "Prepare graph build for upload", "graph save": "Save graph build",
          "background startup and readiness": "Start background tasks", "test execution": "Run tests"}
EXPECTED_COUNTS = {
    SELECTED[0]: {"http_requests": {"setup": {"processed": 49, "succeeded": 49},
                                  "test": {"processed": 181, "succeeded": 181}}},
    SELECTED[1]: {"vitest_suites": [
        {"test_files": {"passed": 19, "total": 19}, "tests": {"passed": 110, "skipped": 9, "total": 119}},
        {"test_files": {"passed": 3, "total": 3}, "tests": {"passed": 48, "total": 48}}]},
    SELECTED[2]: {"playwright": {"passed": 32, "skipped": 8}},
}


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(parsed.tzinfo is not None, "timestamp lacks timezone")
    return parsed


def elapsed(start, end):
    seconds = (timestamp(end) - timestamp(start)).total_seconds()
    require(seconds >= 0, "negative time interval")
    return seconds


def interval(execution):
    start, end = execution["startTime"], execution["endTime"]
    require(type(start) is int and type(end) is int and 0 <= start <= end, "invalid task interval")
    require(type(execution["exitCode"]) is int and execution["exitCode"] == 0, "unsuccessful task")
    return start, end


def task_metrics(summaries, job):
    metrics, identifiers = {}, set()
    for summary in summaries:
        require(summary["version"] == "1" and summary["turboVersion"] == "2.10.12", "unknown Turbo schema")
        require(summary["id"] and summary["id"] not in identifiers, "duplicate task summary")
        identifiers.add(summary["id"])
        execution = summary["execution"]
        command = execution["command"]
        if command == "turbo run compile":
            phase, task_id = "initial graph compile task", "@apps/hash-graph#compile"
        else:
            package = job["name"].removeprefix("Integration (").removesuffix(")")
            require(command == f"turbo run test:integration --filter={package}", "unexpected test invocation")
            phase, task_id = "selected integration test task", package + "#test:integration"
        require(phase not in metrics, "duplicate measured task")
        start, end = interval(execution)
        require(timestamp(job["started_at"]).timestamp() * 1000 - 1000 <= start <= end <=
                timestamp(job["completed_at"]).timestamp() * 1000 + 1000, "task outside job")
        require(len(summary["tasks"]) == 1, "expected one projected selected task")
        task = summary["tasks"][0]
        require(task["taskId"] == task_id and task["turbo_cache_status"] == "MISS", "task is not a current MISS")
        if phase == "initial graph compile task":
            require(task["task_cache_enabled"] is False, "graph task has Turbo caching enabled")
        task_start, task_end = interval(task["execution"])
        require(start <= task_start <= task_end <= end, "task outside invocation")
        metrics[phase] = (task_end - task_start) / 1000
    require(set(metrics) == {"initial graph compile task", "selected integration test task"}, "missing task")
    return metrics


def measure(run):
    metrics, active = {}, []
    for job in run["jobs"]:
        if job["conclusion"] == "skipped":
            continue
        require(job["status"] == "completed" and job["conclusion"] == "success", "unsuccessful measured job")
        active.append(job)
        name = job["name"]
        metrics[name + " / wall"] = elapsed(job["started_at"], job["completed_at"])
        if job["created_at"] is not None:
            metrics[name + " / waiting (dependencies and queue)"] = elapsed(job["created_at"], job["started_at"])
        for phase, step_name in PHASES.items():
            steps = [step for step in job["steps"] if step["name"] == step_name and step["conclusion"] != "skipped"]
            if steps and all(step["conclusion"] == "success" and step["started_at"] and step["completed_at"] for step in steps):
                metrics[name + " / " + phase] = sum(elapsed(step["started_at"], step["completed_at"]) for step in steps)
        if name in SELECTED:
            metrics.update({name + " / " + phase: value for phase, value in
                            task_metrics(run["selected_jobs"][name]["task_summaries"], job).items()})
    require(active, "no measured jobs")
    metrics["workflow / wall"] = elapsed(run["run_started_at"], max(job["completed_at"] for job in active))
    metrics["workflow / initial wait"] = elapsed(run["run_started_at"], min(job["started_at"] for job in active))
    metrics["workflow / aggregate jobs"] = sum(metrics[job["name"] + " / wall"] for job in active)
    return metrics


def validate_pre_cache_ordering(run):
    before = run["cache_observations"]["before"]
    captured, started = timestamp(before["captured_at_utc"]), timestamp(run["run_started_at"])
    proof = run.get("pre_cache_ordering")
    if captured < started:
        require(proof is None or proof == {"method": "snapshot-before-run-start"}, "unexpected dispatch proof")
        return
    require(isinstance(proof, dict) and proof.get("method") == "same-second-local-pre-dispatch",
            "cache preflight lacks retained same-second dispatch evidence")
    require(type(proof.get("run_id")) is int and type(proof.get("run_attempt")) is int
            and proof["run_attempt"] >= 2
            and proof.get("full_workflow") is True and proof.get("source_or_test_changes") is False
            and all(proof.get(key) == run[key] for key in ("run_id", "run_attempt", "head_sha", "run_started_at")),
            "dispatch proof attempt mismatch")
    require(proof.get("snapshot_captured_at_utc") == before["captured_at_utc"], "dispatch snapshot time mismatch")
    intended = timestamp(proof["intent_at_utc"])
    require(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:Z|\+00:00)", run["run_started_at"])
            and started <= captured < intended < started + timedelta(seconds=1), "dispatch ordering is ambiguous")
    require(type(proof.get("response_status")) is int and proof["response_status"] == 201
            and intended <= timestamp(proof["response_date_utc"]) <= timestamp(run["updated_at"]),
            "dispatch response is invalid")
    files = {row["name"]: {key: row[key] for key in ("bytes", "sha256")} for row in run["provenance"]["files"]}
    require(proof.get("inventory_sha256") == before["inventory_sha256"]
            == files["cache-inventory-before.json"]["sha256"], "dispatch inventory digest mismatch")
    require(proof.get("snapshot_receipt_sha256") == files["cache-inventory-before-receipt.json"]["sha256"],
            "dispatch snapshot receipt digest mismatch")
    require(set(proof.get("sources", {})) == {"dispatch-intent.json", "dispatch-response.json"},
            "dispatch proof needs both source records")
    for name, source in proof["sources"].items():
        require(source == files.get(name) and type(source.get("bytes")) is int and source["bytes"] > 0
                and re.fullmatch(r"[0-9a-f]{64}", source.get("sha256", "")), "dispatch source digest mismatch")


def cache_identity(entries):
    fields = ("id", "key", "version", "ref", "size_in_bytes", "created_at")
    return sorted(tuple(entry[field] for field in fields) for entry in entries)


def validate_tool_cache_state(run):
    """Validate the normalized ordinary-cache markers independently of cohort labels."""
    state = run["ordinary_tool_cache"]
    require(state["schema"] == 1 and all(state[field] == run[field] for field in
            ("run_id", "run_attempt", "head_sha", "run_started_at")), "tool-cache attempt identity mismatch")
    files = {row["name"]: {"bytes": row["bytes"], "sha256": row["sha256"]} for row in run["provenance"]["files"]}
    require(set(state["sources"]) == {"run.json", "jobs.json", "full-workflow-logs.zip"} and
            all(value == files[name] for name, value in state["sources"].items()), "tool-cache raw provenance mismatch")
    jobs = {job["id"]: job for job in run["jobs"]}
    expected = {job["id"] for job in run["jobs"] if job["name"] == "setup" or job["name"].startswith(("Unit (", "Integration ("))}
    records = state["observations"]
    require({row["job_id"] for row in records} == expected and len(records) == len(expected) == 74,
            "tool-cache observation topology mismatch")
    require(state["tool_call_count"] == len(records) and state["matrix_tool_hit_count"] == len(records) - 1,
            "tool-cache call counts mismatch")
    require(set(state["cache_keys"]) == {"mise", "corepack"} and state["cache_keys"]["mise"].startswith("mise-")
            and state["cache_keys"]["corepack"].startswith("yarn-corepack-"), "ordinary tool cache keys unavailable")
    setup = jobs[state["setup_job_id"]]
    require(setup["name"] == "setup" and setup["completed_at"] == state["setup_completed_at"], "setup identity mismatch")
    setup_states = []
    for row in records:
        job = jobs[row["job_id"]]
        require(row["job_name"] == job["name"] and row["job_started_at"] == job["started_at"] and
                row["job_completed_at"] == job["completed_at"], "tool-cache job envelope mismatch")
        steps = [step for step in job["steps"] if step["name"] == "Install tools"]
        require(len(steps) == 1, "missing or duplicate Install tools step")
        step = steps[0]
        require(step["conclusion"] == row["step_conclusion"] == "success" and row["step_number"] == step["number"] and
                row["step_started_at"] == step["started_at"] and row["step_completed_at"] == step["completed_at"],
                "tool-cache step envelope mismatch")
        member = f"{job['name'].replace('/', '_')}/{step['number']}_Install tools.txt"
        require(row["log_member"] == member and row["log_member_identity"]["bytes"] > 0 and
                re.fullmatch(r"[0-9a-f]{64}", row["log_member_identity"]["sha256"]), "tool-cache log member identity mismatch")
        require(set(row["tools"]) == {"mise", "corepack"}, "tool-cache tools missing")
        for tool, record in row["tools"].items():
            restore = record["restore"]
            outcome = record["state"]
            require(outcome in {"hit", "miss"} and restore["kind"] == outcome and restore["key"] == state["cache_keys"][tool],
                    "tool-cache restore marker mismatch")
            markers = [restore]
            if outcome == "miss":
                saved = record["save"]
                require(saved is not None and saved["kind"] == "save" and saved["key"] == restore["key"] and
                        timestamp(saved["at"]) >= timestamp(restore["at"]) and saved["line"] > restore["line"],
                        "tool-cache save marker mismatch")
                markers.append(saved)
            else:
                require(record["save"] is None, "tool-cache hit unexpectedly saved")
            for marker in markers:
                require(type(marker["line"]) is int and marker["line"] > 0 and
                        timestamp(step["started_at"]) <= timestamp(marker["at"]) < timestamp(step["completed_at"]) + timedelta(seconds=1),
                        "tool-cache marker outside install step")
            if job["name"] == "setup":
                if outcome == "miss":
                    matrix_start = min(timestamp(jobs[job_id]["started_at"]) for job_id in expected if job_id != setup["id"])
                    require(timestamp(record["save"]["at"]) <= timestamp(setup["completed_at"]) and
                            timestamp(record["save"]["at"]) < matrix_start, "setup tool-cache save was late")
                setup_states.append(outcome)
            else:
                require(outcome == "hit" and timestamp(job["started_at"]) >= timestamp(setup["completed_at"]),
                        "matrix tool caches are not warm after setup")
    require(len(setup_states) == 2 and len(set(setup_states)) == 1, "setup tool caches have mixed states")
    condition = "cold" if setup_states[0] == "miss" else "warm"
    require(state["tool_cache_condition"] == condition, "tool-cache classification mismatch")
    final = max(timestamp(job["completed_at"]) for job in run["jobs"] if job["conclusion"] != "skipped")
    prefix = elapsed(run["run_started_at"], setup["completed_at"])
    tail = (final - timestamp(setup["completed_at"])).total_seconds()
    require(timestamp(state["final_completed_at"]) == final and state["setup_prefix_seconds"] == prefix and
            state["setup_to_final_seconds"] == tail and state["workflow_wall_seconds"] == prefix + tail and
            state["workflow_wall_seconds"] == run["expected_metrics_seconds"]["workflow / wall"], "setup/remaining-workflow envelope mismatch")
    return condition


def validate_run(run, data):
    label = run["label"]
    require(label in (ORDER if data["schema"] == 2 else LEGACY_ORDER), "unknown sample label")
    variant = "baseline" if label.startswith("baseline-") else "candidate"
    condition = "control" if variant == "baseline" else label.rsplit("-", 1)[1]
    require(run["variant"] == variant and run["condition"] == condition, "condition label mismatch")
    require(run["accepted"] is True and run["status"] == "completed" and run["conclusion"] == "success", "run was not accepted")
    require(run["source_sha"] == SOURCE, "executed source mismatch")
    if data["schema"] == 2:
        tool_cohort = validate_tool_cache_state(run)
        if variant == "baseline":
            require(run["control_cohort"] == tool_cohort and label == f"baseline-{run['sample']}-{tool_cohort}-tools",
                    "control cohort differs from observed tool caches")
        else:
            require(run["control_cohort"] is None and tool_cohort == condition,
                    "candidate condition differs from observed tool caches")
        require(run["harness_label"] == (f"baseline-{run['sample']}" if variant == "baseline" else label),
                "original harness label mismatch")
    sample = label.split("-")[1]
    pin = data["bindings"]["branch_commits"][variant + "-" + sample]
    require(run["cold_sha"] == pin, "harness pin mismatch")
    require(run["workflow_sha256"] == data["bindings"]["workflow_sha256"][variant + "-" + sample + ".yml"], "workflow digest mismatch")
    require(run["commit"]["sha"] == run["head_sha"], "commit head mismatch")
    if condition == "warm":
        require(run["commit"]["parents"] == [pin] and run["commit"]["tree"] == run["parent_commit"]["tree"], "warm commit is not an empty child")
        require(run["commit"]["changed_files"] == [], "warm commit changed files")
    else:
        require(run["head_sha"] == pin and run["commit"]["parents"] == [SOURCE], "cold/control source parent mismatch")
        require(run["commit"]["changed_files"] == [run["workflow_path"]], "harness changes extend beyond workflow")
    require(run["parent_commit"]["sha"] == run["commit"]["parents"][0], "parent receipt mismatch")
    expected_url = f"https://github.com/{REPOSITORY}/actions/runs/{run['run_id']}/attempts/{run['run_attempt']}"
    require(run["url"] == expected_url, "attempt link mismatch")
    require(len({job["id"] for job in run["jobs"]}) == len(run["jobs"]) and
            len({job["name"] for job in run["jobs"]}) == len(run["jobs"]), "duplicate job")
    require(set(run["selected_jobs"]) == set(SELECTED), "selected scope mismatch")
    checks = [job for job in run["jobs"] if job["name"] == "Graph build cache checks"]
    require(len(checks) == (1 if variant == "candidate" else 0), "helper checker topology mismatch")
    terminal = [job for job in run["jobs"] if job["name"] == "Tests passed"]
    require(len(terminal) == 1 and terminal[0]["completed_at"] == max(
        job["completed_at"] for job in run["jobs"] if job["conclusion"] != "skipped"), "terminal job mismatch")
    keys, inputs = set(), set()
    for name, selected in run["selected_jobs"].items():
        require(selected["test_counts"] == EXPECTED_COUNTS[name], "behavior count mismatch")
        cpu = selected["cpu_class"]
        require(cpu in data["cpu_classes"] and digest(data["cpu_classes"][cpu]) == cpu, "CPU class identity mismatch")
        environment = selected["tools"]
        require(environment["checkout_sha"] == SOURCE and environment["workflow_sha"] == run["head_sha"] and
                environment["run_id"] == str(run["run_id"]) and environment["run_attempt"] == str(run["run_attempt"]), "environment identity mismatch")
        require(environment["turbo_cache"] == "local:rw" and environment["sccache"] == "disabled" and
                environment["cargo_incremental"] == "0", "cache policy mismatch")
        raw, counts = selected["cache"], selected["cargo_counts"]
        current = [row for row in selected["cargo_invocations"] if row["cache_log_status"] == "bypass; current execution"]
        begin, end = selected["initial_compile_log_group_lines"]
        initial_cargo = [row for row in current if row["group_line"] == begin and type(row["finished_line"]) is int
                         and begin < row["finished_line"] < end]
        actual_counts = {"current": len(current), "source_build": sum(row["compiling_lines"] > 0 for row in current),
                         "initial_current": len(initial_cargo),
                         "initial_source_build": sum(row["compiling_lines"] > 0 for row in initial_cargo)}
        require(counts == actual_counts, "Cargo counts differ from retained invocation facts")
        steps = selected["step_conclusions"]
        require(raw["graph_binary_size_bytes"] > 0, "binary size unavailable")
        graph_tasks = [task for summary in selected["task_summaries"] for task in summary["tasks"]
                       if task["taskId"] == "@apps/hash-graph#compile"]
        expected_command = ("cargo build --bin hash-graph --all-features" if variant == "baseline" else
                            "python3 ../../.github/actions/graph-build-cache/graph_build_cache.py compile")
        require(len(graph_tasks) == 1 and graph_tasks[0]["command"] == expected_command, "graph compile command changed")
        initial = [(event["phase"], event["status"]) for event in selected["initial_helper_events"]
                   if event["phase"] not in {"lock", "cpu-probe"}]
        if condition == "control":
            require(selected["cache_modes"] == ["0"] and not selected["helper_events"], "baseline helper executed")
            require(all(steps[step] == "skipped" for step in CACHE_STEPS), "baseline cache step executed")
            require(all(raw[field] == "" for field in ("GRAPH_CACHE_ENABLED", "GRAPH_CACHE_PRODUCER", "GRAPH_PREPARED_KEY", "GRAPH_CACHE_HIT", "GRAPH_UPLOAD_VALID"))
                    and raw["GRAPH_ACTIONS_KEY"] == run["namespace"] and raw["GRAPH_SAVE_OUTCOME"] == "skipped", "baseline cache receipt mismatch")
            require(counts["initial_current"] == counts["initial_source_build"] == counts["source_build"] == 1, "baseline initial build missing")
            continue
        require(selected["cache_modes"] == ["1"] and raw["GRAPH_CACHE_ENABLED"] == "true", "candidate reuse disabled")
        key = raw["GRAPH_PREPARED_KEY"]
        require(re.fullmatch(r"graph-build-v[0-9]+-[0-9a-f]{64}", key), "prepared key unavailable")
        require(raw["GRAPH_ACTIONS_KEY"] == run["namespace"] + key, "Actions key mismatch")
        keys.add(key)
        inputs.add(selected["graph_inputs_sha256"])
        require(re.fullmatch(r"[0-9a-f]{64}", selected["graph_inputs_sha256"] or ""), "input digest unavailable")
        producer = name == SELECTED[0]
        require(raw["GRAPH_CACHE_PRODUCER"] == str(producer).lower(), "producer mismatch")
        require(selected["reservation_warning_count"] == 0, "reservation warning")
        require(all(steps[step] == "success" for step in CACHE_STEPS[:3]), "candidate preparation failed")
        if condition == "cold":
            require(selected["actions_result"] == "miss" and raw["GRAPH_CACHE_HIT"] in {"", "false"}, "cold was not a miss")
            require(initial == [("prepare", "ready"), ("restore", "miss"), ("build", "success"), ("reuse", "ready")], "cold helper sequence mismatch")
            require(counts["current"] >= 1 and all(counts[field] == 1 for field in
                    ("source_build", "initial_current", "initial_source_build")), "cold compile count mismatch")
        else:
            require(selected["actions_result"] == "hit" and raw["GRAPH_CACHE_HIT"] == "true" and selected["helper_restore_accepted"] is True, "warm payload was not accepted")
            require(initial == [("prepare", "ready"), ("restore", "hit")], "warm helper sequence mismatch")
            require(all(counts[field] == 0 for field in counts), "warm ran Cargo")
        publishing = condition == "cold" and producer
        require(selected["publish_ready_count"] == selected["cache_saved_count"] == int(publishing), "publisher count mismatch")
        require(all(steps[step] == ("success" if publishing else "skipped") for step in CACHE_STEPS[3:]), "upload step mismatch")
        require(raw["GRAPH_SAVE_OUTCOME"] == ("success" if publishing else "skipped"), "save outcome mismatch")
        require(raw["GRAPH_UPLOAD_VALID"] == "true" if publishing else raw["GRAPH_UPLOAD_VALID"] in {None, ""}, "upload validity mismatch")
        manifest = selected["graph_manifest"]
        if publishing or condition == "warm":
            require(manifest is not None and manifest["key"] == key and
                    re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"] or ""), "payload manifest mismatch")
        else:
            require(manifest is None, "cold consumer unexpectedly retained a payload")
    if variant == "candidate":
        require(len(keys) == len(inputs) == 1 and None not in inputs, "candidate keys/input digests differ")
        before, after = run["cache_inventory"]["before"], run["cache_inventory"]["after"]
        require(len(after) == 1 and after[0]["key"] == run["namespace"] + next(iter(keys)), "cache output mismatch")
        require(not before if condition == "cold" else cache_identity(before) == cache_identity(after), "cache condition mismatch")
        observations = run["cache_observations"]
        validate_pre_cache_ordering(run)
        require(timestamp(observations["after"]["captured_at_utc"]) >= timestamp(terminal[0]["completed_at"]), "cache output snapshot was early")
        for when in ("before", "after"):
            require(observations[when]["namespace_entry_count"] == len(run["cache_inventory"][when]), "cache observation count mismatch")
    computed = measure(run)
    require(computed == run["expected_metrics_seconds"], "recomputed metrics differ from verified acquisition")
    return computed


def statistics(values, links):
    return {"samples": len(values), "median_seconds": median(values) if values else None,
            "range_seconds": [min(values), max(values)] if values else None, "run_links": links}


def comparison(before, after, *, allow_savings):
    result = {"baseline": statistics([v for v, _ in before], [u for _, u in before]),
              "candidate": statistics([v for v, _ in after], [u for _, u in after])}
    enough = allow_savings and len(before) >= 3 and len(after) >= 3
    b, a = result["baseline"]["median_seconds"], result["candidate"]["median_seconds"]
    result.update(seconds_saved=b-a if enough else None,
                  percent_saved=100*(b-a)/b if enough and b else None,
                  status="descriptive comparison" if enough else "insufficient accepted samples")
    return result


def in_comparison(run, condition, schema):
    if run["variant"] == "baseline":
        return schema == 1 or run["control_cohort"] == condition
    return run["condition"] == condition


def analyze(data, *, require_complete=True):
    require(data["schema"] in (1, 2) and data["repository"] == REPOSITORY, "unsupported evidence schema")
    require(data["bindings"]["implementation_commit"] == SOURCE and data["bindings"]["base_commit"] == BASE, "source pins mismatch")
    require(data["bindings"]["run_order"] == list(LEGACY_ORDER), "workflow generation order mismatch")
    if data["schema"] == 2:
        require(data["cohort_order"] == list(ORDER), "explicit control cohort order mismatch")
    else:
        require(not require_complete, "schema 1 uses superseded common controls; only historical incomplete analysis is allowed")
    runs = data["runs"]
    require(len({run["label"] for run in runs}) == len(runs), "duplicate label")
    require(len({(run["run_id"], run["run_attempt"]) for run in runs}) == len(runs), "duplicate attempt")
    accepted_ids = {(run["run_id"], run["run_attempt"]) for run in runs}
    excluded_ids = set()
    for excluded in data["excluded_attempts"]:
        identity = (excluded["run_id"], excluded["run_attempt"])
        require(identity not in accepted_ids and identity not in excluded_ids, "excluded attempt is duplicated or measured")
        excluded_ids.add(identity)
        require(excluded["reason"] and excluded["conclusion"] != "success", "exclusion lacks a failure reason")
        require(excluded["url"] == f"https://github.com/{REPOSITORY}/actions/runs/{identity[0]}/attempts/{identity[1]}", "excluded attempt link mismatch")
    complete = data["schema"] == 2 and {run["label"] for run in runs} == set(ORDER)
    require(not require_complete or complete, "campaign is incomplete")
    measured = {run["label"]: validate_run(run, data) for run in runs}
    if data["schema"] == 2:
        key_sets = {json.dumps(run["ordinary_tool_cache"]["cache_keys"], sort_keys=True) for run in runs}
        require(len(key_sets) <= 1, "ordinary tool cache keys differ across the campaign")
    by_label = {run["label"]: run for run in runs}
    topology = None
    for run in runs:
        current = sorted((job["name"], job["conclusion"]) for job in run["jobs"] if job["name"] != "Graph build cache checks")
        require(topology is None or topology == current, "job topology changed")
        topology = current
    for sample in "123":
        cold, warm = by_label.get(f"candidate-{sample}-cold"), by_label.get(f"candidate-{sample}-warm")
        if cold and warm:
            require(warm["commit"]["parents"] == [cold["head_sha"]], "warm parent differs from cold")
            require(cache_identity(cold["cache_inventory"]["after"]) == cache_identity(warm["cache_inventory"]["before"]), "warm entry differs from cold output")
            producer = cold["selected_jobs"][SELECTED[0]]
            for selected in warm["selected_jobs"].values():
                require(selected["graph_manifest"] == producer["graph_manifest"] and
                        selected["graph_inputs_sha256"] == producer["graph_inputs_sha256"] and
                        selected["cache"]["graph_binary_size_bytes"] == producer["cache"]["graph_binary_size_bytes"], "warm payload differs from cold producer")
    rows, cpu_rows = [], []
    metrics = sorted({metric for values in measured.values() for metric in values})
    for condition in ("cold", "warm"):
        cohort = [run for run in runs if in_comparison(run, condition, data["schema"])]
        for metric in metrics:
            values = {variant: [(measured[run["label"]][metric], run["url"]) for run in cohort
                                if run["variant"] == variant and metric in measured[run["label"]]]
                      for variant in ("baseline", "candidate")}
            rows.append({"condition": condition, "metric": metric,
                         **comparison(values["baseline"], values["candidate"], allow_savings=complete)})
            job_name = metric.split(" / ", 1)[0]
            if job_name not in SELECTED:
                continue
            classes = sorted({run["selected_jobs"][job_name]["cpu_class"] for run in cohort})
            for cpu in classes:
                values = {variant: [(measured[run["label"]][metric], run["url"]) for run in cohort
                                    if run["variant"] == variant and metric in measured[run["label"]]
                                    and run["selected_jobs"][job_name]["cpu_class"] == cpu]
                          for variant in ("baseline", "candidate")}
                cpu_rows.append({"condition": condition, "metric": metric, "cpu_class": cpu,
                                 **comparison(values["baseline"], values["candidate"], allow_savings=complete)})
    history = None
    if data.get("history") is not None:
        raw_history = data["history"]
        encoded_history = (json.dumps(raw_history, indent=2, sort_keys=True) + "\n").encode()
        provenance = data["history_projection_provenance"]
        require(len(encoded_history) == provenance["bytes"] and
                hashlib.sha256(encoded_history).hexdigest() == provenance["sha256"], "history projection digest mismatch")
        require(raw_history["schema"] == 1 and raw_history["repository"] == "hashintel/hash" and
                raw_history["base_sha"] == BASE and raw_history["implementation_sha"] == SOURCE,
                "history source binding mismatch")
        require(raw_history["model"]["target_names"] == list(SELECTED) and
                raw_history["model"]["producer_priority"] == list(SELECTED), "history scope mismatch")
        history = derive_history(raw_history)
        require({key: history[key] for key in raw_history["expected_summary"]} == raw_history["expected_summary"],
                "recomputed history differs from verified extraction")
    return {"complete": complete, "accepted_attempts": len(runs),
            "comparison_run_attempts": {condition: {variant: sorted([run["run_id"], run["run_attempt"]] for run in runs
                if run["variant"] == variant and in_comparison(run, condition, data["schema"]))
                for variant in ("baseline", "candidate")} for condition in ("cold", "warm")},
            "control_attempts_by_comparison": {condition: [run["url"] for run in runs if run["variant"] == "baseline"
                                                          and in_comparison(run, condition, data["schema"])]
                                                for condition in ("cold", "warm")},
            "design": "separate tool-cache-matched controls" if data["schema"] == 2 else "superseded common controls; no savings reported",
            "ordinary_tool_cache_summary": {run["label"]: {key: run["ordinary_tool_cache"][key] for key in
                ("tool_cache_condition", "tool_call_count", "matrix_tool_hit_count", "setup_prefix_seconds", "setup_to_final_seconds")}
                for run in runs} if data["schema"] == 2 else None,
            "metrics_seconds": measured, "comparisons": rows, "cpu_class_comparisons": cpu_rows,
            "excluded_attempts": data["excluded_attempts"], "history_opportunity": history}


def derive_history(data):
    """Return derived counts and per-run classification; no cache-hit observation is implied."""
    model = data['model']
    commits = data['commits']
    if len(commits) != 400 or commits[-1]['sha'] != data['base_sha']:
        raise ValueError('Expected the anchored 400 first-parent commits')
    if len({c['sha'] for c in commits}) != len(commits):
        raise ValueError('Duplicate commits')
    prefixes = tuple(directory + '/' for directory in model['closure_directories'] + model['additional_directories'])
    fixed = set(model['fixed_files'])
    by_sha = {}
    epoch = commits[0]['first_parent']
    invalidations = []
    preserving_streaks = []
    streak = 0
    for index, commit in enumerate(commits):
        if index and commit['first_parent'] != commits[index - 1]['sha']:
            raise ValueError('First-parent chain is discontinuous')
        paths = sorted(set(commit['changed_paths']))
        invalidating = [name for name in paths if name in fixed or name.startswith(prefixes)]
        if invalidating:
            epoch = commit['sha']
            if streak:
                preserving_streaks.append(streak)
                streak = 0
        else:
            streak += 1
        row = {'sha':commit['sha'], 'epoch':epoch, 'invalidating_paths':invalidating}
        by_sha[commit['sha']] = row
        invalidations.append(row)
    if streak:
        preserving_streaks.append(streak)
    runs = sorted(data['runs'], key=lambda r:(r['created_at'], r['id'], r['attempt']))
    if len({r['id'] for r in runs}) != len(runs):
        raise ValueError('Duplicate workflow runs')
    seen_epochs = set()
    producer_completions = collections.defaultdict(list)
    classified = []
    counts = collections.Counter()
    job_counts = collections.Counter()
    timed_counts = collections.Counter()
    timed_job_counts = collections.Counter()
    mixes = collections.Counter()
    mix_conditions = collections.defaultdict(collections.Counter)
    target_conditions = collections.defaultdict(collections.Counter)
    excluded = collections.Counter()
    for row in runs:
        if row['head_sha'] not in by_sha:
            raise ValueError('Run source is outside the anchored first-parent sample')
        epoch = by_sha[row['head_sha']]['epoch']
        if row['event'] != 'push' or row['head_branch'] != 'main' or row['workflow_path'] != '.github/workflows/test.yml':
            raise ValueError('Unexpected workflow sample row')
        base = {'id':row['id'], 'attempt':row['attempt'], 'head_sha':row['head_sha'], 'epoch':epoch}
        if row['conclusion'] != 'success' or row['status'] != 'completed':
            excluded[row['conclusion'] or row['status']] += 1
            classified.append(dict(base, condition='excluded-unsuccessful', target_job_count=0))
            continue
        expected_names = sorted(name for name in row['all_job_names'] if name in SELECTED)
        actual_names = sorted(job['name'] for job in row['target_jobs'])
        if actual_names != expected_names or len(actual_names) != len(set(actual_names)):
            raise ValueError('Target selection mismatch')
        if any(j['conclusion'] != 'success' or j['status'] != 'completed' for j in row['target_jobs']):
            raise ValueError('Target job did not succeed')
        if not actual_names:
            counts['no_target'] += 1
            classified.append(dict(base, condition='no-target', target_job_count=0))
            continue
        condition = 'warm' if epoch in seen_epochs else 'cold'
        # Secondary availability model: a prior successful workflow's selected producer
        # must have finished before this workflow/each consumer started. Cache save could
        # happen earlier within that producer; using completion is deliberately later.
        ready_times = producer_completions[epoch]
        ready = min(ready_times) if ready_times else None
        timed = 'warm' if ready and ready <= row['run_started_at'] else 'cold'
        counts[condition] += 1
        job_counts[condition] += len(actual_names)
        timed_counts[timed] += 1
        for job in row['target_jobs']:
            timed_job_counts['warm' if ready and ready <= job['started_at'] else 'cold'] += 1
        mix = ' + '.join(name.removeprefix('Integration (').removesuffix(')') for name in actual_names)
        mixes[mix] += 1
        mix_conditions[mix][condition] += 1
        for name in actual_names:
            target_conditions[name][condition] += 1
        producer = next(job for name in SELECTED for job in row['target_jobs'] if job['name'] == name)
        classified.append(dict(base, condition=condition, target_job_count=len(actual_names),
                               producer_job_id=producer['id'], producer_completed_at=producer['completed_at'],
                               preceding_producer_completed_at=ready, completion_aware_condition=timed))
        seen_epochs.add(epoch)
        producer_completions[epoch].append(producer['completed_at'])
    source = {}
    for size in (100,400):
        cold = sum(bool(c['invalidating_paths']) for c in invalidations[-size:])
        source[str(size)] = {'commits':size,'invalidating':cold,'preserving':size-cold,'preserving_percent':100*(size-cold)/size}
    eligible = counts['cold'] + counts['warm']
    targets = job_counts['cold'] + job_counts['warm']
    return {
        'source':source,
        'preserving_streaks':{'values':preserving_streaks,'median':median(preserving_streaks) if preserving_streaks else None,'maximum':max(preserving_streaks) if preserving_streaks else None},
        'main_push_runs':len(runs),
        'successful_runs':eligible+counts['no_target'],
        'no_target_runs':counts['no_target'],
        'eligible_runs':eligible,
        'modeled_runs':dict(sorted((k,counts[k]) for k in ('cold','warm'))),
        'modeled_warm_run_percent':100*counts['warm']/eligible if eligible else None,
        'target_jobs':targets,
        'modeled_jobs':dict(sorted(job_counts.items())),
        'modeled_warm_job_percent':100*job_counts['warm']/targets if targets else None,
        'completion_aware_runs':dict(sorted(timed_counts.items())),
        'completion_aware_jobs':dict(sorted(timed_job_counts.items())),
        'job_mixes':dict(sorted(mixes.items())),
        'job_mixes_by_condition':{k:dict(sorted(v.items())) for k,v in sorted(mix_conditions.items())},
        'target_scopes_by_condition':{k:dict(sorted(v.items())) for k,v in sorted(target_conditions.items())},
        'excluded':dict(sorted(excluded.items())),
        'commit_classifications':invalidations,
        'run_classifications':classified,
    }


class SelfTests(unittest.TestCase):
    def ordering_fixture(self):
        files = {name: {"bytes": 200, "sha256": character * 64} for name, character in
                 (("cache-inventory-before.json", "a"), ("cache-inventory-before-receipt.json", "b"),
                  ("dispatch-intent.json", "c"), ("dispatch-response.json", "d"))}
        run = {"run_id": 12, "run_attempt": 2, "head_sha": "e" * 40,
               "run_started_at": "2026-09-12T16:21:51Z", "updated_at": "2026-09-12T16:45:00Z",
               "cache_observations": {"before": {"captured_at_utc": "2026-09-12T16:21:51.369323+00:00",
                                                   "inventory_sha256": "a" * 64}},
               "provenance": {"files": [{"name": name, **row} for name, row in files.items()]}}
        run["pre_cache_ordering"] = {
            "method": "same-second-local-pre-dispatch",
            "full_workflow": True, "source_or_test_changes": False,
            **{key: run[key] for key in ("run_id", "run_attempt", "head_sha", "run_started_at")},
            "snapshot_captured_at_utc": run["cache_observations"]["before"]["captured_at_utc"],
            "intent_at_utc": "2026-09-12T16:21:51.371353+00:00",
            "response_status": 201, "response_date_utc": "2026-09-12T16:21:53+00:00",
            "inventory_sha256": "a" * 64, "snapshot_receipt_sha256": "b" * 64,
            "sources": {name: files[name] for name in ("dispatch-intent.json", "dispatch-response.json")}}
        return run

    def test_same_second_ordering_and_strict_earlier_path(self):
        run = self.ordering_fixture()
        validate_pre_cache_ordering(run)
        run["pre_cache_ordering"] = None
        with self.assertRaisesRegex(ValueError, "lacks retained"):
            validate_pre_cache_ordering(run)
        run["cache_observations"]["before"]["captured_at_utc"] = "2026-09-12T16:21:50.999999Z"
        validate_pre_cache_ordering(run)

    def test_same_second_ordering_identity_timing_and_provenance_rejections(self):
        edits = [("run_id", 13), ("run_attempt", 1), ("head_sha", "0" * 40),
                 ("full_workflow", 1), ("source_or_test_changes", True),
                 ("snapshot_captured_at_utc", "2026-09-12T16:21:50Z"),
                 ("intent_at_utc", "2026-09-12T16:21:51.369323+00:00"),
                 ("intent_at_utc", "2026-09-12T16:21:52Z"), ("response_status", 200),
                 ("response_date_utc", "2026-09-12T16:21:51Z"),
                 ("response_date_utc", "2026-09-12T16:46:00Z"),
                 ("snapshot_receipt_sha256", "0" * 64), ("inventory_sha256", "0" * 64),
                 ("sources", {"dispatch-intent.json": {"bytes": 200, "sha256": "c" * 64}})]
        for field, value in edits:
            run = self.ordering_fixture()
            run["pre_cache_ordering"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_pre_cache_ordering(run)
        run = self.ordering_fixture()
        run["provenance"]["files"][-1]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "source digest"):
            validate_pre_cache_ordering(run)

    def bundle(self):
        path = Path(__file__).with_name("campaign.json")
        if not path.is_file():
            self.skipTest("retained campaign not available beside analyzer")
        return json.loads(path.read_text())

    def test_retained_projection_parity(self):
        data = self.bundle()
        report = analyze(data, require_complete=False)
        self.assertEqual(report["accepted_attempts"], len(data["runs"]))

    def history(self):
        history = self.bundle().get("history")
        if history is None:
            self.skipTest("history projection not present")
        return history

    def test_history_parity(self):
        data = self.history()
        result = derive_history(data)
        self.assertEqual({key: result[key] for key in data["expected_summary"]}, data["expected_summary"])

    def test_history_broken_parent_chain_rejected(self):
        data = self.history()
        data["commits"][1]["first_parent"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "discontinuous"):
            derive_history(data)

    def test_history_duplicate_run_rejected(self):
        data = self.history()
        data["runs"].append(deepcopy(data["runs"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate workflow"):
            derive_history(data)

    def test_history_unexpected_workflow_rejected(self):
        data = self.history()
        data["runs"][0]["event"] = "pull_request"
        with self.assertRaisesRegex(ValueError, "Unexpected workflow"):
            derive_history(data)

    def test_history_path_prefix_boundary(self):
        data = self.history()
        commit = data["commits"][0]
        directory = data["model"]["closure_directories"][0]
        commit["changed_paths"] = [directory + "-outside/file.rs"]
        self.assertEqual(derive_history(data)["commit_classifications"][0]["invalidating_paths"], [])
        commit["changed_paths"] = [directory + "/file.rs"]
        self.assertEqual(derive_history(data)["commit_classifications"][0]["invalidating_paths"], commit["changed_paths"])

    def test_retained_wrong_source_rejected(self):
        data = self.bundle()
        data["runs"][0]["source_sha"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "source mismatch"):
            analyze(data, require_complete=False)

    def test_retained_duplicate_attempt_rejected(self):
        data = self.bundle()
        data["runs"].append(deepcopy(data["runs"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate label"):
            analyze(data, require_complete=False)

    def matched_bundle(self):
        data = self.bundle()
        if data["schema"] != 2:
            self.skipTest("matched-control projection not present")
        return data

    def test_control_cohorts_are_separate(self):
        run = {"variant": "baseline", "condition": "control", "control_cohort": "cold"}
        self.assertTrue(in_comparison(run, "cold", 2))
        self.assertFalse(in_comparison(run, "warm", 2))
        run["control_cohort"] = "warm"
        self.assertTrue(in_comparison(run, "warm", 2))
        self.assertFalse(in_comparison(run, "cold", 2))

    def test_wrong_control_cohort_rejected(self):
        data = self.matched_bundle()
        control = next(run for run in data["runs"] if run["variant"] == "baseline")
        control["control_cohort"] = "warm" if control["control_cohort"] == "cold" else "cold"
        with self.assertRaisesRegex(ValueError, "control cohort differs"):
            analyze(data, require_complete=False)

    def test_duplicate_attempt_under_distinct_labels_rejected(self):
        data = self.matched_bundle()
        if len(data["runs"]) < 2:
            self.skipTest("two retained attempts required")
        for field in ("run_id", "run_attempt"):
            data["runs"][1][field] = data["runs"][0][field]
        with self.assertRaisesRegex(ValueError, "duplicate attempt"):
            analyze(data, require_complete=False)

    def test_mismatched_tool_cache_marker_rejected(self):
        data = self.matched_bundle()
        run = data["runs"][0]
        run["ordinary_tool_cache"]["observations"][0]["tools"]["mise"]["restore"]["key"] += "-mismatch"
        with self.assertRaisesRegex(ValueError, "restore marker mismatch"):
            analyze(data, require_complete=False)

    def test_missing_matrix_tool_observation_rejected(self):
        data = self.matched_bundle()
        data["runs"][0]["ordinary_tool_cache"]["observations"].pop()
        with self.assertRaisesRegex(ValueError, "observation topology mismatch"):
            analyze(data, require_complete=False)

    def test_superseded_common_controls_cannot_report_final_savings(self):
        data = {"schema": 1, "repository": REPOSITORY, "bindings": {
            "implementation_commit": SOURCE, "base_commit": BASE, "run_order": list(LEGACY_ORDER)}}
        with self.assertRaisesRegex(ValueError, "superseded common controls"):
            analyze(data)

    def test_retained_count_mismatch_rejected(self):
        data = self.bundle()
        data["runs"][0]["selected_jobs"][SELECTED[2]]["test_counts"]["playwright"]["passed"] += 1
        with self.assertRaisesRegex(ValueError, "behavior count mismatch"):
            analyze(data, require_complete=False)

    def test_retained_metric_mismatch_rejected(self):
        data = self.bundle()
        data["runs"][0]["expected_metrics_seconds"]["workflow / initial wait"] += 1
        with self.assertRaisesRegex(ValueError, "recomputed metrics"):
            analyze(data, require_complete=False)

    def test_parallel_wall_and_aggregate(self):
        jobs = [{"id": n, "name": str(n), "created_at": "2026-01-01T00:00:00Z",
                 "started_at": "2026-01-01T00:00:00Z", "completed_at": f"2026-01-01T00:00:{end:02}Z",
                 "status": "completed", "conclusion": "success", "steps": []} for n, end in [(1, 10), (2, 20)]]
        result = measure({"jobs": jobs, "run_started_at": "2026-01-01T00:00:00Z"})
        self.assertEqual(result["workflow / wall"], 20)
        self.assertEqual(result["workflow / aggregate jobs"], 30)
        self.assertNotIn("1 / graph save", result)

    def test_missing_interval_rejected(self):
        with self.assertRaises((KeyError, TypeError, ValueError)):
            interval({"startTime": 1, "endTime": None, "exitCode": 0})

    def test_cached_task_rejected(self):
        summary = {"id": "a", "version": "1", "turboVersion": "2.10.12",
                   "execution": {"command": "turbo run compile", "startTime": 1000, "endTime": 2000, "exitCode": 0},
                   "tasks": [{"taskId": "@apps/hash-graph#compile", "turbo_cache_status": "HIT"}]}
        with self.assertRaisesRegex(ValueError, "current MISS"):
            task_metrics([summary], {"started_at": "1970-01-01T00:00:00Z", "completed_at": "1970-01-01T00:00:03Z"})

    def test_negative_gain_retained(self):
        row = comparison([(10, "a")] * 3, [(12, "b")] * 3, allow_savings=True)
        self.assertEqual(row["seconds_saved"], -2)
        self.assertEqual(row["percent_saved"], -20)

    def test_small_cohort_has_no_gain(self):
        self.assertIsNone(comparison([(10, "a")] * 2, [(5, "b")] * 3, allow_savings=True)["seconds_saved"])

    def test_partial_campaign_has_no_gain(self):
        self.assertIsNone(comparison([(10, "a")] * 3, [(5, "b")] * 3, allow_savings=False)["seconds_saved"])

    def test_exact_cpu_flags_affect_class(self):
        self.assertNotEqual(digest({"cpu_lines": ["model: example", "flags: a"]}),
                            digest({"cpu_lines": ["model: example", "flags: a b"]}))

    def test_cache_access_time_is_not_identity(self):
        row = dict(id=1, key="k", version="v", ref="r", size_in_bytes=1, created_at="t", last_accessed_at="a")
        other = dict(row, last_accessed_at="b")
        self.assertEqual(cache_identity([row]), cache_identity([other]))
        other["id"] = 2
        self.assertNotEqual(cache_identity([row]), cache_identity([other]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", nargs="?", type=Path, default=Path(__file__).with_name("campaign.json"))
    parser.add_argument("--allow-incomplete", action="store_true", help="validate available attempts without reporting savings")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(SelfTests))
        return 0 if result.wasSuccessful() else 1
    data = json.loads(args.campaign.read_text())
    generator = args.campaign.with_name("generate.py")
    require(hashlib.sha256(generator.read_bytes()).hexdigest() == data["bindings"]["generator_sha256"], "generator bytes changed")
    print(json.dumps(analyze(data, require_complete=not args.allow_incomplete), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, TypeError, OSError) as error:
        print(f"Evidence validation failed: {error}", file=sys.stderr)
        raise SystemExit(1)
