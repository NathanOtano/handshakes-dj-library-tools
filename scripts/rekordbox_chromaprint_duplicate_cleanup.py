#!/usr/bin/env python3
"""Apply conservative Chromaprint duplicate plans to files and Rekordbox."""

from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

from pyrekordbox import Rekordbox6Database
from pyrekordbox.db6 import (
    DjmdContent,
    DjmdSongHistory,
    DjmdSongMyTag,
    DjmdSongPlaylist,
    DjmdSongTagList,
)


def _disable_rekordbox_running_guard() -> None:
    """Allow commits against copied databases while Rekordbox is open."""
    import pyrekordbox.db6.database as database

    database.get_rekordbox_pid = lambda: None


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


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def rb_path(path: str) -> str:
    return str(Path(path)).replace("\\", "/")


def norm_rb_path(path: str) -> str:
    return rb_path(path).casefold()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def compact_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    normalized = normalized.casefold().replace("&", " and ")
    normalized = re.sub(r"\b(feat|featuring|ft)\.?\b", " feat ", normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", "", normalized).strip()


def safe_title_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    normalized = normalized.casefold().replace("&", " and ")
    normalized = re.sub(r"\b(feat|featuring|ft)\.?\b", " feat ", normalized)
    normalized = re.sub(r"\b(explicit|clean|dirty)\b", " ", normalized)
    normalized = re.sub(r"\b(\d{4}\s*)?remaster(ed|ise|ised|ize|ized)?\b", " ", normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", "", normalized).strip()


def loose_title_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    normalized = normalized.casefold()
    normalized = re.sub(r"[\[(].*?[\])]", " ", normalized)
    normalized = re.sub(
        r"\b(original|extended|radio|club|clean|dirty|explicit|instrumental|vocal|dub|edit|mix|remix|remaster|remastered|version)\b",
        " ",
        normalized,
    )
    return compact_text(normalized)


def title_matches(left: Path, right: Path, mode: str) -> bool:
    if mode == "none":
        return True
    if mode == "loose":
        return loose_title_key(left.stem) == loose_title_key(right.stem)
    return safe_title_key(left.stem) == safe_title_key(right.stem)


def as_float(value: Any) -> float | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_int(value: Any) -> int | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def load_report(path: Path) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    by_path: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            row["path_obj"] = Path(row["path"])
            row["keep_path_obj"] = Path(row["recommended_keep_path"])
            row["duration_float"] = as_float(row.get("duration_seconds"))
            row["group_min_similarity_float"] = as_float(row.get("group_min_similarity")) or 0.0
            rows.append(row)
            by_path[row["path"]] = row
    return rows, by_path


def load_fingerprint_cache(path: Path) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return result
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            entry_path = entry.get("path")
            if not entry_path:
                continue
            result[entry_path] = entry
    return result


def run_json(command: list[str], timeout_seconds: int = 120) -> dict[str, Any]:
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
        raise RuntimeError(completed.stderr.strip() or f"command failed with exit code {completed.returncode}")
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
            "stream=codec_name,codec_type,sample_rate,bits_per_sample,bits_per_raw_sample,bit_rate,channels",
            "-show_entries",
            "format=duration,bit_rate,format_name",
            "-of",
            "json",
            "--",
            str(path),
        ]
    )
    streams = [item for item in data.get("streams", []) if item.get("codec_type") == "audio"]
    stream = streams[0] if streams else {}
    fmt = data.get("format", {})
    bits = as_int(stream.get("bits_per_raw_sample")) or as_int(stream.get("bits_per_sample"))
    return {
        "codec": stream.get("codec_name") or "",
        "sample_rate": as_int(stream.get("sample_rate")),
        "bits_per_sample": bits,
        "channels": as_int(stream.get("channels")),
        "stream_bit_rate": as_int(stream.get("bit_rate")),
        "format_bit_rate": as_int(fmt.get("bit_rate")),
        "duration_seconds": as_float(fmt.get("duration")),
        "format": fmt.get("format_name") or "",
    }


def is_high_res_lossless(probe: dict[str, Any]) -> bool:
    codec = (probe.get("codec") or "").lower()
    if codec not in LOSSLESS_CODECS:
        return False
    return (probe.get("sample_rate") or 0) > 44100 or (probe.get("bits_per_sample") or 0) > 16


def content_by_path(db: Rekordbox6Database) -> dict[str, list[DjmdContent]]:
    result: dict[str, list[DjmdContent]] = {}
    for row in db.query(DjmdContent).all():
        result.setdefault(norm_rb_path(row.FolderPath or ""), []).append(row)
    return result


def unique_content_rows(rows: list[DjmdContent]) -> list[DjmdContent]:
    result: dict[str, DjmdContent] = {}
    for row in rows:
        result[str(row.ID)] = row
    return list(result.values())


def membership_count(db: Rekordbox6Database, content_id: Any) -> int:
    return db.query(DjmdSongPlaylist).filter(DjmdSongPlaylist.ContentID == str(content_id)).count()


def history_count(db: Rekordbox6Database, content_id: Any) -> int:
    return db.query(DjmdSongHistory).filter(DjmdSongHistory.ContentID == str(content_id)).count()


def my_tag_count(db: Rekordbox6Database, content_id: Any) -> int:
    return db.query(DjmdSongMyTag).filter(DjmdSongMyTag.ContentID == str(content_id)).count()


def tag_list_count(db: Rekordbox6Database, content_id: Any) -> int:
    return db.query(DjmdSongTagList).filter(DjmdSongTagList.ContentID == str(content_id)).count()


def reference_count(db: Rekordbox6Database, content_id: Any) -> int:
    return (
        membership_count(db, content_id)
        + history_count(db, content_id)
        + my_tag_count(db, content_id)
        + tag_list_count(db, content_id)
    )


def choose_preserved_content(
    db: Rekordbox6Database,
    duplicate_rows: list[DjmdContent],
    keep_rows: list[DjmdContent],
) -> tuple[DjmdContent | None, list[DjmdContent]]:
    candidates = unique_content_rows(keep_rows or duplicate_rows)
    if not candidates:
        return None, []
    candidates.sort(
        key=lambda row: (
            reference_count(db, row.ID),
            int(row.ID),
        ),
        reverse=True,
    )
    preserved = candidates[0]
    return preserved, [row for row in unique_content_rows(duplicate_rows + keep_rows) if row.ID != preserved.ID]


def merge_content_rows(
    db: Rekordbox6Database,
    preserved: DjmdContent,
    deleted_rows: list[DjmdContent],
    keep_path: Path,
) -> dict[str, Any]:
    preserved_id = str(preserved.ID)
    deleted_ids = {str(row.ID) for row in deleted_rows}
    moved_memberships, removed_duplicate_memberships = merge_unique_scoped_links(
        db,
        DjmdSongPlaylist,
        "PlaylistID",
        deleted_ids,
        preserved_id,
    )
    moved_my_tags, removed_duplicate_my_tags = merge_unique_scoped_links(
        db,
        DjmdSongMyTag,
        "MyTagID",
        deleted_ids,
        preserved_id,
    )
    moved_history_entries = repoint_content_links(db, DjmdSongHistory, deleted_ids, preserved_id)
    moved_tag_list_entries = repoint_content_links(db, DjmdSongTagList, deleted_ids, preserved_id)

    keep_rb_path = rb_path(str(keep_path))
    old_preserved_path = preserved.FolderPath
    preserved.FolderPath = keep_rb_path
    if preserved.OrgFolderPath == old_preserved_path or preserved.OrgFolderPath in {row.FolderPath for row in deleted_rows}:
        preserved.OrgFolderPath = keep_rb_path
    preserved.FileNameL = keep_path.name

    for row in deleted_rows:
        db.delete(row)

    return {
        "preserved_content_id": int(preserved.ID),
        "deleted_content_ids": [int(row.ID) for row in deleted_rows],
        "moved_memberships": moved_memberships,
        "removed_duplicate_memberships": removed_duplicate_memberships,
        "moved_history_entries": moved_history_entries,
        "moved_my_tags": moved_my_tags,
        "removed_duplicate_my_tags": removed_duplicate_my_tags,
        "moved_tag_list_entries": moved_tag_list_entries,
        "preserved_path": keep_rb_path,
    }


def merge_unique_scoped_links(
    db: Rekordbox6Database,
    model: Any,
    scope_column: str,
    deleted_ids: set[str],
    preserved_id: str,
) -> tuple[int, int]:
    moved = 0
    removed_duplicates = 0
    content_column = model.ContentID
    scopes_with_preserved = {
        str(getattr(row, scope_column))
        for row in db.query(model).filter(content_column == preserved_id).all()
    }

    for deleted_id in deleted_ids:
        rows = list(db.query(model).filter(content_column == deleted_id).all())
        for row in rows:
            scope_id = str(getattr(row, scope_column))
            if scope_id in scopes_with_preserved:
                db.delete(row)
                removed_duplicates += 1
            else:
                row.ContentID = preserved_id
                scopes_with_preserved.add(scope_id)
                moved += 1
    return moved, removed_duplicates


def repoint_content_links(
    db: Rekordbox6Database,
    model: Any,
    deleted_ids: set[str],
    preserved_id: str,
) -> int:
    moved = 0
    content_column = model.ContentID
    for deleted_id in deleted_ids:
        rows = list(db.query(model).filter(content_column == deleted_id).all())
        for row in rows:
            row.ContentID = preserved_id
            moved += 1
    return moved


def unique_target(root: Path, music_root: Path, source: Path) -> Path:
    try:
        relative = source.resolve().relative_to(music_root.resolve())
    except ValueError:
        relative = Path(source.name)
    target = root / relative
    if not target.exists():
        return target
    parent = target.parent
    stem = target.stem
    suffix = target.suffix
    index = 2
    while True:
        candidate = parent / f"{stem}__dup{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def unique_sibling(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 10000):
        candidate = path.with_name(f"{path.stem} (CD quality {index}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not create unique target beside {path}")


def conversion_target_and_codec_args(source: Path) -> tuple[Path, list[str], str]:
    suffix = source.suffix.lower()
    if suffix in {".aif", ".aiff"}:
        return source, ["-ar", "44100", "-c:a", "pcm_s16be"], "replace_in_place"
    if suffix == ".wav":
        return source, ["-ar", "44100", "-c:a", "pcm_s16le"], "replace_in_place"
    if suffix == ".flac":
        return source, ["-ar", "44100", "-sample_fmt", "s16", "-c:a", "flac"], "replace_in_place"
    if suffix == ".m4a":
        return unique_sibling(source.with_suffix(".aif")), ["-ar", "44100", "-c:a", "pcm_s16be"], "m4a_flac_to_aif"
    raise RuntimeError(f"Unsupported conversion extension: {source}")


def build_duplicate_plan(rows: list[dict[str, Any]], by_path: dict[str, dict[str, Any]], args: argparse.Namespace) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    planned: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    for row in rows:
        if row.get("action") == "keep_candidate":
            continue
        duplicate_path = row["path_obj"]
        keep_path = row["keep_path_obj"]
        keep_row = by_path.get(str(keep_path))
        reasons: list[str] = []
        if duplicate_path == keep_path:
            reasons.append("same_path")
        if not duplicate_path.exists():
            reasons.append("duplicate_path_missing")
        if not keep_path.exists():
            reasons.append("keep_path_missing")
        if row["group_min_similarity_float"] < args.min_group_similarity:
            reasons.append("below_similarity_threshold")
        if not title_matches(duplicate_path, keep_path, args.title_match_mode):
            reasons.append("title_key_mismatch")
        duration_diff = None
        if keep_row and row["duration_float"] is not None and keep_row.get("duration_float") is not None:
            duration_diff = abs(float(row["duration_float"]) - float(keep_row["duration_float"]))
            if duration_diff > args.duration_tolerance_seconds:
                reasons.append("duration_mismatch")
        else:
            reasons.append("duration_unavailable")
        item = {
            "group_id": row.get("group_id"),
            "action": row.get("action"),
            "duplicate_path": str(duplicate_path),
            "keep_path": str(keep_path),
            "group_min_similarity": row["group_min_similarity_float"],
            "duration_diff": duration_diff,
            "quality_tier": row.get("quality_tier"),
            "review_reasons": reasons,
        }
        if reasons:
            review.append(item)
        else:
            planned.append(item)
    return planned, review


def build_conversion_plan(cache: dict[str, dict[str, Any]], duplicate_plan: list[dict[str, Any]], music_root: Path) -> list[dict[str, Any]]:
    moving = {str(Path(item["duplicate_path"])) for item in duplicate_plan}
    conversions: list[dict[str, Any]] = []
    for path_string, entry in cache.items():
        path = Path(path_string)
        if str(path) in moving or not path.exists():
            continue
        try:
            path.resolve().relative_to(music_root.resolve())
        except ValueError:
            continue
        probe = entry.get("probe") or {}
        if not is_high_res_lossless(probe):
            continue
        if path.suffix.lower() not in {".aif", ".aiff", ".wav", ".flac", ".m4a"}:
            conversions.append({"path": str(path), "probe": probe, "blocked": True, "reason": "unsupported_extension"})
            continue
        conversions.append({"path": str(path), "probe": probe, "blocked": False, "reason": ""})
    return conversions


def write_review_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["group_id", "action", "duplicate_path", "keep_path", "group_min_similarity", "duration_diff", "quality_tier", "review_reasons"]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            output = dict(row)
            output["review_reasons"] = ";".join(row.get("review_reasons") or [])
            writer.writerow({key: output.get(key) for key in fieldnames})


def convert_to_cd_quality(ffmpeg: str, ffprobe: str, source: Path, backup_root: Path, music_root: Path, temp_root: Path) -> dict[str, Any]:
    target, codec_args, conversion_kind = conversion_target_and_codec_args(source)
    backup = unique_target(backup_root, music_root, source)
    temp = temp_root / target.relative_to(music_root)
    temp.parent.mkdir(parents=True, exist_ok=True)
    backup.parent.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [ffmpeg, "-y", "-nostdin", "-i", str(source), "-map_metadata", "0", "-vn", *codec_args, str(temp)]
    completed = subprocess.run(command, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or f"ffmpeg failed for {source}")
    probe = probe_audio(ffprobe, temp)
    if (probe.get("sample_rate") or 0) != 44100 or (probe.get("bits_per_sample") or 0) != 16:
        raise RuntimeError(f"Converted file is still above CD quality: {temp} -> {probe}")
    original_sha = sha256_file(source)
    converted_sha = sha256_file(temp)
    shutil.move(str(source), str(backup))
    shutil.move(str(temp), str(target))
    return {
        "path": str(source),
        "converted_path": str(target),
        "path_changed": source != target,
        "conversion_kind": conversion_kind,
        "backup_path": str(backup),
        "original_sha256": original_sha,
        "converted_sha256": converted_sha,
        "probe_after": probe,
        "source_exists_after": source.exists(),
        "converted_exists_after": target.exists(),
        "backup_exists_after": backup.exists(),
    }


def update_converted_content_rows(
    db: Rekordbox6Database,
    conversions: list[dict[str, Any]],
    by_path: dict[str, list[DjmdContent]],
) -> list[dict[str, Any]]:
    updates: list[dict[str, Any]] = []
    for item in conversions:
        source = Path(item["path"])
        target = Path(item["converted_path"])
        if source == target:
            continue
        source_rows = by_path.get(norm_rb_path(str(source)), [])
        target_rows = by_path.get(norm_rb_path(str(target)), [])
        if source_rows and target_rows:
            raise RuntimeError(f"Refusing to repath conversion into existing Rekordbox content: {source} -> {target}")
        target_rb_path = rb_path(str(target))
        content_ids: list[int] = []
        for row in source_rows:
            old_path = row.FolderPath or ""
            row.FolderPath = target_rb_path
            if not row.OrgFolderPath or norm_rb_path(row.OrgFolderPath) == norm_rb_path(old_path):
                row.OrgFolderPath = target_rb_path
            content_ids.append(int(row.ID))
        updates.append(
            {
                "path": str(source),
                "converted_path": str(target),
                "content_ids": content_ids,
                "updated_rows": len(content_ids),
            }
        )
    return updates


def apply_database_plan(db: Rekordbox6Database, duplicate_plan: list[dict[str, Any]], by_path: dict[str, list[DjmdContent]]) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    retired_content_ids: set[str] = set()
    for item in duplicate_plan:
        duplicate_path = Path(item["duplicate_path"])
        keep_path = Path(item["keep_path"])
        duplicate_rows = [
            row for row in by_path.get(norm_rb_path(str(duplicate_path)), [])
            if str(row.ID) not in retired_content_ids
        ]
        keep_rows = [
            row for row in by_path.get(norm_rb_path(str(keep_path)), [])
            if str(row.ID) not in retired_content_ids
        ]
        preserved, deleted_rows = choose_preserved_content(db, duplicate_rows, keep_rows)
        db_action = "file_only"
        merge_result: dict[str, Any] = {}
        if preserved is not None:
            deleted_rows = [row for row in deleted_rows if str(row.ID) not in retired_content_ids]
            if deleted_rows:
                db_action = "merge_or_repath"
                merge_result = merge_content_rows(db, preserved, deleted_rows, keep_path)
                retired_content_ids.update(str(row.ID) for row in deleted_rows)
            else:
                db_action = "skipped_already_merged"
        applied.append(
            item
            | {
                "duplicate_content_ids": [int(row.ID) for row in duplicate_rows],
                "keep_content_ids": [int(row.ID) for row in keep_rows],
                "db_action": db_action,
            }
            | merge_result
        )
    return applied


def sum_applied_field(rows: list[dict[str, Any]], field: str) -> int:
    return sum(int(item.get(field) or 0) for item in rows)


def run(args: argparse.Namespace) -> dict[str, Any]:
    report_csv = Path(args.report_csv).resolve()
    music_root = Path(args.music_root).resolve()
    quarantine_root = Path(args.quarantine_root).resolve()
    cd_backup_root = Path(args.cd_backup_root).resolve()
    operation_report = Path(args.operation_report).resolve()
    review_csv = Path(args.review_csv).resolve()
    temp_root = Path(args.db_dir).resolve() / "cd-conversion-tmp"

    rows, by_report_path = load_report(report_csv)
    duplicate_plan, review_rows = build_duplicate_plan(rows, by_report_path, args)
    cache = load_fingerprint_cache(Path(args.chromaprint_cache).resolve())
    conversion_plan = build_conversion_plan(cache, duplicate_plan, music_root)
    blocked_conversions = [item for item in conversion_plan if item.get("blocked")]
    conversion_plan = [item for item in conversion_plan if not item.get("blocked")]
    write_review_csv(review_csv, review_rows)

    db = Rekordbox6Database(path=Path(args.master).resolve(), db_dir=Path(args.db_dir).resolve())
    moved_files: list[dict[str, Any]] = []
    db_applied: list[dict[str, Any]] = []
    converted_files: list[dict[str, Any]] = []
    conversion_db_updates: list[dict[str, Any]] = []
    try:
        by_db_path = content_by_path(db)
        db_applied = apply_database_plan(db, duplicate_plan, by_db_path)
        if args.apply_db:
            db.commit()
        else:
            db.rollback()

        if args.move_files:
            for item in db_applied:
                duplicate_path = Path(item["duplicate_path"])
                if not duplicate_path.exists():
                    moved_files.append(item | {"file_move": "skipped_missing"})
                    continue
                target = unique_target(quarantine_root, music_root, duplicate_path)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest = sha256_file(duplicate_path)
                shutil.move(str(duplicate_path), str(target))
                moved_files.append(
                    item
                    | {
                        "file_move": "moved",
                        "quarantine_path": str(target),
                        "sha256": digest,
                        "source_exists_after": duplicate_path.exists(),
                        "quarantine_exists_after": target.exists(),
                    }
                )

        if args.convert_masters:
            for item in conversion_plan:
                converted_files.append(
                    convert_to_cd_quality(
                        args.ffmpeg,
                        args.ffprobe,
                        Path(item["path"]),
                        cd_backup_root,
                        music_root,
                        temp_root,
                    )
                )
            conversion_db_updates = update_converted_content_rows(db, converted_files, by_db_path)
            if args.apply_db:
                db.commit()
            else:
                db.rollback()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    report_payload = {
        "success": True,
        "mode": "apply" if args.apply_db or args.move_files or args.convert_masters else "plan",
        "generatedAt": utc_now(),
        "reportCsv": str(report_csv),
        "musicRoot": str(music_root),
        "quarantineRoot": str(quarantine_root),
        "cdBackupRoot": str(cd_backup_root),
        "thresholds": {
            "minGroupSimilarity": args.min_group_similarity,
            "durationToleranceSeconds": args.duration_tolerance_seconds,
            "titleMatchMode": args.title_match_mode,
        },
        "counts": {
            "reportRows": len(rows),
            "duplicateRowsInReport": sum(1 for row in rows if row.get("action") != "keep_candidate"),
            "autoDuplicatePlan": len(duplicate_plan),
            "reviewDuplicateRows": len(review_rows),
            "dbAppliedRows": len(db_applied) if args.apply_db else 0,
            "movedMemberships": sum_applied_field(db_applied, "moved_memberships") if args.apply_db else 0,
            "removedDuplicateMemberships": sum_applied_field(db_applied, "removed_duplicate_memberships") if args.apply_db else 0,
            "movedHistoryEntries": sum_applied_field(db_applied, "moved_history_entries") if args.apply_db else 0,
            "movedMyTags": sum_applied_field(db_applied, "moved_my_tags") if args.apply_db else 0,
            "removedDuplicateMyTags": sum_applied_field(db_applied, "removed_duplicate_my_tags") if args.apply_db else 0,
            "movedTagListEntries": sum_applied_field(db_applied, "moved_tag_list_entries") if args.apply_db else 0,
            "dbConversionRows": sum(item.get("updated_rows", 0) for item in conversion_db_updates) if args.apply_db else 0,
            "movedFiles": len(moved_files),
            "masterConversionsPlanned": len(conversion_plan),
            "masterConversionsApplied": len(converted_files),
            "blockedConversions": len(blocked_conversions),
        },
        "sampleReview": review_rows[:20],
        "sampleDbApplied": db_applied[:20],
        "sampleMoves": moved_files[:20],
        "sampleConversions": converted_files[:20],
        "sampleConversionDbUpdates": conversion_db_updates[:20],
        "blockedConversions": blocked_conversions,
        "operationReport": str(operation_report),
        "reviewCsv": str(review_csv),
    }
    operation_report.parent.mkdir(parents=True, exist_ok=True)
    operation_report.write_text(json.dumps(report_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "success": True,
        "mode": report_payload["mode"],
        "generatedAt": report_payload["generatedAt"],
        "counts": report_payload["counts"],
        "operationReport": str(operation_report),
        "reviewCsv": str(review_csv),
        "quarantineRoot": str(quarantine_root),
        "cdBackupRoot": str(cd_backup_root),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", required=True)
    parser.add_argument("--db-dir", required=True)
    parser.add_argument("--report-csv", required=True)
    parser.add_argument("--music-root", required=True)
    parser.add_argument("--quarantine-root", required=True)
    parser.add_argument("--cd-backup-root", required=True)
    parser.add_argument("--chromaprint-cache", required=True)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--ffprobe", required=True)
    parser.add_argument("--operation-report", required=True)
    parser.add_argument("--review-csv", required=True)
    parser.add_argument("--min-group-similarity", type=float, default=0.98)
    parser.add_argument("--duration-tolerance-seconds", type=float, default=1.0)
    parser.add_argument("--title-match-mode", choices=["safe", "loose", "none"], default="safe")
    parser.add_argument("--apply-db", action="store_true")
    parser.add_argument("--move-files", action="store_true")
    parser.add_argument("--convert-masters", action="store_true")
    parser.add_argument("--allow-rekordbox-running-commit", action="store_true")
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.allow_rekordbox_running_commit:
            _disable_rekordbox_running_guard()
        print_json(run(args))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI reports exact operator error.
        print_json({"success": False, "generatedAt": utc_now(), "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
