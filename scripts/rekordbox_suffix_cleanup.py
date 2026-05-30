#!/usr/bin/env python3
"""Remove trailing _processed particles from Processed_Library_Root filenames and Rekordbox paths.

Dry-run is the default. With --apply-files it renames disk files under the
source root. With --apply-db it updates Rekordbox content paths to the renamed
files. Use a live database backup before --apply-db on the real Rekordbox DB.
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from pyrekordbox import Rekordbox6Database
from pyrekordbox.db6 import DjmdContent


PN_PATTERN = re.compile(r"[_\s]pn(?=\.)", re.IGNORECASE)


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


def rb_path(path: str | Path) -> str:
    return path_from_rekordbox(str(path)).replace("\\", "/")


def normalized(path: str | Path | None) -> str:
    return rb_path(path or "").casefold()


def has_pn_particle(path: str | Path | None) -> bool:
    if not path:
        return False
    return PN_PATTERN.search(Path(path_from_rekordbox(str(path))).name) is not None


def remove_pn_particle(path: str | Path) -> Path:
    source = Path(path_from_rekordbox(str(path)))
    new_name = PN_PATTERN.sub("", source.name)
    return source.with_name(new_name)


def collect_disk_actions(source_root: Path) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for current_root, dirs, names in os.walk(source_root):
        dirs[:] = [
            name
            for name in dirs
            if name not in {".git", "runtime", "reports", "_DUPLICATE_QUARANTINE", "_CD_QUALITY_BACKUP"}
        ]
        for name in names:
            if PN_PATTERN.search(name) is None:
                continue
            source = Path(current_root) / name
            target = remove_pn_particle(source)
            status = "planned"
            reason = "remove_pn_particle"
            if target == source:
                status = "skipped_same_path"
                reason = "target equals source"
            elif target.exists():
                status = "blocked_target_exists"
                reason = "target already exists"
            actions.append(
                {
                    "kind": "file",
                    "status": status,
                    "reason": reason,
                    "old_path": str(source),
                    "new_path": str(target),
                }
            )
    actions.sort(key=lambda row: normalized(row["old_path"]))
    return actions


def collect_db_actions(db: Rekordbox6Database) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for row in db.query(DjmdContent).all():
        old_path = row.FolderPath or ""
        if not has_pn_particle(old_path):
            continue
        target = remove_pn_particle(old_path)
        actions.append(
            {
                "kind": "rekordbox",
                "status": "planned",
                "reason": "remove_pn_particle",
                "content_id": int(row.ID),
                "title": row.Title,
                "old_path": old_path,
                "new_path": rb_path(target),
                "old_exists": Path(path_from_rekordbox(old_path)).exists(),
                "new_exists_before_apply": target.exists(),
            }
        )
    actions.sort(key=lambda row: int(row["content_id"]))
    return actions


def apply_file_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for action in actions:
        result = dict(action)
        if action["status"] != "planned":
            result["apply_status"] = "skipped"
            results.append(result)
            continue
        old_path = Path(action["old_path"])
        new_path = Path(action["new_path"])
        if not old_path.exists():
            result["apply_status"] = "blocked_source_missing"
        elif new_path.exists():
            result["apply_status"] = "blocked_target_exists"
        else:
            old_path.rename(new_path)
            result["apply_status"] = "renamed"
            result["old_exists_after"] = old_path.exists()
            result["new_exists_after"] = new_path.exists()
        results.append(result)
    return results


def apply_db_actions(
    db: Rekordbox6Database,
    actions: list[dict[str, Any]],
    allow_missing_targets: bool,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for action in actions:
        result = dict(action)
        row = db.query(DjmdContent).filter(DjmdContent.ID == str(action["content_id"])).first()
        if row is None:
            result["apply_status"] = "blocked_content_missing"
            results.append(result)
            continue
        new_path = Path(path_from_rekordbox(action["new_path"]))
        if not allow_missing_targets and not new_path.exists():
            result["apply_status"] = "blocked_target_missing"
            results.append(result)
            continue
        old_path = row.FolderPath
        row.FolderPath = rb_path(new_path)
        if not row.OrgFolderPath or row.OrgFolderPath == old_path or has_pn_particle(row.OrgFolderPath):
            row.OrgFolderPath = rb_path(new_path)
        row.FileNameL = new_path.name
        result["apply_status"] = "updated"
        results.append(result)
    db.commit()
    return results


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "kind",
        "status",
        "apply_status",
        "reason",
        "content_id",
        "title",
        "old_path",
        "new_path",
        "old_exists",
        "new_exists_before_apply",
        "old_exists_after",
        "new_exists_after",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def counter(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    return dict(sorted(collections.Counter(str(row.get(field) or "") for row in rows).items()))


def run(args: argparse.Namespace) -> dict[str, Any]:
    source_root = Path(args.source_root).resolve()
    if not source_root.exists():
        raise FileNotFoundError(f"Source root not found: {source_root}")
    reports_root = Path(args.reports_root)
    if not reports_root.is_absolute():
        reports_root = Path(args.repo_root).resolve() / reports_root
    reports_root.mkdir(parents=True, exist_ok=True)

    db_kwargs: dict[str, Any] = {"path": Path(args.master).resolve()}
    if args.db_dir:
        db_kwargs["db_dir"] = Path(args.db_dir).resolve()

    db = Rekordbox6Database(**db_kwargs)
    db.open()
    try:
        file_actions = collect_disk_actions(source_root)
        db_actions = collect_db_actions(db)
        file_results = apply_file_actions(file_actions) if args.apply_files else file_actions
        db_results = (
            apply_db_actions(db, db_actions, args.allow_missing_targets)
            if args.apply_db
            else db_actions
        )
        if not args.apply_db:
            db.rollback()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    stamp = now_stamp()
    report_csv = reports_root / f"rekordbox-pn-suffix-cleanup-{stamp}.csv"
    summary_json = reports_root / f"rekordbox-pn-suffix-cleanup-summary-{stamp}.json"
    rows = file_results + db_results
    write_csv(report_csv, rows)
    payload = {
        "mode": "apply" if args.apply_files or args.apply_db else "plan",
        "success": True,
        "generated_at": now_iso(),
        "source_root": str(source_root),
        "master": str(Path(args.master).resolve()),
        "summary": {
            "file_actions": len(file_actions),
            "file_status": counter(file_actions, "status"),
            "file_apply_status": counter(file_results, "apply_status") if args.apply_files else {},
            "rekordbox_actions": len(db_actions),
            "rekordbox_status": counter(db_actions, "status"),
            "rekordbox_apply_status": counter(db_results, "apply_status") if args.apply_db else {},
        },
        "reports": {
            "summary_json": str(summary_json),
            "csv": str(report_csv),
        },
        "sample": rows[:25],
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
    parser.add_argument("--apply-files", action="store_true")
    parser.add_argument("--apply-db", action="store_true")
    parser.add_argument("--allow-missing-targets", action="store_true")
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        print_json(run(args))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI reports exact operator error.
        print_json({"success": False, "generated_at": now_iso(), "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
