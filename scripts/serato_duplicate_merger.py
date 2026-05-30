#!/usr/bin/env python3
"""Merge duplicates in Serato crates by re-routing them to the master file."""

import argparse
import csv
import os
import shutil
import sys
from pathlib import Path

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

def create_ptrk_chunk(path_str: str) -> bytes:
    """Create a new ptrk chunk for the given path."""
    path_bytes = path_str.encode('utf-16-be')
    length = len(path_bytes)
    header = b'ptrk' + length.to_bytes(4, byteorder='big')
    return header + path_bytes

def get_serato_roots() -> list[Path]:
    roots = []
    user_music = Path.home() / "Music" / "_Serato_"
    if user_music.exists():
        roots.append(user_music)
        
    # Check other drives
    for drive in "DEFGHIJKLMNOPQRSTUVWXYZ":
        drive_path = Path(f"{drive}:\\_Serato_")
        if drive_path.exists():
            roots.append(drive_path)
            
    return roots

def normalize_path(p: str) -> str:
    """Normalize path for comparison."""
    clean_p = p.replace('\x00', '')
    try:
        return str(Path(clean_p).resolve()).lower()
    except Exception:
        return str(Path(clean_p).absolute()).lower()

def load_duplicates_map(csv_path: Path) -> dict[str, str]:
    """Load duplicate mappings from CSV."""
    dup_map = {}
    if not csv_path.exists():
        return dup_map
        
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("action") == "delete_candidate":
                dup = row.get("path")
                keep = row.get("recommended_keep_path")
                if dup and keep:
                    dup_map[normalize_path(dup)] = keep
    return dup_map

def main():
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description="Re-route duplicate files in Serato crates.")
    parser.add_argument("--duplicate-csv", required=True, help="Path to the duplicate candidates CSV")
    parser.add_argument("--apply", action="store_true", help="Actually rewrite the crates")
    args = parser.parse_args()

    csv_path = Path(args.duplicate_csv)
    dup_map = load_duplicates_map(csv_path)
    if not dup_map:
        print("No duplicates mapping found or empty CSV.")
        return

    serato_roots = get_serato_roots()
    if not serato_roots:
        print("No _Serato_ folders found.")
        return

    crates = []
    for root in serato_roots:
        subcrates_dir = root / "Subcrates"
        if subcrates_dir.exists():
            crates.extend(subcrates_dir.glob("*.crate"))

    total_modified_crates = 0
    total_rerouted_tracks = 0

    for crate_path in crates:
        with open(crate_path, 'rb') as f:
            data = f.read()
            
        prefix, chunks = parse_serato_chunks(data)
        if not chunks:
            continue
            
        new_chunks = []
        modified = False
        crate_rerouted = 0
        
        seen_paths = set()
        
        for path_str, original_chunk in chunks:
            if not path_str:
                new_chunks.append(original_chunk)
                continue
            
            norm_p = normalize_path(path_str)
            
            # If this path is a known duplicate, map it to the keep_path!
            if norm_p in dup_map:
                keep_path = dup_map[norm_p]
                
                # Check if we already have the keep_path in this crate (prevent duplicate entries in the same crate)
                keep_norm = normalize_path(keep_path)
                if keep_norm in seen_paths:
                    # We just drop the duplicate entirely, because the kept file is already in the crate!
                    modified = True
                    crate_rerouted += 1
                    continue
                else:
                    # Replace with the kept file
                    new_chunk = create_ptrk_chunk(keep_path)
                    new_chunks.append(new_chunk)
                    seen_paths.add(keep_norm)
                    modified = True
                    crate_rerouted += 1
            else:
                new_chunks.append(original_chunk)
                seen_paths.add(norm_p)
                
        if modified:
            total_modified_crates += 1
            total_rerouted_tracks += crate_rerouted
            print(f"[{'APPLY' if args.apply else 'DRY-RUN'}] {crate_path.name}: re-routed {crate_rerouted} duplicates.")
            
            if args.apply:
                # Backup first
                backup_path = crate_path.with_suffix(".crate.bak")
                if not backup_path.exists():
                    shutil.copy2(crate_path, backup_path)
                    
                # Rebuild data
                new_data = prefix + b''.join(new_chunks)
                with open(crate_path, 'wb') as f:
                    f.write(new_data)
                    
    print("\nSummary:")
    print(f"Crates analyzed: {len(crates)}")
    print(f"Crates modified: {total_modified_crates}")
    print(f"Tracks re-routed/merged: {total_rerouted_tracks}")
    if not args.apply:
        print("Run with --apply to actually write the changes to Serato.")

if __name__ == "__main__":
    main()
