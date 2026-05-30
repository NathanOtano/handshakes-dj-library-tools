#!/usr/bin/env python3
"""Synchronize Rekordbox smart playlists into normal playlists sorted by BPM."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _disable_rekordbox_running_guard() -> None:
    """Allow commits against copied databases while Rekordbox is open."""
    import pyrekordbox.db6.database as database

    database.get_rekordbox_pid = lambda: None


from pyrekordbox import Rekordbox6Database  # noqa: E402
from pyrekordbox.db6 import DjmdContent, DjmdPlaylist, DjmdSongPlaylist, SmartList  # noqa: E402


@dataclass(frozen=True)
class PlaylistPair:
    source: DjmdPlaylist
    target: DjmdPlaylist


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def as_int(value: Any) -> int:
    return int(value)


def as_str(value: Any) -> str:
    return str(value)


def bpm_sort_key(content: DjmdContent) -> tuple[int, int, str, int]:
    bpm = int(content.BPM or 0)
    missing_bpm = 1 if bpm <= 0 else 0
    return (missing_bpm, bpm, (content.Title or "").casefold(), as_int(content.ID))


def playlist_rows(db: Rekordbox6Database, playlist_id: Any) -> list[DjmdSongPlaylist]:
    return list(
        db.query(DjmdSongPlaylist)
        .filter(DjmdSongPlaylist.PlaylistID == as_str(playlist_id))
        .order_by(DjmdSongPlaylist.TrackNo, DjmdSongPlaylist.ID)
    )


def find_pairs(
    db: Rekordbox6Database,
    source_suffix: str,
    playlist_names: list[str],
) -> list[PlaylistPair]:
    playlists = list(db.query(DjmdPlaylist))
    normal_by_name: dict[str, DjmdPlaylist] = {
        pl.Name: pl for pl in playlists if int(pl.Attribute or 0) == 0 and not pl.SmartList
    }
    wanted = {name.casefold() for name in playlist_names}

    pairs: list[PlaylistPair] = []
    for source in playlists:
        if not source.SmartList:
            continue
        if int(source.Attribute or 0) != 4:
            continue
        if not source.Name.endswith(source_suffix):
            continue

        target_name = source.Name[: -len(source_suffix)]
        if wanted and target_name.casefold() not in wanted and source.Name.casefold() not in wanted:
            continue

        target = normal_by_name.get(target_name)
        if target is not None:
            pairs.append(PlaylistPair(source=source, target=target))

    pairs.sort(key=lambda pair: pair.target.Name.casefold())
    return pairs


def smart_contents(db: Rekordbox6Database, source: DjmdPlaylist) -> list[DjmdContent]:
    smart = SmartList()
    smart.parse(source.SmartList)
    return list(db.query(DjmdContent).filter(smart.filter_clause()).all())


def contents_by_id(db: Rekordbox6Database, content_ids: set[str]) -> dict[str, DjmdContent]:
    if not content_ids:
        return {}
    rows = db.query(DjmdContent).filter(DjmdContent.ID.in_(sorted(content_ids))).all()
    return {as_str(row.ID): row for row in rows}


def build_pair_plan(
    db: Rekordbox6Database,
    pair: PlaylistPair,
    remove_stale: bool,
) -> dict[str, Any]:
    smart_rows = smart_contents(db, pair.source)
    smart_ids = {as_str(content.ID) for content in smart_rows}
    target_rows = playlist_rows(db, pair.target.ID)
    target_ids = [as_str(row.ContentID) for row in target_rows]
    target_id_set = set(target_ids)

    desired_ids = smart_ids if remove_stale else smart_ids | target_id_set
    content_lookup = contents_by_id(db, desired_ids)
    desired_ids = {content_id for content_id in desired_ids if content_id in content_lookup}
    desired_order = [
        as_str(content.ID) for content in sorted(content_lookup.values(), key=bpm_sort_key)
    ]

    duplicate_target_memberships = max(0, len(target_ids) - len(target_id_set))
    new_ids = sorted(smart_ids - target_id_set, key=lambda cid: bpm_sort_key(content_lookup[cid]))
    stale_ids = sorted(target_id_set - smart_ids)
    target_unique_ids = [cid for index, cid in enumerate(target_ids) if cid not in target_ids[:index]]

    sample_new = []
    for content_id in new_ids[:8]:
        content = content_lookup[content_id]
        sample_new.append(
            {
                "content_id": as_int(content.ID),
                "title": content.Title,
                "bpm_raw": int(content.BPM or 0),
                "bpm": round(int(content.BPM or 0) / 100, 2),
                "path": content.FolderPath,
            }
        )

    return {
        "sourceSmartPlaylist": pair.source.Name,
        "sourceSmartPlaylistId": as_int(pair.source.ID),
        "targetPlaylist": pair.target.Name,
        "targetPlaylistId": as_int(pair.target.ID),
        "smartCount": len(smart_ids),
        "targetCountBefore": len(target_rows),
        "targetUniqueCountBefore": len(target_id_set),
        "desiredCount": len(desired_order),
        "newToAdd": len(new_ids),
        "staleInTarget": len(stale_ids),
        "staleWillBeRemoved": bool(remove_stale),
        "duplicateTargetMemberships": duplicate_target_memberships,
        "orderAlreadyByBpm": target_unique_ids == desired_order and duplicate_target_memberships == 0,
        "sampleNew": sample_new,
        "_desiredOrder": desired_order,
        "_smartIds": smart_ids,
        "_targetIdsBefore": target_ids,
    }


def apply_pair_plan(
    db: Rekordbox6Database,
    pair: PlaylistPair,
    plan: dict[str, Any],
) -> dict[str, Any]:
    desired_order: list[str] = list(plan["_desiredOrder"])
    existing_rows = playlist_rows(db, pair.target.ID)
    rows_by_content: dict[str, DjmdSongPlaylist] = {}
    duplicate_rows: list[DjmdSongPlaylist] = []

    for row in existing_rows:
        content_id = as_str(row.ContentID)
        if content_id in rows_by_content:
            duplicate_rows.append(row)
        else:
            rows_by_content[content_id] = row

    desired_set = set(desired_order)
    stale_rows = [row for cid, row in rows_by_content.items() if cid not in desired_set]
    for row in duplicate_rows + stale_rows:
        db.delete(row)

    added_rows: list[DjmdSongPlaylist] = []
    for content_id in desired_order:
        if content_id not in rows_by_content:
            added_rows.append(db.add_to_playlist(pair.target, content_id))

    all_rows = playlist_rows(db, pair.target.ID)
    rows_by_content = {}
    for row in all_rows:
        content_id = as_str(row.ContentID)
        if content_id not in rows_by_content:
            rows_by_content[content_id] = row

    now = dt.datetime.now()
    for track_no, content_id in enumerate(desired_order, start=1):
        row = rows_by_content[content_id]
        row.TrackNo = track_no
        row.updated_at = now

    pair.target.updated_at = now

    return {
        "targetPlaylist": pair.target.Name,
        "targetPlaylistId": as_int(pair.target.ID),
        "added": len(added_rows),
        "removedStale": len(stale_rows),
        "removedDuplicateMemberships": len(duplicate_rows),
        "trackNumbersWritten": len(desired_order),
    }


def verify_pair(
    db: Rekordbox6Database,
    pair: PlaylistPair,
    remove_stale: bool,
) -> dict[str, Any]:
    plan = build_pair_plan(db, pair, remove_stale=remove_stale)
    target_ids = [as_str(row.ContentID) for row in playlist_rows(db, pair.target.ID)]
    desired_order: list[str] = list(plan["_desiredOrder"])
    smart_ids: set[str] = set(plan["_smartIds"])
    target_id_set = set(target_ids)

    return {
        "targetPlaylist": pair.target.Name,
        "targetPlaylistId": as_int(pair.target.ID),
        "containsAllSmartTracks": smart_ids.issubset(target_id_set),
        "isSortedByBpm": target_ids == desired_order,
        "targetCountAfter": len(target_ids),
        "smartCountAfter": len(smart_ids),
        "remainingNewToAdd": len(smart_ids - target_id_set),
        "duplicateTargetMembershipsAfter": len(target_ids) - len(target_id_set),
    }


def strip_private_fields(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if not key.startswith("_")}


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.allow_rekordbox_running_commit:
        _disable_rekordbox_running_guard()

    master = Path(args.master).resolve()
    db_dir = Path(args.db_dir).resolve() if args.db_dir else master.parent
    db = Rekordbox6Database(path=master, db_dir=db_dir)
    try:
        pairs = find_pairs(db, args.source_suffix, args.playlist or [])
        plans = [build_pair_plan(db, pair, remove_stale=args.remove_stale) for pair in pairs]

        result: dict[str, Any] = {
            "mode": "apply" if args.apply else "plan",
            "master": str(master),
            "dbDir": str(db_dir),
            "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            "sourceSuffix": args.source_suffix,
            "removeStale": bool(args.remove_stale),
            "requestedPlaylists": args.playlist or [],
            "pairsFound": len(pairs),
            "totals": {
                "newToAdd": sum(plan["newToAdd"] for plan in plans),
                "targetRowsBefore": sum(plan["targetCountBefore"] for plan in plans),
                "desiredRows": sum(plan["desiredCount"] for plan in plans),
                "staleInTarget": sum(plan["staleInTarget"] for plan in plans),
                "duplicateTargetMemberships": sum(
                    plan["duplicateTargetMemberships"] for plan in plans
                ),
                "notSortedByBpm": sum(0 if plan["orderAlreadyByBpm"] else 1 for plan in plans),
            },
            "plans": [strip_private_fields(plan) for plan in plans],
        }

        if args.apply:
            applied = [apply_pair_plan(db, pair, plan) for pair, plan in zip(pairs, plans)]
            db.commit()
            verification = [verify_pair(db, pair, remove_stale=args.remove_stale) for pair in pairs]
            result["applied"] = applied
            result["verification"] = verification
            result["success"] = all(
                item["containsAllSmartTracks"]
                and item["isSortedByBpm"]
                and item["remainingNewToAdd"] == 0
                and item["duplicateTargetMembershipsAfter"] == 0
                for item in verification
            )
        else:
            result["success"] = True

        return result
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Copy smart playlist memberships into normal playlists sorted by BPM."
    )
    parser.add_argument("--master", required=True)
    parser.add_argument("--db-dir", default="")
    parser.add_argument("--source-suffix", default="_")
    parser.add_argument("--playlist", action="append", default=[])
    parser.add_argument("--remove-stale", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-rekordbox-running-commit", action="store_true")
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
        print_json(result)
        return 0 if result.get("success") else 1
    except Exception as exc:  # noqa: BLE001 - CLI reports operator-facing failure.
        print_json({"success": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
