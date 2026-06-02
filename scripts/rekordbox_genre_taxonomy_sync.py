#!/usr/bin/env python3
"""Materialize the local DJ genre taxonomy as Rekordbox playlists."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any


def _disable_rekordbox_running_guard() -> None:
    """Allow commits against copied databases while Rekordbox is open."""
    import pyrekordbox.db6.database as database

    database.get_rekordbox_pid = lambda: None


from pyrekordbox import Rekordbox6Database  # noqa: E402
from pyrekordbox.db6 import DjmdContent, DjmdPlaylist, DjmdSongPlaylist  # noqa: E402

from rekordbox_genre_taxonomy_audit import (  # noqa: E402
    bpm_sort_key,
    build_genre_by_id,
    classify_terms,
    content_genre_terms,
    is_streaming_path,
    make_taxonomy_index,
    normalize_key,
    read_json,
)

logging.getLogger("pyrekordbox").setLevel(logging.CRITICAL + 1)


def as_str(value: Any) -> str:
    return str(value) if value is not None else ""


def as_int(value: Any) -> int:
    return int(value or 0)


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def playlist_rows(db: Rekordbox6Database, playlist_id: Any) -> list[DjmdSongPlaylist]:
    return list(
        db.query(DjmdSongPlaylist)
        .filter(DjmdSongPlaylist.PlaylistID == as_str(playlist_id))
        .order_by(DjmdSongPlaylist.TrackNo, DjmdSongPlaylist.ID)
    )


def playlist_path_parts(db: Rekordbox6Database, playlist: DjmdPlaylist, by_id: dict[str, DjmdPlaylist]) -> tuple[str, ...]:
    parts = [as_str(playlist.Name)]
    parent_id = as_str(playlist.ParentID)
    seen: set[str] = set()
    while parent_id and parent_id != "root" and parent_id not in seen:
        seen.add(parent_id)
        parent = by_id.get(parent_id)
        if parent is None:
            break
        parts.insert(0, as_str(parent.Name))
        parent_id = as_str(parent.ParentID)
    return tuple(parts)


def playlist_indexes(db: Rekordbox6Database) -> tuple[dict[tuple[str, ...], DjmdPlaylist], dict[str, DjmdPlaylist]]:
    rows = list(db.query(DjmdPlaylist).all())
    by_id = {as_str(row.ID): row for row in rows}
    by_path = {playlist_path_parts(db, row, by_id): row for row in rows}
    return by_path, by_id


def max_seq_for_parent(db: Rekordbox6Database, parent_id: str) -> int:
    rows = db.query(DjmdPlaylist).filter(DjmdPlaylist.ParentID == parent_id).all()
    return max([as_int(row.Seq) for row in rows], default=0)


def create_playlist_node(db: Rekordbox6Database, name: str, parent_id: str, attribute: int) -> DjmdPlaylist:
    seq = max_seq_for_parent(db, parent_id) + 1
    if attribute == 1:
        return db.create_playlist_folder(name, parent=parent_id, seq=seq, image_path="")
    return db.create_playlist(name, parent=parent_id, seq=seq, image_path="")


def new_numeric_playlist_id(existing_ids: set[str]) -> str:
    while True:
        candidate = str(uuid.uuid4().int >> 96)
        if candidate not in existing_ids:
            existing_ids.add(candidate)
            return candidate


def managed_path_prefixes(desired: dict[tuple[str, ...], list[DjmdContent]]) -> set[tuple[str, ...]]:
    prefixes: set[tuple[str, ...]] = set()
    for path in desired:
        for index in range(1, len(path) + 1):
            prefixes.add(path[:index])
    return prefixes


def repair_managed_playlist_ids(
    db: Rekordbox6Database,
    desired: dict[tuple[str, ...], list[DjmdContent]],
    *,
    apply: bool,
) -> dict[str, Any]:
    prefixes = managed_path_prefixes(desired)
    by_path, by_id = playlist_indexes(db)
    targets: list[DjmdPlaylist] = []
    for path in sorted(prefixes, key=lambda item: (len(item), " / ".join(item).casefold())):
        playlist = by_path.get(path)
        if playlist is None:
            continue
        playlist_id = as_str(playlist.ID)
        if playlist_id and not playlist_id.isdigit():
            targets.append(playlist)

    id_map: dict[str, str] = {}
    existing_ids = set(by_id)
    for playlist in targets:
        old_id = as_str(playlist.ID)
        id_map[old_id] = new_numeric_playlist_id(existing_ids)
    items = [
        {
            "path": " / ".join(playlist_path_parts(db, playlist, by_id)),
            "oldId": as_str(playlist.ID),
            "newId": id_map[as_str(playlist.ID)],
        }
        for playlist in targets
    ]

    parent_updates = 0
    membership_updates = 0
    if apply and id_map:
        for playlist in db.query(DjmdPlaylist).all():
            old_parent_id = as_str(playlist.ParentID)
            if old_parent_id in id_map:
                playlist.ParentID = id_map[old_parent_id]
                parent_updates += 1

        for row in db.query(DjmdSongPlaylist).all():
            old_playlist_id = as_str(row.PlaylistID)
            if old_playlist_id in id_map:
                row.PlaylistID = id_map[old_playlist_id]
                membership_updates += 1

        now = dt.datetime.now()
        for playlist in targets:
            playlist.ID = id_map[as_str(playlist.ID)]
            playlist.updated_at = now

    return {
        "enabled": True,
        "candidateCount": len(targets),
        "applied": bool(apply and id_map),
        "parentReferencesUpdated": parent_updates,
        "membershipRowsUpdated": membership_updates,
        "items": items,
    }


def sync_managed_playlist_xml(
    db: Rekordbox6Database,
    desired: dict[tuple[str, ...], list[DjmdContent]],
    *,
    apply: bool,
) -> dict[str, Any]:
    if db.playlist_xml is None:
        return {
            "enabled": True,
            "available": False,
            "candidateCount": 0,
            "applied": False,
            "items": [],
        }

    prefixes = managed_path_prefixes(desired)
    by_path, by_id = playlist_indexes(db)
    items: list[dict[str, Any]] = []
    now = dt.datetime.now()
    for path in sorted(prefixes, key=lambda item: (len(item), " / ".join(item).casefold())):
        playlist = by_path.get(path)
        if playlist is None:
            continue
        playlist_id = as_str(playlist.ID)
        if not playlist_id or not playlist_id.isdigit():
            continue
        if db.playlist_xml.get(playlist_id) is not None:
            continue
        parent_id = as_str(playlist.ParentID) or "root"
        items.append(
            {
                "path": " / ".join(playlist_path_parts(db, playlist, by_id)),
                "playlistId": playlist_id,
                "parentId": parent_id,
                "attribute": as_int(playlist.Attribute),
            }
        )
        if apply:
            db.playlist_xml.add(
                playlist_id,
                parent_id,
                as_int(playlist.Attribute),
                getattr(playlist, "updated_at", None) or now,
                lib_type=0,
                check_type=0,
            )

    return {
        "enabled": True,
        "available": True,
        "candidateCount": len(items),
        "applied": bool(apply and items),
        "items": items,
    }


def get_or_create_path(
    db: Rekordbox6Database,
    path: tuple[str, ...],
    *,
    apply: bool,
    created: list[dict[str, Any]],
) -> DjmdPlaylist | None:
    by_path, _ = playlist_indexes(db)
    parent_id = "root"
    current_path: list[str] = []

    for index, part in enumerate(path):
        current_path.append(part)
        target_path = tuple(current_path)
        is_leaf = index == len(path) - 1
        expected_attribute = 0 if is_leaf else 1
        existing = by_path.get(target_path)
        if existing is not None:
            existing_attribute = as_int(existing.Attribute)
            if existing_attribute != expected_attribute:
                raise ValueError(
                    f"Playlist path collision for {' / '.join(target_path)}: "
                    f"expected attribute {expected_attribute}, found {existing_attribute}"
                )
            parent_id = as_str(existing.ID)
            continue

        if not apply:
            return None

        created_node = create_playlist_node(db, part, parent_id, expected_attribute)
        created.append(
            {
                "path": " / ".join(target_path),
                "id": as_str(created_node.ID),
                "attribute": expected_attribute,
            }
        )
        by_path[target_path] = created_node
        parent_id = as_str(created_node.ID)

    return by_path.get(path)


def build_desired_playlists(db: Rekordbox6Database, config: dict[str, Any]) -> dict[tuple[str, ...], list[DjmdContent]]:
    index = make_taxonomy_index(config)
    alias_to_matches = index["aliasToMatches"]
    roots = config["playlistRoots"]
    subgenre_min = int(config["defaults"]["subgenreMinTracks"])
    genre_by_id = build_genre_by_id(db)
    contents = [
        row
        for row in db.query(DjmdContent).all()
        if not is_streaming_path(as_str(getattr(row, "FolderPath", "") or ""))
    ]

    sub_bucket_parents = {
        as_str(item["parent"]) for item in config.get("djSubBuckets", [])
    }
    subgenre_parent_by_name = {
        as_str(item["name"]): as_str(item["parent"]) for item in config.get("subgenres", [])
    }

    members: dict[tuple[str, ...], dict[str, DjmdContent]] = defaultdict(dict)
    suppressed_subgenres: dict[tuple[str, ...], int] = {}

    for content in contents:
        terms, _ = content_genre_terms(content, genre_by_id)
        classification = classify_terms(terms, alias_to_matches)
        content_id = as_str(content.ID)

        for bucket in classification["djBuckets"]:
            path = (
                roots["djBuckets"],
                bucket,
                "Tous",
            ) if bucket in sub_bucket_parents else (roots["djBuckets"], bucket)
            members[path][content_id] = content

        for parent, sub_bucket in classification["djSubBuckets"]:
            members[(roots["djBuckets"], parent, sub_bucket)][content_id] = content

        for strict in classification["strictGenres"]:
            members[(roots["musicologicalGenres"], strict)][content_id] = content

        for subgenre in classification["subgenres"]:
            parent = subgenre_parent_by_name.get(subgenre, "")
            path = (roots["subgenres"], parent, subgenre) if parent else (roots["subgenres"], subgenre)
            members[path][content_id] = content

    result: dict[tuple[str, ...], list[DjmdContent]] = {}
    for path, content_map in members.items():
        if len(path) >= 3 and path[0] == roots["subgenres"] and len(content_map) < subgenre_min:
            suppressed_subgenres[path] = len(content_map)
            continue
        result[path] = sorted(content_map.values(), key=bpm_sort_key)

    return result


def build_plan(db: Rekordbox6Database, desired: dict[tuple[str, ...], list[DjmdContent]], *, remove_stale: bool) -> list[dict[str, Any]]:
    by_path, _ = playlist_indexes(db)
    plans: list[dict[str, Any]] = []

    for path, contents in sorted(desired.items(), key=lambda item: " / ".join(item[0]).casefold()):
        playlist = by_path.get(path)
        current_rows = playlist_rows(db, playlist.ID) if playlist is not None and as_int(playlist.Attribute) == 0 else []
        current_ids = [as_str(row.ContentID) for row in current_rows]
        current_set = set(current_ids)
        desired_ids = [as_str(content.ID) for content in contents]
        desired_set = set(desired_ids)
        final_set = desired_set if remove_stale else desired_set | current_set
        final_contents = [content for content in contents if as_str(content.ID) in final_set]
        final_ids = [as_str(content.ID) for content in final_contents]
        duplicate_current = len(current_ids) - len(current_set)
        stale = sorted(current_set - desired_set)

        plans.append(
            {
                "pathParts": list(path),
                "path": " / ".join(path),
                "playlistExists": playlist is not None,
                "playlistId": as_str(playlist.ID) if playlist is not None else "",
                "desiredCount": len(final_ids),
                "currentCount": len(current_ids),
                "newToAdd": len([content_id for content_id in desired_ids if content_id not in current_set]),
                "staleInTarget": len(stale),
                "staleWillBeRemoved": bool(remove_stale),
                "duplicateTargetMemberships": duplicate_current,
                "orderAlreadyByBpm": current_ids == final_ids and duplicate_current == 0,
                "_desiredIds": final_ids,
            }
        )
    return plans


def apply_plan(db: Rekordbox6Database, plan: list[dict[str, Any]], *, remove_stale: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    created: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []

    for item in plan:
        path = tuple(item["pathParts"])
        playlist = get_or_create_path(db, path, apply=True, created=created)
        if playlist is None:
            raise ValueError(f"Unable to create playlist path: {item['path']}")

        desired_ids: list[str] = list(item["_desiredIds"])
        desired_set = set(desired_ids)
        existing_rows = playlist_rows(db, playlist.ID)
        rows_by_content: dict[str, DjmdSongPlaylist] = {}
        duplicate_rows: list[DjmdSongPlaylist] = []
        for row in existing_rows:
            content_id = as_str(row.ContentID)
            if content_id in rows_by_content:
                duplicate_rows.append(row)
            else:
                rows_by_content[content_id] = row

        stale_rows = [
            row for content_id, row in rows_by_content.items() if remove_stale and content_id not in desired_set
        ]
        for row in duplicate_rows + stale_rows:
            db.delete(row)

        added = 0
        for content_id in desired_ids:
            if content_id not in rows_by_content:
                db.add_to_playlist(playlist, content_id)
                added += 1

        all_rows = playlist_rows(db, playlist.ID)
        rows_by_content = {}
        for row in all_rows:
            content_id = as_str(row.ContentID)
            if content_id not in rows_by_content:
                rows_by_content[content_id] = row

        now = dt.datetime.now()
        for track_no, content_id in enumerate(desired_ids, start=1):
            row = rows_by_content[content_id]
            row.TrackNo = track_no
            row.updated_at = now
        playlist.updated_at = now

        applied.append(
            {
                "path": item["path"],
                "playlistId": as_str(playlist.ID),
                "added": added,
                "removedStale": len(stale_rows),
                "removedDuplicateMemberships": len(duplicate_rows),
                "trackNumbersWritten": len(desired_ids),
            }
        )

    return applied, created


def verify_plan(db: Rekordbox6Database, plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_path, _ = playlist_indexes(db)
    result: list[dict[str, Any]] = []
    for item in plan:
        path = tuple(item["pathParts"])
        playlist = by_path.get(path)
        desired_ids: list[str] = list(item["_desiredIds"])
        current_ids = [as_str(row.ContentID) for row in playlist_rows(db, playlist.ID)] if playlist is not None else []
        result.append(
            {
                "path": item["path"],
                "playlistExists": playlist is not None,
                "isSortedByBpm": current_ids == desired_ids,
                "targetCountAfter": len(current_ids),
                "desiredCount": len(desired_ids),
                "remainingMissing": len(set(desired_ids) - set(current_ids)),
                "extraRows": len(set(current_ids) - set(desired_ids)),
                "duplicateTargetMembershipsAfter": len(current_ids) - len(set(current_ids)),
            }
        )
    return result


def strip_private(plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in item.items() if not key.startswith("_")} for item in plan]


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.allow_rekordbox_running_commit:
        _disable_rekordbox_running_guard()

    master = Path(args.master).resolve()
    db_dir = Path(args.db_dir).resolve() if args.db_dir else master.parent
    config = read_json(Path(args.taxonomy).resolve())
    db = Rekordbox6Database(path=master, db_dir=db_dir)
    try:
        desired = build_desired_playlists(db, config)
        repair_result: dict[str, Any] = {"enabled": False}
        if args.repair_managed_playlist_ids:
            repair_result = repair_managed_playlist_ids(db, desired, apply=args.apply)
        xml_sync_result: dict[str, Any] = {"enabled": False}
        if args.sync_managed_playlist_xml:
            xml_sync_result = sync_managed_playlist_xml(db, desired, apply=args.apply)
        plan = build_plan(db, desired, remove_stale=not args.keep_stale)
        result: dict[str, Any] = {
            "mode": "apply" if args.apply else "plan",
            "master": str(master),
            "dbDir": str(db_dir),
            "taxonomy": str(Path(args.taxonomy).resolve()),
            "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            "keepStale": bool(args.keep_stale),
            "playlistCount": len(plan),
            "totals": {
                "desiredRows": sum(item["desiredCount"] for item in plan),
                "newToAdd": sum(item["newToAdd"] for item in plan),
                "staleInTarget": sum(item["staleInTarget"] for item in plan),
                "duplicateTargetMemberships": sum(item["duplicateTargetMemberships"] for item in plan),
                "notSortedByBpm": sum(0 if item["orderAlreadyByBpm"] else 1 for item in plan),
                "missingPlaylists": sum(0 if item["playlistExists"] else 1 for item in plan),
            },
            "playlistIdRepair": repair_result,
            "playlistXmlSync": xml_sync_result,
        }
        if args.include_plans:
            result["plans"] = strip_private(plan)

        if args.apply:
            applied, created = apply_plan(db, plan, remove_stale=not args.keep_stale)
            db.commit()
            verification = verify_plan(db, plan)
            result["created"] = created
            result["applied"] = applied
            result["verification"] = verification
            result["success"] = all(
                item["playlistExists"]
                and item["isSortedByBpm"]
                and item["remainingMissing"] == 0
                and item["duplicateTargetMembershipsAfter"] == 0
                and (args.keep_stale or item["extraRows"] == 0)
                for item in verification
            )
        else:
            result["success"] = True

        return result
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Materialize DJ genre taxonomy playlists.")
    parser.add_argument("--master", required=True)
    parser.add_argument("--db-dir", default="")
    parser.add_argument("--taxonomy", default="config/dj-genre-taxonomy.json")
    parser.add_argument("--keep-stale", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--repair-managed-playlist-ids", action="store_true")
    parser.add_argument("--sync-managed-playlist-xml", action="store_true")
    parser.add_argument("--include-plans", action="store_true")
    parser.add_argument("--allow-rekordbox-running-commit", action="store_true")
    return parser


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        result = run(args)
        print_json(result)
        return 0 if result.get("success") else 1
    except Exception as exc:
        print_json({"success": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
