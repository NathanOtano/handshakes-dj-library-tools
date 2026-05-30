#!/usr/bin/env python3
"""Export Rekordbox playlists to M3U format for Serato mirroring."""

import argparse
import json
import os
import sys
from pathlib import Path

# Add local path if needed, but we assume it's run via venv
try:
    from pyrekordbox import Rekordbox6Database
    from pyrekordbox.db6 import DjmdPlaylist, DjmdSongPlaylist, DjmdContent
except ImportError:
    print("Error: pyrekordbox not found. Please run this script in the pyrekordbox-venv.", file=sys.stderr)
    sys.exit(1)

def as_str(value) -> str:
    return str(value) if value else ""

def get_playlist_path(db: Rekordbox6Database, playlist: DjmdPlaylist) -> str:
    """Reconstruct the folder path of a playlist."""
    path_parts = [playlist.Name]
    parent_id = playlist.ParentID
    
    while parent_id and as_str(parent_id) != "root":
        # Find parent
        parent = db.query(DjmdPlaylist).filter(DjmdPlaylist.ID == parent_id).first()
        if parent:
            path_parts.insert(0, parent.Name)
            parent_id = parent.ParentID
        else:
            break
            
    return "/".join(path_parts)

def export_m3u(db: Rekordbox6Database, output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    
    playlists = db.query(DjmdPlaylist).filter(DjmdPlaylist.Attribute == 0).all()
    exported_count = 0
    total_tracks = 0
    
    for pl in playlists:
        # Don't export Smart Playlists as M3U for Serato, since they are dynamic
        if pl.SmartList:
            continue
            
        pl_path_str = get_playlist_path(db, pl)
        # Clean for filesystem
        safe_path = pl_path_str.replace(":", "_").replace("?", "_").replace("*", "_")
        m3u_file = output_dir / f"{safe_path}.m3u"
        m3u_file.parent.mkdir(parents=True, exist_ok=True)
        
        songs = db.query(DjmdSongPlaylist).filter(DjmdSongPlaylist.PlaylistID == pl.ID).order_by(DjmdSongPlaylist.TrackNo).all()
        if not songs:
            continue
            
        with open(m3u_file, "w", encoding="utf-8") as f:
            f.write("#EXTM3U\n")
            for song in songs:
                content = db.query(DjmdContent).filter(DjmdContent.ID == song.ContentID).first()
                if content and content.FolderPath:
                    f.write(f"{content.FolderPath}\n")
                    total_tracks += 1
                    
        exported_count += 1
        
    return {
        "output_dir": str(output_dir),
        "exported_playlists": exported_count,
        "total_tracks_exported": total_tracks
    }

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description="Export Rekordbox playlists to M3U.")
    parser.add_argument("--master", required=True, help="Path to Rekordbox master.db")
    parser.add_argument("--output", required=True, help="Directory to save M3U files")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    args = parser.parse_args()
    
    master_db = Path(args.master)
    if not master_db.exists():
        if args.json:
            print(json.dumps({"error": f"master.db not found at {args.master}"}))
        else:
            print(f"Error: master.db not found at {args.master}")
        return 1
        
    db = Rekordbox6Database(path=str(master_db))
    try:
        results = export_m3u(db, Path(args.output))
        if args.json:
            print(json.dumps(results, indent=2, ensure_ascii=False))
        else:
            print(f"Exported {results['exported_playlists']} playlists to {results['output_dir']}")
            print(f"Total track references: {results['total_tracks_exported']}")
    finally:
        db.close()
        
    return 0

if __name__ == "__main__":
    sys.exit(main())
