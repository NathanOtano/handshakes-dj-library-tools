#!/usr/bin/env python3
"""Audit Processed_Library_Root/local audio, Rekordbox paths, and local playlist coverage.

This helper is read-only. It writes reports under the configured reports folder
and never deletes audio files or writes the Rekordbox database.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import ntpath
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from pyrekordbox import Rekordbox6Database
from pyrekordbox.db6 import DjmdArtist, DjmdContent, DjmdPlaylist, DjmdSongPlaylist


LOSSLESS_CODECS = {
    "flac",
    "alac",
    "pcm_s16le",
    "pcm_s16be",
    "pcm_s24le",
    "pcm_s24be",
    "pcm_s32le",
    "pcm_s32be",
    "pcm_f32le",
    "pcm_f32be",
    "wavpack",
    "ape",
}

STREAMING_PREFIXES = ("tidal:", "qobuz:", "beatport:", "beatsource:", "soundcloud:")


@dataclass
class AudioFile:
    path: str
    normalized_path: str
    root_role: str
    root_path: str
    relative_path: str
    file_name: str
    extension: str
    length_bytes: int
    modified_utc: str
    identity_key: str
    artist_hint: str
    title_hint: str
    sha256: str | None = None
    decoded_audio_sha256: str | None = None
    probe: dict[str, Any] | None = None
    probe_error: str | None = None
    audio_hash_error: str | None = None


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    value = value.casefold()
    value = re.sub(r"\b(explicit|clean|radio edit|original mix|remaster(?:ed)?|processed_library_root notes?)\b", " ", value)
    value = re.sub(r"[_]+", " ", value)
    value = re.sub(r"[^\w\s&'+.-]", " ", value, flags=re.UNICODE)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def split_artist_title(stem: str) -> tuple[str, str]:
    cleaned = re.sub(r"\s+", " ", stem).strip()
    parts = re.split(r"\s+-\s+", cleaned, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", cleaned


def identity_key(artist: str | None, title: str | None) -> str:
    artist_key = normalize_text(artist)
    title_key = normalize_text(title)
    return f"{artist_key} - {title_key}".strip(" -")


def path_from_rekordbox(value: str | None) -> str:
    if not value:
        return ""
    raw = str(value).strip()
    parsed = urlparse(raw)
    if parsed.scheme == "file":
        decoded = unquote(parsed.path or "")
        if parsed.netloc and parsed.netloc.lower() not in {"localhost", ""}:
            decoded = f"//{parsed.netloc}{decoded}"
        if re.match(r"^/[A-Za-z]:", decoded):
            decoded = decoded[1:]
        return decoded.replace("/", "\\")
    return unquote(raw).replace("/", "\\")


def normalize_windows_path(value: str | None) -> str:
    path = path_from_rekordbox(value)
    if not path:
        return ""
    return ntpath.normcase(ntpath.normpath(path))


def is_streaming_path(value: str | None) -> bool:
    if not value:
        return False
    return str(value).strip().casefold().startswith(STREAMING_PREFIXES)


def resolve_configured_path(value: str | None, repo_root: Path) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    return (repo_root / path).resolve()


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def run_json(args: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout).strip())
    return json.loads(completed.stdout)


def probe_audio(path: str, ffprobe: str) -> tuple[dict[str, Any], str | None]:
    args = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_name,codec_type,sample_rate,bits_per_sample,bit_rate,channels,channel_layout:"
        "format=duration,bit_rate,format_name:format_tags=title,artist,album",
        "-of",
        "json",
        path,
    ]
    try:
        return run_json(args), None
    except Exception as exc:  # noqa: BLE001 - per-file report diagnostic.
        return {}, str(exc)


def decoded_audio_hash(path: str, ffmpeg: str) -> tuple[str | None, str | None]:
    args = [
        ffmpeg,
        "-v",
        "error",
        "-i",
        path,
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "2",
        "-ar",
        "44100",
        "-c:a",
        "pcm_s16le",
        "-f",
        "hash",
        "-hash",
        "SHA256",
        "-",
    ]
    completed = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        return None, (completed.stderr or completed.stdout).strip()
    match = re.search(r"SHA256=([A-Fa-f0-9]+)", completed.stdout)
    if not match:
        return None, "ffmpeg hash output did not include SHA256"
    return match.group(1).upper(), None


def extract_probe_fields(audio_file: AudioFile) -> dict[str, Any]:
    probe = audio_file.probe or {}
    streams = probe.get("streams") or []
    stream = streams[0] if streams else {}
    fmt = probe.get("format") or {}
    tags = fmt.get("tags") or {}
    return {
        "codec": stream.get("codec_name"),
        "sample_rate": int(stream["sample_rate"]) if stream.get("sample_rate") else None,
        "bits_per_sample": int(stream["bits_per_sample"]) if stream.get("bits_per_sample") else None,
        "bit_rate": int(stream.get("bit_rate") or fmt.get("bit_rate") or 0) or None,
        "duration_seconds": float(fmt["duration"]) if fmt.get("duration") else None,
        "artist": tags.get("artist") or tags.get("ARTIST") or audio_file.artist_hint,
        "title": tags.get("title") or tags.get("TITLE") or audio_file.title_hint,
    }


def quality_flags(fields: dict[str, Any]) -> dict[str, Any]:
    codec = (fields.get("codec") or "").casefold()
    sample_rate = int(fields.get("sample_rate") or 0)
    bits = int(fields.get("bits_per_sample") or 0)
    bitrate = int(fields.get("bit_rate") or 0)
    lossless = codec in LOSSLESS_CODECS
    above_cd = bool(lossless and ((sample_rate > 44100) or (bits > 16)))
    cd_lossless = bool(lossless and sample_rate == 44100 and bits == 16)
    lossy = bool(codec and not lossless)
    if cd_lossless:
        category = 400
    elif lossy:
        category = 300
    elif lossless and not above_cd:
        category = 250
    elif above_cd:
        category = 200
    else:
        category = 100
    score = (category * 1_000_000_000) + (sample_rate * 10_000) + (bits * 100_000) + bitrate
    return {
        "lossless": lossless,
        "above_cd_quality": above_cd,
        "cd_lossless": cd_lossless,
        "lossy": lossy,
        "quality_score": score,
    }


def choose_keep(items: list[AudioFile]) -> AudioFile:
    enriched = []
    for item in items:
        fields = extract_probe_fields(item)
        flags = quality_flags(fields)
        enriched.append((item, fields, flags))
    non_master = [entry for entry in enriched if not entry[2]["above_cd_quality"]]
    candidates = non_master if non_master else enriched
    candidates.sort(
        key=lambda entry: (
            entry[2]["quality_score"],
            entry[0].length_bytes,
            entry[0].modified_utc,
            entry[0].path.casefold(),
        ),
        reverse=True,
    )
    return candidates[0][0]


def collect_audio_files(
    roots: list[tuple[str, Path]],
    extensions: set[str],
    warnings: list[str],
) -> list[AudioFile]:
    records: list[AudioFile] = []
    seen: set[str] = set()
    for role, root in roots:
        if not root.exists():
            warnings.append(f"Audio root not found: {role}={root}")
            continue
        if not root.is_dir():
            warnings.append(f"Audio root is not a directory: {role}={root}")
            continue
        for current_root, _, filenames in os.walk(root):
            for filename in sorted(filenames):
                path = Path(current_root) / filename
                if path.suffix.casefold() not in extensions:
                    continue
                full = str(path.resolve())
                normalized = normalize_windows_path(full)
                if normalized in seen:
                    warnings.append(f"Duplicate scan path skipped: {full}")
                    continue
                seen.add(normalized)
                stat = path.stat()
                artist, title = split_artist_title(path.stem)
                records.append(
                    AudioFile(
                        path=full,
                        normalized_path=normalized,
                        root_role=role,
                        root_path=str(root),
                        relative_path=str(path.relative_to(root)),
                        file_name=path.name,
                        extension=path.suffix.casefold(),
                        length_bytes=int(stat.st_size),
                        modified_utc=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                        identity_key=identity_key(artist, title),
                        artist_hint=artist,
                        title_hint=title,
                    )
                )
    return records


def probe_files(files: list[AudioFile], ffprobe: str) -> None:
    for item in files:
        if item.probe is not None or item.probe_error is not None:
            continue
        item.probe, item.probe_error = probe_audio(item.path, ffprobe)


def exact_duplicate_groups(files: list[AudioFile], ffprobe: str) -> list[dict[str, Any]]:
    by_size: dict[int, list[AudioFile]] = defaultdict(list)
    for item in files:
        by_size[item.length_bytes].append(item)

    groups = []
    for same_size in by_size.values():
        if len(same_size) < 2:
            continue
        by_hash: dict[str, list[AudioFile]] = defaultdict(list)
        for item in same_size:
            item.sha256 = sha256_file(item.path)
            by_hash[item.sha256].append(item)
        for digest, same_hash in by_hash.items():
            if len(same_hash) < 2:
                continue
            probe_files(same_hash, ffprobe)
            keep = choose_keep(same_hash)
            groups.append(build_duplicate_group("exact_file_duplicate", digest, keep, same_hash))
    return groups


def decoded_duplicate_groups(
    files: list[AudioFile],
    ffprobe: str,
    ffmpeg: str | None,
    mode: str,
    file_limit: int,
    warnings: list[str],
) -> list[dict[str, Any]]:
    if mode == "none":
        return []
    if not ffmpeg:
        warnings.append("ffmpeg not found; decoded audio duplicate audit skipped.")
        return []

    by_identity: dict[str, list[AudioFile]] = defaultdict(list)
    for item in files:
        if item.identity_key:
            by_identity[item.identity_key].append(item)

    candidates = [item for group in by_identity.values() if len(group) > 1 for item in group]
    if mode == "all":
        candidates = files
    if file_limit > 0 and len(candidates) > file_limit:
        warnings.append(
            f"Decoded audio hashing limited to {file_limit} files out of {len(candidates)} candidates."
        )
        candidates = candidates[:file_limit]

    probe_files(candidates, ffprobe)
    duration_buckets: dict[str, list[AudioFile]] = defaultdict(list)
    for item in candidates:
        fields = extract_probe_fields(item)
        duration = fields.get("duration_seconds")
        if duration is None:
            continue
        key = item.identity_key if mode != "all" else f"duration:{round(float(duration))}"
        duration_buckets[f"{key}|{round(float(duration) / 2.0) * 2:.0f}"].append(item)

    groups = []
    for bucket in duration_buckets.values():
        if len(bucket) < 2:
            continue
        by_audio_hash: dict[str, list[AudioFile]] = defaultdict(list)
        for item in bucket:
            item.decoded_audio_sha256, item.audio_hash_error = decoded_audio_hash(item.path, ffmpeg)
            if item.decoded_audio_sha256:
                by_audio_hash[item.decoded_audio_sha256].append(item)
        for digest, same_audio in by_audio_hash.items():
            if len(same_audio) < 2:
                continue
            keep = choose_keep(same_audio)
            groups.append(build_duplicate_group("decoded_audio_duplicate", digest, keep, same_audio))
    return groups


def build_duplicate_group(match_kind: str, key: str, keep: AudioFile, items: list[AudioFile]) -> dict[str, Any]:
    rows = []
    for item in sorted(items, key=lambda candidate: candidate.path.casefold()):
        fields = extract_probe_fields(item)
        flags = quality_flags(fields)
        rows.append(
            {
                "action": "keep" if item.path == keep.path else "delete_candidate",
                "path": item.path,
                "root_role": item.root_role,
                "relative_path": item.relative_path,
                "length_bytes": item.length_bytes,
                "sha256": item.sha256,
                "decoded_audio_sha256": item.decoded_audio_sha256,
                **fields,
                **flags,
            }
        )
    return {
        "group_id": f"{match_kind}:{hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]}",
        "match_kind": match_kind,
        "key": key,
        "recommended_keep_path": keep.path,
        "delete_candidate_count": sum(1 for row in rows if row["action"] == "delete_candidate"),
        "items": rows,
    }


def content_artist_map(db: Rekordbox6Database) -> dict[str, str]:
    return {str(row.ID): row.Name or "" for row in db.query(DjmdArtist).all()}


def record_content(row: DjmdContent, artists: dict[str, str]) -> dict[str, Any]:
    folder_path = row.FolderPath or ""
    artist = artists.get(str(row.ArtistID), "") if row.ArtistID is not None else ""
    title = row.Title or row.SrcTitle or ""
    src_artist = row.SrcArtistName or ""
    return {
        "content_id": int(row.ID),
        "title": title,
        "artist": artist or src_artist,
        "src_artist": src_artist,
        "folder_path": folder_path,
        "normalized_path": normalize_windows_path(folder_path),
        "is_streaming": is_streaming_path(folder_path) or bool(row.ServiceID),
        "service_id": row.ServiceID,
        "file_size": int(row.FileSize or 0),
        "bit_rate": int(row.BitRate or 0),
        "bit_depth": int(row.BitDepth or 0),
        "sample_rate": int(row.SampleRate or 0),
        "length_raw": int(row.Length or row.SrcLength or 0),
        "identity_key": identity_key(artist or src_artist, title),
    }


def path_under(path: str, root: Path) -> bool:
    normalized_path = normalize_windows_path(path)
    normalized_root = normalize_windows_path(str(root))
    return normalized_path == normalized_root or normalized_path.startswith(normalized_root + "\\")


def remap_to_existing_audio_root(path: str, roots: list[tuple[str, Path]]) -> str:
    normalized_path = normalize_windows_path(path)
    if not normalized_path:
        return ""
    _, path_tail = ntpath.splitdrive(normalized_path)
    for _, root in roots:
        normalized_root = normalize_windows_path(str(root))
        root_drive, root_tail = ntpath.splitdrive(normalized_root)
        if not root_drive or not root_tail:
            continue
        if path_tail == root_tail or path_tail.startswith(root_tail + "\\"):
            candidate = ntpath.normpath(root_drive + path_tail)
            if os.path.exists(candidate):
                return candidate
    return ""


def audit_rekordbox_paths(
    contents: list[dict[str, Any]],
    local_by_path: dict[str, AudioFile],
    local_by_filename: dict[str, list[AudioFile]],
    expected_roots: list[tuple[str, Path]],
) -> list[dict[str, Any]]:
    rows = []
    for item in contents:
        if item["is_streaming"]:
            continue
        folder_path = item["folder_path"]
        normalized = item["normalized_path"]
        exists = bool(normalized and os.path.exists(path_from_rekordbox(folder_path)))
        local_match = local_by_path.get(normalized)
        drive_remap_match = remap_to_existing_audio_root(folder_path, expected_roots)
        filename_matches = local_by_filename.get(ntpath.basename(path_from_rekordbox(folder_path)).casefold(), [])
        missing_expected_root = ""
        under_existing_root = False
        for role, root in expected_roots:
            if path_under(folder_path, root):
                if root.exists():
                    under_existing_root = True
                else:
                    missing_expected_root = f"{role}:{root}"
                break

        if exists or local_match:
            status = "ok"
            action = "none"
        elif drive_remap_match:
            status = "wrong_drive_letter_local_file_exists"
            action = "relink_rekordbox_path_to_mounted_drive"
        elif len(filename_matches) == 1:
            status = "local_equivalent_found_by_filename"
            action = "review_relink_before_delete"
        elif missing_expected_root:
            status = "blocked_missing_expected_root"
            action = "do_not_delete_until_root_verified"
        elif under_existing_root:
            status = "missing_under_existing_root"
            action = "rekordbox_delete_candidate"
        else:
            status = "missing_outside_expected_roots"
            action = "manual_review"

        if status != "ok":
            rows.append(
                {
                    **item,
                    "path_exists": exists,
                    "status": status,
                    "recommended_action": action,
                    "missing_expected_root": missing_expected_root,
                    "drive_remap_match_path": drive_remap_match,
                    "filename_match_count": len(filename_matches),
                    "filename_match_path": filename_matches[0].path if len(filename_matches) == 1 else "",
                }
            )
    return rows


def playlist_rows(db: Rekordbox6Database, playlist_id: Any) -> list[DjmdSongPlaylist]:
    return list(
        db.query(DjmdSongPlaylist)
        .filter(DjmdSongPlaylist.PlaylistID == str(playlist_id))
        .order_by(DjmdSongPlaylist.TrackNo, DjmdSongPlaylist.ID)
    )


def playlist_targets(playlists: list[DjmdPlaylist], queries: list[str]) -> list[DjmdPlaylist]:
    aliases = {
        \"playlist1\": [\"101_playlist1_songs\", \"playlist1\"],
        \"playlist2\": [\"101_playlist2_songs\", \"playlist2\"],
        "easy": ["101_easy candies", "easy"],
        "sof": ["101_soft candies", "soft", "sof"],
        "soft": ["101_soft candies", "soft", "sof"],
        "chill": ["chill", "sleep", "101_sleep candies"],
    }
    normal = [pl for pl in playlists if int(pl.Attribute or 0) == 0 and not pl.SmartList]
    selected: dict[str, DjmdPlaylist] = {}
    for query in queries:
        terms = aliases.get(query.casefold(), [query.casefold()])
        for playlist in normal:
            name_key = (playlist.Name or "").casefold()
            if any(term in name_key for term in terms):
                selected[str(playlist.ID)] = playlist
    return sorted(selected.values(), key=lambda pl: (pl.Name or "").casefold())


def audit_playlist_local_coverage(
    db: Rekordbox6Database,
    contents_by_id: dict[str, dict[str, Any]],
    playlists: list[DjmdPlaylist],
    roots: list[tuple[str, Path]],
) -> list[dict[str, Any]]:
    local_by_identity: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for content in contents_by_id.values():
        local_path_exists = os.path.exists(path_from_rekordbox(content["folder_path"]))
        local_path_exists = local_path_exists or bool(remap_to_existing_audio_root(content["folder_path"], roots))
        if not content["is_streaming"] and content["identity_key"] and local_path_exists:
            local_by_identity[content["identity_key"]].append(content)

    rows = []
    for playlist in playlists:
        memberships = playlist_rows(db, playlist.ID)
        ids = [str(row.ContentID) for row in memberships]
        id_set = set(ids)
        local_count = 0
        streaming_count = 0
        missing_local_path_count = 0
        relinkable_local_path_count = 0
        for content_id in ids:
            content = contents_by_id.get(content_id)
            if not content:
                continue
            if content["is_streaming"]:
                streaming_count += 1
                candidates = [
                    candidate for candidate in local_by_identity.get(content["identity_key"], [])
                    if str(candidate["content_id"]) not in id_set
                ]
                for candidate in candidates[:3]:
                    rows.append(
                        {
                            "playlist": playlist.Name,
                            "playlist_id": int(playlist.ID),
                            "status": "local_available_not_in_playlist",
                            "streaming_content_id": content["content_id"],
                            "streaming_title": content["title"],
                            "streaming_artist": content["artist"],
                            "local_content_id": candidate["content_id"],
                            "local_path": candidate["folder_path"],
                            "recommended_action": "add_local_content_to_playlist",
                        }
                    )
            else:
                local_count += 1
                if not os.path.exists(path_from_rekordbox(content["folder_path"])):
                    if remap_to_existing_audio_root(content["folder_path"], roots):
                        relinkable_local_path_count += 1
                    else:
                        missing_local_path_count += 1
        rows.append(
            {
                "playlist": playlist.Name,
                "playlist_id": int(playlist.ID),
                "status": "summary",
                "streaming_content_id": "",
                "streaming_title": "",
                "streaming_artist": "",
                "local_content_id": "",
                "local_path": "",
                "recommended_action": (
                    f"local={local_count}; streaming={streaming_count}; "
                    f"relinkable_local_paths={relinkable_local_path_count}; "
                    f"missing_local_paths={missing_local_path_count}"
                ),
            }
        )
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def run(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    config = load_json(Path(args.config).resolve())
    reports_root = resolve_configured_path(args.reports_root or config.get("reportsRoot"), repo_root) or (repo_root / "reports")
    reports_root.mkdir(parents=True, exist_ok=True)
    warnings: list[str] = []

    extensions = {str(ext).casefold() for ext in config.get("audioExtensions", [])}
    roots: list[tuple[str, Path]] = []
    for value in args.audio_root:
        roots.append(("custom", Path(value).resolve()))
    if not roots:
        for key in ("libraryRoot", "postProcessed_Library_RootRoot"):
            root = resolve_configured_path(config.get(key), repo_root)
            if root:
                roots.append((key, root))

    ffprobe = shutil.which("ffprobe")
    ffmpeg = shutil.which("ffmpeg")
    if not ffprobe:
        raise RuntimeError("ffprobe not found in PATH.")

    audio_files = collect_audio_files(roots, extensions, warnings)
    local_by_path = {item.normalized_path: item for item in audio_files}
    local_by_filename: dict[str, list[AudioFile]] = defaultdict(list)
    for item in audio_files:
        local_by_filename[item.file_name.casefold()].append(item)

    db = Rekordbox6Database(path=Path(args.master).resolve(), db_dir=Path(args.db_dir).resolve())
    try:
        artists = content_artist_map(db)
        content_rows = [record_content(row, artists) for row in db.query(DjmdContent).all()]
        contents_by_id = {str(row["content_id"]): row for row in content_rows}
        playlists = list(db.query(DjmdPlaylist).all())
        target_playlists = playlist_targets(playlists, args.playlist)

        missing_rows = audit_rekordbox_paths(content_rows, local_by_path, local_by_filename, roots)
        exact_groups = exact_duplicate_groups(audio_files, ffprobe)
        decoded_groups = decoded_duplicate_groups(
            audio_files,
            ffprobe,
            ffmpeg,
            args.audio_hash_mode,
            args.audio_hash_file_limit,
            warnings,
        )
        playlist_rows_out = audit_playlist_local_coverage(db, contents_by_id, target_playlists, roots)
    finally:
        db.close()

    duplicate_groups = exact_groups + decoded_groups
    stamp = now_stamp()
    json_path = reports_root / f"dj-cleanup-audit-{stamp}.json"
    missing_csv = reports_root / f"rekordbox-missing-files-{stamp}.csv"
    duplicates_csv = reports_root / f"local-duplicate-candidates-{stamp}.csv"
    playlists_csv = reports_root / f"playlist-local-coverage-{stamp}.csv"

    duplicate_rows = []
    for group in duplicate_groups:
        for item in group["items"]:
            duplicate_rows.append(
                {
                    "group_id": group["group_id"],
                    "match_kind": group["match_kind"],
                    "recommended_keep_path": group["recommended_keep_path"],
                    **item,
                }
            )

    write_csv(
        missing_csv,
        missing_rows,
        [
            "content_id",
            "title",
            "artist",
            "folder_path",
            "status",
            "recommended_action",
            "path_exists",
            "missing_expected_root",
            "drive_remap_match_path",
            "filename_match_count",
            "filename_match_path",
        ],
    )
    write_csv(
        duplicates_csv,
        duplicate_rows,
        [
            "group_id",
            "match_kind",
            "action",
            "recommended_keep_path",
            "path",
            "root_role",
            "relative_path",
            "codec",
            "sample_rate",
            "bits_per_sample",
            "bit_rate",
            "duration_seconds",
            "above_cd_quality",
            "cd_lossless",
            "length_bytes",
            "sha256",
            "decoded_audio_sha256",
        ],
    )
    write_csv(
        playlists_csv,
        playlist_rows_out,
        [
            "playlist",
            "playlist_id",
            "status",
            "streaming_content_id",
            "streaming_title",
            "streaming_artist",
            "local_content_id",
            "local_path",
            "recommended_action",
        ],
    )

    missing_by_status: dict[str, int] = defaultdict(int)
    for row in missing_rows:
        missing_by_status[row["status"]] += 1
    playlist_actions = sum(1 for row in playlist_rows_out if row["status"] == "local_available_not_in_playlist")
    duplicate_delete_candidates = sum(group["delete_candidate_count"] for group in duplicate_groups)

    payload = {
        "generated_at": now_iso(),
        "audio_roots": [{"role": role, "path": str(path), "exists": path.exists()} for role, path in roots],
        "rekordbox_master_copy": str(Path(args.master).resolve()),
        "summary": {
            "audio_files_scanned": len(audio_files),
            "rekordbox_contents": len(content_rows),
            "rekordbox_missing_rows": len(missing_rows),
            "rekordbox_missing_by_status": dict(sorted(missing_by_status.items())),
            "exact_duplicate_groups": len(exact_groups),
            "decoded_audio_duplicate_groups": len(decoded_groups),
            "duplicate_delete_candidates": duplicate_delete_candidates,
            "playlist_targets_found": [playlist.Name for playlist in target_playlists],
            "playlist_local_add_candidates": playlist_actions,
            "warnings": len(warnings),
        },
        "reports": {
            "json": str(json_path),
            "rekordbox_missing_csv": str(missing_csv),
            "duplicates_csv": str(duplicates_csv),
            "playlist_coverage_csv": str(playlists_csv),
        },
        "warnings": warnings,
        "duplicate_groups": duplicate_groups,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only DJ cleanup audit.")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "config" / "dj-library.paths.json"))
    parser.add_argument("--master", required=True)
    parser.add_argument("--db-dir", required=True)
    parser.add_argument("--reports-root")
    parser.add_argument("--audio-root", action="append", default=[])
    parser.add_argument("--playlist", action="append", default=["dancing", "easy", "sof", "chill"])
    parser.add_argument("--audio-hash-mode", choices=["none", "candidate", "all"], default="candidate")
    parser.add_argument("--audio-hash-file-limit", type=int, default=0)
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        print(json.dumps(run(args), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI reports operator-facing failure.
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
