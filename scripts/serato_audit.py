#!/usr/bin/env python3
"""Experimental parser for Serato to identify missing tracks."""

import argparse
import json
import os
from pathlib import Path
import sys

def parse_serato_paths(filepath: Path) -> set[str]:
    """Extract paths from a Serato crate or database V2 file."""
    paths = set()
    try:
        with open(filepath, 'rb') as f:
            data = f.read()
            
        # ptrk block stores utf-16-be encoded paths
        idx = 0
        while True:
            idx = data.find(b'ptrk', idx)
            if idx == -1:
                break
            if idx + 8 <= len(data):
                length = int.from_bytes(data[idx+4:idx+8], byteorder='big')
                if length > 0 and idx + 8 + length <= len(data):
                    path_bytes = data[idx+8:idx+8+length]
                    try:
                        path_str = path_bytes.decode('utf-16-be').strip('\x00')
                        if path_str:
                            paths.add(path_str)
                    except UnicodeDecodeError:
                        pass
            idx += 4
    except Exception as e:
        print(f"Error parsing {filepath}: {e}", file=sys.stderr)
    return paths

def get_serato_roots() -> list[Path]:
    """Return common Serato paths on Windows."""
    roots = []
    # Local music folder
    user_music = Path.home() / "Music" / "_Serato_"
    if user_music.exists():
        roots.append(user_music)
    
    # Drives (e.g. F:\_Serato_)
    import string
    for drive in string.ascii_uppercase:
        drive_path = Path(f"{drive}:\\_Serato_")
        if drive_path.exists():
            roots.append(drive_path)
            
    return roots

def audit_serato(roots: list[Path]) -> dict:
    all_referenced_paths = set()
    scanned_files = 0
    
    for root in roots:
        db_v2 = root / "database V2"
        if db_v2.exists():
            all_referenced_paths.update(parse_serato_paths(db_v2))
            scanned_files += 1
            
        subcrates = root / "Subcrates"
        if subcrates.exists():
            for crate_file in subcrates.glob("*.crate"):
                all_referenced_paths.update(parse_serato_paths(crate_file))
                scanned_files += 1

    missing_paths = []
    existing_paths = []
    
    for p in all_referenced_paths:
        # Resolving path based on Serato conventions.
        # If it doesn't start with a drive letter, it's relative to the drive of the _Serato_ folder.
        # Assuming F:\ for now, but we'll try prepending the drive letter of the root.
        
        found = False
        path_str = str(p)
        
        # Check as-is (if absolute)
        if Path(path_str).exists():
            existing_paths.append(path_str)
            found = True
        else:
            # Try against each Serato root drive
            for root in roots:
                drive = root.anchor # e.g. 'F:\'
                test_path = Path(drive) / path_str
                if test_path.exists():
                    existing_paths.append(str(test_path))
                    found = True
                    break
                    
        if not found:
            missing_paths.append(path_str)
            
    return {
        "scanned_serato_files": scanned_files,
        "total_referenced_tracks": len(all_referenced_paths),
        "existing_tracks": len(existing_paths),
        "missing_tracks_count": len(missing_paths),
        "missing_tracks": missing_paths,
    }

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description="Audit Serato library for missing files.")
    parser.add_argument("--json", action="store_true", help="Output in JSON")
    args = parser.parse_args()
    
    roots = get_serato_roots()
    if not roots:
        if args.json:
            print(json.dumps({"error": "No _Serato_ folders found"}))
        else:
            print("Error: No _Serato_ folders found on this system.")
        return 1
        
    results = audit_serato(roots)
    
    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print(f"Scanned {results['scanned_serato_files']} Serato database/crate files.")
        print(f"Total track references: {results['total_referenced_tracks']}")
        print(f"Existing tracks: {results['existing_tracks']}")
        print(f"Missing tracks: {results['missing_tracks_count']}")
        if results['missing_tracks']:
            print("\nSample of missing tracks:")
            for p in results['missing_tracks'][:10]:
                print(f"  - {p}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
