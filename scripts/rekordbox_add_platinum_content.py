#!/usr/bin/env python3
"""Plan or apply Rekordbox collection additions for Processed_Library_Root output files."""

from __future__ import annotations

import argparse
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
from pyrekordbox.db6 import DjmdContent


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


def normalize_windows_path(value: str | None) -> str:
    path = path_from_rekordbox(value)
    if not path:
        return ""
    return ntpath.normcase(ntpath.normpath(path))


def title_from_path(path: Path) -> str:
    stem = path.stem
    stem = re.sub(r"[_\s]+pn$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem or path.stem


def collect_audio_files(source_root: Path, extensions: set[str]) -> list[Path]:
    files: list[Path] = []
    for current_root, dirs, names in os.walk(source_root):
        dirs[:] = [name for name in dirs if name not in {".git", "runtime", "reports"}]
        for name in names:
            path = Path(current_root) / name
            if path.suffix.casefold() in extensions:
                files.append(path.resolve())
    files.sort(key=lambda item: normalize_windows_path(str(item)))
    return files


def load_duplicate_delete_candidates(path: Path | None) -> set[str]:
    if path is None:
        return set()
    if not path.exists():
        raise FileNotFoundError(f"Duplicate CSV not found: {path}")

    result: set[str] = set()
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if (row.get("action") or "").casefold() == "delete_candidate":
                result.add(normalize_windows_path(row.get("path") or ""))
    return result


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "status",
        "path",
        "title",
        "reason",
        "rekordbox_content_id",
        "error",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def content_paths(db: Rekordbox6Database) -> set[str]:
    paths: set[str] = set()
    for row in db.query(DjmdContent).all():
        normalized = normalize_windows_path(row.FolderPath or "")
        if normalized:
            paths.add(normalized)
    return paths


def build_plan(
    db: Rekordbox6Database,
    source_root: Path,
    extensions: set[str],
    duplicate_delete_candidates: set[str],
    limit: int,
) -> tuple[list[Path], list[dict[str, Any]], dict[str, Any]]:
    audio_files = collect_audio_files(source_root, extensions)
    existing_paths = content_paths(db)
    candidates: list[Path] = []
    rows: list[dict[str, Any]] = []
    skipped_existing = 0
    skipped_duplicate = 0

    for path in audio_files:
        normalized = normalize_windows_path(str(path))
        title = title_from_path(path)
        if normalized in existing_paths:
            skipped_existing += 1
            rows.append(
                {
                    "status": "already_in_rekordbox",
                    "path": str(path),
                    "title": title,
                    "reason": "exact normalized path already exists in Rekordbox",
                }
            )
            continue
        if normalized in duplicate_delete_candidates:
            skipped_duplicate += 1
            rows.append(
                {
                    "status": "skipped_exact_duplicate_delete_candidate",
                    "path": str(path),
                    "title": title,
                    "reason": "marked delete_candidate in duplicate CSV",
                }
            )
            continue
        if limit > 0 and len(candidates) >= limit:
            rows.append(
                {
                    "status": "skipped_by_limit",
                    "path": str(path),
                    "title": title,
                    "reason": f"limit {limit} reached",
                }
            )
            continue

        candidates.append(path)
        rows.append(
            {
                "status": "add_candidate",
                "path": str(path),
                "title": title,
                "reason": "not present by normalized path",
            }
        )

    summary = {
        "audio_files_scanned": len(audio_files),
        "rekordbox_existing_paths": len(existing_paths),
        "already_in_rekordbox": skipped_existing,
        "skipped_exact_duplicate_delete_candidates": skipped_duplicate,
        "add_candidates": len(candidates),
        "limit": limit,
    }
    return candidates, rows, summary


def apply_candidates(db: Rekordbox6Database, candidates: list[Path], rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_path = {str(path): row for row in rows if row["status"] == "add_candidate" for path in [Path(row["path"])]}
    before = db.query(DjmdContent).count()
    added = 0
    errors: list[dict[str, str]] = []

    try:
        for path in candidates:
            title = title_from_path(path)
            content = db.add_content(str(path), Title=title)
            added += 1
            row = by_path.get(str(path))
            if row is not None:
                row["status"] = "added"
                row["rekordbox_content_id"] = str(content.ID)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - batch rollback report.
        db.rollback()
        errors.append({"path": str(path), "error": str(exc)})
        for row in rows:
            if row["status"] == "added":
                row["status"] = "rolled_back"
                row["reason"] = "batch rolled back after error"
        added = 0

    after = db.query(DjmdContent).count()
    found_after_add = 0
    if not errors:
        existing = content_paths(db)
        found_after_add = sum(1 for path in candidates if normalize_windows_path(str(path)) in existing)

    return {
        "count_before": before,
        "count_after": after,
        "added": added,
        "found_after_add": found_after_add,
        "errors": errors,
        "success": not errors and found_after_add == len(candidates) and after == before + len(candidates),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    source_root = Path(args.source_root).resolve()
    if not source_root.exists() or not source_root.is_dir():
        raise FileNotFoundError(f"Source root not found: {source_root}")

    extensions = {item.casefold() if item.startswith(".") else f".{item.casefold()}" for item in args.extension}
    reports_root = Path(args.reports_root)
    if not reports_root.is_absolute():
        reports_root = Path(args.repo_root).resolve() / reports_root
    reports_root.mkdir(parents=True, exist_ok=True)

    duplicate_csv = Path(args.duplicate_csv).resolve() if args.duplicate_csv else None
    duplicate_delete_candidates = load_duplicate_delete_candidates(duplicate_csv)

    stamp = now_stamp()
    plan_csv = reports_root / f"rekordbox-processed_library_root-add-plan-{stamp}.csv"
    summary_json = reports_root / f"rekordbox-processed_library_root-add-summary-{stamp}.json"

    db_kwargs: dict[str, Any] = {"path": Path(args.master).resolve()}
    if args.db_dir:
        db_kwargs["db_dir"] = Path(args.db_dir).resolve()

    db = Rekordbox6Database(**db_kwargs)
    db.open()
    try:
        candidates, rows, summary = build_plan(
            db,
            source_root,
            extensions,
            duplicate_delete_candidates,
            args.limit,
        )
        apply_result = None
        if args.apply:
            apply_result = apply_candidates(db, candidates, rows)
        write_csv(plan_csv, rows)
    finally:
        db.close()

    payload = {
        "mode": "apply" if args.apply else "plan",
        "success": True if not args.apply else bool(apply_result and apply_result["success"]),
        "generated_at": now_iso(),
        "source_root": str(source_root),
        "master": str(Path(args.master).resolve()),
        "duplicate_csv": str(duplicate_csv) if duplicate_csv else None,
        "duplicate_delete_candidates_loaded": len(duplicate_delete_candidates),
        "summary": summary,
        "apply": apply_result,
        "reports": {
            "summary_json": str(summary_json),
            "plan_csv": str(plan_csv),
        },
        "sample_candidates": [str(path) for path in candidates[:20]],
    }
    summary_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Add missing Processed_Library_Root files to a Rekordbox database.")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--master", required=True)
    parser.add_argument("--db-dir")
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--reports-root", default="reports")
    parser.add_argument("--duplicate-csv")
    parser.add_argument("--extension", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
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
