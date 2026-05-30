#!/usr/bin/env python3
"""Merge Rekordbox rows for exact duplicate files and quarantine duplicates."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys, traceback
sys.stdout.reconfigure(encoding='utf-8')
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


def decoded_audio_sha256(path: Path, ffmpeg: str) -> str:
    completed = subprocess.run(
        [
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
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip())
    prefix = "SHA256="
    for line in completed.stdout.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip().upper()
    raise RuntimeError(f"ffmpeg hash output did not include SHA256 for {path}")


def load_pairs(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("action") != "delete_candidate":
                continue
            if row.get("match_kind") not in {"exact_file_duplicate", "decoded_audio_duplicate"}:
                continue
            rows.append(
                {
                    "duplicate_path": Path(row["path"]),
                    "keep_path": Path(row["recommended_keep_path"]),
                    "expected_sha256": (row.get("sha256") or "").upper(),
                    "expected_decoded_audio_sha256": (row.get("decoded_audio_sha256") or "").upper(),
                    "match_kind": row.get("match_kind") or "",
                    "group_id": row.get("group_id") or "",
                }
            )
    return rows


def content_by_path(db: Rekordbox6Database) -> dict[str, list[DjmdContent]]:
    result: dict[str, list[DjmdContent]] = {}
    for row in db.query(DjmdContent).all():
        key = norm_rb_path(row.FolderPath or "")
        result.setdefault(key, []).append(row)
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
    deleted = [row for row in unique_content_rows(duplicate_rows + keep_rows) if row.ID != preserved.ID]
    return preserved, deleted


def unique_quarantine_path(quarantine_root: Path, music_root: Path, duplicate_path: Path) -> Path:
    try:
        relative = duplicate_path.relative_to(music_root)
    except ValueError:
        relative = Path(duplicate_path.name)
    target = quarantine_root / relative
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    parent = target.parent
    index = 2
    while True:
        candidate = parent / f"{stem}__dup{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


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
    playlist_ids_with_preserved = {
        str(getattr(row, scope_column))
        for row in db.query(model).filter(content_column == preserved_id).all()
    }

    for deleted_id in deleted_ids:
        rows = list(db.query(model).filter(content_column == deleted_id).all())
        for row in rows:
            scope_id = str(getattr(row, scope_column))
            if scope_id in playlist_ids_with_preserved:
                db.delete(row)
                removed_duplicates += 1
            else:
                row.ContentID = preserved_id
                playlist_ids_with_preserved.add(scope_id)
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


def run(args: argparse.Namespace) -> dict[str, Any]:
    duplicate_csv = Path(args.duplicate_csv).resolve()
    music_root = Path(args.music_root).resolve()
    quarantine_root = Path(args.quarantine_root).resolve()
    pairs = load_pairs(duplicate_csv)

    db = Rekordbox6Database(path=Path(args.master).resolve(), db_dir=Path(args.db_dir).resolve())
    applied: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    try:
        by_path = content_by_path(db)
        for pair in pairs:
            duplicate_path: Path = pair["duplicate_path"]
            keep_path: Path = pair["keep_path"]
            item: dict[str, Any] = {
                "group_id": pair["group_id"],
                "match_kind": pair["match_kind"],
                "duplicate_path": str(duplicate_path),
                "keep_path": str(keep_path),
                "expected_sha256": pair["expected_sha256"],
            }
            if not duplicate_path.exists():
                blocked.append(item | {"reason": "duplicate_path_missing"})
                continue
            if not keep_path.exists():
                blocked.append(item | {"reason": "keep_path_missing"})
                continue
            duplicate_sha = sha256_file(duplicate_path)
            keep_sha = sha256_file(keep_path)
            item["duplicate_sha256"] = duplicate_sha
            item["keep_sha256"] = keep_sha
            if pair["match_kind"] == "exact_file_duplicate":
                if pair["expected_sha256"] and duplicate_sha != pair["expected_sha256"]:
                    blocked.append(item | {"reason": "duplicate_sha_mismatch"})
                    continue
                if duplicate_sha != keep_sha:
                    blocked.append(item | {"reason": "keep_sha_mismatch"})
                    continue
            elif pair["match_kind"] == "decoded_audio_duplicate":
                if not args.ffmpeg:
                    blocked.append(item | {"reason": "ffmpeg_required_for_decoded_audio_duplicate"})
                    continue
                duplicate_decoded_sha = decoded_audio_sha256(duplicate_path, args.ffmpeg)
                keep_decoded_sha = decoded_audio_sha256(keep_path, args.ffmpeg)
                item["duplicate_decoded_audio_sha256"] = duplicate_decoded_sha
                item["keep_decoded_audio_sha256"] = keep_decoded_sha
                if pair["expected_decoded_audio_sha256"] and duplicate_decoded_sha != pair["expected_decoded_audio_sha256"]:
                    blocked.append(item | {"reason": "duplicate_decoded_audio_sha_mismatch"})
                    continue
                if duplicate_decoded_sha != keep_decoded_sha:
                    blocked.append(item | {"reason": "keep_decoded_audio_sha_mismatch"})
                    continue
            else:
                blocked.append(item | {"reason": "unsupported_match_kind"})
                continue

            duplicate_rows = by_path.get(norm_rb_path(str(duplicate_path)), [])
            keep_rows = by_path.get(norm_rb_path(str(keep_path)), [])
            preserved, deleted_rows = choose_preserved_content(db, duplicate_rows, keep_rows)
            if preserved is None:
                blocked.append(item | {"reason": "no_rekordbox_rows_found"})
                continue
            item["duplicate_content_ids"] = [int(row.ID) for row in duplicate_rows]
            item["keep_content_ids"] = [int(row.ID) for row in keep_rows]
            item["preserved_content_id"] = int(preserved.ID)
            item["deleted_content_ids"] = [int(row.ID) for row in deleted_rows]
            item["quarantine_path"] = str(unique_quarantine_path(quarantine_root, music_root, duplicate_path))
            applied.append(item)

        result: dict[str, Any] = {
            "mode": "apply" if args.apply else "plan",
            "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            "duplicateCsv": str(duplicate_csv),
            "musicRoot": str(music_root),
            "quarantineRoot": str(quarantine_root),
            "pairs": len(pairs),
            "planned": applied,
            "blocked": blocked,
            "blockedCount": len(blocked),
        }

        if args.apply:
            if blocked:
                result["success"] = True
            applied_results = []
            retired_content_ids: set[str] = set()
            for item in applied:
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
                deleted_rows = [row for row in deleted_rows if str(row.ID) not in retired_content_ids]
                if preserved is None or not deleted_rows:
                    item["db_action"] = "skipped_already_merged"
                    applied_results.append(item)
                    continue
                if str(preserved.ID) in retired_content_ids:
                    raise RuntimeError(f"No Rekordbox row found during apply for {duplicate_path}")
                merge_result = merge_content_rows(db, preserved, deleted_rows, keep_path)
                retired_content_ids.update(str(row.ID) for row in deleted_rows)
                item.update(merge_result)
                applied_results.append(item)
            db.commit()

            moved_files = []
            if args.move_files:
                for item in applied_results:
                    duplicate_path = Path(item["duplicate_path"])
                    quarantine_path = Path(item["quarantine_path"])
                    quarantine_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(duplicate_path), str(quarantine_path))
                    moved_files.append(
                        {
                            "from": str(duplicate_path),
                            "to": str(quarantine_path),
                            "sha256": item["duplicate_sha256"],
                            "from_exists_after": duplicate_path.exists(),
                            "to_exists_after": quarantine_path.exists(),
                        }
                    )

            remaining_deleted_ids = []
            for item in applied_results:
                for content_id in item["deleted_content_ids"]:
                    if db.query(DjmdContent).filter(DjmdContent.ID == str(content_id)).count():
                        remaining_deleted_ids.append(content_id)
            result.update(
                {
                    "applied": applied_results,
                    "movedFiles": moved_files,
                    "remainingDeletedContentIds": remaining_deleted_ids,
                    "success": len(remaining_deleted_ids) == 0,
                }
            )
        else:
            result["success"] = True
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Clean exact duplicate DJ files with Rekordbox membership merge.")
    parser.add_argument("--master", required=True)
    parser.add_argument("--db-dir", required=True)
    parser.add_argument("--duplicate-csv", required=True)
    parser.add_argument("--music-root", default="C:/DJ_Music")
    parser.add_argument("--quarantine-root", required=True)
    parser.add_argument("--ffmpeg")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--move-files", action="store_true")
    parser.add_argument("--allow-rekordbox-running-commit", action="store_true")
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.allow_rekordbox_running_commit:
            _disable_rekordbox_running_guard()
        result = run(args)
        print_json(result)
        return 0 if result.get("success") else 1
    except Exception as exc:  # noqa: BLE001 - CLI reports exact operator error.
        print_json({"success": False, "error": traceback.format_exc()})
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
