#!/usr/bin/env python3
"""Repair Rekordbox history rows whose ContentID no longer exists.

The script uses an older Rekordbox master.db as evidence for the orphaned
ContentID metadata, then maps each old row to the current collection by exact
path first and by unique filename stem only when the exact path moved.
Dry-run is the default; --apply updates DjmdSongHistory.ContentID.
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
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from pyrekordbox import Rekordbox6Database
from pyrekordbox.db6 import DjmdContent, DjmdSongHistory


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
    normalized = ntpath.normcase(ntpath.normpath(path)).replace("\\", "/")
    return normalized.replace("d:/dancing/", "f:/dancing/")


def rb_path(value: str | Path) -> str:
    return path_from_rekordbox(str(value)).replace("\\", "/")


def compact_text(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    normalized = normalized.casefold()
    normalized = re.sub(
        r"\b(original|extended|radio|club|clean|dirty|explicit|instrumental|vocal|dub|edit|mix|remix|remaster|remastered|version)\b",
        " ",
        normalized,
    )
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return re.sub(r"\s+", "", normalized).strip()


def filename_stem_key(path: str | None) -> str:
    local_path = path_from_rekordbox(path)
    return compact_text(Path(local_path).stem)


def content_by_id(db: Rekordbox6Database, ids: list[str]) -> dict[str, DjmdContent]:
    if not ids:
        return {}
    return {str(row.ID): row for row in db.query(DjmdContent).filter(DjmdContent.ID.in_(ids)).all()}


def current_indexes(rows: list[DjmdContent]) -> tuple[dict[str, list[DjmdContent]], dict[str, list[DjmdContent]]]:
    by_path: dict[str, list[DjmdContent]] = collections.defaultdict(list)
    by_stem: dict[str, list[DjmdContent]] = collections.defaultdict(list)
    for row in rows:
        by_path[normalize_windows_path(row.FolderPath)].append(row)
        by_stem[filename_stem_key(row.FolderPath)].append(row)
    return by_path, by_stem


def build_plan(
    live_db: Rekordbox6Database,
    backup_db: Rekordbox6Database,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    current_rows = list(live_db.query(DjmdContent).all())
    current_ids = {str(row.ID) for row in current_rows}
    orphan_counts = collections.Counter(
        str(row.ContentID)
        for row in live_db.query(DjmdSongHistory).filter(~DjmdSongHistory.ContentID.in_(current_ids)).all()
    )
    old_rows = content_by_id(backup_db, list(orphan_counts.keys()))
    by_path, by_stem = current_indexes(current_rows)

    plan: list[dict[str, Any]] = []
    for old_id, history_count in sorted(orphan_counts.items(), key=lambda item: int(item[0])):
        old = old_rows.get(old_id)
        if old is None:
            plan.append(
                {
                    "status": "unresolved_missing_old_content",
                    "old_content_id": old_id,
                    "history_rows": history_count,
                }
            )
            continue

        exact_matches = by_path.get(normalize_windows_path(old.FolderPath), [])
        stem_matches = by_stem.get(filename_stem_key(old.FolderPath), [])
        if len(exact_matches) == 1:
            new = exact_matches[0]
            status = "exact_path_match"
        elif len(stem_matches) == 1:
            new = stem_matches[0]
            status = "unique_filename_stem"
        else:
            plan.append(
                {
                    "status": "unresolved_ambiguous_or_missing_match",
                    "old_content_id": old_id,
                    "history_rows": history_count,
                    "old_title": old.Title,
                    "old_path": old.FolderPath,
                    "exact_candidate_count": len(exact_matches),
                    "stem_candidate_count": len(stem_matches),
                    "stem_candidates": [
                        {"content_id": int(row.ID), "title": row.Title, "path": row.FolderPath}
                        for row in stem_matches[:10]
                    ],
                }
            )
            continue

        plan.append(
            {
                "status": status,
                "old_content_id": old_id,
                "new_content_id": str(new.ID),
                "history_rows": history_count,
                "old_title": old.Title,
                "new_title": new.Title,
                "old_path": old.FolderPath,
                "new_path": new.FolderPath,
            }
        )

    summary = {
        "orphan_content_ids": len(orphan_counts),
        "orphan_history_rows": sum(orphan_counts.values()),
        "plan_status": dict(collections.Counter(row["status"] for row in plan)),
        "planned_history_rows": sum(
            int(row.get("history_rows") or 0)
            for row in plan
            if row["status"] in {"exact_path_match", "unique_filename_stem"}
        ),
        "unresolved_content_ids": sum(1 for row in plan if row["status"].startswith("unresolved")),
    }
    return plan, summary


def apply_plan(db: Rekordbox6Database, plan: list[dict[str, Any]]) -> dict[str, Any]:
    updated_rows = 0
    updated_content_ids = 0
    for item in plan:
        if item["status"] not in {"exact_path_match", "unique_filename_stem"}:
            continue
        rows = list(db.query(DjmdSongHistory).filter(DjmdSongHistory.ContentID == item["old_content_id"]).all())
        for row in rows:
            row.ContentID = item["new_content_id"]
            updated_rows += 1
        if rows:
            updated_content_ids += 1
    db.commit()
    return {
        "updated_history_rows": updated_rows,
        "updated_content_ids": updated_content_ids,
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "status",
        "old_content_id",
        "new_content_id",
        "history_rows",
        "old_title",
        "new_title",
        "old_path",
        "new_path",
        "exact_candidate_count",
        "stem_candidate_count",
        "stem_candidates",
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


def run(args: argparse.Namespace) -> dict[str, Any]:
    reports_root = Path(args.reports_root)
    if not reports_root.is_absolute():
        reports_root = Path(args.repo_root).resolve() / reports_root
    reports_root.mkdir(parents=True, exist_ok=True)
    stamp = now_stamp()
    summary_json = reports_root / f"rekordbox-history-orphan-repair-summary-{stamp}.json"
    plan_csv = reports_root / f"rekordbox-history-orphan-repair-{stamp}.csv"

    live_kwargs: dict[str, Any] = {"path": Path(args.master).resolve()}
    if args.db_dir:
        live_kwargs["db_dir"] = Path(args.db_dir).resolve()
    live_db = Rekordbox6Database(**live_kwargs)
    backup_path = Path(args.backup_master).resolve()
    backup_db = Rekordbox6Database(path=backup_path, db_dir=backup_path.parent)

    live_db.open()
    backup_db.open()
    apply_result: dict[str, Any] | None = None
    try:
        plan, summary = build_plan(live_db, backup_db)
        if args.apply:
            apply_result = apply_plan(live_db, plan)
        else:
            live_db.rollback()
    except Exception:
        live_db.rollback()
        raise
    finally:
        backup_db.close()
        live_db.close()

    write_csv(plan_csv, plan)
    payload = {
        "mode": "apply" if args.apply else "plan",
        "success": True,
        "generated_at": now_iso(),
        "master": str(Path(args.master).resolve()),
        "backup_master": str(backup_path),
        "summary": summary,
        "apply": apply_result,
        "reports": {
            "summary_json": str(summary_json),
            "plan_csv": str(plan_csv),
        },
        "sample": plan[:25],
    }
    summary_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--master", required=True)
    parser.add_argument("--db-dir")
    parser.add_argument("--backup-master", required=True)
    parser.add_argument("--reports-root", default="reports")
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
