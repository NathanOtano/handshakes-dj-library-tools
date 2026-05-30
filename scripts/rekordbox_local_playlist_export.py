#!/usr/bin/env python3
"""Export Rekordbox local playlist memberships from a copied database."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import ntpath
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from pyrekordbox import Rekordbox6Database
from pyrekordbox.db6 import DjmdContent, DjmdPlaylist, DjmdSongPlaylist


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


def is_streaming_path(value: str | None) -> bool:
    if not value:
        return False
    return str(value).strip().casefold().startswith(STREAMING_PREFIXES)


def clean_windows_path(value: str | None) -> str:
    path = path_from_rekordbox(value)
    if not path:
        return ""
    return ntpath.normpath(path)


def playlist_rows(db: Rekordbox6Database, playlist_id: Any) -> list[DjmdSongPlaylist]:
    return list(
        db.query(DjmdSongPlaylist)
        .filter(DjmdSongPlaylist.PlaylistID == str(playlist_id))
        .order_by(DjmdSongPlaylist.TrackNo, DjmdSongPlaylist.ID)
    )


def export_playlists(master: Path, db_dir: Path) -> dict[str, Any]:
    db = Rekordbox6Database(path=master, db_dir=db_dir)
    try:
        contents = {str(row.ID): row for row in db.query(DjmdContent).all()}
        playlists = [
            row
            for row in db.query(DjmdPlaylist).all()
            if int(row.Attribute or 0) == 0 and not row.SmartList
        ]
        playlists.sort(key=lambda row: (row.Name or "").casefold())

        exported: list[dict[str, Any]] = []
        total_rows = 0
        total_local_rows = 0
        total_streaming_rows = 0
        missing_content_rows = 0

        for playlist in playlists:
            rows = playlist_rows(db, playlist.ID)
            entries: list[dict[str, Any]] = []
            streaming_rows = 0
            missing_rows = 0
            for row in rows:
                total_rows += 1
                content_id = str(row.ContentID)
                content = contents.get(content_id)
                if content is None:
                    missing_rows += 1
                    missing_content_rows += 1
                    continue
                raw_path = str(content.FolderPath or "")
                streaming = is_streaming_path(raw_path) or bool(content.ServiceID)
                if streaming:
                    streaming_rows += 1
                    total_streaming_rows += 1
                    continue
                clean_path = clean_windows_path(raw_path)
                entries.append(
                    {
                        "content_id": int(content.ID),
                        "title": content.Title,
                        "artist": getattr(getattr(content, "Artist", None), "Name", None),
                        "folder_path": clean_path,
                        "track_no": int(row.TrackNo or 0),
                    }
                )
                total_local_rows += 1

            exported.append(
                {
                    "name": playlist.Name,
                    "id": int(playlist.ID),
                    "rows": len(rows),
                    "local_rows": len(entries),
                    "streaming_rows_ignored": streaming_rows,
                    "missing_content_rows": missing_rows,
                    "entries": entries,
                }
            )

        return {
            "success": True,
            "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "master": str(master),
            "db_dir": str(db_dir),
            "playlist_count": len(exported),
            "total_rows": total_rows,
            "total_local_rows": total_local_rows,
            "total_streaming_rows_ignored": total_streaming_rows,
            "missing_content_rows": missing_content_rows,
            "playlists": exported,
        }
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", required=True)
    parser.add_argument("--db-dir", required=True)
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = export_playlists(Path(args.master).resolve(), Path(args.db_dir).resolve())
        print_json(payload)
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI reports operator-facing failure.
        print_json({"success": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
