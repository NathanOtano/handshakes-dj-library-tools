#!/usr/bin/env python3
"""Organize Rekordbox label playlists into a single Labels folder."""

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

# Try to import pyrekordbox
try:
    from pyrekordbox import Rekordbox6Database
    from pyrekordbox.db6 import DjmdPlaylist
except ImportError:
    print("Error: pyrekordbox not found. Please run this script in the pyrekordbox-venv.", file=sys.stderr)
    sys.exit(1)

def _disable_rekordbox_running_guard() -> None:
    """Allow commits against copied databases while Rekordbox is open."""
    import pyrekordbox.db6.database as database
    database.get_rekordbox_pid = lambda: None

def get_or_create_folder(db: Rekordbox6Database, folder_name: str) -> DjmdPlaylist:
    """Get an existing folder by name or create a new one at the root."""
    # Attribute 1 is a folder
    folder = db.query(DjmdPlaylist).filter(
        DjmdPlaylist.Name == folder_name,
        DjmdPlaylist.Attribute == 1,
        DjmdPlaylist.ParentID == "root"
    ).first()
    
    if not folder:
        # We need to find the next Seq in root
        root_items = db.query(DjmdPlaylist).filter(DjmdPlaylist.ParentID == "").all()
        max_seq = max([int(item.Seq or 0) for item in root_items], default=0)
        
        import uuid
        now = dt.datetime.now()
        # Create a new playlist node
        folder = DjmdPlaylist(
            ID=uuid.uuid4().hex,
            Seq=max_seq + 1,
            Name=folder_name,
            ImagePath="",
            Attribute=1, # 1 for folder
            ParentID="root",
            SmartList="",
            created_at=now,
            updated_at=now
        )
        db.add(folder)
        
    return folder

def run(args: argparse.Namespace) -> dict:
    if args.allow_rekordbox_running_commit:
        _disable_rekordbox_running_guard()

    master = Path(args.master).resolve()
    db_dir = Path(args.db_dir).resolve() if args.db_dir else master.parent
    db = Rekordbox6Database(path=str(master), db_dir=str(db_dir))
    
    label_prefixes = [
        "Sous-genre - ", 
        "Fonction - ", 
        "Vibe - ", 
        "Contexte - ", 
        "Énergie - ",
        "nergie - "
    ]
    if args.prefixes:
        prefixes = args.prefixes
    else:
        prefixes = label_prefixes
        
    try:
        # Find the target folder
        folder_name = args.folder_name
        
        # Find playlists to move
        all_playlists = db.query(DjmdPlaylist).filter(DjmdPlaylist.Attribute == 0).all()
        to_move = []
        for pl in all_playlists:
            if any(pl.Name.startswith(p) for p in prefixes):
                # Ensure it's not already in the target folder
                # We won't know the target folder ID until we get/create it, so we'll check later
                to_move.append(pl)
                
        result = {
            "mode": "apply" if args.apply else "plan",
            "master": str(master),
            "folderName": folder_name,
            "prefixes": prefixes,
            "playlistsFound": len(to_move),
            "playlistsToMove": [pl.Name for pl in to_move]
        }
        
        if args.apply and to_move:
            folder = get_or_create_folder(db, folder_name)
            folder_id = folder.ID
            
            # Find the max Seq in the folder
            folder_items = db.query(DjmdPlaylist).filter(DjmdPlaylist.ParentID == folder_id).all()
            max_seq = max([int(item.Seq or 0) for item in folder_items], default=0)
            
            moved_count = 0
            now = dt.datetime.now()
            
            for pl in to_move:
                if pl.ParentID != folder_id:
                    pl.ParentID = folder_id
                    max_seq += 1
                    pl.Seq = max_seq
                    pl.updated_at = now
                    moved_count += 1
                    
            folder.updated_at = now
            db.commit()
            
            result["movedCount"] = moved_count
            result["success"] = True
        else:
            result["success"] = True
            
        return result
    finally:
        db.close()

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Organize Rekordbox label playlists into a folder.")
    parser.add_argument("--master", required=True)
    parser.add_argument("--db-dir", default="")
    parser.add_argument("--folder-name", default="Labels")
    parser.add_argument("--prefixes", action="append", help="Prefixes of playlists to move")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-rekordbox-running-commit", action="store_true")
    return parser

def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding='utf-8')
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result.get("success") else 1
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False))
        return 1

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
