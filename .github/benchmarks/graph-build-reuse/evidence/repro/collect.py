#!/usr/bin/env python3
"""Enrich saved campaign receipts locally; never dispatch or change raw evidence."""
import argparse
import copy
from datetime import datetime
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
STAMP = re.compile(r"^(\d{4}-\d\d-\d\dT[\d:.]+Z) (.*)$")
GRAPH = "@apps/hash-graph:compile"
PATH_MAPS = ("task_timings", "test_results", "cache_evidence", "runner_details", "job_logs", "graph_cache_receipts")


def date(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def clean_log(text):
    rows = []
    for number, raw in enumerate(text.splitlines(), 1):
        match = STAMP.fullmatch(ANSI.sub("", raw))
        if match:
            rows.append({"line": number, "time": date(match[1]), "text": match[2].strip()})
    return rows


def event(row):
    try:
        value = json.loads(row["text"])
    except ValueError:
        return None
    if not isinstance(value, dict) or "phase" not in value:
        return None
    if (value.get("phase") not in {"lock", "prepare", "restore", "build", "store", "reject", "cpu-probe"}
            or type(value.get("seconds")) not in (int, float)
            or not math.isfinite(value["seconds"]) or value["seconds"] < 0):
        raise ValueError("invalid helper event")
    return {"line": row["line"], **{key: value[key] for key in ("phase", "status", "key", "seconds")}}


def project(raw, job, summaries, log, parser):
    """Bind current helper output to the initial native Turbo summary."""
    output = copy.deepcopy(raw)
    output.pop("helper_restore_accepted", None)
    output["collection_issues"] = []
    output["timing_notes"] = [
        "Monotonic helper durations are retained; buffered display timestamps are not durations.",
        "Initial compile prepare measures fingerprint work. Workflow prepare additionally includes lock/patch/write work.",
        "Store includes another fingerprint and payload copy/link. CPU probe counts cover only the named verified contexts; absent phase events are unavailable, not zero.",
    ]
    output["job_total_native_probe_seconds"] = None
    rows = clean_log(log)
    key = raw.get("GRAPH_PREPARED_KEY")
    try:
        if not isinstance(key, str) or not re.fullmatch(r"graph-build-v\d+-[0-9a-f]{64}", key):
            raise ValueError("missing prepared key")
        restore, = [step for step in job["steps"] if step["name"] == "Restore graph build"]
        if restore["conclusion"] != "success":
            raise ValueError("Actions restore step did not succeed")
        window = [row for row in rows if date(restore["started_at"]) - 1 <= row["time"] <= date(restore["completed_at"]) + 1]
        hit = [row for row in window if row["text"].startswith("Cache restored from key: ")
               and row["text"].endswith("-" + key)]
        miss = [row for row in window if row["text"].startswith("Cache not found for input keys: ")
                and row["text"].endswith("-" + key)]
        if (len(hit), len(miss)) not in {(1, 0), (0, 1)}:
            raise ValueError("missing or ambiguous Actions result")
        if raw.get("GRAPH_CACHE_HIT") not in ({"true"} if hit else {"", "false"}):
            raise ValueError("Actions result contradicts raw receipt")
        output["actions_evidence"] = {"line": (hit or miss)[0]["line"], "result": "hit" if hit else "miss",
                                      "key": (hit or miss)[0]["text"].split(": ", 1)[1]}
    except (KeyError, TypeError, ValueError) as error:
        output["GRAPH_CACHE_HIT"] = "unknown"
        output["collection_issues"].append(str(error))
        return output
    try:
        parser.measure_tasks(summaries, job)
        summary, = [item for item in summaries if item["execution"]["command"] == "turbo run compile"]
        marker, = [row for row in rows if re.fullmatch(r"Summary:\s+.*[/\\]" + re.escape(summary["id"]) + r"\.json", row["text"])]
        start = summary["execution"]["startTime"] / 1000
        end = summary["execution"]["endTime"] / 1000
        # Task logs are buffered until completion, but their summary id is current.
        if abs(marker["time"] - end) > 2:
            raise ValueError("summary marker is outside current invocation")
        candidates = [row for row in rows if row["text"] == "##[group]" + GRAPH
                      and start - 1 <= row["time"] <= end + 1 and row["line"] < marker["line"]]
        begin, = candidates
        close = next(row for row in rows if row["line"] > begin["line"] and row["text"] == "##[endgroup]")
        if close["line"] >= marker["line"]:
            raise ValueError("incomplete initial graph log group")
        group = [row for row in rows if begin["line"] < row["line"] < close["line"]]
        if not any(row["text"].startswith("cache bypass, force executing ") for row in group):
            raise ValueError("initial graph output is not current cache-bypass execution")
        if any("cache hit, replaying logs" in row["text"] for row in group):
            raise ValueError("initial graph output was replayed")
        events = [value for row in group if (value := event(row)) is not None]
        if not events or any(value["key"] != ("" if value["phase"] in {"lock", "cpu-probe"} else key) for value in events):
            raise ValueError("helper event key differs from prepared key")
        prepared, = [value for value in events if value["phase"] == "prepare"]
        restored, = [value for value in events if value["phase"] == "restore"]
        if prepared["status"] != "ready" or prepared["line"] >= restored["line"]:
            raise ValueError("missing preparation before restore")
        if restored["status"] not in {"hit", "miss"}:
            raise ValueError("unknown helper restore status")
        output["initial_compile_summary_id"] = summary["id"]
        output["initial_compile_log_group_lines"] = [begin["line"], close["line"]]
        output["initial_compile_helper_events"] = events
        output["initial_compile_fingerprint_seconds"] = prepared["seconds"]
        probes = [value for value in events if value["phase"] == "cpu-probe" and value["status"] == "ready"]
        if probes:
            output["initial_compile_cpu_probes"] = {"events": probes, "count": len(probes), "seconds": sum(value["seconds"] for value in probes)}
        earlier_build = any(row["line"] < begin["line"] and re.match(r'^\{"phase":\s*"build"', row["text"])
                            for row in rows)
        output["helper_restore_accepted"] = (not earlier_build and raw["GRAPH_CACHE_HIT"] == "true" and restored["status"] == "hit"
                                              and not any(value["phase"] in {"build", "store", "reject"} for value in events))
        output["helper_acceptance_reason"] = ("current matching-key initial restore hit without a build"
                                               if output["helper_restore_accepted"] else "Actions miss or helper miss/build/rejection before acceptance")
        prepare_step, = [step for step in job["steps"] if step["name"] == "Prepare graph build"]
        direct = [value for row in rows if date(prepare_step["started_at"]) - 1 <= row["time"] <= date(prepare_step["completed_at"]) + 1
                  if (value := event(row)) is not None and value["phase"] == "prepare" and value["status"] == "ready" and value["key"] == key]
        if len(direct) == 1:
            output["workflow_prepare_event"] = direct[0]
            probes = [value for row in rows if date(prepare_step["started_at"]) - 1 <= row["time"] <= date(prepare_step["completed_at"]) + 1
                      if (value := event(row)) is not None and value["phase"] == "cpu-probe" and value["status"] == "ready"]
            if probes:
                output["workflow_prepare_cpu_probes"] = {"events": probes, "count": len(probes), "seconds": sum(value["seconds"] for value in probes)}
        else:
            output["collection_issues"].append("workflow preparation timing unavailable or ambiguous")
    except (KeyError, TypeError, ValueError, StopIteration) as error:
        output.pop("helper_restore_accepted", None)
        output["collection_issues"].append("initial helper acceptance unverified: " + str(error))
    contexts = [output[name] for name in ("workflow_prepare_cpu_probes", "initial_compile_cpu_probes") if name in output]
    if contexts:
        output["verified_context_cpu_probe_count"] = sum(context["count"] for context in contexts)
        output["verified_context_cpu_probe_seconds"] = sum(context["seconds"] for context in contexts)
    return output


def load(path):
    return json.loads(path.read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def collect(manifest_path, output, parser):
    original = load(manifest_path)
    manifest = copy.deepcopy(original)
    directory = manifest_path.parent
    output.mkdir(parents=True, exist_ok=False)
    for entry in manifest["runs"]:
        for name in ("run", "jobs"):
            entry[name] = str((directory / entry[name]).resolve())
        for name in PATH_MAPS:
            if isinstance(entry.get(name), dict):
                entry[name] = {job: str((directory / path).resolve()) for job, path in entry[name].items()}
        run = load(Path(entry["run"]))
        jobs = parser.load_jobs(load(Path(entry["jobs"])))
        entry["graph_cache_receipts"] = {}
        if entry["variant"] != "candidate":
            continue
        for job in jobs:
            if not any(step["name"] == "Prepare graph build" and step.get("conclusion") != "skipped" for step in job["steps"]):
                continue
            projected = {"GRAPH_PREPARED_KEY": "", "GRAPH_CACHE_HIT": "unknown", "collection_issues": []}
            paths = {}
            try:
                if job["run_id"] != run["id"] or job["run_attempt"] != run["run_attempt"]:
                    raise ValueError("job attempt identity mismatch")
                if run["head_sha"] != entry["workflow_sha"]:
                    raise ValueError("workflow source mismatch")
                for kind, mapping in (("raw_receipt", "cache_evidence"), ("task_timings", "task_timings"), ("environment", "runner_details")):
                    paths[kind] = Path(entry[mapping][job["name"]])
                paths["log"] = Path(entry.get("job_logs", {}).get(job["name"], str(Path(entry["run"]).parent / f"job-{job['id']}.log")))
                raw = load(paths["raw_receipt"])
                environment = load(paths["environment"])
                if environment["checkout_sha"] != entry["source_sha"] or environment["workflow_sha"] != run["head_sha"]:
                    raise ValueError("artifact source identity mismatch")
                if str(environment["run_id"]) != str(run["id"]) or str(environment["run_attempt"]) != str(run["run_attempt"]):
                    raise ValueError("artifact attempt identity mismatch")
                if environment["runner_name"] != job["runner_name"]:
                    raise ValueError("artifact runner identity mismatch")
                projected = project(raw, job, load(paths["task_timings"]), paths["log"].read_text(), parser)
            except (KeyError, TypeError, ValueError, OSError) as error:
                projected["collection_issues"].append("invalid collection inputs: " + str(error))
            projected["provenance"] = {"run_id": run["id"], "attempt": run["run_attempt"], "job_id": job["id"], "job_name": job["name"],
                                       "source_sha": entry.get("source_sha"), "workflow_sha": entry.get("workflow_sha"),
                                       "files": {name: {"path": str(path), "sha256": sha(path)} for name, path in paths.items() if path.is_file()}}
            path = output / f"run-{run['id']}-attempt-{run['run_attempt']}-job-{job['id']}.json"
            path.write_text(json.dumps(projected, indent=2) + "\n")
            entry["graph_cache_receipts"][job["name"]] = str(path.resolve())
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    report = parser.report(manifest, output)
    for before, after in zip(parser.report(original, directory)["runs"], report["runs"]):
        if before["metrics_seconds"] != after["metrics_seconds"]:
            raise ValueError("collection changed timing metrics")
        if before["exclusions"] != after["exclusions"]:
            raise ValueError("collection changed existing exclusions")
    (output / "timing-report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main():
    argument = argparse.ArgumentParser(description=__doc__)
    argument.add_argument("manifest", type=Path)
    argument.add_argument("output", type=Path)
    argument.add_argument("--parser", required=True, type=Path)
    args = argument.parse_args()
    spec = importlib.util.spec_from_file_location("timings", args.parser)
    parser = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(parser)
    report = collect(args.manifest.resolve(), args.output.resolve(), parser)
    print(json.dumps([{name: run.get(name) for name in ("run_id", "graph_cache_state", "graph_cache_job_states", "graph_cache_issues", "exclusions")}
                      for run in report["runs"]]))


if __name__ == "__main__":
    main()
