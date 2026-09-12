"""Describe a checksum-bound graph ELF without executing or modifying it."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess


SOURCE = "2d8904b0bdfe7e65578294b921e0b6ef1bf2aaae"
GRAPH_KEY = "graph-build-v1-8c1978d9ce5b6e302db690fa67209bd6b4f695433a56970929112069f021334b"
PRODUCERS = {
    "1": {
        "run_id": 34693434058, "attempt": 1,
        "workflow_commit": "bf4bfd2759a9ce0fd69b14c5643f3769fc3fd910",
        "job_id": 103552887446, "artifact_id": 10297793452,
        "cache_id": 7618642151,
        "sha256": "cc2fa790440648d1a36fbc3ffc5d6c3f259e225442b8a61b1eb5e9ad9493ceff",
        "size_bytes": 2726584984,
    },
    "2": {
        "run_id": 34697755835, "attempt": 1,
        "workflow_commit": "efaad6f98023805d6954687217b367de08c346db",
        "job_id": 103564270847, "artifact_id": 10298699820,
        "cache_id": 7620117539,
        "sha256": "722f6384a08d70fbf7b6731e4e4309b68a6803404445a303f20029858ec0a7ee",
        "size_bytes": 2726585816,
    },
}


class InvalidEvidence(Exception):
    """A fixed diagnostic precondition or ELF constraint failed."""


def require(condition, reason):
    if not condition:
        raise InvalidEvidence(reason)


def span(offset, length, file_size):
    require(0 <= offset <= file_size and 0 <= length <= file_size - offset,
            "range-outside-file")


def read_at(stream, offset, length, file_size):
    span(offset, length, file_size)
    stream.seek(offset)
    value = stream.read(length)
    require(len(value) == length, "truncated-file")
    return value


def hash_range(stream, offset, length, file_size):
    span(offset, length, file_size)
    stream.seek(offset)
    digest = hashlib.sha256()
    remaining = length
    while remaining:
        chunk = stream.read(min(1024 * 1024, remaining))
        require(bool(chunk), "truncated-file")
        digest.update(chunk)
        remaining -= len(chunk)
    return digest.hexdigest()


def overlaps(start, length, other_start, other_length):
    return length > 0 and other_length > 0 and max(start, other_start) < min(
        start + length, other_start + other_length)


def inspect_elf(stream, file_size):
    raw_header = read_at(stream, 0, 64, file_size)
    header = dict(zip(
        ("ident", "type", "machine", "version", "entry", "program_offset", "section_offset",
         "flags", "header_size", "program_entry_size", "program_count", "section_entry_size",
         "section_count", "section_names_index"),
        struct.unpack("<16sHHIQQQIHHHHHH", raw_header), strict=True))
    ident = header.pop("ident")
    require(ident[:7] == b"\x7fELF\x02\x01\x01", "unsupported-elf-format")
    require(header["type"] in (2, 3) and header["machine"] == 62
            and header["version"] == 1, "unsupported-elf-target")
    require(header["header_size"] == 64 and header["program_entry_size"] == 56
            and header["section_entry_size"] == 64, "unsupported-entry-size")
    # Extended numbering is deliberately unsupported; fail instead of misreading it.
    require(0 < header["program_count"] <= 1024 and 0 < header["section_count"] <= 4096
            and 0 < header["section_names_index"] < header["section_count"],
            "unsupported-table-count")
    header.update({"os_abi": ident[7], "abi_version": ident[8],
                   "raw_sha256": hashlib.sha256(raw_header).hexdigest()})
    segments = []
    for index in range(header["program_count"]):
        values = struct.unpack("<IIQQQQQQ", read_at(
            stream, header["program_offset"] + index * 56, 56, file_size))
        segment = dict(zip(("type", "flags", "offset", "virtual_address", "physical_address",
                            "file_size", "memory_size", "alignment"), values, strict=True))
        require(segment["type"] != 1 or segment["file_size"] <= segment["memory_size"],
                "invalid-load-segment")
        segment.update({"index": index, "sha256": hash_range(
            stream, segment["offset"], segment["file_size"], file_size)})
        segments.append(segment)
    sections = []
    for index in range(header["section_count"]):
        values = struct.unpack("<IIQQQQIIQQ", read_at(
            stream, header["section_offset"] + index * 64, 64, file_size))
        sections.append(dict(zip(("name_offset", "type", "flags", "address", "offset", "size",
                                  "link", "info", "alignment", "entry_size"), values, strict=True)))
    names_section = sections[header["section_names_index"]]
    require(names_section["type"] == 3 and names_section["size"] <= 1024 * 1024,
            "invalid-section-name-table")
    names = read_at(stream, names_section["offset"], names_section["size"], file_size)
    for index, section in enumerate(sections):
        start = section.pop("name_offset")
        require(start < len(names), "invalid-section-name-offset")
        end = names.find(b"\0", start)
        require(end >= start and end - start <= 128, "invalid-section-name")
        name_bytes = names[start:end]
        require(re.fullmatch(rb"[A-Za-z0-9_.-]*", name_bytes) is not None,
                "unsupported-section-name")
        # SHT_NULL has no contents; SHT_NOBITS occupies memory but no file bytes.
        backed = section["type"] not in (0, 8)
        section.update({"index": index, "name": name_bytes.decode("ascii"),
                        "allocated": bool(section["flags"] & 2), "file_backed": backed,
                        "sha256": hash_range(stream, section["offset"], section["size"], file_size)
                        if backed else None,
                        "overlapping_load_segments": [segment["index"] for segment in segments
                            if backed and segment["type"] == 1 and overlaps(
                                section["offset"], section["size"],
                                segment["offset"], segment["file_size"])]})
    return {"header": header, "segments": segments, "sections": sections}


def file_state(value):
    return [value.st_dev, value.st_ino, value.st_mode, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns]


def git_head(directory):
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=directory,
                            check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    require(result.returncode == 0, "git-head-unavailable")
    return result.stdout.decode("ascii").strip()


def main():
    root = Path.cwd()
    sample = os.environ.get("GRAPH_ELF_SAMPLE", "")
    require(sample in PRODUCERS, "unknown-sample")
    producer = PRODUCERS[sample]
    branch = "refs/heads/speedup/integration-build-benchmark-v9-final-candidate-" + sample
    actions_key = "graph-build-benchmark-v9final-" + sample + "-" + GRAPH_KEY
    workflow_sha = os.environ.get("DIAGNOSTIC_WORKFLOW_SHA", "")
    require(os.environ.get("GITHUB_REPOSITORY") == "dnikolayev/hash"
            and os.environ.get("GITHUB_REF") == branch, "wrong-repository-or-ref")
    require(re.fullmatch(r"[a-f0-9]{40}", workflow_sha) is not None
            and workflow_sha == os.environ.get("GITHUB_SHA"), "wrong-workflow-commit")
    require(git_head(root) == SOURCE, "wrong-source-checkout")
    require(git_head(root / ".elf-diagnostic-source") == workflow_sha,
            "wrong-inspector-checkout")
    require(os.environ.get("GRAPH_CACHE_HIT") == "true"
            and os.environ.get("GRAPH_CACHE_PRIMARY_KEY") == actions_key
            and os.environ.get("GRAPH_CACHE_MATCHED_KEY") == actions_key, "not-exact-cache-hit")
    for name in ("target/debug/hash-graph", "target/graph-build-cache.ready.json"):
        require(not (root / name).exists() and not (root / name).is_symlink(),
                "unexpected-live-build")
    payload = root / "target/graph-build-cache"
    require(not (root / "target").is_symlink() and not payload.is_symlink(),
            "payload-directory-symlink")
    manifest_path = payload / "manifest.json"
    require(stat.S_ISREG(manifest_path.lstat().st_mode)
            and manifest_path.stat().st_size <= 4096, "invalid-manifest-file")
    manifest_bytes = manifest_path.read_bytes()
    require(json.loads(manifest_bytes) == {"key": GRAPH_KEY, "sha256": producer["sha256"]},
            "producer-manifest-mismatch")
    binary = payload / "hash-graph"
    with os.fdopen(os.open(binary, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode) and before.st_mode & 0o111
                and before.st_size == producer["size_bytes"], "producer-file-mismatch")
        digest = hash_range(stream, 0, before.st_size, before.st_size)
        require(digest == producer["sha256"], "producer-digest-mismatch")
        elf = inspect_elf(stream, before.st_size)
        require(file_state(before) == file_state(os.fstat(stream.fileno()))
                == file_state(binary.lstat()), "payload-changed-during-inspection")
    require(manifest_path.read_bytes() == manifest_bytes, "manifest-changed-during-inspection")
    report = {
        "schema": 1, "result": "passed", "performance_excluded": True,
        "source_commit": SOURCE, "workflow_commit": workflow_sha,
        "repository": "dnikolayev/hash", "ref": branch,
        "run_id": int(os.environ["GITHUB_RUN_ID"]),
        "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]), "sample": int(sample),
        "producer": producer, "graph_key": GRAPH_KEY,
        "restore": {"cache_hit": True, "primary_key": actions_key, "matched_key": actions_key},
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "inspector_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "binary": {"sha256": digest, "size_bytes": before.st_size}, "elf": elf,
        "limits": "No payload execution or rebuild. Section differences alone do not establish a behavior change.",
    }
    output = root / "elf-report"
    output.mkdir(exist_ok=False)
    (output / "elf.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"result": "passed", "sample": int(sample), "sha256": digest,
                      "size_bytes": before.st_size, "sections": len(elf["sections"])}))


if __name__ == "__main__":
    try:
        main()
    except (InvalidEvidence, OSError, ValueError, KeyError, UnicodeError, struct.error):
        raise SystemExit("ELF diagnostic failed its input or format checks; no passing report was written.")
