#!/usr/bin/env python3
"""Relink Rekordbox local file paths from one mounted drive prefix to another."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import ntpath
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from pyrekordbox import Rekordbox6Database
from pyrekordbox.db6 import DjmdContent


STREAMING_PREFIXES = ("tidal:", "qobuz:", "beatport:", "beatsource:", "soundcloud:")


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def path_from_rekordbox(value: str | None) -> str:
    if not value:
        return ""
    raw = str(value).strip()
    parsed = urlparse(raw)
    if parsed.scheme == "file":
        decoded = unquote(parsed.path or "")
        if re.match(r"^/[A-Za-z]:", decoded):
            decoded = decoded[1:]
        return decoded.replace("/", "\\")
    return unquote(raw).replace("/", "\\")


def normalize_windows_path(value: str | None) -> str:
    path = path_from_rekordbox(value)
    if not path:
        return ""
    return ntpath.normcase(ntpath.normpath(path))


def clean_windows_path(value: str | None) -> str:
    path = path_from_rekordbox(value)
    if not path:
        return ""
    return ntpath.normpath(path)


def rekordbox_path(path: str) -> str:
    return ntpath.normpath(path).replace("\\", "/")


def is_streaming_path(value: str | None) -> bool:
    if not value:
        return False
    return str(value).strip().casefold().startswith(STREAMING_PREFIXES)


def build_relink_target(folder_path: str, source_prefix: str, target_prefix: str) -> str:
    clean_path = clean_windows_path(folder_path)
    clean_source = clean_windows_path(source_prefix)
    clean_target = clean_windows_path(target_prefix)
    normalized_path = ntpath.normcase(clean_path)
    normalized_source = ntpath.normcase(clean_source)
    if not normalized_path or not normalized_path.startswith(normalized_source + "\\"):
        return ""
    suffix = clean_path[len(clean_source) :]
    target = clean_target + suffix
    return ntpath.normpath(target)


def content_plan(row: DjmdContent, source_prefix: str, target_prefix: str) -> dict[str, Any] | None:
    old_path = row.FolderPath or ""
    if is_streaming_path(old_path) or row.ServiceID:
        return None
    target = build_relink_target(old_path, source_prefix, target_prefix)
    if not target:
        return None
    target_exists = os.path.exists(target)
    return {
        "content_id": int(row.ID),
        "title": row.Title or "",
        "old_path": old_path,
        "new_path": rekordbox_path(target),
        "target_exists": target_exists,
        "file_name": ntpath.basename(target),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    master = Path(args.master).resolve()
    db_dir = Path(args.db_dir).resolve() if args.db_dir else master.parent
    db = Rekordbox6Database(path=master, db_dir=db_dir)
    try:
        rows = list(db.query(DjmdContent).all())
        plans = [
            plan for row in rows
            if (plan := content_plan(row, args.source_prefix, args.target_prefix)) is not None
        ]
        relinkable = [plan for plan in plans if plan["target_exists"]]
        blocked = [plan for plan in plans if not plan["target_exists"]]

        result: dict[str, Any] = {
            "mode": "apply" if args.apply else "plan",
            "master": str(master),
            "dbDir": str(db_dir),
            "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            "sourcePrefix": args.source_prefix,
            "targetPrefix": args.target_prefix,
            "contentsScanned": len(rows),
            "candidateRows": len(plans),
            "relinkableRows": len(relinkable),
            "blockedRows": len(blocked),
            "sampleRelinkable": relinkable[:10],
            "sampleBlocked": blocked[:10],
        }

        if args.apply:
            by_id = {plan["content_id"]: plan for plan in relinkable}
            changed = 0
            for row in rows:
                plan = by_id.get(int(row.ID))
                if not plan:
                    continue
                old_folder_path = row.FolderPath
                row.FolderPath = plan["new_path"]
                if row.OrgFolderPath == old_folder_path:
                    row.OrgFolderPath = plan["new_path"]
                row.FileNameL = plan["file_name"]
                changed += 1
            db.commit()

            remaining = []
            for row in db.query(DjmdContent).all():
                plan = content_plan(row, args.source_prefix, args.target_prefix)
                if plan and plan["target_exists"]:
                    remaining.append(plan)
            result.update(
                {
                    "changedRows": changed,
                    "remainingRelinkableRows": len(remaining),
                    "success": changed == len(relinkable) and len(remaining) == 0,
                }
            )
        else:
            result["success"] = True
        return result
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Relink Rekordbox paths between mounted drive prefixes.")
    parser.add_argument("--master", required=True)
    parser.add_argument("--db-dir", default="")
    parser.add_argument("--source-prefix", default="D:/DJ_Music")
    parser.add_argument("--target-prefix", default="C:/DJ_Music")
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
        print_json(result)
        return 0 if result.get("success") else 1
    except Exception as exc:  # noqa: BLE001 - CLI reports exact operator error.
        print_json({"success": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
