#!/usr/bin/env python3
"""Small pyrekordbox operations used by Invoke-RekordboxControlledWrite.ps1.

The PowerShell wrapper owns safety gates, backups, and cleanup. This helper only
performs pyrekordbox operations against the database path it is given.
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path
from typing import Any

from pyrekordbox import Rekordbox6Database
from pyrekordbox.db6 import DjmdContent


TEST_TITLE = "Codex Rekordbox Copy Test"


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def create_silent_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(44100)
        wav.writeframes(b"\x00\x00" * 44100)


def copy_smoke(master: Path, work_root: Path) -> dict[str, Any]:
    fake_path = work_root / "test-audio" / "Codex Rekordbox Copy Test - Silence.wav"
    renamed_path = work_root / "test-audio" / "Codex Rekordbox Copy Test - Silence Renamed.wav"
    create_silent_wav(fake_path)

    result: dict[str, Any] = {
        "mode": "copy-smoke",
        "master": str(master),
        "fake_audio": str(fake_path),
    }
    db = Rekordbox6Database(path=master)
    try:
        db.open()
        query_test = lambda: db.query(DjmdContent).filter(DjmdContent.Title == TEST_TITLE)
        before_total = db.query(DjmdContent).count()

        leftovers = list(query_test())
        for item in leftovers:
            db.delete(item)
        if leftovers:
            db.commit()

        before_clean = db.query(DjmdContent).count()
        content = db.add_content(str(fake_path), Title=TEST_TITLE)
        added_id = int(content.ID)
        db.commit()

        after_add = db.query(DjmdContent).count()
        found_after_add = db.query(DjmdContent).filter(DjmdContent.ID == added_id).count()

        fake_path.rename(renamed_path)
        content_for_relink = db.query(DjmdContent).filter(DjmdContent.ID == added_id).one()
        old_folder_path = content_for_relink.FolderPath
        relink_target = str(renamed_path).replace("\\", "/")
        # Fake smoke tracks have no ANLZ files, so this proves only the DB relink path.
        content_for_relink.FolderPath = relink_target
        if content_for_relink.OrgFolderPath == old_folder_path:
            content_for_relink.OrgFolderPath = relink_target
        content_for_relink.FileNameL = renamed_path.name
        db.commit()
        relinked = db.query(DjmdContent).filter(DjmdContent.ID == added_id).one()
        relinked_path = str(relinked.FolderPath)
        found_after_relink = db.query(DjmdContent).filter(
            DjmdContent.ID == added_id,
            DjmdContent.FolderPath == relink_target,
        ).count()

        content_for_delete = db.query(DjmdContent).filter(DjmdContent.ID == added_id).one()
        db.delete(content_for_delete)
        db.commit()

        after_delete = db.query(DjmdContent).count()
        found_after_delete = db.query(DjmdContent).filter(DjmdContent.ID == added_id).count()
        title_leftovers = query_test().count()

        result.update(
            {
                "opened": True,
                "count_before_total": before_total,
                "removed_leftovers_before_test": len(leftovers),
                "count_before_clean_test": before_clean,
                "added_id": added_id,
                "count_after_add": after_add,
                "found_after_add": found_after_add,
                "renamed_audio_path": str(renamed_path),
                "relink_method": "manual_orm_fields_for_copy_smoke",
                "relinked_path": relinked_path,
                "found_after_relink": found_after_relink,
                "count_after_delete": after_delete,
                "found_after_delete": found_after_delete,
                "title_leftovers_after_delete": title_leftovers,
                "success": found_after_add == 1
                and found_after_relink == 1
                and found_after_delete == 0
                and title_leftovers == 0
                and after_delete == before_clean,
            }
        )
    except Exception as exc:
        db.rollback()
        result.update({"opened": True, "success": False, "error": str(exc)})
        raise
    finally:
        db.close()
        fake_exists_before_cleanup = fake_path.exists()
        renamed_exists_before_cleanup = renamed_path.exists()
        fake_path.unlink(missing_ok=True)
        renamed_path.unlink(missing_ok=True)
        try:
            fake_path.parent.rmdir()
        except OSError:
            pass
        result["fake_audio_existed_before_cleanup"] = fake_exists_before_cleanup
        result["renamed_audio_existed_before_cleanup"] = renamed_exists_before_cleanup
        result["fake_audio_exists_after_cleanup"] = fake_path.exists()
        result["renamed_audio_exists_after_cleanup"] = renamed_path.exists()
        result["test_audio_dir_exists_after_cleanup"] = fake_path.parent.exists()

    return result


def add_content(master: Path, track_path: Path, title: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "mode": "add-content",
        "master": str(master),
        "track_path": str(track_path),
        "title": title,
    }
    db = Rekordbox6Database(path=master)
    try:
        db.open()
        before = db.query(DjmdContent).count()
        content = db.add_content(str(track_path), Title=title)
        added_id = int(content.ID)
        db.commit()
        after = db.query(DjmdContent).count()
        found = db.query(DjmdContent).filter(DjmdContent.ID == added_id).count()
        result.update(
            {
                "opened": True,
                "added_id": added_id,
                "count_before": before,
                "count_after": after,
                "found_after_add": found,
                "success": found == 1 and after == before + 1,
            }
        )
    except Exception as exc:
        db.rollback()
        result.update({"opened": True, "success": False, "error": str(exc)})
        raise
    finally:
        db.close()

    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run guarded pyrekordbox operations.")
    sub = parser.add_subparsers(dest="mode", required=True)

    smoke = sub.add_parser("copy-smoke")
    smoke.add_argument("--master", required=True)
    smoke.add_argument("--work-root", required=True)

    add = sub.add_parser("add-content")
    add.add_argument("--master", required=True)
    add.add_argument("--track-path", required=True)
    add.add_argument("--title", required=True)

    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.mode == "copy-smoke":
            result = copy_smoke(Path(args.master).resolve(), Path(args.work_root).resolve())
        else:
            result = add_content(
                Path(args.master).resolve(),
                Path(args.track_path).resolve(),
                args.title,
            )
        print_json(result)
        return 0 if result.get("success") else 1
    except Exception as exc:  # noqa: BLE001 - CLI reports exact operator error.
        print_json({"mode": args.mode, "success": False, "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
