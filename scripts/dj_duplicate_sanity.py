#!/usr/bin/env python3
"""Read-only duplicate sanity checks for the DJ library.

The script writes reports only. It never deletes, renames, retags, or writes a
Rekordbox database.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LOSSLESS_CODECS = {
    "flac": 5,
    "alac": 5,
    "pcm_s16le": 5,
    "pcm_s24le": 5,
    "pcm_s32le": 5,
    "pcm_f32le": 5,
    "wavpack": 5,
    "ape": 4,
}


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def resolve_path(value: str | None, repo_root: Path) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    return (repo_root / path).resolve()


def path_contains_or_equals(parent: Path, child: Path) -> bool:
    parent_resolved = parent.resolve()
    child_resolved = child.resolve()
    return parent_resolved == child_resolved or parent_resolved in child_resolved.parents


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    value = value.lower()
    value = re.sub(r"[_]+", " ", value)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"[^\w\s&'+.-]", "", value, flags=re.UNICODE)
    return value.strip()


def parse_name_from_stem(stem: str) -> tuple[str, str]:
    cleaned = re.sub(r"\s+", " ", stem).strip()
    parts = re.split(r"\s+-\s+", cleaned, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", cleaned


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def run_json(args: list[str]) -> dict[str, Any]:
    completed = subprocess.run(args, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip())
    return json.loads(completed.stdout)


def probe_audio(path: Path, ffprobe: str) -> tuple[dict[str, Any], str | None]:
    args = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name,codec_type,sample_rate,bits_per_sample,bit_rate,channels,channel_layout:format=duration,bit_rate,format_name:format_tags=title,artist,album",
        "-of",
        "json",
        str(path),
    ]
    try:
        data = run_json(args)
    except Exception as exc:  # noqa: BLE001 - report per-file diagnostics.
        return {}, str(exc)
    return data, None


def decoded_audio_hash(path: Path, ffmpeg: str) -> tuple[str | None, str | None]:
    args = [
        ffmpeg,
        "-v",
        "error",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "2",
        "-ar",
        "44100",
        "-c:a",
        "pcm_s16le",
        "-f",
        "hash",
        "-hash",
        "SHA256",
        "-",
    ]
    completed = subprocess.run(args, check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        return None, (completed.stderr or completed.stdout).strip()
    match = re.search(r"SHA256=([A-Fa-f0-9]+)", completed.stdout)
    if not match:
        return None, "ffmpeg hash output did not include SHA256"
    return match.group(1).upper(), None


def chromaprint_fingerprint(path: Path, fpcalc: str) -> tuple[str | None, float | None, str | None]:
    completed = subprocess.run(
        [fpcalc, str(path)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        return None, None, (completed.stderr or completed.stdout).strip()

    fingerprint = None
    duration = None
    for line in completed.stdout.splitlines():
        if line.startswith("FINGERPRINT="):
            fingerprint = line.split("=", 1)[1].strip()
        elif line.startswith("DURATION="):
            raw_duration = line.split("=", 1)[1].strip()
            try:
                duration = float(raw_duration)
            except ValueError:
                duration = None
    if not fingerprint:
        return None, duration, "fpcalc output did not include FINGERPRINT"
    return fingerprint, duration, None


def quality_rank(record: dict[str, Any]) -> float:
    codec = (record.get("codec") or "").lower()
    lossless = LOSSLESS_CODECS.get(codec, 1)
    sample_rate = record.get("sample_rate") or 0
    bits = record.get("bits_per_sample") or 0
    bitrate = record.get("bit_rate") or 0
    length = record.get("length_bytes") or 0
    return (lossless * 1_000_000) + (sample_rate * 10) + (bits * 1_000) + (bitrate / 1_000) + (length / 1_000_000_000)


def group_recommendation(match_kind: str) -> str:
    if match_kind in {"exact_file_duplicate", "decoded_audio_duplicate"}:
        return "Conserver le meilleur candidat qualite, fusionner les metadonnees/playlists, puis supprimer ou mettre en quarantaine les doublons seulement apres validation."
    if match_kind == "same_name_same_duration_candidate":
        return "Verifier l'audio ou Chromaprint avant suppression; si c'est le meme morceau, garder le meilleur candidat qualite."
    return "Ne pas supprimer automatiquement; si l'audio ou l'arrangement est different, renommer le titre avec une variante explicite, par exemple '(alt)' ou le nom du mix."


@dataclass
class RootSpec:
    role: str
    path: Path


def collect_records(
    roots: list[RootSpec],
    extensions: set[str],
    ffprobe: str,
    ffmpeg: str | None,
    fpcalc: str | None,
    include_audio_hash: bool,
    include_fingerprint: bool,
    limit: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    warnings: list[str] = []
    records: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()

    for spec in roots:
        if not spec.path.exists():
            warnings.append(f"Root not found: {spec.role}={spec.path}")
            continue
        if not spec.path.is_dir():
            warnings.append(f"Root is not a directory: {spec.role}={spec.path}")
            continue

        for current_root, _, file_names in os.walk(spec.path):
            for file_name in sorted(file_names):
                path = (Path(current_root) / file_name).resolve()
                if path.suffix.lower() not in extensions:
                    continue
                if path in seen_paths:
                    warnings.append(f"Skipped duplicate scan path: {path}")
                    continue
                seen_paths.add(path)
                if limit and len(records) >= limit:
                    return records, warnings

                stat = path.stat()
                probe, probe_error = probe_audio(path, ffprobe)
                streams = probe.get("streams") or []
                stream = streams[0] if streams else {}
                fmt = probe.get("format") or {}
                tags = fmt.get("tags") or {}
                parsed_artist, parsed_title = parse_name_from_stem(path.stem)
                artist = tags.get("artist") or tags.get("ARTIST") or parsed_artist
                title = tags.get("title") or tags.get("TITLE") or parsed_title
                duration = float(fmt.get("duration")) if fmt.get("duration") else None
                sample_rate = int(stream.get("sample_rate")) if stream.get("sample_rate") else None
                bits = int(stream.get("bits_per_sample")) if stream.get("bits_per_sample") else None
                stream_bitrate = int(stream.get("bit_rate")) if stream.get("bit_rate") else None
                format_bitrate = int(fmt.get("bit_rate")) if fmt.get("bit_rate") else None

                audio_sha = None
                audio_hash_error = None
                if include_audio_hash and ffmpeg:
                    audio_sha, audio_hash_error = decoded_audio_hash(path, ffmpeg)

                fingerprint = None
                fingerprint_duration = None
                fingerprint_error = None
                if include_fingerprint and fpcalc:
                    fingerprint, fingerprint_duration, fingerprint_error = chromaprint_fingerprint(path, fpcalc)

                record = {
                    "path": str(path),
                    "root_role": spec.role,
                    "root_path": str(spec.path),
                    "relative_path": str(path.relative_to(spec.path)),
                    "file_name": path.name,
                    "extension": path.suffix.lower(),
                    "length_bytes": stat.st_size,
                    "modified_utc": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                    "file_sha256": sha256_file(path),
                    "decoded_audio_sha256": audio_sha,
                    "chromaprint_fingerprint": fingerprint,
                    "chromaprint_duration_seconds": fingerprint_duration,
                    "probe_error": probe_error,
                    "audio_hash_error": audio_hash_error,
                    "chromaprint_error": fingerprint_error,
                    "artist": artist,
                    "title": title,
                    "album": tags.get("album") or tags.get("ALBUM") or "",
                    "identity_key": f"{normalize_text(artist)} - {normalize_text(title)}".strip(" -"),
                    "codec": stream.get("codec_name"),
                    "format": fmt.get("format_name"),
                    "sample_rate": sample_rate,
                    "bits_per_sample": bits,
                    "channels": stream.get("channels"),
                    "channel_layout": stream.get("channel_layout"),
                    "bit_rate": stream_bitrate or format_bitrate,
                    "duration_seconds": duration,
                }
                record["quality_rank"] = quality_rank(record)
                records.append(record)
    return records, warnings


def build_groups(records: list[dict[str, Any]], duration_tolerance: float) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []

    def add_groups(match_kind: str, key_name: str, grouped: dict[str, list[dict[str, Any]]]) -> None:
        for key, items in sorted(grouped.items()):
            if not key or len(items) < 2:
                continue
            sorted_items = sorted(items, key=lambda item: item.get("quality_rank") or 0, reverse=True)
            durations = [item["duration_seconds"] for item in sorted_items if item.get("duration_seconds") is not None]
            spread = (max(durations) - min(durations)) if len(durations) >= 2 else None
            group_id = f"{match_kind}:{hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]}"
            keep_path = sorted_items[0]["path"]
            for index, item in enumerate(sorted_items):
                item["group_action"] = "keep_candidate" if index == 0 else "duplicate_or_variant_review"
            groups.append(
                {
                    "group_id": group_id,
                    "match_kind": match_kind,
                    "key_name": key_name,
                    "key": key,
                    "count": len(sorted_items),
                    "duration_spread_seconds": spread,
                    "recommended_keep_path": keep_path,
                    "recommendation": group_recommendation(match_kind),
                    "items": sorted_items,
                }
            )

    by_file_hash: dict[str, list[dict[str, Any]]] = {}
    by_audio_hash: dict[str, list[dict[str, Any]]] = {}
    by_fingerprint: dict[str, list[dict[str, Any]]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_file_hash.setdefault(record.get("file_sha256") or "", []).append(record)
        by_audio_hash.setdefault(record.get("decoded_audio_sha256") or "", []).append(record)
        by_fingerprint.setdefault(record.get("chromaprint_fingerprint") or "", []).append(record)
        by_name.setdefault(record.get("identity_key") or "", []).append(record)

    add_groups("exact_file_duplicate", "file_sha256", by_file_hash)
    add_groups("decoded_audio_duplicate", "decoded_audio_sha256", by_audio_hash)
    add_groups("chromaprint_fingerprint_duplicate", "chromaprint_fingerprint", by_fingerprint)

    same_name_same_duration: dict[str, list[dict[str, Any]]] = {}
    same_name_different_duration: dict[str, list[dict[str, Any]]] = {}
    for key, items in by_name.items():
        if not key or len(items) < 2:
            continue
        durations = [item["duration_seconds"] for item in items if item.get("duration_seconds") is not None]
        if len(durations) >= 2 and (max(durations) - min(durations)) <= duration_tolerance:
            same_name_same_duration[key] = items
        else:
            same_name_different_duration[key] = items

    add_groups("same_name_same_duration_candidate", "identity_key", same_name_same_duration)
    add_groups("same_name_different_audio_candidate", "identity_key", same_name_different_duration)
    return groups


def write_reports(groups: list[dict[str, Any]], records: list[dict[str, Any]], reports_root: Path) -> tuple[Path, Path]:
    reports_root.mkdir(parents=True, exist_ok=True)
    stamp = now_stamp()
    json_path = reports_root / f"dj-duplicate-sanity-{stamp}.json"
    csv_path = reports_root / f"dj-duplicate-sanity-{stamp}.csv"

    json_path.write_text(json.dumps({"records": records, "groups": groups}, ensure_ascii=False, indent=2), encoding="utf-8")

    fields = [
        "group_id",
        "match_kind",
        "key",
        "recommendation",
        "recommended_keep_path",
        "group_action",
        "quality_rank",
        "root_role",
        "path",
        "artist",
        "title",
        "codec",
        "sample_rate",
        "bits_per_sample",
        "bit_rate",
        "duration_seconds",
        "length_bytes",
        "file_sha256",
        "decoded_audio_sha256",
        "chromaprint_fingerprint",
        "chromaprint_duration_seconds",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for group in groups:
            for item in group["items"]:
                row = {field: "" for field in fields}
                row.update({field: item.get(field, "") for field in fields})
                row.update(
                    {
                        "group_id": group["group_id"],
                        "match_kind": group["match_kind"],
                        "key": group["key"],
                        "recommendation": group["recommendation"],
                        "recommended_keep_path": group["recommended_keep_path"],
                    }
                )
                writer.writerow(row)
    return json_path, csv_path


def print_error(message: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"error": message}, ensure_ascii=False, indent=2), file=sys.stderr)
        return
    print(message, file=sys.stderr)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Read-only duplicate sanity check for the DJ library.")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "config" / "dj-library.paths.json"))
    parser.add_argument("--candidate-root")
    parser.add_argument("--compare-root", action="append", default=[])
    parser.add_argument("--reports-root")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--audio-hash", action="store_true")
    parser.add_argument("--fingerprint", action="store_true")
    parser.add_argument("--duration-tolerance", type=float, default=2.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    config_path = Path(args.config).resolve()
    config = load_json(config_path)
    extensions = {extension.lower() for extension in config.get("audioExtensions", [])}

    candidate_root = resolve_path(args.candidate_root, repo_root) or resolve_path(config.get("intakeRoot"), repo_root)
    compare_roots = [resolve_path(value, repo_root) for value in args.compare_root]
    if not compare_roots:
        compare_roots = [
            resolve_path(config.get("libraryRoot"), repo_root),
            resolve_path(config.get("postProcessed_Library_RootRoot"), repo_root),
        ]
    reports_root = resolve_path(args.reports_root, repo_root) or resolve_path(config.get("reportsRoot"), repo_root) or (repo_root / "reports")

    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    fpcalc = shutil.which("fpcalc")
    if not ffprobe:
        print_error("ffprobe not found in PATH.", args.json)
        return 2
    if args.audio_hash and not ffmpeg:
        print_error("ffmpeg not found in PATH; --audio-hash cannot run.", args.json)
        return 2
    if args.fingerprint and not fpcalc:
        print_error("fpcalc not found in PATH; --fingerprint cannot run.", args.json)
        return 2

    if candidate_root:
        contained_compare_roots = [
            str(root)
            for root in compare_roots
            if root and path_contains_or_equals(candidate_root, root)
        ]
        if contained_compare_roots:
            print_error(
                "Candidate root must be one playlist lot, not a parent folder that contains compare roots. "
                f"Pass --candidate-root/ -CandidateRoot like F:\\Dancing\\Playlist\\nom-playlist. "
                f"Contained compare roots: {', '.join(contained_compare_roots)}",
                args.json,
            )
            return 2

    roots = [RootSpec("candidate", candidate_root)] if candidate_root else []
    roots.extend(RootSpec("compare", root) for root in compare_roots if root)
    records, warnings = collect_records(roots, extensions, ffprobe, ffmpeg, fpcalc, args.audio_hash, args.fingerprint, args.limit)
    groups = build_groups(records, args.duration_tolerance)
    json_path, csv_path = write_reports(groups, records, reports_root)

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "records_scanned": len(records),
        "duplicate_groups": len(groups),
        "json_path": str(json_path),
        "csv_path": str(csv_path),
        "audio_hash_enabled": args.audio_hash,
        "fingerprint_enabled": args.fingerprint,
        "fpcalc_path": fpcalc,
        "warning_count": len(warnings),
    }
    result = {"summary": summary, "warnings": warnings, "groups": groups}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("Duplicate sanity report generated.")
        for key, value in summary.items():
            print(f"{key}: {value}")
        for warning in warnings:
            print(f"WARNING: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
