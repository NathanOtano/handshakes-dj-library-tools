#!/usr/bin/env python3
"""Apply genre recommendations from a CSV to Rekordbox metadata."""

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
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from pyrekordbox import Rekordbox6Database
from pyrekordbox.db6 import DjmdContent
from pyrekordbox.db6.tables import DjmdGenre
from sqlalchemy.exc import OperationalError

SCHEMA_VERSION = "dj-genre-recommendation-apply-v2"
STREAMING_PREFIXES = ("tidal:", "qobuz:", "beatport:", "beatsource:", "soundcloud:")
CONFIDENCE_RANK = {"low": 1, "medium": 2, "high": 3}
GENRE_SPLIT_RE = re.compile(r"[;,/]")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def normalize_key(value: str | None) -> str:
    return normalize_text(value).casefold()


def parse_bool(value: str | None) -> bool:
    return normalize_key(value) in {"1", "true", "yes", "y", "on"}


def parse_confidence(value: str | None) -> str:
    value_norm = normalize_key(value)
    return value_norm if value_norm in CONFIDENCE_RANK else "low"


def sha1_text(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", errors="replace")).hexdigest()


def parse_csv_bool(value: str | None) -> bool:
    return parse_bool(value)


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def run_command(cmd: list[str], timeout: int = 180) -> tuple[int, str]:
    completed = subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return completed.returncode, completed.stderr.strip()


def path_from_rekordbox(value: str | None) -> str:
    if not value:
        return ""
    raw = str(value).strip()
    parsed = urlparse(raw)
    if parsed.scheme == "file":
        decoded = unquote(parsed.path or "")
        if parsed.netloc and parsed.netloc.lower() not in {"", "localhost"}:
            decoded = f"//{parsed.netloc}{decoded}"
        if re.match(r"^/[A-Za-z]:", decoded):
            decoded = decoded[1:]
        return decoded.replace("/", "\\")
    return unquote(raw).replace("/", "\\")


def normalize_windows_path(value: str | None) -> str:
    value_norm = path_from_rekordbox(value)
    if not value_norm:
        return ""
    return os.path.normcase(os.path.normpath(value_norm))


def get_content_field(value: Any, field: str) -> str:
    if not hasattr(value, field):
        return ""
    return normalize_text(str(getattr(value, field) or ""))


def is_streaming_path(value: str | None) -> bool:
    return normalize_key(value).startswith(tuple(p.casefold() for p in STREAMING_PREFIXES))


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def ffprobe_current_genre(path: Path, ffprobe: str) -> str:
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format_tags=genre,Genre,TCON",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        "--",
        str(path),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        return ""
    return normalize_text(completed.stdout)


def update_file_tag(
    path: Path,
    genre: str,
    ffmpeg: str,
    ffprobe: str,
    overwrite: bool,
) -> tuple[bool, str]:
    existing = normalize_text(ffprobe_current_genre(path, ffprobe))
    if existing and normalize_key(existing) == normalize_key(genre) and not overwrite:
        return True, "already_matching_file_genre"

    temp = path.with_name(f".codex-genre-update-{sha1_text(str(path))}{path.suffix}")
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(path),
        "-map",
        "0",
        "-map_metadata",
        "0",
        "-c",
        "copy",
        "-metadata",
        f"genre={genre}",
        str(temp),
    ]
    code, stderr = run_command(command, timeout=300)
    if code != 0:
        if temp.exists():
            temp.unlink(missing_ok=True)
        return False, f"ffmpeg_failed:{stderr or 'unknown error'}"

    try:
        temp.replace(path)
    except OSError as exc:
        if temp.exists():
            temp.unlink(missing_ok=True)
        return False, f"rename_failed:{exc}"

    return True, "updated_file_genre"


def file_tag_needs_update(path: Path, genre: str, ffprobe: str, *, overwrite: bool) -> bool:
    if overwrite:
        return True
    existing = normalize_text(ffprobe_current_genre(path, ffprobe))
    return normalize_key(existing) != normalize_key(genre)


def split_playlist_values(value: str | None) -> list[str]:
    return [normalize_text(part) for part in GENRE_SPLIT_RE.split(normalize_text(value)) if normalize_text(part)]


def pick_genre(primary_playlist: str, secondary_playlists: str) -> str:
    if primary_playlist and normalize_key(primary_playlist) != "manual_review":
        return normalize_text(primary_playlist)

    for term in split_playlist_values(secondary_playlists):
        if normalize_key(term) != "manual_review":
            return term

    return ""


def is_excluded_genre(value: str, exclude_tools_samples: bool) -> bool:
    return exclude_tools_samples and normalize_key(value) == "tools samples"


def validate_genre(value: str) -> bool:
    return bool(normalize_key(value))


def build_content_index(db: Rekordbox6Database) -> dict[str, DjmdContent]:
    index: dict[str, DjmdContent] = {}
    for item in db.query(DjmdContent).all():
        key = None
        if hasattr(item, "ID"):
            key = getattr(item, "ID", None)
        elif isinstance(item, str):
            key = item
        elif isinstance(item, dict):
            key = item.get("ID") or item.get("Id") or item.get("id")
        elif isinstance(item, (tuple, list)) and item:
            key = item[0]

        if key is None:
            continue
        index[str(key)] = item  # type: ignore[assignment]

    return index


def normalize_genre_name(value: str) -> str:
    return normalize_key(value)


def build_genre_cache(db: Rekordbox6Database) -> dict[str, DjmdGenre]:
    genres: dict[str, DjmdGenre] = {}
    for genre in db.query(DjmdGenre).all():
        genres[normalize_genre_name(getattr(genre, "Name", ""))] = genre
    return genres


def resolve_genre_entity(
    db: Rekordbox6Database,
    genres_by_key: dict[str, DjmdGenre],
    name: str,
    *,
    allow_create: bool,
) -> DjmdGenre | None:
    cleaned = normalize_text(name)
    if not cleaned:
        return None
    key = normalize_genre_name(cleaned)

    existing = genres_by_key.get(key)
    if existing is not None:
        return existing

    try:
        genre = db.get_genre(Name=cleaned).one_or_none()
    except Exception:
        genre = None
    if genre is not None:
        genres_by_key[key] = genre
        return genre

    if not allow_create:
        return None

    try:
        genre = db.add_genre(cleaned)
    except ValueError:
        genre = db.get_genre(Name=cleaned).one_or_none()
        if genre is None:
            raise

    if genre is None:
        return None
    genres_by_key[key] = genre
    return genre


def resolve_content_row(
    db: Rekordbox6Database,
    content_by_id: dict[str, DjmdContent],
    content_id: str,
) -> DjmdContent | None:
    content = content_by_id.get(content_id)
    if hasattr(content, "_sa_instance_state"):
        return content

    try:
        return db.query(DjmdContent).filter(DjmdContent.ID == content_id).first()
    except Exception:
        return None


def commit_with_retry(db: Rekordbox6Database, max_attempts: int = 3, base_sleep: float = 0.75) -> None:
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            db.commit()
            return
        except Exception as exc:
            last_error = exc
            if not isinstance(exc, OperationalError):
                raise
            if "database is locked" not in str(exc).lower() or attempt >= max_attempts:
                raise
            delay = base_sleep * attempt
            import time

            time.sleep(delay)

    if last_error is not None:
        raise last_error


def read_recommendations(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        raw_rows = list(csv.DictReader(handle))

    normalized: list[dict[str, str]] = []
    for row in raw_rows:
        normalized.append(
            {
                "content_id": normalize_text(row.get("content_id")),
                "artist": normalize_text(row.get("artist")),
                "title": normalize_text(row.get("title")),
                "primary_playlist": normalize_text(row.get("primary_playlist")),
                "secondary_playlists": normalize_text(row.get("secondary_playlists")),
                "confidence": parse_confidence(row.get("confidence")),
                "evidence_summary": normalize_text(row.get("evidence_summary")),
                "source_terms": normalize_text(row.get("source_terms")),
                "review_required": str(parse_csv_bool(row.get("review_required"))).lower(),
            }
        )
    return normalized


def build_rows(
    recommendations: list[dict[str, str]],
    content_by_id: dict[str, DjmdContent],
    db: Rekordbox6Database,
    *,
    confidence_cutoff: str,
    include_review: bool,
    exclude_tools_samples: bool,
    overwrite_existing: bool,
    apply: bool,
    apply_file_tags: bool,
    include_streaming: bool,
    genre_fields: list[str],
    ffmpeg: str,
    ffprobe: str,
) -> tuple[list[dict[str, Any]], dict[str, Any], bool]:
    min_rank = CONFIDENCE_RANK[confidence_cutoff]
    counters: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    db_updates = 0
    file_updates = 0
    file_failures = 0
    db_failures = 0
    genres_by_key = build_genre_cache(db)

    for row in recommendations:
        payload: dict[str, Any] = {
            "content_id": row["content_id"],
            "artist_csv": row["artist"],
            "title_csv": row["title"],
            "primary_playlist": row["primary_playlist"],
            "secondary_playlists": row["secondary_playlists"],
            "confidence": row["confidence"],
            "evidence_summary": row["evidence_summary"],
            "source_terms": row["source_terms"],
            "review_required": row["review_required"],
            "selected_genre": "",
            "target_genre": "",
            "path": "",
            "path_exists": False,
            "current_genre": "",
            "current_src_genre": "",
            "changed_fields": "",
            "db_would_change": False,
            "db_updated": False,
            "file_would_change": False,
            "file_updated": False,
            "status": "",
            "status_reason": "",
        }

        content_id = row["content_id"]
        if not content_id:
            payload["status"] = "skipped_invalid_content_id"
            payload["status_reason"] = "missing content_id"
            counters[payload["status"]] += 1
            rows.append(payload)
            continue

        content = resolve_content_row(db, content_by_id, content_id)
        if content is None:
            payload["status"] = "not_found_in_db"
            payload["status_reason"] = "content_id not found in master.db"
            counters[payload["status"]] += 1
            rows.append(payload)
            continue

        if is_streaming_path(content.FolderPath) and not include_streaming:
            payload["status"] = "skipped_streaming"
            payload["status_reason"] = "streaming row"
            counters[payload["status"]] += 1
            rows.append(payload)
            continue

        genre = pick_genre(row["primary_playlist"], row["secondary_playlists"])
        payload["selected_genre"] = genre
        payload["target_genre"] = genre
        payload["path"] = normalize_windows_path(content.FolderPath or "")
        payload["path_exists"] = bool(payload["path"] and Path(payload["path"]).exists())
        payload["current_genre"] = get_content_field(content, "GenreName") if hasattr(content, "GenreName") else get_content_field(content, "Genre")
        payload["current_src_genre"] = get_content_field(content, "SrcGenre")

        if not include_review and row["review_required"] == "true":
            payload["status"] = "skipped_review_required"
            payload["status_reason"] = "review_required=true"
            counters[payload["status"]] += 1
            rows.append(payload)
            continue

        conf = parse_confidence(row["confidence"])
        if CONFIDENCE_RANK[conf] < min_rank:
            payload["status"] = "skipped_low_confidence"
            payload["status_reason"] = f"confidence {conf} below minimum {confidence_cutoff}"
            counters[payload["status"]] += 1
            rows.append(payload)
            continue

        if not validate_genre(genre):
            payload["status"] = "skipped_no_genre_candidate"
            payload["status_reason"] = "primary and secondary playlists empty or manual_review"
            counters[payload["status"]] += 1
            rows.append(payload)
            continue

        if is_excluded_genre(genre, exclude_tools_samples):
            payload["status"] = "skipped_genre_filter"
            payload["status_reason"] = "excluded by tools-samples rule"
            counters[payload["status"]] += 1
            rows.append(payload)
            continue

        changes: dict[str, Any] = {}
        if "Genre" in genre_fields and (overwrite_existing or not payload["current_genre"]):
            genre_obj = resolve_genre_entity(db, genres_by_key, genre, allow_create=bool(apply))
            if genre_obj is not None:
                changes["Genre"] = genre_obj
            else:
                changes["Genre"] = genre
        if "SrcGenre" in genre_fields and (overwrite_existing or not payload["current_src_genre"]):
            changes["SrcGenre"] = genre

        # Avoid no-op writes.
        if "Genre" in changes:
            if normalize_key(payload["current_genre"]) == normalize_key(genre):
                changes.pop("Genre", None)

        if "SrcGenre" in changes and normalize_key(payload["current_src_genre"]) == normalize_key(genre):
            changes.pop("SrcGenre", None)

        payload["changed_fields"] = ";".join(sorted(changes.keys()))
        payload["db_would_change"] = bool(changes)

        if not changes:
            payload["status"] = "would_not_change_existing_genre"
            payload["status_reason"] = "genre fields already contain matching values"
            if apply and apply_file_tags:
                file_would_update = False
                if not payload["path"]:
                    file_failures += 1
                    payload["status"] = "file_missing_path"
                    payload["status_reason"] = "missing local path in Rekordbox row"
                    payload["file_updated"] = False
                    counters[payload["status"]] += 1
                    rows.append(payload)
                    continue

                file_path = Path(payload["path"])
                if not file_path.exists():
                    file_failures += 1
                    payload["status"] = "file_missing_path"
                    payload["status_reason"] = f"local file not found: {payload['path']}"
                    payload["file_updated"] = False
                    counters[payload["status"]] += 1
                    rows.append(payload)
                    continue

                payload["file_would_change"] = file_tag_needs_update(file_path, genre, ffprobe, overwrite=overwrite_existing)
                if payload["file_would_change"]:
                    ok, detail = update_file_tag(file_path, genre, ffmpeg, ffprobe, overwrite_existing)
                    payload["file_updated"] = ok
                    if ok:
                        if detail == "already_matching_file_genre":
                            payload["status"] = "file_already_matching"
                            payload["status_reason"] = detail
                        else:
                            file_updates += 1
                            payload["status"] = "file_updated"
                            payload["status_reason"] = detail
                    else:
                        file_failures += 1
                        payload["status"] = "file_update_failed"
                        payload["status_reason"] = detail
                    counters[payload["status"]] += 1
                    rows.append(payload)
                else:
                    payload["file_updated"] = False
                    payload["status_reason"] = "file genre already matches selected value"
                    counters[payload["status"]] += 1
                    rows.append(payload)
                continue

            counters[payload["status"]] += 1
            rows.append(payload)
            continue

        if apply:
            try:
                for field_name, new_value in changes.items():
                    if field_name == "Genre" and not isinstance(new_value, DjmdGenre):
                        raise TypeError(f"Genre must be DjmdGenre, got {type(new_value)}")
                    setattr(content, field_name, new_value)
                db_updates += 1
                payload["db_updated"] = True
                payload["status"] = "db_updated"
                payload["status_reason"] = "updated Rekordbox metadata fields"
            except Exception as exc:
                db_failures += 1
                payload["status"] = "db_update_failed"
                payload["status_reason"] = str(exc)
                counters[payload["status"]] += 1
                rows.append(payload)
                continue
        else:
            payload["status"] = "db_would_update"
            payload["status_reason"] = "dry-run"

        if apply and apply_file_tags:
            payload["file_would_change"] = True
            if not payload["path"]:
                file_failures += 1
                payload["status"] = "file_missing_path"
                payload["status_reason"] = "missing local path in Rekordbox row"
                payload["file_updated"] = False
                counters[payload["status"]] += 1
                rows.append(payload)
            else:
                file_path = Path(payload["path"])
                if not file_path.exists():
                    file_failures += 1
                    payload["status"] = "file_missing_path"
                    payload["status_reason"] = f"local file not found: {payload['path']}"
                    payload["file_updated"] = False
                    counters[payload["status"]] += 1
                    rows.append(payload)
                else:
                    payload["file_would_change"] = file_tag_needs_update(file_path, genre, ffprobe, overwrite=overwrite_existing)
                    ok, detail = update_file_tag(file_path, genre, ffmpeg, ffprobe, overwrite_existing)
                    payload["file_updated"] = ok and detail != "already_matching_file_genre"
                    if ok:
                        if detail == "already_matching_file_genre":
                            payload["status"] = "file_already_matching"
                            payload["status_reason"] = detail
                        else:
                            file_updates += 1
                            payload["status"] = "file_updated"
                            payload["status_reason"] = detail
                    else:
                        file_failures += 1
                        payload["status"] = "file_update_failed"
                        payload["status_reason"] = detail
                    counters[payload["status"]] += 1
                    rows.append(payload)
        else:
            rows.append(payload)
            counters[payload["status"]] += 1

    summary = {
        "total_rows": len(recommendations),
        "db_rows_updated": db_updates,
        "file_rows_updated": file_updates,
        "db_update_failures": db_failures,
        "file_update_failures": file_failures,
        "had_db_error": db_failures > 0,
        "had_file_error": file_failures > 0,
        "status_counts": dict(sorted(counters.items(), key=lambda item: item[0])),
    }

    had_errors = db_failures > 0
    return rows, summary, had_errors


def write_m3u(path: Path, rows: list[dict[str, Any]]) -> int:
    ensure_dir(path.parent)
    include = {
        "db_would_update",
        "db_updated",
        "file_updated",
        "file_update_failed",
    }
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("#EXTM3U\n")
        for row in rows:
            if row.get("status") in include and row.get("path"):
                handle.write(row["path"] + "\n")
                count += 1
    return count


def run(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    config = read_json(Path(args.config).resolve())

    input_csv = Path(args.input_csv).resolve()
    if not input_csv.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_csv}")
    if args.apply_file_tags and not args.apply:
        raise ValueError("--apply-file-tags requires --apply.")
    if args.apply_file_tags and not args.ffmpeg:
        raise ValueError("--ffmpeg is required when --apply-file-tags is set.")
    if args.apply_file_tags and not args.ffprobe:
        raise ValueError("--ffprobe is required when --apply-file-tags is set.")

    reports_root = Path(args.reports_root)
    if not reports_root.is_absolute():
        reports_root = (repo_root / reports_root).resolve()
    ensure_dir(reports_root)

    recommendations = read_recommendations(input_csv)
    if args.limit > 0:
        recommendations = recommendations[: args.limit]

    db_kwargs: dict[str, Any] = {"path": Path(args.master).resolve()}
    if args.db_dir:
        db_kwargs["db_dir"] = Path(args.db_dir).resolve()

    db = Rekordbox6Database(**db_kwargs)
    had_errors = False
    rows: list[dict[str, Any]] = []
    summary_payload: dict[str, Any] = {}

    try:
        db.open()
        content_by_id = build_content_index(db)
        genre_fields = []
        if content_by_id:
            sample = next(iter(content_by_id.values()))
            for field in ("Genre", "SrcGenre"):
                if hasattr(sample, field):
                    genre_fields.append(field)
        if not genre_fields:
            genre_fields.append("Genre")
        rows, summary_payload, had_errors = build_rows(
            recommendations,
            content_by_id,
            db,
            confidence_cutoff=args.confidence_cutoff,
            include_review=args.include_review,
            exclude_tools_samples=args.exclude_tools_samples,
            overwrite_existing=args.overwrite_existing,
            apply=args.apply,
            apply_file_tags=args.apply_file_tags,
            include_streaming=args.include_streaming,
            genre_fields=genre_fields,
            ffmpeg=args.ffmpeg,
            ffprobe=args.ffprobe,
        )

        if args.apply:
            if summary_payload.get("had_db_error"):
                db.rollback()
            else:
                if summary_payload.get("db_rows_updated", 0) > 0:
                    commit_with_retry(db)
                else:
                    db.rollback()
    except Exception:
        if args.apply:
            db.rollback()
        raise
    finally:
        db.close()

    stamp = now_stamp()
    plan_csv = reports_root / f"genre-recommendation-plan-{stamp}.csv"
    summary_json = reports_root / f"genre-recommendation-summary-{stamp}.json"
    m3u_path = reports_root / f"genre-recommendation-updated-{stamp}.m3u"

    csv_fields = [
        "content_id",
        "artist_csv",
        "title_csv",
        "primary_playlist",
        "secondary_playlists",
        "confidence",
        "evidence_summary",
        "source_terms",
        "review_required",
        "selected_genre",
        "target_genre",
        "path",
        "path_exists",
        "current_genre",
        "current_src_genre",
        "changed_fields",
        "db_would_change",
        "db_updated",
        "file_would_change",
        "file_updated",
        "status",
        "status_reason",
    ]
    write_csv(plan_csv, rows, csv_fields)
    updated_count = write_m3u(m3u_path, rows)

    # Commit is considered a success when:
    # - plan run always true
    # - apply run without database error.
    success = (not args.apply) or not bool(summary_payload.get("had_db_error"))

    payload = {
        "success": success,
        "schema": SCHEMA_VERSION,
        "generatedAt": now_iso(),
        "mode": args.runtime_mode,
        "inputCsv": str(input_csv),
        "config": str(Path(args.config).resolve()),
        "master": str(Path(args.master).resolve()),
        "dbDir": str(Path(args.db_dir).resolve()) if args.db_dir else "",
        "reportsRoot": str(reports_root),
        "apply": bool(args.apply),
        "applyFileTags": bool(args.apply_file_tags),
        "summary": {
            "inputCount": len(recommendations),
            "rowsWritten": len(rows),
            "dbUpdatesApplied": summary_payload.get("db_rows_updated", 0),
            "fileTagUpdatesApplied": summary_payload.get("file_rows_updated", 0),
            "dbUpdateFailures": summary_payload.get("db_update_failures", 0),
            "fileUpdateFailures": summary_payload.get("file_update_failures", 0),
            "updatedM3uRows": updated_count,
            "policy": {
                "minConfidence": args.confidence_cutoff,
                "includeReview": bool(args.include_review),
                "excludeToolsSamples": bool(args.exclude_tools_samples),
                "overwriteExisting": bool(args.overwrite_existing),
                "includeStreaming": bool(args.include_streaming),
                "writeFileTags": bool(args.apply_file_tags),
            },
            "statusCounts": summary_payload.get("status_counts", {}),
        },
        "reports": {
            "planCsv": str(plan_csv),
            "summaryJson": str(summary_json),
            "m3u": str(m3u_path),
        },
        "inputCount": len(recommendations),
    }
    summary_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply genre recommendations to Rekordbox metadata.")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--master", required=True, help="Path to Rekordbox master.db")
    parser.add_argument("--db-dir", default="")
    parser.add_argument("--input-csv", required=True)
    parser.add_argument("--reports-root", default="reports")
    parser.add_argument("--runtime-mode", default="plan", choices=["plan", "copyapply", "liveapply"])
    parser.add_argument("--confidence-cutoff", choices=["low", "medium", "high"], default="low")
    parser.add_argument("--include-review", action="store_true", help="Keep rows with review_required=true.")
    parser.add_argument("--exclude-tools-samples", action="store_true", help="Skip rows classified as tools/samples.")
    parser.add_argument("--overwrite-existing", action="store_true")
    parser.add_argument("--include-streaming", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--apply-file-tags", action="store_true")
    parser.add_argument("--ffmpeg", default="")
    parser.add_argument("--ffprobe", default="")
    parser.add_argument("--limit", type=int, default=0)
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = run(args)
        print_json(payload)
        return 0 if payload.get("success") else 1
    except Exception as exc:
        print_json({
            "success": False,
            "schema": SCHEMA_VERSION,
            "error": str(exc),
            "generatedAt": now_iso(),
        })
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
