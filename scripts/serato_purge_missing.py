#!/usr/bin/env python3
"""Purge missing tracks from Serato crates."""

import argparse
import json
import os
import shutil
from pathlib import Path
import sys

def parse_serato_chunks(data: bytes) -> tuple[bytes, list[tuple[str, bytes]]]:
    """Parse a Serato crate file into prefix and a list of ptrk chunks."""
    ptrk_idx = data.find(b'ptrk')
    if ptrk_idx == -1:
        return data, []
        
    prefix = data[:ptrk_idx]
    
    ptrk_chunks = []
    idx = ptrk_idx
    while True:
        idx = data.find(b'ptrk', idx)
        if idx == -1:
            break
            
        if idx + 8 <= len(data):
            length = int.from_bytes(data[idx+4:idx+8], byteorder='big')
            chunk_end = idx + 8 + length
            if chunk_end <= len(data):
                chunk = data[idx:chunk_end]
                
                # Extract path
                path_bytes = data[idx+8:chunk_end]
                try:
                    path_str = path_bytes.decode('utf-16-be').strip('\x00')
                    ptrk_chunks.append((path_str, chunk))
                except UnicodeDecodeError:
                    ptrk_chunks.append(("", chunk))
                    
                idx = chunk_end
            else:
                break
        else:
            break
            
    return prefix, ptrk_chunks

def get_serato_roots() -> list[Path]:
    roots = []
    user_music = Path.home() / "Music" / "_Serato_"
    if user_music.exists():
        roots.append(user_music)
    import string
    for drive in string.ascii_uppercase:
        drive_path = Path(f"{drive}:\\_Serato_")
        if drive_path.exists():
            roots.append(drive_path)
    return roots

def purge_serato_crates(roots: list[Path], apply: bool) -> dict:
    scanned_crates = 0
    total_tracks = 0
    missing_tracks = 0
    purged_crates = 0
    
    for root in roots:
        subcrates = root / "Subcrates"
        if not subcrates.exists():
            continue
            
        for crate_file in subcrates.glob("*.crate"):
            try:
                with open(crate_file, 'rb') as f:
                    data = f.read()
                    
                prefix, chunks = parse_serato_chunks(data)
                if not chunks:
                    continue
                    
                scanned_crates += 1
                keep_chunks = []
                crate_has_missing = False
                
                for path_str, chunk in chunks:
                    total_tracks += 1
                    
                    # Resolve path
                    found = False
                    if Path(path_str).exists():
                        found = True
                    else:
                        for r in roots:
                            drive = r.anchor
                            test_path = Path(drive) / path_str
                            if test_path.exists():
                                found = True
                                break
                                
                    if found:
                        keep_chunks.append(chunk)
                    else:
                        missing_tracks += 1
                        crate_has_missing = True
                        
                if crate_has_missing and apply:
                    # Backup the crate just in case
                    backup_file = crate_file.with_suffix(".crate.bak")
                    if not backup_file.exists():
                        shutil.copy2(crate_file, backup_file)
                        
                    # Write new crate
                    with open(crate_file, 'wb') as f:
                        f.write(prefix)
                        for chunk in keep_chunks:
                            f.write(chunk)
                            
                    purged_crates += 1
                    
            except Exception as e:
                print(f"Error parsing {crate_file}: {e}", file=sys.stderr)
                
    return {
        "mode": "apply" if apply else "plan",
        "scanned_crates": scanned_crates,
        "total_tracks": total_tracks,
        "missing_tracks": missing_tracks,
        "purged_crates_count": purged_crates if apply else 0
    }

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description="Purge missing files from Serato crates.")
    parser.add_argument("--apply", action="store_true", help="Actually rewrite crates to remove missing files")
    args = parser.parse_args()
    
    roots = get_serato_roots()
    if not roots:
        print(json.dumps({"error": "No _Serato_ folders found"}))
        return 1
        
    results = purge_serato_crates(roots, args.apply)
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
