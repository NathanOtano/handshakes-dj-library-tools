#!/usr/bin/env python3
"""Repair Rekordbox rows so Processed_Library_Root files and collection rows line up.

The helper is dry-run by default. With --apply it only mutates Rekordbox rows:
missing *_pn rows are relinked to the matching existing Processed_Library_Root file when the
match is unambiguous, duplicate content rows for the same path are merged, and
playlist/history/tag links are repointed to the preserved content row.
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import ntpath
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from pyrekordbox import Rekordbox6Database
from pyrekordbox.db6 import (
    DjmdContent,
    DjmdSongHistory,
    DjmdSongMyTag,
    DjmdSongPlaylist,
    DjmdSongTagList,
)


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def path_from_rekordbox(value: str | None) -> str:
    if not value:
        return ""
    raw = str(value).strip()
    parsed = urlparse(raw)
    if parsed.scheme == "file":
        decoded = unquote(parsed.path or "")
        if parsed.netloc and parsed.netloc.lower() not in {"localhost", ""}:
            decoded = f"//{parsed.netloc}{decoded}"
        if re.match(r"^/[A-Za-z]:", decoded):
            decoded = decoded[1:]
        return decoded.replace("/", "\\")
    return unquote(raw).replace("/", "\\")


def normalize_windows_path(value: str | Path | None) -> str:
    path = path_from_rekordbox(str(value)) if value is not None else ""
    if not path:
        return ""
    return ntpath.normcase(ntpath.normpath(path))


def rb_path(value: str | Path) -> str:
    return path_from_rekordbox(str(value)).replace("\\", "/")


def is_under_path(path_norm: str, root_norm: str) -> bool:
    return path_norm == root_norm or path_norm.startswith(root_norm + "\\")


def path_exists(value: str | None) -> bool:
    local_path = path_from_rekordbox(value)
    return bool(local_path) and Path(local_path).exists()


def canonical_stem(stem: str) -> str:
    return re.sub(r"[_\s]+pn$", "", stem, flags=re.IGNORECASE).casefold()


def title_from_path(path: Path) -> str:
    stem = re.sub(r"[_\s]+pn$", "", path.stem, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", stem).strip() or path.stem


def collect_audio_files(source_root: Path, extensions: set[str]) -> list[Path]:
    files: list[Path] = []
    for current_root, dirs, names in os.walk(source_root):
        dirs[:] = [name for name in dirs if name not in {".git", "runtime", "reports"}]
        for name in names:
            path = Path(current_root) / name
            if path.suffix.casefold() in extensions:
                files.append(path.resolve())
    files.sort(key=normalize_windows_path)
    return files


def build_file_indexes(files: list[Path]) -> tuple[dict[str, Path], dict[tuple[str, str], list[Path]]]:
    by_norm = {normalize_windows_path(path): path for path in files}
    by_canonical: dict[tuple[str, str], list[Path]] = collections.defaultdict(list)
    for path in files:
        by_canonical[(canonical_stem(path.stem), path.suffix.casefold())].append(path)
    for paths in by_canonical.values():
        paths.sort(key=normalize_windows_path)
    return by_norm, by_canonical


def content_by_path(rows: list[DjmdContent]) -> dict[str, list[DjmdContent]]:
    result: dict[str, list[DjmdContent]] = collections.defaultdict(list)
    for row in rows:
        normalized = normalize_windows_path(row.FolderPath or "")
        if normalized:
            result[normalized].append(row)
    return result


def count_content_links(db: Rekordbox6Database) -> dict[str, collections.Counter[str]]:
    counters: dict[str, collections.Counter[str]] = {
        "playlist": collections.Counter(),
        "history": collections.Counter(),
        "mytag": collections.Counter(),
        "taglist": collections.Counter(),
    }
    for row in db.query(DjmdSongPlaylist).all():
        counters["playlist"][str(row.ContentID)] += 1
    for row in db.query(DjmdSongHistory).all():
        counters["history"][str(row.ContentID)] += 1
    for row in db.query(DjmdSongMyTag).all():
        counters["mytag"][str(row.ContentID)] += 1
    for row in db.query(DjmdSongTagList).all():
        counters["taglist"][str(row.ContentID)] += 1
    return counters


def reference_breakdown(counters: dict[str, collections.Counter[str]], content_id: Any) -> dict[str, int]:
    key = str(content_id)
    return {name: int(counter[key]) for name, counter in counters.items()}


def reference_count(counters: dict[str, collections.Counter[str]], content_id: Any) -> int:
    return sum(reference_breakdown(counters, content_id).values())


def live_reference_count(db: Rekordbox6Database, content_id: Any) -> int:
    cid = str(content_id)
    return (
        db.query(DjmdSongPlaylist).filter(DjmdSongPlaylist.ContentID == cid).count()
        + db.query(DjmdSongHistory).filter(DjmdSongHistory.ContentID == cid).count()
        + db.query(DjmdSongMyTag).filter(DjmdSongMyTag.ContentID == cid).count()
        + db.query(DjmdSongTagList).filter(DjmdSongTagList.ContentID == cid).count()
    )


def choose_preserved(rows: list[DjmdContent], counters: dict[str, collections.Counter[str]]) -> DjmdContent | None:
    unique = {str(row.ID): row for row in rows}
    if not unique:
        return None
    return sorted(
        unique.values(),
        key=lambda row: (reference_count(counters, row.ID), int(row.ID)),
        reverse=True,
    )[0]


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


def merge_content_rows(
    db: Rekordbox6Database,
    preserved: DjmdContent,
    deleted_rows: list[DjmdContent],
    target_path: Path,
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

    target_rb_path = rb_path(target_path)
    old_preserved_path = preserved.FolderPath
    preserved.FolderPath = target_rb_path
    if not preserved.OrgFolderPath or preserved.OrgFolderPath == old_preserved_path:
        preserved.OrgFolderPath = target_rb_path
    preserved.FileNameL = target_path.name
    if not preserved.Title:
        preserved.Title = title_from_path(target_path)

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
        "target_path": target_rb_path,
    }


def candidate_targets(
    old_path: Path,
    files_by_norm: dict[str, Path],
    files_by_canonical: dict[tuple[str, str], list[Path]],
) -> tuple[list[Path], str]:
    candidates: list[Path] = []
    seen: set[str] = set()
    reason = "canonical_filename"
    new_stem = re.sub(r"[_\s]+pn$", "", old_path.stem, flags=re.IGNORECASE)
    if new_stem != old_path.stem:
        same_folder = old_path.with_name(new_stem + old_path.suffix)
        same_norm = normalize_windows_path(same_folder)
        if same_norm in files_by_norm:
            candidates.append(files_by_norm[same_norm])
            seen.add(same_norm)
            reason = "same_folder_without_pn_suffix"

    key = (canonical_stem(old_path.stem), old_path.suffix.casefold())
    for path in files_by_canonical.get(key, []):
        normalized = normalize_windows_path(path)
        if normalized not in seen:
            candidates.append(path)
            seen.add(normalized)
    return candidates, reason


def select_target(old_path: Path, candidates: list[Path]) -> tuple[Path | None, str]:
    if not candidates:
        return None, "no_candidate"
    if len(candidates) == 1:
        return candidates[0], "single_candidate"

    old_parent = normalize_windows_path(old_path.parent)
    same_parent = [path for path in candidates if normalize_windows_path(path.parent) == old_parent]
    if len(same_parent) == 1:
        return same_parent[0], "one_same_folder_candidate"
    return None, "ambiguous_candidates"


def build_missing_actions(
    rows: list[DjmdContent],
    by_norm: dict[str, list[DjmdContent]],
    counters: dict[str, collections.Counter[str]],
    source_root_norm: str,
    files_by_norm: dict[str, Path],
    files_by_canonical: dict[tuple[str, str], list[Path]],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for row in rows:
        row_norm = normalize_windows_path(row.FolderPath or "")
        if not row_norm or not is_under_path(row_norm, source_root_norm):
            continue
        if path_exists(row.FolderPath):
            continue

        old_path = Path(path_from_rekordbox(row.FolderPath))
        candidates, reason = candidate_targets(old_path, files_by_norm, files_by_canonical)
        target, target_status = select_target(old_path, candidates)
        refs = reference_breakdown(counters, row.ID)
        ref_total = sum(refs.values())

        action_status = "review"
        target_rows = []
        if target is not None:
            target_rows = [item for item in by_norm.get(normalize_windows_path(target), []) if str(item.ID) != str(row.ID)]
            action_status = "auto_relink_merge" if target_rows else "auto_relink_only"
        elif ref_total == 0:
            action_status = "auto_delete_unreferenced_missing"

        actions.append(
            {
                "action": "missing_path",
                "status": action_status,
                "reason": reason if action_status != "review" else target_status,
                "content_id": int(row.ID),
                "old_path": row.FolderPath,
                "target_path": rb_path(target) if target is not None else "",
                "target_content_ids": [int(item.ID) for item in target_rows],
                "candidate_count": len(candidates),
                "candidate_paths": [rb_path(path) for path in candidates[:10]],
                "references": refs,
            }
        )
    return actions


def build_same_path_actions(
    rows: list[DjmdContent],
    source_root_norm: str,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for path_norm, group_rows in sorted(content_by_path(rows).items(), key=lambda item: item[0]):
        if len(group_rows) < 2 or not is_under_path(path_norm, source_root_norm):
            continue
        sample_path = group_rows[0].FolderPath or ""
        if not path_exists(sample_path):
            continue
        actions.append(
            {
                "action": "duplicate_same_path",
                "status": "auto_merge_same_path",
                "path": rb_path(sample_path),
                "content_ids": [int(row.ID) for row in group_rows],
            }
        )
    return actions


def get_content(db: Rekordbox6Database, content_id: Any) -> DjmdContent | None:
    return db.query(DjmdContent).filter(DjmdContent.ID == str(content_id)).first()


def apply_missing_actions(db: Rekordbox6Database, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    counters = count_content_links(db)
    for action in actions:
        status = action["status"]
        if status not in {"auto_relink_merge", "auto_relink_only", "auto_delete_unreferenced_missing"}:
            continue
        row = get_content(db, action["content_id"])
        if row is None:
            applied.append(action | {"apply_status": "skipped_missing_content_row"})
            continue

        if status == "auto_delete_unreferenced_missing":
            refs = live_reference_count(db, row.ID)
            if refs > 0:
                applied.append(action | {"apply_status": "blocked_references_found", "live_reference_count": refs})
                continue
            db.delete(row)
            applied.append(action | {"apply_status": "deleted_unreferenced_missing"})
            continue

        target_path = Path(path_from_rekordbox(action["target_path"]))
        if not target_path.exists():
            applied.append(action | {"apply_status": "blocked_target_missing"})
            continue
        target_rows = []
        for target_content_id in action.get("target_content_ids", []):
            target_row = get_content(db, target_content_id)
            if target_row is not None and str(target_row.ID) != str(row.ID):
                target_rows.append(target_row)
        if target_rows:
            preserved = choose_preserved([row] + target_rows, counters)
            if preserved is None:
                applied.append(action | {"apply_status": "blocked_no_preserved_row"})
                continue
            deleted_rows = [item for item in [row] + target_rows if str(item.ID) != str(preserved.ID)]
            result = merge_content_rows(db, preserved, deleted_rows, target_path)
            applied.append(action | {"apply_status": "merged", **result})
        else:
            old_path = row.FolderPath
            row.FolderPath = rb_path(target_path)
            if not row.OrgFolderPath or row.OrgFolderPath == old_path:
                row.OrgFolderPath = rb_path(target_path)
            row.FileNameL = target_path.name
            applied.append(action | {"apply_status": "relinked", "preserved_content_id": int(row.ID)})
    return applied


def apply_same_path_actions(db: Rekordbox6Database, source_root_norm: str) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    rows = list(db.query(DjmdContent).all())
    counters = count_content_links(db)
    for action in build_same_path_actions(rows, source_root_norm):
        group_rows = [
            get_content(db, content_id)
            for content_id in action["content_ids"]
        ]
        live_rows = [row for row in group_rows if row is not None]
        if len(live_rows) < 2:
            continue
        preserved = choose_preserved(live_rows, counters)
        if preserved is None:
            continue
        deleted_rows = [row for row in live_rows if str(row.ID) != str(preserved.ID)]
        result = merge_content_rows(db, preserved, deleted_rows, Path(path_from_rekordbox(action["path"])))
        applied.append(action | {"apply_status": "merged", **result})
    return applied


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "action",
        "status",
        "apply_status",
        "reason",
        "content_id",
        "old_path",
        "target_path",
        "path",
        "target_content_ids",
        "content_ids",
        "preserved_content_id",
        "deleted_content_ids",
        "moved_memberships",
        "removed_duplicate_memberships",
        "moved_history_entries",
        "moved_my_tags",
        "removed_duplicate_my_tags",
        "moved_tag_list_entries",
        "candidate_count",
        "candidate_paths",
        "references",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            clean = dict(row)
            for key, value in list(clean.items()):
                if isinstance(value, (dict, list)):
                    clean[key] = json.dumps(value, ensure_ascii=False)
            writer.writerow(clean)


def summarize_rows(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counter = collections.Counter(str(row.get(key) or "") for row in rows)
    return dict(sorted(counter.items()))


def run(args: argparse.Namespace) -> dict[str, Any]:
    source_root = Path(args.source_root).resolve()
    if not source_root.exists() or not source_root.is_dir():
        raise FileNotFoundError(f"Source root not found: {source_root}")

    extensions = {item.casefold() if item.startswith(".") else f".{item.casefold()}" for item in args.extension}
    reports_root = Path(args.reports_root)
    if not reports_root.is_absolute():
        reports_root = Path(args.repo_root).resolve() / reports_root
    reports_root.mkdir(parents=True, exist_ok=True)
    stamp = now_stamp()
    action_csv = reports_root / f"rekordbox-processed_library_root-reconcile-{stamp}.csv"
    summary_json = reports_root / f"rekordbox-processed_library_root-reconcile-summary-{stamp}.json"

    files = collect_audio_files(source_root, extensions)
    files_by_norm, files_by_canonical = build_file_indexes(files)
    source_root_norm = normalize_windows_path(source_root)

    db_kwargs: dict[str, Any] = {"path": Path(args.master).resolve()}
    if args.db_dir:
        db_kwargs["db_dir"] = Path(args.db_dir).resolve()

    db = Rekordbox6Database(**db_kwargs)
    db.open()
    applied_missing: list[dict[str, Any]] = []
    applied_same_path: list[dict[str, Any]] = []
    try:
        rows = list(db.query(DjmdContent).all())
        by_norm = content_by_path(rows)
        counters = count_content_links(db)
        missing_actions = build_missing_actions(
            rows,
            by_norm,
            counters,
            source_root_norm,
            files_by_norm,
            files_by_canonical,
        )
        same_path_actions = build_same_path_actions(rows, source_root_norm)

        if args.apply:
            applied_missing = apply_missing_actions(db, missing_actions)
            applied_same_path = apply_same_path_actions(db, source_root_norm)
            db.commit()
        else:
            db.rollback()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    report_rows = missing_actions + same_path_actions
    if args.apply:
        report_rows = applied_missing + applied_same_path
    write_csv(action_csv, report_rows)

    payload = {
        "mode": "apply" if args.apply else "plan",
        "success": True,
        "generated_at": now_iso(),
        "source_root": str(source_root),
        "master": str(Path(args.master).resolve()),
        "summary": {
            "audio_files_scanned": len(files),
            "missing_path_rows": len(missing_actions),
            "missing_path_status": summarize_rows(missing_actions, "status"),
            "same_path_duplicate_groups": len(same_path_actions),
            "same_path_duplicate_rows": sum(max(0, len(row.get("content_ids", [])) - 1) for row in same_path_actions),
            "applied_missing_rows": len(applied_missing),
            "applied_same_path_groups": len(applied_same_path),
            "apply_status": summarize_rows(applied_missing + applied_same_path, "apply_status") if args.apply else {},
            "moved_memberships": sum(int(row.get("moved_memberships") or 0) for row in applied_missing + applied_same_path),
            "removed_duplicate_memberships": sum(
                int(row.get("removed_duplicate_memberships") or 0) for row in applied_missing + applied_same_path
            ),
            "moved_history_entries": sum(int(row.get("moved_history_entries") or 0) for row in applied_missing + applied_same_path),
            "moved_my_tags": sum(int(row.get("moved_my_tags") or 0) for row in applied_missing + applied_same_path),
            "removed_duplicate_my_tags": sum(
                int(row.get("removed_duplicate_my_tags") or 0) for row in applied_missing + applied_same_path
            ),
            "moved_tag_list_entries": sum(int(row.get("moved_tag_list_entries") or 0) for row in applied_missing + applied_same_path),
            "deleted_content_rows": sum(len(row.get("deleted_content_ids", [])) for row in applied_missing + applied_same_path),
        },
        "reports": {
            "summary_json": str(summary_json),
            "action_csv": str(action_csv),
        },
        "sample_actions": report_rows[:20],
    }
    summary_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--master", required=True)
    parser.add_argument("--db-dir")
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--reports-root", default="reports")
    parser.add_argument("--extension", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
        print_json(result)
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI reports exact operator error.
        print_json({"success": False, "generated_at": now_iso(), "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
