#!/usr/bin/env python3
"""Find overlapping Chromaprint fingerprints in a DJ audio folder."""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import csv
import datetime as dt
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any


LOSSLESS_CODECS = {
    "flac",
    "alac",
    "pcm_s16be",
    "pcm_s16le",
    "pcm_s24be",
    "pcm_s24le",
    "pcm_s32be",
    "pcm_s32le",
    "pcm_f32be",
    "pcm_f32le",
    "pcm_f64be",
    "pcm_f64le",
}
LOSSY_CODECS = {"aac", "mp3", "opus", "vorbis", "wma", "wmav2"}
FINGERPRINT_CACHE_SCHEMA = "dj-chromaprint-overlap-cache-v1"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def audio_files(root: Path, extensions: set[str], limit: int) -> list[Path]:
    files: list[Path] = []
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in {"_DUPLICATE_QUARANTINE"}]
        for filename in filenames:
            path = Path(current_root) / filename
            if path.suffix.lower() in extensions:
                files.append(path)
                if limit > 0 and len(files) >= limit:
                    return sorted(files, key=lambda item: str(item).casefold())
    return sorted(files, key=lambda item: str(item).casefold())


def compact_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    normalized = normalized.casefold()
    normalized = normalized.replace("&", " and ")
    normalized = re.sub(r"\b(feat|featuring|ft)\.?\b", " feat ", normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", "", normalized).strip()


def loose_title_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    normalized = normalized.casefold()
    normalized = re.sub(r"[\[(].*?[\])]", " ", normalized)
    normalized = re.sub(
        r"\b(original|extended|radio|club|clean|dirty|explicit|instrumental|vocal|dub|edit|mix|remix|remaster|remastered|master|version)\b",
        " ",
        normalized,
    )
    return compact_key(normalized)


def select_filename_candidates(files: list[Path]) -> tuple[list[Path], dict[str, Any]]:
    buckets: dict[str, list[Path]] = collections.defaultdict(list)
    for path in files:
        keys = {compact_key(path.stem), loose_title_key(path.stem)}
        for key in keys:
            if len(key) >= 8:
                buckets[key].append(path)

    selected: set[Path] = set()
    bucket_count = 0
    largest_bucket = 0
    for members in buckets.values():
        unique_members = sorted(set(members), key=lambda item: str(item).casefold())
        if len(unique_members) < 2:
            continue
        bucket_count += 1
        largest_bucket = max(largest_bucket, len(unique_members))
        selected.update(unique_members)

    return sorted(selected, key=lambda item: str(item).casefold()), {
        "mode": "filename",
        "bucketCount": bucket_count,
        "candidateFileCount": len(selected),
        "largestBucketSize": largest_bucket,
    }


def prefer_all_path(path: Path) -> int:
    parts = {part.casefold() for part in path.parts}
    return 1 if "_all" in parts else 0


def work_unit_key(path: Path) -> tuple[str, str, int]:
    stat = path.stat()
    return (compact_key(path.stem), path.suffix.casefold(), stat.st_size)


def build_work_units(candidate_files: list[Path], dedupe: bool) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not dedupe:
        return (
            [{"representative": path, "member_paths": [path], "work_key": str(index)} for index, path in enumerate(candidate_files)],
            {"enabled": False, "workUnitCount": len(candidate_files), "largestWorkUnitSize": 1},
        )

    buckets: dict[tuple[str, str, int], list[Path]] = collections.defaultdict(list)
    for path in candidate_files:
        buckets[work_unit_key(path)].append(path)

    units: list[dict[str, Any]] = []
    largest = 0
    for key, members in buckets.items():
        ordered = sorted(set(members), key=lambda item: (-prefer_all_path(item), str(item).casefold()))
        largest = max(largest, len(ordered))
        units.append(
            {
                "representative": ordered[0],
                "member_paths": ordered,
                "work_key": "|".join(map(str, key)),
            }
        )
    units.sort(key=lambda item: str(item["representative"]).casefold())
    return units, {"enabled": True, "workUnitCount": len(units), "largestWorkUnitSize": largest}


def file_signature(path: Path, fingerprint_length: int) -> dict[str, Any]:
    stat = path.stat()
    return {
        "schema": FINGERPRINT_CACHE_SCHEMA,
        "path": str(path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "fingerprint_length_seconds": fingerprint_length,
    }


def cache_key(entry: dict[str, Any]) -> tuple[str, int, int, int]:
    return (
        str(entry["path"]),
        int(entry["size"]),
        int(entry["mtime_ns"]),
        int(entry["fingerprint_length_seconds"]),
    )


def load_cache(path: Path) -> dict[tuple[str, int, int, int], dict[str, Any]]:
    cache: dict[tuple[str, int, int, int], dict[str, Any]] = {}
    if not path.exists():
        return cache
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("schema") != FINGERPRINT_CACHE_SCHEMA:
                continue
            if entry.get("status") != "ok":
                continue
            try:
                cache[cache_key(entry)] = entry
            except (KeyError, TypeError, ValueError):
                continue
    return cache


def append_cache_entry(path: Path, entry: dict[str, Any], lock: threading.Lock) -> None:
    with lock:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")


def run_json(command: list[str], timeout_seconds: int) -> dict[str, Any]:
    completed = subprocess.run(
        command,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        raise RuntimeError(stderr or f"command failed with exit code {completed.returncode}")
    return json.loads(completed.stdout)


def probe_audio(ffprobe: str, path: Path) -> dict[str, Any]:
    data = run_json(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,codec_type,sample_rate,bits_per_sample,bits_per_raw_sample,bit_rate,channels,channel_layout",
            "-show_entries",
            "format=duration,bit_rate,format_name",
            "-of",
            "json",
            "--",
            str(path),
        ],
        timeout_seconds=60,
    )
    streams = [item for item in data.get("streams", []) if item.get("codec_type") == "audio"]
    stream = streams[0] if streams else {}
    fmt = data.get("format", {})

    def as_int(value: Any) -> int | None:
        if value in (None, "", "N/A"):
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    def as_float(value: Any) -> float | None:
        if value in (None, "", "N/A"):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    bits = as_int(stream.get("bits_per_raw_sample"))
    if not bits:
        bits = as_int(stream.get("bits_per_sample"))
    return {
        "codec": stream.get("codec_name") or "",
        "format": fmt.get("format_name") or "",
        "sample_rate": as_int(stream.get("sample_rate")),
        "bits_per_sample": bits,
        "channels": as_int(stream.get("channels")),
        "channel_layout": stream.get("channel_layout") or "",
        "stream_bit_rate": as_int(stream.get("bit_rate")),
        "format_bit_rate": as_int(fmt.get("bit_rate")),
        "duration_seconds": as_float(fmt.get("duration")),
    }


def fingerprint_audio(fpcalc: str, path: Path, fingerprint_length: int) -> dict[str, Any]:
    data = run_json(
        [
            fpcalc,
            "-raw",
            "-json",
            "-length",
            str(fingerprint_length),
            str(path),
        ],
        timeout_seconds=max(120, fingerprint_length + 60),
    )
    fingerprint = data.get("fingerprint") or []
    if isinstance(fingerprint, str):
        fingerprint = [int(item) for item in fingerprint.split(",") if item.strip()]
    return {
        "fpcalc_duration_seconds": data.get("duration"),
        "fingerprint": [int(item) for item in fingerprint],
    }


def compute_entry(
    path: Path,
    fpcalc: str,
    ffprobe: str,
    fingerprint_length: int,
    cache_path: Path,
    cache_lock: threading.Lock,
) -> dict[str, Any]:
    signature = file_signature(path, fingerprint_length)
    entry: dict[str, Any] = signature | {
        "status": "ok",
        "updated_at": utc_now(),
    }
    try:
        entry["probe"] = probe_audio(ffprobe, path)
        entry.update(fingerprint_audio(fpcalc, path, fingerprint_length))
        if not entry.get("fingerprint"):
            raise RuntimeError("empty fingerprint")
    except Exception as exc:  # noqa: BLE001 - report per-file failures without stopping the whole scan.
        entry["status"] = "error"
        entry["error"] = str(exc)
    append_cache_entry(cache_path, entry, cache_lock)
    return entry


def classify_quality(path: Path, probe: dict[str, Any]) -> dict[str, Any]:
    codec = (probe.get("codec") or "").lower()
    sample_rate = probe.get("sample_rate")
    bits = probe.get("bits_per_sample")
    bitrate = probe.get("stream_bit_rate") or probe.get("format_bit_rate")
    lossless = codec in LOSSLESS_CODECS
    lossy = codec in LOSSY_CODECS
    exceeds_cd = bool(lossless and ((sample_rate or 0) > 44100 or (bits or 0) > 16))
    cd_exact = bool(lossless and sample_rate == 44100 and bits == 16)
    master_like = exceeds_cd or "master" in str(path).casefold()

    if cd_exact:
        tier = "cd_lossless"
        capped_score = 1000
    elif lossless and not exceeds_cd:
        tier = "lossless_not_above_cd"
        capped_score = 900 + min(bits or 0, 16)
    elif lossy:
        tier = "lossy_below_cd"
        capped_score = 600 + min(int((bitrate or 0) / 1000), 320)
    elif lossless and exceeds_cd:
        tier = "master_above_cd"
        capped_score = -1000
    else:
        tier = "unknown"
        capped_score = 0

    if lossless and exceeds_cd:
        overshoot = max(0, (sample_rate or 44100) - 44100) + max(0, (bits or 16) - 16) * 10000
        fallback_score = 500000 - overshoot
    else:
        fallback_score = capped_score

    extension_score = {
        ".flac": 60,
        ".alac": 55,
        ".m4a": 45,
        ".aif": 40,
        ".aiff": 40,
        ".wav": 35,
        ".mp3": 30,
        ".opus": 25,
        ".ogg": 20,
    }.get(path.suffix.lower(), 0)

    return {
        "codec": codec,
        "sample_rate": sample_rate,
        "bits_per_sample": bits,
        "bitrate": bitrate,
        "quality_tier": tier,
        "is_lossless": lossless,
        "is_lossy": lossy,
        "exceeds_cd": exceeds_cd,
        "cd_exact": cd_exact,
        "master_like": master_like,
        "capped_score": capped_score,
        "fallback_score": fallback_score,
        "extension_score": extension_score,
    }


def representative_keep_path(entry: dict[str, Any]) -> Path:
    return sorted(entry.get("member_paths") or [entry["path"]], key=lambda item: (-prefer_all_path(item), str(item).casefold()))[0]


def choose_keep(members: list[dict[str, Any]]) -> tuple[dict[str, Any], Path, str, str]:
    capped = [item for item in members if not item["quality"]["exceeds_cd"]]
    if capped:
        keep = max(
            capped,
            key=lambda item: (
                item["quality"]["capped_score"],
                item["quality"]["extension_score"],
                prefer_all_path(representative_keep_path(item)),
                item["probe"].get("duration_seconds") or 0,
                -len(str(item["path"])),
            ),
        )
        if keep["quality"]["cd_exact"]:
            reason = "exact_cd_lossless_preferred"
            decision = "keep_cd_quality_candidate"
        elif keep["quality"]["is_lossy"]:
            reason = "best_non_master_candidate_below_cd_but_lossy"
            decision = "review_lossy_keep_over_master_if_applicable"
        else:
            reason = "best_candidate_not_above_cd"
            decision = "keep_best_not_above_cd"
        return keep, representative_keep_path(keep), decision, reason

    keep = max(
        members,
        key=lambda item: (
            item["quality"]["fallback_score"],
            item["quality"]["extension_score"],
            prefer_all_path(representative_keep_path(item)),
            item["probe"].get("duration_seconds") or 0,
            -len(str(item["path"])),
        ),
    )
    return keep, representative_keep_path(keep), "keep_master_only_available", "all_candidates_above_cd_keep_closest_master"


def fingerprint_shingles(fingerprint: list[int], size: int, step: int) -> list[tuple[tuple[int, ...], int]]:
    result: list[tuple[tuple[int, ...], int]] = []
    if len(fingerprint) < size:
        return result
    for pos in range(0, len(fingerprint) - size + 1, step):
        result.append((tuple(fingerprint[pos : pos + size]), pos))
    return result


def overlap_bounds(len_a: int, len_b: int, offset_b_minus_a: int) -> tuple[int, int, int]:
    if offset_b_minus_a >= 0:
        start_a = 0
        start_b = offset_b_minus_a
    else:
        start_a = -offset_b_minus_a
        start_b = 0
    count = min(len_a - start_a, len_b - start_b)
    return start_a, start_b, max(0, count)


def compare_fingerprints(fp_a: list[int], fp_b: list[int], offset_b_minus_a: int) -> dict[str, Any] | None:
    start_a, start_b, count = overlap_bounds(len(fp_a), len(fp_b), offset_b_minus_a)
    if count <= 0:
        return None
    exact = 0
    bit_distance = 0
    for index in range(count):
        left = int(fp_a[start_a + index])
        right = int(fp_b[start_b + index])
        if left == right:
            exact += 1
        bit_distance += ((left ^ right) & 0xFFFFFFFF).bit_count()
    return {
        "offset_b_minus_a": offset_b_minus_a,
        "overlap_frames": count,
        "exact_rate": exact / count,
        "bit_similarity": 1.0 - (bit_distance / (count * 32.0)),
    }


class UnionFind:
    def __init__(self, count: int) -> None:
        self.parent = list(range(count))

    def find(self, item: int) -> int:
        parent = self.parent[item]
        if parent != item:
            self.parent[item] = self.find(parent)
        return self.parent[item]

    def union(self, left: int, right: int) -> None:
        root_left = self.find(left)
        root_right = self.find(right)
        if root_left != root_right:
            self.parent[root_right] = root_left


def build_pairs(
    entries: list[dict[str, Any]],
    shingle_size: int,
    shingle_step: int,
    max_shingle_frequency: int,
    min_shared_shingles: int,
    min_similarity: float,
    min_overlap_frames: int,
) -> list[dict[str, Any]]:
    exact_groups: dict[tuple[int, ...], list[int]] = collections.defaultdict(list)
    index: dict[tuple[int, ...], list[tuple[int, int]]] = collections.defaultdict(list)

    for file_index, entry in enumerate(entries):
        fingerprint = entry["fingerprint"]
        exact_groups[tuple(fingerprint)].append(file_index)
        for shingle, pos in fingerprint_shingles(fingerprint, shingle_size, shingle_step):
            index[shingle].append((file_index, pos))

    candidate_offsets: dict[tuple[int, int], collections.Counter[int]] = collections.defaultdict(collections.Counter)
    for postings in index.values():
        if len(postings) < 2 or len(postings) > max_shingle_frequency:
            continue
        for left_index in range(len(postings)):
            left_file, left_pos = postings[left_index]
            for right_index in range(left_index + 1, len(postings)):
                right_file, right_pos = postings[right_index]
                if left_file == right_file:
                    continue
                a, b = sorted((left_file, right_file))
                if a == left_file:
                    offset = right_pos - left_pos
                else:
                    offset = left_pos - right_pos
                candidate_offsets[(a, b)][offset] += 1

    for group_members in exact_groups.values():
        if len(group_members) < 2:
            continue
        for left_index in range(len(group_members)):
            for right_index in range(left_index + 1, len(group_members)):
                a, b = sorted((group_members[left_index], group_members[right_index]))
                candidate_offsets[(a, b)][0] += min_shared_shingles

    pairs: list[dict[str, Any]] = []
    for (a, b), offsets in candidate_offsets.items():
        strong_offsets = [(offset, count) for offset, count in offsets.most_common(5) if count >= min_shared_shingles]
        if not strong_offsets:
            continue
        best: dict[str, Any] | None = None
        for offset, shared_count in strong_offsets:
            comparison = compare_fingerprints(entries[a]["fingerprint"], entries[b]["fingerprint"], offset)
            if not comparison:
                continue
            comparison["shared_shingles"] = shared_count
            if comparison["overlap_frames"] < min_overlap_frames:
                continue
            if comparison["bit_similarity"] < min_similarity:
                continue
            if best is None or comparison["bit_similarity"] > best["bit_similarity"]:
                best = comparison
        if best:
            pairs.append(
                {
                    "left_index": a,
                    "right_index": b,
                    "left_path": str(entries[a]["path"]),
                    "right_path": str(entries[b]["path"]),
                    **best,
                }
            )

    return sorted(
        pairs,
        key=lambda item: (-float(item["bit_similarity"]), -int(item["overlap_frames"]), item["left_path"], item["right_path"]),
    )


def write_reports(
    reports_root: Path,
    root_path: Path,
    entries: list[dict[str, Any]],
    pairs: list[dict[str, Any]],
    timestamp: str,
    args: argparse.Namespace,
) -> dict[str, str]:
    reports_root.mkdir(parents=True, exist_ok=True)
    group_csv = reports_root / f"chromaprint-overlap-groups-{timestamp}.csv"
    pair_csv = reports_root / f"chromaprint-overlap-pairs-{timestamp}.csv"
    summary_json = reports_root / f"chromaprint-overlap-summary-{timestamp}.json"

    uf = UnionFind(len(entries))
    for pair in pairs:
        uf.union(int(pair["left_index"]), int(pair["right_index"]))

    pair_by_group: dict[int, list[dict[str, Any]]] = collections.defaultdict(list)
    for pair in pairs:
        root = uf.find(int(pair["left_index"]))
        pair_by_group[root].append(pair)

    members_by_group: dict[int, list[int]] = collections.defaultdict(list)
    for index in range(len(entries)):
        root = uf.find(index)
        members_by_group[root].append(index)

    rows: list[dict[str, Any]] = []
    groups: list[dict[str, Any]] = []
    group_id = 1
    for root, indexes in sorted(members_by_group.items(), key=lambda item: min(str(entries[i]["path"]).casefold() for i in item[1])):
        expanded_member_count = sum(len(entries[index].get("member_paths") or [entries[index]["path"]]) for index in indexes)
        if len(indexes) < 2 and expanded_member_count < 2:
            continue
        members = [entries[index] for index in indexes]
        keep, keep_path, decision, reason = choose_keep(members)
        group_pairs = pair_by_group.get(root, [])
        max_similarity = max((float(pair["bit_similarity"]) for pair in group_pairs), default=1.0)
        min_similarity = min((float(pair["bit_similarity"]) for pair in group_pairs), default=1.0)
        max_overlap = max((int(pair["overlap_frames"]) for pair in group_pairs), default=len(keep["fingerprint"]))
        group_label = f"chromaprint_overlap_{group_id:04d}"
        groups.append(
            {
                "group_id": group_label,
                "member_count": expanded_member_count,
                "work_unit_count": len(members),
                "pair_count": len(group_pairs),
                "recommended_keep_path": str(keep_path),
                "decision": decision,
                "reason": reason,
                "max_similarity": max_similarity,
                "min_similarity": min_similarity,
                "max_overlap_frames": max_overlap,
            }
        )
        for member in sorted(members, key=lambda item: str(item["path"]).casefold()):
            quality = member["quality"]
            probe = member["probe"]
            for path in sorted(member.get("member_paths") or [member["path"]], key=lambda item: str(item).casefold()):
                action = "keep_candidate" if path == keep_path else "duplicate_candidate_review"
                if action != "keep_candidate" and quality["exceeds_cd"]:
                    action = "master_review_remove_candidate"
                if action != "keep_candidate" and decision == "review_lossy_keep_over_master_if_applicable":
                    action = "manual_quality_review"
                rows.append(
                    {
                        "group_id": group_label,
                        "action": action,
                        "recommended_keep_path": str(keep_path),
                        "keep_reason": reason,
                        "path": str(path),
                        "relative_path": os.path.relpath(path, root_path),
                        "representative_path": str(member["path"]),
                        "work_unit_member_count": len(member.get("member_paths") or [member["path"]]),
                        "quality_tier": quality["quality_tier"],
                        "exceeds_cd": quality["exceeds_cd"],
                        "cd_exact": quality["cd_exact"],
                        "master_like": quality["master_like"],
                        "codec": probe.get("codec"),
                        "sample_rate": probe.get("sample_rate"),
                        "bits_per_sample": probe.get("bits_per_sample"),
                        "bitrate": probe.get("stream_bit_rate") or probe.get("format_bit_rate"),
                        "duration_seconds": probe.get("duration_seconds"),
                        "fingerprint_frames": len(member["fingerprint"]),
                        "group_max_similarity": max_similarity,
                        "group_min_similarity": min_similarity,
                        "group_max_overlap_frames": max_overlap,
                    }
                )
        group_id += 1

    with group_csv.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "group_id",
            "action",
            "recommended_keep_path",
            "keep_reason",
            "path",
            "relative_path",
            "representative_path",
            "work_unit_member_count",
            "quality_tier",
            "exceeds_cd",
            "cd_exact",
            "master_like",
            "codec",
            "sample_rate",
            "bits_per_sample",
            "bitrate",
            "duration_seconds",
            "fingerprint_frames",
            "group_max_similarity",
            "group_min_similarity",
            "group_max_overlap_frames",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    with pair_csv.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "left_path",
            "right_path",
            "bit_similarity",
            "exact_rate",
            "overlap_frames",
            "offset_b_minus_a",
            "shared_shingles",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows({key: pair.get(key) for key in fieldnames} for pair in pairs)

    summary = {
        "success": True,
        "generatedAt": utc_now(),
        "rootPath": str(root_path),
        "workUnitCount": len(entries),
        "pairCount": len(pairs),
        "groupCount": len(groups),
        "fingerprintLengthSeconds": args.fingerprint_length_seconds,
        "candidateMode": args.candidate_mode,
        "minSimilarity": args.min_similarity,
        "minOverlapFrames": args.min_overlap_frames,
        "groups": groups,
        "reports": {
            "groupsCsv": str(group_csv),
            "pairsCsv": str(pair_csv),
            "summaryJson": str(summary_json),
        },
    }
    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "groupsCsv": str(group_csv),
        "pairsCsv": str(pair_csv),
        "summaryJson": str(summary_json),
        "groupCount": str(len(groups)),
        "pairCount": str(len(pairs)),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    config = load_config(Path(args.config))
    extensions = {str(item).lower() for item in config.get("audioExtensions", [])}
    root_path = Path(args.root_path).resolve()
    reports_root = Path(args.reports_root).resolve()
    runtime_root = Path(args.runtime_root).resolve()
    cache_path = runtime_root / "fingerprint-cache.jsonl"
    runtime_root.mkdir(parents=True, exist_ok=True)

    files = audio_files(root_path, extensions, args.limit)
    if args.candidate_mode == "filename":
        candidate_files, candidate_summary = select_filename_candidates(files)
    else:
        candidate_files = files
        candidate_summary = {"mode": "all", "bucketCount": None, "candidateFileCount": len(files), "largestBucketSize": None}
    work_units, work_summary = build_work_units(candidate_files, dedupe=(args.candidate_mode == "filename"))
    unit_by_representative = {unit["representative"]: unit for unit in work_units}
    candidate_summary["workDedupe"] = work_summary
    cache = load_cache(cache_path)
    cache_lock = threading.Lock()
    entries: list[dict[str, Any]] = []
    missing: list[Path] = []
    errors: list[dict[str, Any]] = []

    for unit in work_units:
        path = unit["representative"]
        signature = file_signature(path, args.fingerprint_length_seconds)
        cached = cache.get(cache_key(signature))
        if cached:
            cached["path"] = Path(cached["path"])
            cached["member_paths"] = unit["member_paths"]
            cached["work_key"] = unit["work_key"]
            cached["quality"] = classify_quality(cached["path"], cached.get("probe", {}))
            entries.append(cached)
        else:
            missing.append(path)

    progress = {"done": 0}
    last_progress_at = time.monotonic()
    if missing:
        print(
            f"Chromaprint cache miss: {len(missing)} / {len(work_units)} work units "
            f"from {len(candidate_files)} candidate files",
            file=sys.stderr,
            flush=True,
        )
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [
            executor.submit(
                compute_entry,
                path,
                args.fpcalc,
                args.ffprobe,
                args.fingerprint_length_seconds,
                cache_path,
                cache_lock,
            )
            for path in missing
        ]
        for future in concurrent.futures.as_completed(futures):
            item = future.result()
            progress["done"] += 1
            path = Path(item["path"])
            item["path"] = path
            unit = unit_by_representative.get(path, {"member_paths": [path], "work_key": str(path)})
            item["member_paths"] = unit["member_paths"]
            item["work_key"] = unit["work_key"]
            if item.get("status") == "ok":
                item["quality"] = classify_quality(path, item.get("probe", {}))
                entries.append(item)
            else:
                errors.append({"path": str(path), "error": item.get("error")})
            now = time.monotonic()
            if now - last_progress_at >= 30:
                last_progress_at = now
                print(
                    f"Chromaprint progress: {progress['done']} / {len(missing)} computed, "
                    f"{len(entries)} usable, {len(errors)} errors",
                    file=sys.stderr,
                    flush=True,
                )

    entries = [entry for entry in entries if entry.get("fingerprint")]
    entries.sort(key=lambda item: str(item["path"]).casefold())
    pairs = build_pairs(
        entries,
        shingle_size=args.shingle_size,
        shingle_step=args.shingle_step,
        max_shingle_frequency=args.max_shingle_frequency,
        min_shared_shingles=args.min_shared_shingles,
        min_similarity=args.min_similarity,
        min_overlap_frames=args.min_overlap_frames,
    )
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    reports = write_reports(reports_root, root_path, entries, pairs, timestamp, args)
    elapsed = time.monotonic() - started
    return {
        "success": True,
        "generatedAt": utc_now(),
        "rootPath": str(root_path),
        "cachePath": str(cache_path),
        "fileCount": len(files),
        "candidateSelection": candidate_summary,
        "workUnitCount": len(work_units),
        "usableFingerprintCount": len(entries),
        "computedCount": len(missing),
        "cacheHitCount": len(work_units) - len(missing),
        "errorCount": len(errors),
        "errorsSample": errors[:20],
        "pairCount": int(reports["pairCount"]),
        "groupCount": int(reports["groupCount"]),
        "fingerprintLengthSeconds": args.fingerprint_length_seconds,
        "candidateMode": args.candidate_mode,
        "minSimilarity": args.min_similarity,
        "minOverlapFrames": args.min_overlap_frames,
        "elapsedSeconds": round(elapsed, 3),
        "reports": {
            "groupsCsv": reports["groupsCsv"],
            "pairsCsv": reports["pairsCsv"],
            "summaryJson": reports["summaryJson"],
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--root-path", required=True)
    parser.add_argument("--reports-root", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--fpcalc", required=True)
    parser.add_argument("--ffprobe", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--fingerprint-length-seconds", type=int, default=180)
    parser.add_argument("--min-similarity", type=float, default=0.88)
    parser.add_argument("--min-overlap-frames", type=int, default=80)
    parser.add_argument("--candidate-mode", choices=["filename", "all"], default="filename")
    parser.add_argument("--shingle-size", type=int, default=4)
    parser.add_argument("--shingle-step", type=int, default=8)
    parser.add_argument("--max-shingle-frequency", type=int, default=50)
    parser.add_argument("--min-shared-shingles", type=int, default=3)
    return parser.parse_args()


def main() -> int:
    try:
        result = run(parse_args())
        print_json(result)
        return 0
    except Exception as exc:  # noqa: BLE001 - top-level CLI error is serialized for PowerShell wrapper.
        print_json({"success": False, "generatedAt": utc_now(), "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
