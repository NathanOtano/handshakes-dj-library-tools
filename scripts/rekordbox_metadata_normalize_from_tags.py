#!/usr/bin/env python3
"""Normalize local Rekordbox metadata from local audio file tags.

The script is read-only by default and only mutates the database when
``--apply`` is passed. It updates local non-streaming rows by matching file
paths between the local music roots and Rekordbox content rows, then syncing
all normalized available metadata fields that exist in ``DjmdContent``.
"""

from __future__ import annotations

import argparse
import csv
import json
import ntpath
import os
import re
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from pyrekordbox import Rekordbox6Database
from pyrekordbox.db6 import DjmdArtist, DjmdContent, DjmdKey
from sqlalchemy.exc import OperationalError
try:
    from sqlalchemy.orm.properties import RelationshipProperty
except ImportError:  # pragma: no cover - optional when SQLAlchemy internals differ.
    RelationshipProperty = None

STREAMING_PREFIXES = ("tidal:", "qobuz:", "beatport:", "beatsource:", "soundcloud:")


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def resolve_configured_path(value: str | None, repo_root: Path) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    return (repo_root / path).resolve()


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


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def normalize_text_key(value: str | None) -> str:
    return normalize_text(value).casefold()


def split_artist_title(stem: str) -> tuple[str, str]:
    cleaned = normalize_text(stem)
    parts = re.split(r"\s+-\s+", cleaned, maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return "", cleaned


def choose_artists_for_db(value: str) -> list[str]:
    primary = normalize_text(value)
    if not primary:
        return []
    primary_only = re.split(r"\s+(?:&|\+|,|x|feat\.?|featuring)\s+", primary, maxsplit=1, flags=re.IGNORECASE)[0]
    return [artist.strip() for artist in [primary_only] if artist.strip()]


def normalize_tag_key(value: str) -> str:
    return str(value).strip().casefold()


def split_identifier(value: str) -> list[str]:
    return [normalize_tag_key(part) for part in re.findall(r"[A-Za-z0-9]+", str(value)) if part.strip()]


def field_aliases(field: str) -> list[str]:
    parts = split_identifier(field)
    if not parts:
        return []

    aliases: list[str] = []
    compact = "".join(parts)
    spaced = " ".join(parts)
    underscored = "_".join(parts)
    aliases.extend([compact, spaced, underscored])
    if parts[0] == "src":
        stripped = parts[1:]
        if stripped:
            aliases.extend(
                [
                    "".join(stripped),
                    " ".join(stripped),
                    "_".join(stripped),
                    "".join(stripped[:-1]) if len(stripped) > 1 and stripped[-1] in {"name", "names", "title"} else "",
                    " ".join(stripped[:-1]) if len(stripped) > 1 and stripped[-1] in {"name", "names", "title"} else "",
                    "_".join(stripped[:-1]) if len(stripped) > 1 and stripped[-1] in {"name", "names", "title"} else "",
                ]
            )
    aliases.extend([f"src{compact}", f"src_{underscored}"])
    return [alias for alias in dict.fromkeys([alias for alias in aliases if alias]) if alias]


def pick_tag(tags: dict[str, str], keys: list[str]) -> str:
    lowered = {normalize_tag_key(k): str(v).strip() for k, v in tags.items() if v is not None}
    for key in keys:
        value = lowered.get(normalize_tag_key(key), "")
        if value:
            return value
    return ""


def first_tag(tags: dict[str, Any], keys: list[str]) -> str:
    lowered = {normalize_tag_key(k): str(v).strip() for k, v in tags.items() if v is not None}
    for key in keys:
        value = lowered.get(normalize_tag_key(key), "")
        if value:
            return value
    return ""


def energy_candidate(value: str) -> str:
    clean = normalize_text(value)
    if re.fullmatch(r"(?:10|[1-9])", clean):
        return clean
    compact = re.sub(r"\s+", "", clean)
    if compact and len(compact) <= 10 and all(char in {"*", "★"} for char in compact):
        return str(len(compact))
    return ""


def is_energy_comment(value: str | None) -> bool:
    return bool(energy_candidate(str(value or "")))


def parse_int(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\d+", str(value))
    if not match:
        return None
    try:
        parsed = int(match.group(0))
    except ValueError:
        return None
    if parsed < 0 or parsed > 50000:
        return None
    return parsed


def parse_year(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"(19\d{2}|20\d{2})", str(value))
    if not match:
        return None
    try:
        year = int(match.group(1))
    except ValueError:
        return None
    return year if 1900 <= year <= 2100 else None


def parse_track_or_disc(value: str | None) -> int | None:
    if not value:
        return None
    text = str(value).strip()
    if "/" in text:
        text = text.split("/", 1)[0]
    match = re.match(r"^\s*(\d+)\s*$", text)
    if not match:
        return None
    parsed = int(match.group(1))
    if parsed < 0 or parsed > 9999:
        return None
    return parsed


def parse_bpm(value: str | None) -> int | None:
    if not value:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", str(value))
    if not m:
        return None
    try:
        parsed = float(m.group(0).replace(",", "."))
    except ValueError:
        return None
    if not 20 <= parsed <= 400:
        return None
    return int(round(parsed * 100))


def energy_from_text(value: str | None) -> str:
    clean = normalize_text(value)
    if not clean:
        return ""
    candidates = [
        re.sub(r"[^0-9*★]", "", token)
        for token in re.split(r"\s*[;,/\\-]\s*", clean)
        if re.sub(r"[^0-9*★]", "", token).strip()
    ]
    for candidate in candidates:
        found = energy_candidate(candidate)
        if found:
            return found
    compact = re.sub(r"\s+", "", clean)
    if compact and all(char in {"*", "★"} for char in compact):
        return str(len(compact))
    return ""


def parse_duration_to_seconds(value: str | None) -> int | None:
    if not value:
        return None
    try:
        seconds = float(str(value).strip())
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return int(round(seconds))


def merge_tags(dest: dict[str, str], source: dict[str, Any] | None) -> None:
    if not source:
        return
    for key, value in source.items():
        if value is None:
            continue
        tag_key = normalize_tag_key(str(key))
        if not tag_key:
            continue
        text = str(value).strip()
        if not text:
            continue
        if tag_key not in dest:
            dest[tag_key] = text


def has_field(row: Any, field: str) -> bool:
    return hasattr(row, field)


def is_scalar_field(row: Any, field: str) -> bool:
    try:
        attr = row.__mapper__.attrs[field]
    except Exception:
        return False
    if RelationshipProperty is not None and isinstance(attr, RelationshipProperty):
        return False
    return attr.__class__.__name__ == "ColumnProperty"


def set_if_changed_text(row: Any, update: dict[str, Any], field: str, value: str) -> None:
    if not value or not has_field(row, field) or not is_scalar_field(row, field):
        return
    current = str(getattr(row, field) or "")
    if normalize_text_key(current) != normalize_text_key(value):
        update[field] = normalize_text(value)


def set_if_changed_int(row: Any, update: dict[str, Any], field: str, value: int | None) -> None:
    if value is None or not has_field(row, field) or not is_scalar_field(row, field):
        return
    if int(getattr(row, field) or 0) != int(value):
        update[field] = value


def key_name_variants(value: str) -> list[str]:
    raw = str(value).strip().casefold()
    if not raw:
        return []

    raw = raw.replace("♯", "#").replace("♭", "b")
    raw = raw.replace("sharp", "#").replace("flat", "b")
    raw = raw.replace("major", "")
    raw = raw.replace("maj", "")
    raw = raw.replace("minor", "m").replace("min", "m")
    raw = re.sub(r"\(.*?\)", "", raw)
    raw = raw.replace(" ", "").replace("-", "")
    raw = raw.replace("_", "")

    variants: set[str] = set()
    cleaned = re.sub(r"[^a-g0-9#bm/ab]", "", raw)
    if not cleaned:
        return []

    for token in cleaned.split("/"):
        token = token.strip()
        if not token:
            continue
        m = re.fullmatch(r"\d{1,2}[ab]", token)
        if m:
            variants.add(m.group(0))
            continue
        m = re.fullmatch(r"([a-g])([#b]?)(m?)", token)
        if m:
            note, accidental, suffix = m.groups()
            variants.add(f"{note}{accidental}{suffix}")
            if suffix == "m":
                variants.add(f"{note}{accidental}")
            continue
        variants.add(token)

    return list(dict.fromkeys(variants))


def resolve_key_id(value: str, lookup: dict[str, str]) -> str | None:
    for variant in key_name_variants(value):
        key = variant.casefold()
        found = lookup.get(key)
        if found:
            return found
    return None


def run_json(args: list[str]) -> tuple[dict[str, Any], str | None]:
    completed = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        return {}, (completed.stderr or completed.stdout).strip() or "ffprobe exited with non-zero code"
    try:
        return json.loads(completed.stdout), None
    except json.JSONDecodeError as exc:
        return {}, str(exc)


def probe_audio(path: Path, ffprobe: str) -> tuple[dict[str, str], str | None]:
    args = [
        ffprobe,
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        "--",
        str(path),
    ]
    payload, error = run_json(args)
    if error:
        return {}, error
    tags: dict[str, str] = {}
    fmt = payload.get("format") or {}
    streams = payload.get("streams") or []
    merge_tags(tags, fmt.get("tags"))
    for stream in streams:
        if isinstance(stream, dict):
            merge_tags(tags, stream.get("tags"))
            if stream.get("duration"):
                if "duration" not in tags:
                    tags["duration"] = str(stream["duration"])
    if fmt.get("duration") and "duration" not in tags:
        tags["duration"] = str(fmt["duration"])
    return tags, None


def collect_audio_file_index(roots: list[tuple[str, Path]], extensions: set[str]) -> tuple[dict[str, Path], dict[str, list[str]]]:
    files_by_path: dict[str, Path] = {}
    files_by_filename: dict[str, list[str]] = defaultdict(list)
    for role, root in roots:
        if not root.exists():
            continue
        if not root.is_dir():
            continue
        for current_root, dirs, filenames in os.walk(root):
            dirs[:] = [name for name in dirs if name not in {".git", "runtime", "reports"}]
            for filename in filenames:
                path = Path(current_root) / filename
                if path.suffix.casefold() not in extensions:
                    continue
                norm = normalize_windows_path(str(path.resolve()))
                if norm not in files_by_path:
                    files_by_path[norm] = path.resolve()
                files_by_filename[path.name.casefold()].append(norm)
    return files_by_path, files_by_filename


def load_key_lookup(db: Rekordbox6Database) -> tuple[dict[str, str], dict[str, str]]:
    by_name: dict[str, str] = {}
    by_id: dict[str, str] = {}
    for row in db.query(DjmdKey).all():
        key_id = str(row.ID)
        scale = str(row.ScaleName or "")
        if not scale:
            continue
        by_id[key_id] = scale
        for variant in key_name_variants(scale):
            by_name[variant.casefold()] = key_id
            by_name[re.sub(r"\s+", "", variant).casefold()] = key_id
    return by_name, by_id


def load_artist_cache(db: Rekordbox6Database) -> dict[str, DjmdArtist]:
    artists: dict[str, DjmdArtist] = {}
    for row in db.query(DjmdArtist).all():
        if not row.Name:
            continue
        artists[normalize_text_key(row.Name)] = row
    return artists


def get_artist_id(
    name: str,
    db: Rekordbox6Database,
    artists: dict[str, DjmdArtist],
    create_if_missing: bool = True,
) -> str:
    # Avoid creating new rows when DB is write-guarded (e.g. Rekordbox running) or
    # when we only want metadata enrichment from local tags.
    if not create_if_missing:
        return ""

    base_name = normalize_text(name)
    key = normalize_text_key(base_name)
    existing = artists.get(key)
    if existing is not None:
        return str(existing.ID)

    artist = DjmdArtist(Name=base_name, SearchStr=base_name.casefold())
    db.add(artist)
    db.flush()
    artists[key] = artist
    return str(artist.ID)


def artist_name_by_id(artists_by_id: dict[str, str], artist_id: str | None) -> str:
    if not artist_id:
        return ""
    return artists_by_id.get(str(artist_id), "")


def record_changed_fields(changes: dict[str, Any]) -> str:
    return "; ".join(sorted(changes.keys()))


def build_updates(
    row: DjmdContent,
    tags: dict[str, str],
    path_text: str,
    key_lookup: dict[str, str],
    db: Rekordbox6Database,
    artist_cache: dict[str, DjmdArtist],
    artist_by_id: dict[str, str],
) -> tuple[dict[str, Any], str]:
    update: dict[str, Any] = {}

    title = first_tag(
        tags,
        ["title", "titlesort", "tts", "title_sort"],
    )
    album = first_tag(tags, ["album", "albumsort", "grouping"])
    album_artist = first_tag(tags, ["albumartist", "album_artist", "album artists"])
    artist = first_tag(tags, ["artist", "album_artist", "albumartist", "composer", "performer"])
    if not artist:
        stem = Path(path_text).stem
        artist_candidates, title_from_file = split_artist_title(stem)
        if artist_candidates:
            artist = artist_candidates
        if not title:
            title = title_from_file

    if not title:
        title = Path(path_text).stem

    bpm = parse_bpm(first_tag(tags, ["bpm", "tbpm", "tempo"]))
    key_id = resolve_key_id(first_tag(tags, ["tkey", "key", "initialkey", "initial_key"]), key_lookup)
    track_no = parse_track_or_disc(first_tag(tags, ["track", "tracknumber", "trck"]))
    disc_no = parse_track_or_disc(first_tag(tags, ["disc", "discnumber", "discnumber"]))
    release_year = parse_year(first_tag(tags, ["date", "year", "originaldate", "encodingdate"]))
    energy = energy_from_text(first_tag(tags, ["comment", "commentary", "description"]))
    if not energy:
        comment_candidates = [
            value
            for key, value in tags.items()
            if "comment" in key or "description" in key or "energy" in key
        ]
        for candidate in comment_candidates:
            energy = energy_from_text(candidate)
            if energy:
                break

    if title and normalize_text_key(row.Title) != normalize_text_key(title):
        update["Title"] = title
    if title and normalize_text_key(row.SrcTitle or "") != normalize_text_key(title):
        update["SrcTitle"] = title

    primary_artists = choose_artists_for_db(artist)
    artist_name = primary_artists[0] if primary_artists else ""
    if artist_name:
        if normalize_text_key(row.SrcArtistName or "") != normalize_text_key(artist_name):
            update["SrcArtistName"] = artist_name
        # Avoid creating new Artist rows while normalizing tags.
        artist_id = get_artist_id(artist_name, db, artist_cache, create_if_missing=False)
        if artist_id and str(row.ArtistID or "") != artist_id:
            update["ArtistID"] = artist_id

    if album and normalize_text_key(row.SrcAlbumName or "") != normalize_text_key(album):
        update["SrcAlbumName"] = album

    set_if_changed_text(row, update, "SrcAlbumArtistName", album_artist)
    set_if_changed_text(row, update, "SrcComposer", first_tag(tags, ["composer", "writer", "arranger"]))
    set_if_changed_text(row, update, "Composer", first_tag(tags, ["composer", "writer", "arranger"]))
    set_if_changed_text(row, update, "SrcGenre", first_tag(tags, ["genre", "style"]))
    set_if_changed_text(row, update, "Genre", first_tag(tags, ["genre", "style"]))
    set_if_changed_text(row, update, "Label", first_tag(tags, ["label", "publisher", "organization", "copyright"]))
    set_if_changed_text(row, update, "SrcLabel", first_tag(tags, ["label", "publisher", "organization", "copyright"]))
    set_if_changed_text(row, update, "Grouping", first_tag(tags, ["grouping"]))
    set_if_changed_text(row, update, "Comment", first_tag(tags, ["comment", "commentary", "description"]))
    set_if_changed_text(row, update, "SrcComment", first_tag(tags, ["comment", "commentary", "description"]))
    set_if_changed_text(row, update, "Copyright", first_tag(tags, ["copyright"]))
    set_if_changed_text(row, update, "Remixer", first_tag(tags, ["remixer", "remix", "remixed_by"]))
    set_if_changed_text(row, update, "SrcRemixer", first_tag(tags, ["remixer", "remix", "remixed_by"]))
    set_if_changed_text(row, update, "SrcYear", first_tag(tags, ["date", "year", "originaldate"]))
    set_if_changed_text(row, update, "SrcTrack", first_tag(tags, ["track", "tracknumber", "track_no"]))
    set_if_changed_text(row, update, "Language", first_tag(tags, ["language"]))
    set_if_changed_text(row, update, "MusicBrainzTrack", first_tag(tags, ["musicbrainz_trackid", "musicbrainz track id"]))
    set_if_changed_text(row, update, "ISRC", first_tag(tags, ["isrc"]))

    if bpm is not None and int(row.BPM or 0) != int(bpm):
        update["BPM"] = bpm

    if key_id is not None and str(row.KeyID or "") != str(key_id):
        update["KeyID"] = key_id

    set_if_changed_int(row, update, "TrackNo", track_no)
    set_if_changed_int(row, update, "DiscNo", disc_no)
    set_if_changed_int(row, update, "ReleaseYear", release_year)
    set_if_changed_int(row, update, "Length", parse_duration_to_seconds(first_tag(tags, ["duration"])))
    set_if_changed_int(row, update, "SrcLength", parse_duration_to_seconds(first_tag(tags, ["duration"])))

    if energy:
        if not row.Commnt and not is_energy_comment(str(row.Commnt or "")):
            update["Commnt"] = energy
        elif not row.DeliveryComment and not is_energy_comment(str(row.DeliveryComment or "")):
            update["DeliveryComment"] = energy
        elif str(row.Commnt or "") != energy and is_energy_comment(str(row.Commnt or "")):
            update["Commnt"] = energy

    # Generic metadata sync for remaining ``Src*``/text fields if they exist in DB.
    explicit_fields = {
        "Title",
        "SrcTitle",
        "SrcArtistName",
        "ArtistID",
        "SrcAlbumName",
        "SrcAlbumArtistName",
        "SrcComposer",
        "Composer",
        "SrcGenre",
        "Genre",
        "Label",
        "SrcLabel",
        "Grouping",
        "Comment",
        "SrcComment",
        "Copyright",
        "Remixer",
        "SrcRemixer",
        "SrcYear",
        "SrcTrack",
        "Language",
        "MusicBrainzTrack",
        "ISRC",
        "TrackNo",
        "DiscNo",
        "ReleaseYear",
        "Length",
        "SrcLength",
        "BPM",
        "KeyID",
    }
    generic_fields_override: dict[str, list[str]] = {
        "SrcArtistName": ["artist", "artistname", "artist name", "srcartist", "srcartistname"],
        "SrcAlbumName": ["album", "albumname", "album name", "recording", "collection", "grouping"],
        "SrcAlbumArtistName": ["albumartist", "album artist", "album_artist", "albumartistname"],
        "SrcComposer": ["composer", "writer", "arranger"],
        "Composer": ["composer", "writer", "arranger", "orchestrator"],
        "SrcGenre": ["genre", "style", "mood", "style2"],
        "Genre": ["genre", "style", "mood"],
        "Label": ["label", "publisher", "organization"],
        "SrcLabel": ["label", "publisher", "organization", "copyright"],
        "Grouping": ["grouping", "group", "work", "ensemble"],
        "Comment": ["comment", "description", "commentary", "summary", "note", "notes"],
        "SrcComment": ["comment", "description", "commentary", "summary", "note", "notes"],
        "Copyright": ["copyright", "copyrights", "copyright holder", "license"],
        "Remixer": ["remixer", "remix", "remix artist", "remixed_by", "mixing"],
        "SrcRemixer": ["remixer", "remix", "remixed_by", "mixing"],
        "SrcYear": ["year", "date", "originaldate", "release_date", "releasedate"],
        "SrcTrack": ["track", "tracknumber", "track_no"],
        "Language": ["language", "lang", "shtitle"],
        "MusicBrainzTrack": ["musicbrainz_trackid", "musicbrainz track id", "mb_trackid", "musicbrainzid", "mbid"],
        "ISRC": ["isrc", "isrc_code", "isrcid"],
    }

    if hasattr(row, "__table__"):
        for field in row.__table__.columns.keys():
            if field not in explicit_fields and has_field(row, field):
                if field.startswith("Src") or field in {"Language", "Comment", "Grouping", "Copyright", "ISRC", "MusicBrainzTrack"}:
                    key_candidates = generic_fields_override.get(field, field_aliases(field))
                    value = pick_tag(tags, key_candidates)
                    if value:
                        if field in {"TrackNo", "DiscNo", "ReleaseYear"}:
                            set_if_changed_int(row, update, field, parse_track_or_disc(value) if field in {"TrackNo", "DiscNo"} else parse_year(value))
                        elif field in {"Length", "SrcLength"}:
                            set_if_changed_int(row, update, field, parse_duration_to_seconds(value))
                        else:
                            set_if_changed_text(row, update, field, value)

    row_artist_name = ""
    if row.ArtistID:
        row_artist_name = normalize_text_key(artist_by_id.get(str(row.ArtistID), ""))
    return update, row_artist_name


def collect_sources(root_pairs: list[tuple[str, Path]], extensions: set[str], warnings: list[str]) -> tuple[dict[str, Path], dict[str, list[str]], int]:
    files_by_path: dict[str, Path] = {}
    files_by_filename: dict[str, list[str]] = defaultdict(list)
    count = 0

    for role, root in root_pairs:
        if not root.exists():
            warnings.append(f"Audio root not found: {role}={root}")
            continue
        if not root.is_dir():
            warnings.append(f"Audio root is not a directory: {role}={root}")
            continue
        for current_root, dirs, filenames in os.walk(root):
            dirs[:] = [name for name in dirs if name not in {".git", "runtime", "reports"}]
            for filename in filenames:
                path = Path(current_root) / filename
                if path.suffix.casefold() not in extensions:
                    continue
                full = str(path.resolve())
                norm = normalize_windows_path(full)
                if norm in files_by_path:
                    continue
                files_by_path[norm] = path
                files_by_filename[path.name.casefold()].append(norm)
                count += 1
    return files_by_path, files_by_filename, count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Normalize existing Rekordbox rows from local audio tags.")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "config" / "dj-library.paths.json"))
    parser.add_argument("--master", required=True)
    parser.add_argument("--db-dir", default="")
    parser.add_argument("--source-root", action="append", default=[])
    parser.add_argument("--extension", action="append", default=[])
    parser.add_argument("--ffprobe", default="")
    parser.add_argument("--allow-filename-fallback", action="store_true")
    parser.add_argument("--reports-root", default="reports")
    parser.add_argument("--path-contains", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--commit-every", type=int, default=50, help="Commit every N updates while applying (0 disables batching).")
    parser.add_argument("--apply", action="store_true")
    return parser


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def commit_with_retry(db: Rekordbox6Database, max_attempts: int = 3, backoff: float = 0.75) -> None:
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            db.commit()
            return
        except Exception as exc:  # pragma: no cover - narrow compatibility with pyrekordbox/db wrapper behavior.
            last_error = exc
            if not isinstance(exc, OperationalError):
                raise
            if "database is locked" not in str(exc).lower() or attempt >= max_attempts:
                raise
            time.sleep(backoff * attempt)
    if last_error is not None:
        raise last_error


def run(args: argparse.Namespace) -> dict[str, Any]:
    repo_root = Path(args.repo_root).resolve()
    config = load_json(Path(args.config).resolve())
    reports_root = resolve_configured_path(args.reports_root, repo_root) or (repo_root / "reports")
    reports_root.mkdir(parents=True, exist_ok=True)
    extensions = {str(ext).casefold() for ext in (args.extension or config.get("audioExtensions", []))}
    if not extensions:
        raise ValueError("No audio extensions configured or provided.")

    source_pairs: list[tuple[str, Path]] = []
    for raw in args.source_root:
        resolved = resolve_configured_path(raw, repo_root)
        if resolved:
            source_pairs.append(("arg", resolved))
    if not source_pairs:
        for key in ("libraryRoot", "postProcessed_Library_RootRoot"):
            resolved = resolve_configured_path(config.get(key), repo_root)
            if resolved:
                source_pairs.append((key, resolved))
    if not source_pairs:
        raise ValueError("No source roots available. Provide --source-root or set libraryRoot/postProcessed_Library_RootRoot in config.")

    ffprobe = args.ffprobe or shutil.which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe is required in PATH or via --ffprobe.")

    warnings: list[str] = []
    files_by_path, files_by_filename, audio_files_scanned = collect_sources(source_pairs, extensions, warnings)

    stamp = now_stamp()
    csv_path = reports_root / f"rekordbox-metadata-normalize-{stamp}.csv"
    summary_json = reports_root / f"rekordbox-metadata-normalize-{stamp}.json"

    db_kwargs = {"path": Path(args.master).resolve()}
    if args.db_dir:
        db_kwargs["db_dir"] = Path(args.db_dir).resolve()

    db = Rekordbox6Database(**db_kwargs)
    report_rows: list[dict[str, Any]] = []
    probe_cache: dict[str, dict[str, str]] = {}
    probe_error_cache: dict[str, str] = {}
    applied = 0
    matched = 0
    would_update = 0
    unchanged = 0
    skipped_streaming = 0
    skipped_no_local = 0
    had_errors = False
    commit_every = max(0, int(args.commit_every))

    try:
        db.open()
        key_lookup, key_name_by_id = load_key_lookup(db)
        artist_cache = load_artist_cache(db)
        artist_by_id = {str(row.ID): str(row.Name or "") for row in db.query(DjmdArtist).all()}

        limit = max(0, int(args.limit))
        path_contains_norm = normalize_text_key(args.path_contains)
        for row in db.query(DjmdContent).all():
            if limit > 0 and matched >= limit:
                break
            if args.path_contains:
                path_match = normalize_text_key(row.FolderPath or "")
                if path_contains_norm not in path_match:
                    continue

            rb_path = row.FolderPath or ""
            if is_streaming_path(rb_path) or row.ServiceID:
                skipped_streaming += 1
                report_rows.append(
                    {
                        "status": "skipped_streaming",
                        "content_id": int(row.ID),
                        "path": rb_path,
                        "path_match_type": "",
                        "changed_fields": "",
                        "artist_name_before": artist_by_id.get(str(row.ArtistID), ""),
                        "artist_name_after": "",
                        "title_before": row.Title or "",
                        "title_after": "",
                        "note": "streaming_or_service_row",
                    }
                )
                continue

            normalized_path = normalize_windows_path(rb_path)
            source_path_norm = ""
            path_match_type = "exact"
            if normalized_path and normalized_path in files_by_path:
                source_path_norm = normalized_path
            elif rb_path and args.allow_filename_fallback:
                filename = ntpath.basename(path_from_rekordbox(rb_path)).casefold()
                candidates = files_by_filename.get(filename, [])
                if len(candidates) == 1:
                    source_path_norm = candidates[0]
                    path_match_type = "filename_fallback"
                else:
                    skipped_no_local += 1
                    report_rows.append(
                        {
                            "status": "skipped_no_unique_local_match",
                            "content_id": int(row.ID),
                            "path": rb_path,
                            "path_match_type": path_match_type,
                            "changed_fields": "",
                            "artist_name_before": artist_by_id.get(str(row.ArtistID), ""),
                            "artist_name_after": "",
                            "title_before": row.Title or "",
                            "title_after": "",
                            "note": "no_local_file_or_filename_ambiguous",
                        }
                    )
                    continue
            elif rb_path:
                skipped_no_local += 1
                report_rows.append(
                    {
                        "status": "no_local_file_match",
                        "content_id": int(row.ID),
                        "path": rb_path,
                        "path_match_type": "",
                        "changed_fields": "",
                        "artist_name_before": artist_by_id.get(str(row.ArtistID), ""),
                        "artist_name_after": "",
                        "title_before": row.Title or "",
                        "title_after": "",
                        "note": "no_local_file_in_scanned_roots",
                    }
                )
                continue

            matched += 1
            if not source_path_norm:
                continue

            source_path = files_by_path[source_path_norm]
            probe_error = probe_error_cache.get(source_path_norm)
            tags = probe_cache.get(source_path_norm)
            if tags is None and probe_error is None:
                tags, probe_error = probe_audio(source_path, ffprobe)
                if probe_error:
                    probe_error_cache[source_path_norm] = probe_error
                    probe_cache[source_path_norm] = {}
                else:
                    probe_cache[source_path_norm] = tags

            if not tags or probe_error:
                report_rows.append(
                    {
                        "status": "probe_error",
                        "content_id": int(row.ID),
                        "path": rb_path,
                        "path_match_type": path_match_type,
                        "changed_fields": "",
                        "artist_name_before": artist_by_id.get(str(row.ArtistID), ""),
                        "artist_name_after": "",
                        "title_before": row.Title or "",
                        "title_after": "",
                        "note": probe_error or "empty tags",
                    }
                )
                had_errors = True
                continue

            update_payload, row_artist_name = build_updates(
                row,
                tags,
                str(source_path),
                key_lookup,
                db,
                artist_cache,
                artist_by_id,
            )
            for artist_id in [str(row.ArtistID)] if str(row.ArtistID) else []:
                if artist_id not in artist_by_id:
                    artist_by_id[artist_id] = row_artist_name

            if not update_payload:
                unchanged += 1
                report_rows.append(
                    {
                        "status": "unchanged",
                        "content_id": int(row.ID),
                        "path": rb_path,
                        "path_match_type": path_match_type,
                        "changed_fields": "",
                        "artist_name_before": artist_by_id.get(str(row.ArtistID), ""),
                        "artist_name_after": "",
                        "title_before": row.Title or "",
                        "title_after": row.Title or "",
                        "note": "",
                    }
                )
                continue

            changed = record_changed_fields(update_payload)
            if args.apply:
                for field_name, new_value in update_payload.items():
                    setattr(row, field_name, new_value)
                applied += 1
                key_after = ""
                if "KeyID" in update_payload:
                    key_after = key_name_by_id.get(str(update_payload["KeyID"]), "")
                artist_id_after = str(update_payload.get("ArtistID") or row.ArtistID or "")
                report_rows.append(
                    {
                        "status": "updated",
                        "content_id": int(row.ID),
                        "path": rb_path,
                        "path_match_type": path_match_type,
                        "changed_fields": changed,
                        "artist_name_before": artist_by_id.get(str(row.ArtistID or ""), ""),
                        "artist_name_after": artist_by_id.get(artist_id_after, ""),
                        "title_before": row.Title or "",
                        "title_after": update_payload.get("Title") or row.Title or "",
                        "key_before": key_name_by_id.get(str(row.KeyID or ""), ""),
                        "key_after": key_after,
                        "bpm_before": int(row.BPM or 0),
                        "bpm_after": int(update_payload.get("BPM") or row.BPM or 0),
                        "track_no_before": int(row.TrackNo or 0),
                        "track_no_after": int(update_payload.get("TrackNo") or row.TrackNo or 0),
                        "disc_no_before": int(row.DiscNo or 0),
                        "disc_no_after": int(update_payload.get("DiscNo") or row.DiscNo or 0),
                        "release_year_before": int(row.ReleaseYear or 0),
                        "release_year_after": int(update_payload.get("ReleaseYear") or row.ReleaseYear or 0),
                        "src_title_before": row.SrcTitle or "",
                        "src_title_after": update_payload.get("SrcTitle") or row.SrcTitle or "",
                        "src_artist_before": row.SrcArtistName or "",
                        "src_artist_after": update_payload.get("SrcArtistName") or row.SrcArtistName or "",
                        "src_album_before": row.SrcAlbumName or "",
                        "src_album_after": update_payload.get("SrcAlbumName") or row.SrcAlbumName or "",
                        "commnt_before": row.Commnt or "",
                        "commnt_after": update_payload.get("Commnt") or row.Commnt or "",
                        "delivery_comment_before": row.DeliveryComment or "",
                        "delivery_comment_after": update_payload.get("DeliveryComment") or row.DeliveryComment or "",
                        "note": "",
                    }
                )
                if commit_every and applied % commit_every == 0:
                    commit_with_retry(db)
            else:
                would_update += 1
                report_rows.append(
                    {
                        "status": "would_update",
                        "content_id": int(row.ID),
                        "path": rb_path,
                        "path_match_type": path_match_type,
                        "changed_fields": changed,
                        "artist_name_before": artist_by_id.get(str(row.ArtistID), ""),
                        "artist_name_after": "",
                        "title_before": row.Title or "",
                        "title_after": update_payload.get("Title") or row.Title or "",
                        "note": "dry_run_only",
                    }
                )

            for field_name, new_value in update_payload.items():
                if field_name == "ArtistID":
                    artist_by_id[str(new_value)] = artist_by_id.get(str(new_value), artist_by_id.get(str(row.ArtistID), ""))
                if field_name == "KeyID":
                    key_name_by_id[str(new_value)] = key_name_by_id.get(str(new_value), "")

        if args.apply:
            commit_with_retry(db)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    write_csv(
        csv_path,
        report_rows,
        [
            "status",
            "content_id",
            "path",
            "path_match_type",
            "changed_fields",
            "note",
            "artist_name_before",
            "artist_name_after",
            "title_before",
            "title_after",
            "key_before",
            "key_after",
            "bpm_before",
            "bpm_after",
            "track_no_before",
            "track_no_after",
            "disc_no_before",
            "disc_no_after",
            "release_year_before",
            "release_year_after",
            "src_title_before",
            "src_title_after",
            "src_artist_before",
            "src_artist_after",
            "src_album_before",
            "src_album_after",
            "commnt_before",
            "commnt_after",
            "delivery_comment_before",
            "delivery_comment_after",
        ],
    )

    summary = {
        "generated_at": now_iso(),
        "mode": "apply" if args.apply else "plan",
        "success": not had_errors if args.apply else True,
        "master": str(Path(args.master).resolve()),
        "db_dir": str(Path(args.db_dir).resolve()) if args.db_dir else "",
        "audio_roots": [
            {"role": role, "path": str(root), "exists": root.exists()} for role, root in source_pairs
        ],
        "scanned_audio_files": audio_files_scanned,
        "content_rows": 0,
        "matched_content_rows": matched,
        "would_update_rows": would_update,
        "updated_rows": applied,
        "unchanged_rows": unchanged,
        "skipped_streaming": skipped_streaming,
        "skipped_no_local_match": skipped_no_local,
        "reports": {
            "summary_json": str(summary_json),
            "changes_csv": str(csv_path),
        },
        "ffprobe": ffprobe,
        "warnings": warnings,
        "errors": bool(had_errors),
    }
    # Re-query count for final report after commit/rollback.
    db_count = 0
    db = Rekordbox6Database(**db_kwargs)
    try:
        db.open()
        db_count = db.query(DjmdContent).count()
    finally:
        db.close()
    summary["content_rows"] = db_count

    summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
        print_json(result)
        return 0 if result.get("success", False) else 1
    except Exception as exc:  # noqa: BLE001 - CLI surfaces operator-facing error.
        print_json({"success": False, "generated_at": now_iso(), "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
