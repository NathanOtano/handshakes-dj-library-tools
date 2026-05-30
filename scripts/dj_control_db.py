#!/usr/bin/env python3
"""Maintain the local DJ Library control database.

This script only writes the Codex-owned runtime database when --apply is passed.
It never writes audio files or Rekordbox databases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


AUDIO_DEFAULTS = [".flac", ".wav", ".aiff", ".aif", ".m4a", ".mp3", ".ogg", ".opus"]
SCHEMA_VERSION = "0.1.0"
OPERATION_STATUSES = {"draft", "staged", "applied", "blocked", "dropped"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def parse_json_arg(value: str | None, field_name: str) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return parsed


def resolve_configured_path(value: str | None, repo_root: Path) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    return (repo_root / path).resolve()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    return con


def apply_schema(db_path: Path, schema_path: Path) -> None:
    schema_sql = schema_path.read_text(encoding="utf-8")
    with connect(db_path) as con:
        con.executescript(schema_sql)
        con.execute(
            "INSERT OR REPLACE INTO control_meta(key, value, updated_at) VALUES (?, ?, ?)",
            ("schema_version", SCHEMA_VERSION, now_iso()),
        )


def print_result(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    print(result.get("message", "Done."))
    for warning in result.get("warnings", []):
        print(f"WARNING: {warning}")
    for key, value in result.get("summary", {}).items():
        print(f"{key}: {value}")


def create_run(con: sqlite3.Connection, kind: str, inputs: dict[str, Any], status: str = "planned") -> int:
    cur = con.execute(
        "INSERT INTO scan_run(kind, started_at, status, inputs_json, warnings_json) VALUES (?, ?, ?, ?, ?)",
        (kind, now_iso(), status, json.dumps(inputs, ensure_ascii=False), "[]"),
    )
    return int(cur.lastrowid)


def finish_run(con: sqlite3.Connection, run_id: int, status: str, warnings: list[str]) -> None:
    con.execute(
        "UPDATE scan_run SET completed_at = ?, status = ?, warnings_json = ? WHERE id = ?",
        (now_iso(), status, json.dumps(warnings, ensure_ascii=False), run_id),
    )


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def scan_audio_files(
    con: sqlite3.Connection,
    run_id: int,
    root: Path,
    extensions: set[str],
    include_hash: bool,
    limit: int,
) -> tuple[int, list[str]]:
    warnings: list[str] = []
    if not root.exists():
        return 0, [f"Library root not found: {root}"]
    if not root.is_dir():
        return 0, [f"Library root is not a directory: {root}"]

    count = 0
    for current_root, _, file_names in os.walk(root):
        for file_name in file_names:
            file_path = Path(current_root) / file_name
            extension = file_path.suffix.lower()
            if extension not in extensions:
                continue
            if limit and count >= limit:
                return count, warnings

            try:
                stat = file_path.stat()
                relative_path = str(file_path.relative_to(root))
                sha256 = hash_file(file_path) if include_hash else None
                con.execute(
                    """
                    INSERT INTO file_asset(
                        absolute_path, root_key, relative_path, file_name, extension,
                        length_bytes, created_utc, modified_utc, sha256,
                        first_seen_run_id, last_seen_run_id, missing_since_run_id, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    ON CONFLICT(absolute_path) DO UPDATE SET
                        root_key = excluded.root_key,
                        relative_path = excluded.relative_path,
                        file_name = excluded.file_name,
                        extension = excluded.extension,
                        length_bytes = excluded.length_bytes,
                        created_utc = excluded.created_utc,
                        modified_utc = excluded.modified_utc,
                        sha256 = excluded.sha256,
                        last_seen_run_id = excluded.last_seen_run_id,
                        missing_since_run_id = NULL,
                        updated_at = excluded.updated_at
                    """,
                    (
                        str(file_path.resolve()),
                        "libraryRoot",
                        relative_path,
                        file_path.name,
                        extension,
                        int(stat.st_size),
                        datetime.fromtimestamp(stat.st_ctime, timezone.utc).isoformat(),
                        datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                        sha256,
                        run_id,
                        run_id,
                        now_iso(),
                    ),
                )
                count += 1
            except OSError as exc:
                warnings.append(f"Skipped file {file_path}: {exc}")
    return count, warnings


def read_header_hex(path: Path, size: int = 32) -> tuple[str | None, str | None]:
    try:
        with path.open("rb") as handle:
            data = handle.read(size)
        return data.hex(" ").upper(), None
    except OSError as exc:
        return None, str(exc)


def test_sqlite_readable(path: Path) -> tuple[bool, str | None]:
    try:
        uri = f"file:{path.as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            con.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
        finally:
            con.close()
        return True, None
    except Exception as exc:  # noqa: BLE001 - store exact diagnostic for operator review.
        return False, str(exc)


def upsert_rekordbox_source(con: sqlite3.Connection, source_key: str, path: Path) -> dict[str, Any]:
    exists = path.exists()
    length = None
    modified = None
    header_hex = None
    read_error = None
    sqlite_readable = False

    if exists:
        try:
            stat = path.stat()
            length = int(stat.st_size)
            modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
        except OSError as exc:
            read_error = str(exc)
        header_hex, header_error = read_header_hex(path)
        if header_error:
            read_error = header_error
        if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
            sqlite_readable, sqlite_error = test_sqlite_readable(path)
            if sqlite_error:
                read_error = sqlite_error

    con.execute(
        """
        INSERT INTO rekordbox_source(
            source_key, path, exists_flag, length_bytes, modified_utc,
            header_hex, sqlite_readable, read_error, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_key) DO UPDATE SET
            path = excluded.path,
            exists_flag = excluded.exists_flag,
            length_bytes = excluded.length_bytes,
            modified_utc = excluded.modified_utc,
            header_hex = excluded.header_hex,
            sqlite_readable = excluded.sqlite_readable,
            read_error = excluded.read_error,
            updated_at = excluded.updated_at
        """,
        (
            source_key,
            str(path),
            1 if exists else 0,
            length,
            modified,
            header_hex,
            1 if sqlite_readable else 0,
            read_error,
            now_iso(),
        ),
    )
    return {
        "source_key": source_key,
        "exists": exists,
        "sqlite_readable": sqlite_readable,
        "read_error": read_error,
    }


def import_playlist_nodes(con: sqlite3.Connection, run_id: int, xml_path: Path) -> tuple[int, list[str]]:
    if not xml_path.exists():
        return 0, [f"Rekordbox playlist XML not found: {xml_path}"]

    warnings: list[str] = []
    count = 0
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as exc:
        return 0, [f"Could not parse Rekordbox playlist XML {xml_path}: {exc}"]

    for node in tree.findall(".//NODE"):
        node_id = node.attrib.get("Id")
        if not node_id:
            continue
        con.execute(
            """
            INSERT INTO rekordbox_playlist_node(
                node_id, parent_id, attribute, timestamp_raw, lib_type,
                check_type, source_path, last_seen_run_id, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                parent_id = excluded.parent_id,
                attribute = excluded.attribute,
                timestamp_raw = excluded.timestamp_raw,
                lib_type = excluded.lib_type,
                check_type = excluded.check_type,
                source_path = excluded.source_path,
                last_seen_run_id = excluded.last_seen_run_id,
                updated_at = excluded.updated_at
            """,
            (
                node_id,
                node.attrib.get("ParentId"),
                node.attrib.get("Attribute"),
                node.attrib.get("Timestamp"),
                node.attrib.get("Lib_Type"),
                node.attrib.get("CheckType"),
                str(xml_path),
                run_id,
                now_iso(),
            ),
        )
        count += 1
    return count, warnings


def command_init(args: argparse.Namespace) -> dict[str, Any]:
    db_path = Path(args.db).resolve()
    schema_path = Path(args.schema).resolve()
    if not args.apply:
        return {
            "message": "Dry-run: control database initialization planned.",
            "summary": {
                "database": str(db_path),
                "schema": str(schema_path),
                "would_write": True,
            },
            "warnings": [],
        }
    apply_schema(db_path, schema_path)
    return {
        "message": "Control database initialized.",
        "summary": {
            "database": str(db_path),
            "schema": str(schema_path),
            "schema_version": SCHEMA_VERSION,
        },
        "warnings": [],
    }


def command_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    config_path = Path(args.config).resolve()
    schema_path = Path(args.schema).resolve()
    db_path = Path(args.db).resolve()
    config = load_json(config_path)
    library_root = resolve_configured_path(args.root_path or config.get("libraryRoot"), repo_root)
    extensions = {str(ext).lower() for ext in config.get("audioExtensions", AUDIO_DEFAULTS)}

    rb_root = Path.home() / "AppData" / "Roaming" / "Pioneer" / "rekordbox"
    rb_sources = {
        "master_db": rb_root / "master.db",
        "master_db_wal": rb_root / "master.db-wal",
        "master_playlists_xml": rb_root / "masterPlaylists6.xml",
        "automix_playlist_xml": rb_root / "automixPlaylist6.xml",
    }

    inputs = {
        "config": str(config_path),
        "library_root": str(library_root) if library_root else None,
        "include_hash": bool(args.hash),
        "limit": int(args.limit),
        "rekordbox_root": str(rb_root),
    }

    if not args.apply:
        return {
            "message": "Dry-run: control snapshot planned.",
            "summary": inputs | {
                "database": str(db_path),
                "would_write": True,
            },
            "warnings": [] if library_root and library_root.exists() else [f"Library root not found: {library_root}"],
        }

    apply_schema(db_path, schema_path)
    warnings: list[str] = []
    with connect(db_path) as con:
        run_id = create_run(con, "snapshot", inputs)
        file_count = 0
        if library_root is not None:
            file_count, file_warnings = scan_audio_files(con, run_id, library_root, extensions, args.hash, args.limit)
            warnings.extend(file_warnings)

        source_results = [upsert_rekordbox_source(con, key, path) for key, path in rb_sources.items()]
        playlist_count, playlist_warnings = import_playlist_nodes(con, run_id, rb_sources["master_playlists_xml"])
        warnings.extend(playlist_warnings)
        status = "warning" if warnings else "ok"
        finish_run(con, run_id, status, warnings)

    return {
        "message": "Control snapshot written.",
        "summary": {
            "database": str(db_path),
            "run_id": run_id,
            "file_assets_seen": file_count,
            "playlist_nodes_seen": playlist_count,
            "rekordbox_sources_seen": len(source_results),
        },
        "warnings": warnings,
        "rekordbox_sources": source_results,
    }


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    db_path = Path(args.db).resolve()
    if not db_path.exists():
        return {
            "message": "Control database does not exist yet.",
            "summary": {"database": str(db_path)},
            "warnings": [],
        }

    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        tables = [
            "scan_run",
            "file_asset",
            "rekordbox_source",
            "rekordbox_playlist_node",
            "operation_plan",
            "operation_event",
            "duplicate_candidate",
        ]
        existing_tables = {
            row["name"]
            for row in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        counts = {
            table: con.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"]
            for table in tables
            if table in existing_tables
        }
        missing_tables = [table for table in tables if table not in existing_tables]
        latest = con.execute(
            "SELECT id, kind, status, completed_at, warnings_json FROM scan_run ORDER BY id DESC LIMIT 1"
        ).fetchone() if "scan_run" in existing_tables else None

    summary: dict[str, Any] = {"database": str(db_path), **counts}
    if latest:
        summary["latest_run"] = dict(latest)
    warnings = [f"Missing table in existing control DB: {table}" for table in missing_tables]
    return {"message": "Control database status.", "summary": summary, "warnings": warnings}


def command_plan_operation(args: argparse.Namespace) -> dict[str, Any]:
    db_path = Path(args.db).resolve()
    schema_path = Path(args.schema).resolve()
    payload = parse_json_arg(args.payload_json, "--payload-json")
    verifier = parse_json_arg(args.verifier_json, "--verifier-json")
    op_id = args.operation_id or f"op-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}"
    status = args.status or "draft"

    if status not in OPERATION_STATUSES:
        raise ValueError(f"--status must be one of: {', '.join(sorted(OPERATION_STATUSES))}")

    summary = {
        "database": str(db_path),
        "operation_id": op_id,
        "op_type": args.operation_type,
        "status": status,
        "target_kind": args.target_kind,
        "target_ref": args.target_ref,
        "file_action_status": args.file_action_status,
        "rekordbox_action_status": args.rekordbox_action_status,
        "would_write": True,
    }

    if not args.apply:
        return {
            "message": "Dry-run: operation plan staged in memory only.",
            "summary": summary,
            "payload": payload,
            "verifier": verifier,
            "warnings": [],
        }

    apply_schema(db_path, schema_path)
    timestamp = now_iso()
    with connect(db_path) as con:
        con.execute(
            """
            INSERT INTO operation_plan(
                id, op_type, status, target_kind, target_ref, payload_json,
                file_action_status, rekordbox_action_status, verifier_json,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                op_type = excluded.op_type,
                status = excluded.status,
                target_kind = excluded.target_kind,
                target_ref = excluded.target_ref,
                payload_json = excluded.payload_json,
                file_action_status = excluded.file_action_status,
                rekordbox_action_status = excluded.rekordbox_action_status,
                verifier_json = excluded.verifier_json,
                updated_at = excluded.updated_at
            """,
            (
                op_id,
                args.operation_type,
                status,
                args.target_kind,
                args.target_ref,
                json.dumps(payload, ensure_ascii=False),
                args.file_action_status,
                args.rekordbox_action_status,
                json.dumps(verifier, ensure_ascii=False),
                timestamp,
                timestamp,
            ),
        )
        con.execute(
            """
            INSERT INTO operation_event(operation_id, event_type, message, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                op_id,
                "plan_upserted",
                args.event_message or "Operation plan upserted.",
                json.dumps({"payload": payload, "verifier": verifier}, ensure_ascii=False),
                timestamp,
            ),
        )

    return {
        "message": "Operation plan written.",
        "summary": summary | {"would_write": False},
        "warnings": [],
    }


def command_list_operations(args: argparse.Namespace) -> dict[str, Any]:
    db_path = Path(args.db).resolve()
    if not db_path.exists():
        return {
            "message": "Control database does not exist yet.",
            "summary": {"database": str(db_path), "operation_count": 0},
            "operations": [],
            "warnings": [],
        }

    filters = []
    params: list[Any] = []
    if args.status:
        filters.append("status = ?")
        params.append(args.status)
    if args.operation_type:
        filters.append("op_type = ?")
        params.append(args.operation_type)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    limit = args.limit if args.limit and args.limit > 0 else 50

    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            f"""
            SELECT id, op_type, status, target_kind, target_ref,
                   file_action_status, rekordbox_action_status,
                   payload_json, verifier_json, created_at, updated_at
            FROM operation_plan
            {where}
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        operations = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
            item["verifier"] = json.loads(item.pop("verifier_json") or "{}")
            operations.append(item)

    return {
        "message": "Operation plans listed.",
        "summary": {"database": str(db_path), "operation_count": len(operations)},
        "operations": operations,
        "warnings": [],
    }


def build_parser() -> argparse.ArgumentParser:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Maintain the DJ Library control database.")
    parser.add_argument("mode", choices=["init", "snapshot", "status", "plan-operation", "list-operations"])
    parser.add_argument("--repo-root", default=str(repo_root))
    parser.add_argument("--config", default=str(repo_root / "config" / "dj-library.paths.json"))
    parser.add_argument("--schema", default=str(repo_root / "schemas" / "dj-control-db.schema.sql"))
    parser.add_argument("--db", default=str(repo_root / "runtime" / "dj-control" / "control.sqlite"))
    parser.add_argument("--root-path", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--hash", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--operation-id")
    parser.add_argument("--operation-type")
    parser.add_argument("--status")
    parser.add_argument("--target-kind")
    parser.add_argument("--target-ref")
    parser.add_argument("--payload-json", default="{}")
    parser.add_argument("--verifier-json", default="{}")
    parser.add_argument("--file-action-status", default="pending")
    parser.add_argument("--rekordbox-action-status", default="pending")
    parser.add_argument("--event-message")
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.mode == "init":
            result = command_init(args)
        elif args.mode == "snapshot":
            result = command_snapshot(args)
        elif args.mode == "status":
            result = command_status(args)
        elif args.mode == "plan-operation":
            if not args.operation_type or not args.target_kind or not args.target_ref:
                raise ValueError("--operation-type, --target-kind, and --target-ref are required for plan-operation")
            result = command_plan_operation(args)
        else:
            result = command_list_operations(args)
        print_result(result, args.json)
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI should return a clear operator error.
        if args.json:
            print(json.dumps({"error": str(exc)}, indent=2, ensure_ascii=False), file=sys.stderr)
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
