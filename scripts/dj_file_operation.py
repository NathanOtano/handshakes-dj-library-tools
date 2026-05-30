#!/usr/bin/env python3
"""Apply Codex-planned file operations with explicit filesystem proof."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


AUDIO_DEFAULTS = [".flac", ".wav", ".aiff", ".aif", ".m4a", ".mp3", ".ogg", ".opus"]
SUPPORTED_FILE_OPS = {"rename", "move", "relink"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def print_result(result: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    print(result.get("message", "Done."))
    for warning in result.get("warnings", []):
        print(f"WARNING: {warning}")
    for key, value in result.get("summary", {}).items():
        print(f"{key}: {value}")


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def resolve_configured_path(value: str | None, repo_root: Path) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path.resolve(strict=False)
    return (repo_root / path).resolve(strict=False)


def norm_path(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False)))


def path_under(child: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath([norm_path(child), norm_path(parent)]) == norm_path(parent)
    except ValueError:
        return False


def configured_root_items(config: dict[str, Any], repo_root: Path) -> list[tuple[str, Path]]:
    roots: list[tuple[str, Path]] = []
    for key in ("musicRoot", "playlistRoot", "libraryRoot", "intakeRoot", "postProcessed_Library_RootRoot"):
        root = resolve_configured_path(config.get(key), repo_root)
        if root is not None and root not in [item[1] for item in roots]:
            roots.append((key, root))
    return roots


def path_in_allowed_roots(path: Path, roots: list[Path]) -> bool:
    return any(path_under(path, root) for root in roots)


def read_operation(con: sqlite3.Connection, operation_id: str) -> dict[str, Any]:
    con.row_factory = sqlite3.Row
    row = con.execute(
        """
        SELECT id, op_type, status, target_kind, target_ref, payload_json,
               file_action_status, rekordbox_action_status, verifier_json
        FROM operation_plan
        WHERE id = ?
        """,
        (operation_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Operation not found: {operation_id}")

    operation = dict(row)
    operation["payload"] = json.loads(operation.pop("payload_json") or "{}")
    operation["verifier"] = json.loads(operation.pop("verifier_json") or "{}")
    return operation


def get_destination(operation: dict[str, Any]) -> Path:
    payload = operation["payload"]
    destination = payload.get("newPath") or payload.get("destinationPath") or payload.get("new_path")
    if not destination:
        raise ValueError("Operation payload must include newPath, destinationPath, or new_path.")
    path = Path(str(destination))
    if not path.is_absolute():
        raise ValueError(f"Destination path must be absolute: {destination}")
    return path.resolve(strict=False)


def validate_plan(operation: dict[str, Any], config: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    if operation["op_type"] not in SUPPORTED_FILE_OPS:
        raise ValueError(f"Unsupported file operation type: {operation['op_type']}")
    if operation["target_kind"] != "file":
        raise ValueError(f"File executor only accepts target_kind=file, got {operation['target_kind']}")
    if operation["file_action_status"] not in {"pending", "blocked"}:
        raise ValueError(f"File action is not pending or blocked: {operation['file_action_status']}")

    source = Path(str(operation["target_ref"]))
    if not source.is_absolute():
        raise ValueError(f"Source path must be absolute: {operation['target_ref']}")
    source = source.resolve(strict=False)
    destination = get_destination(operation)
    root_items = configured_root_items(config, repo_root)
    allowed_roots = [root for _, root in root_items]
    extensions = {str(ext).lower() for ext in config.get("audioExtensions", AUDIO_DEFAULTS)}

    warnings: list[str] = []
    if source == destination:
        raise ValueError("Source and destination are the same path.")
    if source.suffix.lower() not in extensions:
        raise ValueError(f"Source extension is not allowed: {source.suffix}")
    if destination.suffix.lower() not in extensions:
        raise ValueError(f"Destination extension is not allowed: {destination.suffix}")
    if source.suffix.lower() != destination.suffix.lower():
        raise ValueError("File executor does not change audio extensions.")
    if not path_in_allowed_roots(source, allowed_roots):
        raise ValueError(f"Source is outside configured DJ roots: {source}")
    if not path_in_allowed_roots(destination, allowed_roots):
        raise ValueError(f"Destination is outside configured DJ roots: {destination}")
    if not source.exists():
        raise ValueError(f"Source file not found: {source}")
    if not source.is_file():
        raise ValueError(f"Source is not a file: {source}")
    if destination.exists():
        raise ValueError(f"Destination already exists: {destination}")

    destination_parent = destination.parent
    return {
        "source": source,
        "destination": destination,
        "destination_parent": destination_parent,
        "destination_parent_exists": destination_parent.exists(),
        "would_create_destination_parent": not destination_parent.exists(),
        "root_items": [(key, str(root)) for key, root in root_items],
        "allowed_roots": [str(root) for root in allowed_roots],
        "warnings": warnings,
    }


def insert_event(
    con: sqlite3.Connection,
    operation_id: str,
    event_type: str,
    message: str,
    payload: dict[str, Any],
) -> None:
    con.execute(
        """
        INSERT INTO operation_event(operation_id, event_type, message, payload_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (operation_id, event_type, message, json.dumps(payload, ensure_ascii=False), now_iso()),
    )


def mark_blocked(con: sqlite3.Connection, operation: dict[str, Any], message: str, payload: dict[str, Any]) -> None:
    verifier = operation["verifier"] | {"file_operation_error": payload}
    con.execute(
        """
        UPDATE operation_plan
        SET status = 'blocked',
            file_action_status = 'blocked',
            verifier_json = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (json.dumps(verifier, ensure_ascii=False), now_iso(), operation["id"]),
    )
    insert_event(con, operation["id"], "file_apply_blocked", message, payload)


def update_success(con: sqlite3.Connection, operation: dict[str, Any], proof: dict[str, Any]) -> None:
    verifier = operation["verifier"] | {"file_operation": proof}
    status = "applied" if operation["rekordbox_action_status"] in {"applied", "verified"} else "staged"
    con.execute(
        """
        UPDATE operation_plan
        SET status = ?,
            file_action_status = 'applied',
            verifier_json = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (status, json.dumps(verifier, ensure_ascii=False), now_iso(), operation["id"]),
    )
    insert_event(con, operation["id"], "file_applied", "File operation applied.", proof)


def update_file_asset(
    con: sqlite3.Connection,
    source: Path,
    destination: Path,
    sha256: str,
    root_items: list[tuple[str, str]],
) -> None:
    row = con.execute(
        "SELECT id, root_key FROM file_asset WHERE absolute_path = ?",
        (str(source),),
    ).fetchone()
    if row is None:
        return

    stat = destination.stat()
    root_key = row["root_key"]
    relative_path = destination.name
    matching_roots = [
        (key, Path(root))
        for key, root in root_items
        if path_under(destination, Path(root))
    ]
    if matching_roots:
        root_key, root_path = max(matching_roots, key=lambda item: len(str(item[1])))
        relative_path = os.path.relpath(str(destination), str(root_path))

    con.execute(
        """
        UPDATE file_asset
        SET absolute_path = ?,
            root_key = ?,
            relative_path = ?,
            file_name = ?,
            extension = ?,
            length_bytes = ?,
            modified_utc = ?,
            sha256 = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            str(destination),
            root_key,
            relative_path,
            destination.name,
            destination.suffix.lower(),
            int(stat.st_size),
            datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            sha256,
            now_iso(),
            row["id"],
        ),
    )


def apply_file_operation(db_path: Path, operation_id: str, config: dict[str, Any], repo_root: Path, apply: bool) -> dict[str, Any]:
    if not db_path.exists():
        raise ValueError(f"Control database not found: {db_path}")

    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        operation = read_operation(con, operation_id)
        try:
            plan = validate_plan(operation, config, repo_root)
        except Exception as exc:
            if apply:
                mark_blocked(
                    con,
                    operation,
                    "File operation validation failed.",
                    {"operation_id": operation_id, "error": str(exc), "validated_at": now_iso()},
                )
                con.commit()
            raise
        source: Path = plan["source"]
        destination: Path = plan["destination"]
        before_stat = source.stat()
        before_sha = hash_file(source)

        summary = {
            "database": str(db_path),
            "operation_id": operation_id,
            "op_type": operation["op_type"],
            "source": str(source),
            "destination": str(destination),
            "length_bytes": int(before_stat.st_size),
            "sha256_before": before_sha,
            "would_create_destination_parent": plan["would_create_destination_parent"],
            "would_write_files": True,
            "would_update_control_db": True,
        }

        if not apply:
            return {
                "message": "Dry-run: file operation validated only.",
                "summary": summary,
                "warnings": plan["warnings"],
            }

        insert_event(con, operation_id, "file_apply_started", "File operation started.", summary)
        con.commit()

        moved = False
        rollback: dict[str, Any] = {"attempted": False, "success": False}
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            moved = True
            after_sha = hash_file(destination)
            if after_sha != before_sha:
                raise ValueError("Destination hash does not match source hash after move.")

            proof = summary | {
                "would_write_files": False,
                "would_update_control_db": False,
                "sha256_after": after_sha,
                "source_exists_after": source.exists(),
                "destination_exists_after": destination.exists(),
                "applied_at": now_iso(),
            }
            update_file_asset(con, source, destination, after_sha, plan["root_items"])
            update_success(con, operation, proof)
            con.commit()
            return {
                "message": "File operation applied.",
                "summary": proof,
                "warnings": plan["warnings"],
            }
        except Exception as exc:  # noqa: BLE001 - rollback is more important than exception shape here.
            if moved and destination.exists() and not source.exists():
                rollback["attempted"] = True
                try:
                    source.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(destination), str(source))
                    rollback["success"] = source.exists() and not destination.exists()
                except Exception as rollback_exc:  # noqa: BLE001
                    rollback["error"] = str(rollback_exc)
            payload = summary | {"error": str(exc), "rollback": rollback}
            mark_blocked(con, operation, "File operation failed.", payload)
            con.commit()
            raise RuntimeError(json.dumps(payload, ensure_ascii=False)) from exc


def build_parser() -> argparse.ArgumentParser:
    repo_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Apply a planned DJ Library file operation.")
    parser.add_argument("--repo-root", default=str(repo_root))
    parser.add_argument("--config", default=str(repo_root / "config" / "dj-library.paths.json"))
    parser.add_argument("--db", default=str(repo_root / "runtime" / "dj-control" / "control.sqlite"))
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        repo_root = Path(args.repo_root).resolve()
        config = load_json(Path(args.config).resolve())
        result = apply_file_operation(
            db_path=Path(args.db).resolve(),
            operation_id=args.operation_id,
            config=config,
            repo_root=repo_root,
            apply=args.apply,
        )
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
