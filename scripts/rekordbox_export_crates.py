#!/usr/bin/env python3
"""Export Rekordbox playlists directly to native Serato .crate files."""

import argparse
import json
import os
import struct
import sys
from pathlib import Path

try:
    from pyrekordbox import Rekordbox6Database
    from pyrekordbox.db6 import DjmdPlaylist, DjmdSongPlaylist, DjmdContent
except ImportError:
    print("Error: pyrekordbox not found. Please run this script in the pyrekordbox-venv.", file=sys.stderr)
    sys.exit(1)

def as_str(value) -> str:
    return str(value) if value else ""

def get_playlist_path_parts(db: Rekordbox6Database, playlist: DjmdPlaylist) -> list[str]:
    """Reconstruct the folder path of a playlist as a list of names."""
    path_parts = [playlist.Name]
    parent_id = playlist.ParentID
    
    while parent_id and as_str(parent_id) != "root":
        parent = db.query(DjmdPlaylist).filter(DjmdPlaylist.ID == parent_id).first()
        if parent:
            path_parts.insert(0, parent.Name)
            parent_id = parent.ParentID
        else:
            break
            
    return path_parts

def create_serato_crate(filepath: Path, track_paths: list[str]):
    """Write a native Serato .crate file."""
    # Ensure parent dir exists
    filepath.parent.mkdir(parents=True, exist_ok=True)
    
    # 1.0/Serato ScratchLive Crate
    vrsn_data = '1.0/Serato ScratchLive Crate'.encode('utf-16-be')
    vrsn_chunk = b'vrsn' + struct.pack('>I', len(vrsn_data)) + vrsn_data
    
    # To be perfectly compliant, we also add minimal display columns (not strictly required but safer)
    osrt_data = b'tvcn\x00\x00\x00\x02\x00#brev\x00\x00\x00\x01\x00'
    osrt_chunk = b'osrt' + struct.pack('>I', len(osrt_data)) + osrt_data
    
    with open(filepath, 'wb') as f:
        f.write(vrsn_chunk)
        f.write(osrt_chunk)
        
        for path_str in track_paths:
            # Serato paths shouldn't contain the drive letter if they are on the same drive?
            # Actually, absolute paths with drive letters usually work, or relative paths.
            # We'll use absolute paths without any special trick, Serato generally resolves them.
            path_data = path_str.encode('utf-16-be')
            pnam_chunk = b'pnam' + struct.pack('>I', len(path_data)) + path_data
            ptrk_chunk = b'ptrk' + struct.pack('>I', len(pnam_chunk)) + pnam_chunk
            f.write(ptrk_chunk)

def export_crates(db: Rekordbox6Database, subcrates_dir: Path, prefix: str) -> dict:
    subcrates_dir.mkdir(parents=True, exist_ok=True)
    
    playlists = db.query(DjmdPlaylist).filter(DjmdPlaylist.Attribute == 0).all()
    exported_count = 0
    total_tracks = 0
    
    for pl in playlists:
        if pl.SmartList:
            continue
            
        path_parts = get_playlist_path_parts(db, pl)
        # Prefix the root folder (e.g. 'Rekordbox')
        if prefix:
            path_parts.insert(0, prefix)
            
        # Clean names for filesystem
        safe_parts = [p.replace(":", "_").replace("?", "_").replace("*", "_").replace("%%", "_") for p in path_parts]
        
        # Serato nested crates use '%%' as the separator
        crate_filename = "%%".join(safe_parts) + ".crate"
        crate_filepath = subcrates_dir / crate_filename
        
        # Get tracks
        songs = db.query(DjmdSongPlaylist).filter(DjmdSongPlaylist.PlaylistID == pl.ID).order_by(DjmdSongPlaylist.TrackNo).all()
        if not songs:
            continue
            
        track_paths = []
        for song in songs:
            content = db.query(DjmdContent).filter(DjmdContent.ID == song.ContentID).first()
            if content and content.FolderPath:
                track_paths.append(content.FolderPath)
                
        if track_paths:
            create_serato_crate(crate_filepath, track_paths)
            exported_count += 1
            total_tracks += len(track_paths)
            
    return {
        "output_dir": str(subcrates_dir),
        "exported_crates": exported_count,
        "total_tracks_exported": total_tracks
    }

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description="Export Rekordbox playlists to Serato .crate files.")
    parser.add_argument("--master", required=True, help="Path to Rekordbox master.db")
    parser.add_argument("--subcrates", required=True, help="Path to Serato Subcrates folder")
    parser.add_argument("--prefix", default="Rekordbox", help="Root folder name in Serato")
    parser.add_argument("--json", action="store_true")
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
        results = export_crates(db, Path(args.subcrates), args.prefix)
        if args.json:
            print(json.dumps(results, indent=2, ensure_ascii=False))
        else:
            print(f"Exported {results['exported_crates']} crates to {results['output_dir']}")
            print(f"Total track references: {results['total_tracks_exported']}")
    finally:
        db.close()
        
    return 0

if __name__ == "__main__":
    sys.exit(main())
