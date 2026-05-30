#!/usr/bin/env python3
"""Compare online TIDAL playlists and liked tracks with the local DJ library."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from requests_cache import EXPIRE_IMMEDIATELY
from rich.console import Console
from tiddl.cli.ctx import ContextObject


API_URL = "https://api.tidal.com/v1"
DEFAULT_AUDIO_EXTENSIONS = {".flac", ".wav", ".aiff", ".aif", ".m4a", ".mp3", ".ogg", ".opus"}


@dataclass(frozen=True)
class TidalTrack:
    source: str
    playlist: str
    id: str
    title: str
    version: str
    artist: str
    artists: tuple[str, ...]
    duration: int
    isrc: str
    url: str
    added_at: str

    @property
    def display_title(self) -> str:
        return f"{self.title} ({self.version})" if self.version else self.title


@dataclass(frozen=True)
class LocalTrack:
    source: str
    playlist: str
    path: str
    title: str
    artist: str
    norm_title: str
    norm_title_stripped: str
    artist_tokens: frozenset[str]


def parse_timestamp(value: str) -> dt.datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    if re.search(r"[+-]\d{4}$", raw):
        raw = raw[:-5] + raw[-5:-2] + ":" + raw[-2:]
    parsed = dt.datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def normalize_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().replace("&", " and ")
    text = re.sub(r"\b(feat|ft|featuring)\.?\b", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def compact_key(value: str) -> str:
    return normalize_text(value).replace(" ", "")


def strip_version(value: str) -> str:
    stripped = re.sub(r"\([^)]*\)|\[[^]]*\]", " ", value or "")
    stripped = re.sub(r"\b(original|extended|radio|club|edit|mix|remaster(?:ed)?)\b", " ", stripped, flags=re.I)
    return re.sub(r"\s+", " ", stripped).strip()


def artist_tokens(value: str) -> frozenset[str]:
    normalized = normalize_text(value)
    stop = {"and", "the", "feat", "ft", "featuring", "vs", "x"}
    return frozenset(token for token in normalized.split() if token and token not in stop)


def split_local_stem(stem: str) -> tuple[str, str]:
    clean = re.sub(r"_processed$", "", stem, flags=re.I).strip()
    if " - " in clean:
        artist, title = clean.split(" - ", 1)
        return artist.strip(), title.strip()
    return "", clean


def build_local_track(path: Path, root: Path, source: str) -> LocalTrack:
    try:
        rel = path.relative_to(root)
        playlist = rel.parts[0] if len(rel.parts) > 1 else root.name
    except ValueError:
        playlist = path.parent.name
    artist, title = split_local_stem(path.stem)
    return LocalTrack(
        source=source,
        playlist=playlist,
        path=str(path),
        title=title,
        artist=artist,
        norm_title=normalize_text(title),
        norm_title_stripped=normalize_text(strip_version(title)),
        artist_tokens=artist_tokens(artist),
    )


def scan_audio_files(paths_config: dict[str, Any], config: dict[str, Any]) -> list[LocalTrack]:
    extensions = {str(ext).casefold() for ext in paths_config.get("audioExtensions") or DEFAULT_AUDIO_EXTENSIONS}
    excluded = {str(name).casefold() for name in config.get("excludedLocalFolders") or []}
    tracks: list[LocalTrack] = []

    roots = [
        ("processed_library_root", Path(paths_config["postProcessed_Library_RootRoot"])),
        ("playlist", Path(paths_config["playlistRoot"])),
    ]
    library_root = Path(paths_config.get("libraryRoot") or "")
    if str(library_root) and library_root.exists():
        roots.append(("library", library_root))

    for source, root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.casefold() not in extensions:
                continue
            try:
                rel_parts = path.relative_to(root).parts
            except ValueError:
                rel_parts = ()
            if rel_parts and rel_parts[0].casefold() in excluded:
                continue
            tracks.append(build_local_track(path, root, source))
    return tracks


def score_match(tidal: TidalTrack, local: LocalTrack) -> tuple[int, str]:
    titles = tidal_title_keys(tidal)
    titles.discard("")
    score = 0
    reasons: list[str] = []
    local_keys = {
        local.norm_title,
        local.norm_title_stripped,
        compact_key(local.title),
        compact_key(strip_version(local.title)),
    }
    local_keys.discard("")

    if local.norm_title in titles:
        score += 55
        reasons.append("title")
    elif local.norm_title_stripped and local.norm_title_stripped in titles:
        score += 45
        reasons.append("title_stripped")
    elif local_keys & titles:
        score += 50
        reasons.append("title_compact")

    tidal_artist_tokens = artist_tokens(" ".join(tidal.artists) or tidal.artist)
    overlap = tidal_artist_tokens & local.artist_tokens
    if overlap:
        score += min(35, 15 + len(overlap) * 10)
        reasons.append("artist")
    elif not tidal_artist_tokens:
        score += 5

    return score, "+".join(reasons)


def tidal_title_keys(tidal: TidalTrack) -> set[str]:
    keys = {
        normalize_text(tidal.display_title),
        normalize_text(tidal.title),
        normalize_text(strip_version(tidal.display_title)),
        normalize_text(strip_version(tidal.title)),
        compact_key(tidal.display_title),
        compact_key(tidal.title),
        compact_key(strip_version(tidal.display_title)),
        compact_key(strip_version(tidal.title)),
    }
    keys.discard("")
    return keys


def build_match_index(locals_: list[LocalTrack]) -> dict[str, list[LocalTrack]]:
    index: dict[str, list[LocalTrack]] = {}
    for local in locals_:
        for key in {
            local.norm_title,
            local.norm_title_stripped,
            compact_key(local.title),
            compact_key(strip_version(local.title)),
        }:
            if key:
                index.setdefault(key, []).append(local)
    return index


def candidate_locals(tidal: TidalTrack, index: dict[str, list[LocalTrack]]) -> list[LocalTrack]:
    seen: set[str] = set()
    candidates: list[LocalTrack] = []
    for key in tidal_title_keys(tidal):
        for local in index.get(key, []):
            if local.path in seen:
                continue
            seen.add(local.path)
            candidates.append(local)
    return candidates


def best_match(tidal: TidalTrack, locals_: list[LocalTrack]) -> tuple[LocalTrack | None, int, str]:
    best: tuple[LocalTrack | None, int, str] = (None, 0, "")
    for local in locals_:
        score, reason = score_match(tidal, local)
        if score > best[1]:
            best = (local, score, reason)
    return best


def status_from_score(score: int) -> str:
    if score >= 70:
        return "solid"
    if score >= 55:
        return "review"
    return "missing"


def raw_get(api: Any, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    response = api.client.session.get(
        f"{API_URL}/{endpoint}",
        params=params,
        expire_after=EXPIRE_IMMEDIATELY,
    )
    if response.status_code == 401 and api.client.on_token_expiry:
        token = api.client.on_token_expiry()
        if token:
            api.client.token = token
            response = api.client.session.get(
                f"{API_URL}/{endpoint}",
                params=params,
                expire_after=EXPIRE_IMMEDIATELY,
            )
    response.raise_for_status()
    return response.json()


def fetch_all(api: Any, endpoint: str, limit: int = 100) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        payload = raw_get(
            api,
            endpoint,
            {"countryCode": api.country_code, "limit": limit, "offset": offset},
        )
        items = payload.get("items") or []
        rows.extend(items)
        total = int(payload.get("totalNumberOfItems") or len(rows))
        if not items or offset + len(items) >= total:
            break
        offset += len(items)
    return rows


def playlist_track_from_item(playlist_name: str, row: dict[str, Any]) -> TidalTrack | None:
    if row.get("type") != "track":
        return None
    item = row.get("item") or {}
    artists = tuple(str(a.get("name") or "") for a in item.get("artists") or [] if a.get("name"))
    artist = str((item.get("artist") or {}).get("name") or (artists[0] if artists else ""))
    return TidalTrack(
        source="playlist",
        playlist=playlist_name,
        id=str(item.get("id") or ""),
        title=str(item.get("title") or ""),
        version=str(item.get("version") or ""),
        artist=artist,
        artists=artists,
        duration=int(item.get("duration") or 0),
        isrc=str(item.get("isrc") or ""),
        url=str(item.get("url") or ""),
        added_at=str(item.get("dateAdded") or row.get("created") or ""),
    )


def favorite_track_from_item(row: dict[str, Any]) -> TidalTrack | None:
    item = row.get("item") or {}
    if not item.get("id"):
        return None
    artists = tuple(str(a.get("name") or "") for a in item.get("artists") or [] if a.get("name"))
    artist = str((item.get("artist") or {}).get("name") or (artists[0] if artists else ""))
    return TidalTrack(
        source="liked",
        playlist="TIDAL_LIKED",
        id=str(item.get("id") or ""),
        title=str(item.get("title") or ""),
        version=str(item.get("version") or ""),
        artist=artist,
        artists=artists,
        duration=int(item.get("duration") or 0),
        isrc=str(item.get("isrc") or ""),
        url=str(item.get("url") or ""),
        added_at=str(row.get("created") or ""),
    )


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def determine_since(config: dict[str, Any], state_path: Path, explicit_since: str) -> tuple[str, str]:
    floor = parse_timestamp(config["minLikedSince"])
    source = "config_min"
    since = floor
    if state_path.exists():
        state = load_json(state_path)
        state_since = state.get("lastSuccessfulImportAt") or state.get("lastLikedCreatedAt")
        if state_since:
            since = max(since, parse_timestamp(str(state_since)))
            source = "runtime_state"
    if explicit_since:
        since = max(floor, parse_timestamp(explicit_since))
        source = "argument"
    return since.isoformat(), source


def resolve_imported_playlists(
    online_playlists: list[dict[str, Any]],
    local_tracks: list[LocalTrack],
    config: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    imported_re = re.compile(config["importedPlaylistTitlePattern"], re.I)
    local_re = re.compile(config.get("localPlaylistNamePattern") or config["importedPlaylistTitlePattern"], re.I)
    excluded_res = [re.compile(pattern, re.I) for pattern in config.get("excludeLocalPlaylistNamePatterns") or []]
    aliases = {str(k): str(v) for k, v in (config.get("localPlaylistAliases") or {}).items()}
    alias_targets = {value.casefold() for value in aliases.values()}

    wanted_names: set[str] = set(aliases)
    for track in local_tracks:
        name = track.playlist
        if name.casefold() in alias_targets:
            continue
        if local_re.search(name) and not any(pattern.search(name) for pattern in excluded_res):
            wanted_names.add(name)
    for playlist in online_playlists:
        title = str(playlist.get("title") or "")
        if imported_re.search(title):
            wanted_names.add(title)

    by_title = {str(playlist.get("title") or "").casefold(): playlist for playlist in online_playlists}
    resolved: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for name in sorted(wanted_names, key=str.casefold):
        playlist = by_title.get(name.casefold())
        if playlist is None:
            unresolved.append(name)
            continue
        resolved.append(playlist)
    return resolved, unresolved


def local_matches_for_track(
    track: TidalTrack,
    local_index: dict[str, list[LocalTrack]],
    preferred_playlist: str | None = None,
) -> dict[str, Any]:
    candidates = candidate_locals(track, local_index)
    preferred_locals = [
        row for row in candidates if preferred_playlist and row.playlist.casefold() == preferred_playlist.casefold()
    ]
    preferred_match, preferred_score, preferred_reason = best_match(track, preferred_locals)
    global_match, global_score, global_reason = best_match(track, candidates)

    if preferred_match and preferred_score >= 55:
        bucket = f"{status_from_score(preferred_score)}_in_preferred_local"
        match = preferred_match
        score = preferred_score
        reason = preferred_reason
    elif global_match and global_score >= 55:
        bucket = f"{status_from_score(global_score)}_elsewhere_processed_library_root_or_dancing"
        match = global_match
        score = global_score
        reason = global_reason
    else:
        bucket = "missing_from_processed_library_root_and_dancing"
        match = None
        score = max(preferred_score, global_score)
        reason = ""

    return {
        "bucket": bucket,
        "score": score,
        "reason": reason,
        "path": match.path if match else "",
        "local_playlist": match.playlist if match else "",
        "local_source": match.source if match else "",
    }


def load_rekordbox_export(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    return load_json(path)


def rekordbox_playlist_locals(export: dict[str, Any], names: list[str]) -> tuple[list[LocalTrack], dict[str, Any]]:
    if not export:
        return [], {}
    by_name = {str(row.get("name") or "").casefold(): row for row in export.get("playlists") or []}
    chosen: dict[str, Any] = {}
    for name in names:
        row = by_name.get(name.casefold())
        if row:
            chosen = row
            break
    locals_: list[LocalTrack] = []
    for entry in chosen.get("entries") or []:
        path = Path(str(entry.get("folder_path") or ""))
        artist = str(entry.get("artist") or "")
        title = str(entry.get("title") or path.stem)
        parsed_artist, parsed_title = split_local_stem(path.stem)
        if not artist:
            artist = parsed_artist
        if not title:
            title = parsed_title
        locals_.append(
            LocalTrack(
                source="rekordbox",
                playlist=str(chosen.get("name") or ""),
                path=str(path),
                title=title,
                artist=artist,
                norm_title=normalize_text(title),
                norm_title_stripped=normalize_text(strip_version(title)),
                artist_tokens=artist_tokens(artist),
            )
        )
    return locals_, chosen


def run_downloads(
    resources: list[str],
    config: dict[str, Any],
    paths_config: dict[str, Any],
    tiddl_exe: Path,
    reports_root: Path,
    timestamp: str,
    output_folder: str,
    log_prefix: str,
    track_quality: str,
    dolby_atmos: str,
) -> list[dict[str, Any]]:
    if not resources:
        return []
    chunk_size = int(config.get("downloadChunkSize") or 60)
    output_template = f"{output_folder}/{{item.artist}} - {{item.title_version}}"
    env = os.environ.copy()
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1"})
    results: list[dict[str, Any]] = []
    for index in range(0, len(resources), chunk_size):
        chunk = resources[index : index + chunk_size]
        chunk_no = (index // chunk_size) + 1
        log_path = reports_root / f"{log_prefix}-{timestamp}-chunk{chunk_no:02d}.log"
        command = [
            str(tiddl_exe),
            "download",
            "--track-quality",
            track_quality,
            "--dolby-atmos",
            dolby_atmos,
            "--path",
            str(paths_config["playlistRoot"]),
            "--scan-path",
            str(paths_config["postProcessed_Library_RootRoot"]),
            "--threads-count",
            str(config.get("threadsCount") or 2),
            "--output",
            output_template,
            "url",
            *chunk,
        ]
        with log_path.open("w", encoding="utf-8", newline="") as handle:
            completed = subprocess.run(
                command,
                stdout=handle,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                check=False,
            )
        results.append(
            {
                "chunk": chunk_no,
                "resources": len(chunk),
                "exit_code": completed.returncode,
                "log_path": str(log_path),
            }
        )
    return results


def run(args: argparse.Namespace) -> dict[str, Any]:
    config_path = Path(args.config).resolve()
    paths_config_path = Path(args.paths_config).resolve()
    reports_root = Path(args.reports_root).resolve()
    reports_root.mkdir(parents=True, exist_ok=True)
    runtime_root = Path(args.runtime_root).resolve()
    runtime_root.mkdir(parents=True, exist_ok=True)
    state_path = Path(args.state_path).resolve()
    timestamp = args.timestamp or dt.datetime.now().strftime("%Y%m%d-%H%M%S")

    config = load_json(config_path)
    paths_config = load_json(paths_config_path)
    track_quality = args.track_quality or str(config.get("trackQuality") or "max")
    dolby_atmos = args.dolby_atmos or str(config.get("dolbyAtmos") or "none")
    if dolby_atmos not in {"none", "allow", "only"}:
        raise ValueError(f"Invalid Dolby Atmos filter: {dolby_atmos}")
    since_iso, since_source = determine_since(config, state_path, args.since or "")
    since_dt = parse_timestamp(since_iso)

    ctx = ContextObject(api_omit_cache=True, debug_path=None, console=Console())
    api = ctx.api
    api.get_session()

    online_playlists = fetch_all(api, f"users/{api.user_id}/playlists")
    local_tracks = scan_audio_files(paths_config, config)
    local_index = build_match_index(local_tracks)
    resolved_playlists, unresolved_local_playlist_names = resolve_imported_playlists(
        online_playlists, local_tracks, config
    )
    aliases = {str(k): str(v) for k, v in (config.get("localPlaylistAliases") or {}).items()}

    playlist_summary_rows: list[dict[str, Any]] = []
    comparison_rows: list[dict[str, Any]] = []
    tracklist_rows: list[dict[str, Any]] = []
    rekordbox_export = load_rekordbox_export(Path(args.rekordbox_export).resolve() if args.rekordbox_export else None)

    for playlist in resolved_playlists:
        title = str(playlist.get("title") or "")
        uuid = str(playlist.get("uuid") or "")
        preferred = aliases.get(title, title)
        raw_items = fetch_all(api, f"playlists/{uuid}/items")
        tracks = [track for row in raw_items if (track := playlist_track_from_item(title, row))]
        summary = {
            "tidal_playlist": title,
            "tidal_uuid": uuid,
            "tidal_tracks_reported": int(playlist.get("numberOfTracks") or 0),
            "tidal_tracks_fetched": len(tracks),
            "tidal_last_updated": playlist.get("lastUpdated") or "",
            "tidal_url": playlist.get("url") or "",
            "preferred_local_playlist": preferred,
            "solid_in_preferred_local": 0,
            "review_in_preferred_local": 0,
            "solid_elsewhere_processed_library_root_or_dancing": 0,
            "review_elsewhere_processed_library_root_or_dancing": 0,
            "missing_from_processed_library_root_and_dancing": 0,
            "rekordbox_playlist": "",
            "rekordbox_local_rows": 0,
            "rekordbox_streaming_rows_ignored": 0,
            "rekordbox_solid_matches": 0,
            "rekordbox_review_matches": 0,
            "rekordbox_missing": 0,
            "covered_by_local_or_rekordbox": 0,
            "missing_from_local_and_rekordbox": 0,
        }
        rb_locals, rb_playlist = rekordbox_playlist_locals(rekordbox_export, [title, preferred])
        if rb_playlist:
            summary["rekordbox_playlist"] = rb_playlist.get("name") or ""
            summary["rekordbox_local_rows"] = rb_playlist.get("local_rows") or 0
            summary["rekordbox_streaming_rows_ignored"] = rb_playlist.get("streaming_rows_ignored") or 0
        rb_index = build_match_index(rb_locals)

        for track in tracks:
            local_match = local_matches_for_track(track, local_index, preferred)
            summary[local_match["bucket"]] += 1
            rb_match = local_matches_for_track(track, rb_index, rb_playlist.get("name") if rb_playlist else None)
            rb_status = status_from_score(int(rb_match["score"]))
            if rb_status == "solid":
                summary["rekordbox_solid_matches"] += 1
            elif rb_status == "review":
                summary["rekordbox_review_matches"] += 1
            else:
                summary["rekordbox_missing"] += 1
            local_covered = local_match["bucket"] != "missing_from_processed_library_root_and_dancing"
            rekordbox_covered = rb_status in {"solid", "review"}
            if local_covered or rekordbox_covered:
                summary["covered_by_local_or_rekordbox"] += 1
            else:
                summary["missing_from_local_and_rekordbox"] += 1

            row = {
                "source": "playlist",
                "tidal_playlist": title,
                "tidal_track_id": track.id,
                "resource": f"track/{track.id}",
                "tidal_title": track.display_title,
                "tidal_artist": track.artist,
                "tidal_duration": track.duration,
                "tidal_isrc": track.isrc,
                "tidal_added_at": track.added_at,
                "tidal_url": track.url,
                "local_status": local_match["bucket"],
                "local_score": local_match["score"],
                "local_path": local_match["path"],
                "local_playlist": local_match["local_playlist"],
                "local_source": local_match["local_source"],
                "rekordbox_playlist": summary["rekordbox_playlist"],
                "rekordbox_status": rb_status,
                "rekordbox_score": rb_match["score"],
                "rekordbox_path": rb_match["path"],
                "covered_by_local_or_rekordbox": local_covered or rekordbox_covered,
                "playlist_missing_download_folder": title,
                "playlist_missing_download_requested": False,
                "local_status_after_playlist_download": "",
                "local_path_after_playlist_download": "",
                "covered_after_playlist_download": "",
            }
            comparison_rows.append(row)
            tracklist_rows.append(
                {
                    "tidal_playlist": title,
                    "tidal_uuid": uuid,
                    "tidal_track_id": track.id,
                    "tidal_title": track.display_title,
                    "tidal_artist": track.artist,
                    "tidal_duration": track.duration,
                    "tidal_isrc": track.isrc,
                    "tidal_added_at": track.added_at,
                    "tidal_url": track.url,
                }
            )
        playlist_summary_rows.append(summary)

    favorite_items = fetch_all(api, f"users/{api.user_id}/favorites/tracks")
    liked_tracks = [
        track
        for row in favorite_items
        if (track := favorite_track_from_item(row)) and track.added_at and parse_timestamp(track.added_at) >= since_dt
    ]
    liked_tracks.sort(key=lambda track: parse_timestamp(track.added_at))

    liked_rows: list[dict[str, Any]] = []
    download_resources: list[str] = []
    for track in liked_tracks:
        local_match = local_matches_for_track(track, local_index, config["likedOutputFolderName"])
        resource = f"track/{track.id}"
        if local_match["bucket"] == "missing_from_processed_library_root_and_dancing":
            download_resources.append(resource)
        liked_rows.append(
            {
                "liked_created_at": track.added_at,
                "tidal_track_id": track.id,
                "tidal_title": track.display_title,
                "tidal_artist": track.artist,
                "tidal_duration": track.duration,
                "tidal_isrc": track.isrc,
                "tidal_url": track.url,
                "resource": resource,
                "status_before_download": local_match["bucket"],
                "score_before_download": local_match["score"],
                "local_path_before_download": local_match["path"],
                "download_requested": local_match["bucket"] == "missing_from_processed_library_root_and_dancing",
                "status_after_download": "",
                "local_path_after_download": "",
            }
        )

    resources_path = reports_root / f"tidal-liked-download-resources-{timestamp}.txt"
    resources_path.write_text("\n".join(download_resources) + ("\n" if download_resources else ""), encoding="utf-8")

    download_results: list[dict[str, Any]] = []
    if args.apply_downloads and download_resources:
        download_results = run_downloads(
            download_resources,
            config,
            paths_config,
            Path(args.tiddl_exe).resolve(),
            reports_root,
            timestamp,
            config["likedOutputFolderName"],
            "tidal-liked-download",
            track_quality,
            dolby_atmos,
        )
        local_tracks_after = scan_audio_files(paths_config, config)
        local_index_after = build_match_index(local_tracks_after)
        for row in liked_rows:
            if not row["download_requested"]:
                row["status_after_download"] = row["status_before_download"]
                row["local_path_after_download"] = row["local_path_before_download"]
                continue
            track = next((item for item in liked_tracks if item.id == row["tidal_track_id"]), None)
            if not track:
                continue
            after = local_matches_for_track(track, local_index_after, config["likedOutputFolderName"])
            row["status_after_download"] = after["bucket"]
            row["local_path_after_download"] = after["path"]
        local_tracks = local_tracks_after
        local_index = local_index_after
    else:
        for row in liked_rows:
            row["status_after_download"] = row["status_before_download"]
            row["local_path_after_download"] = row["local_path_before_download"]

    report_prefix = f"tidal-online-library-check-{timestamp}"
    summary_csv = reports_root / f"{report_prefix}-summary.csv"
    comparison_csv = reports_root / f"{report_prefix}-comparison.csv"
    tracklists_csv = reports_root / f"{report_prefix}-tracklists.csv"
    liked_csv = reports_root / f"{report_prefix}-liked.csv"
    playlist_missing_resources = reports_root / f"{report_prefix}-playlist-missing-resources.txt"
    summary_json = reports_root / f"{report_prefix}.json"
    playlist_missing: list[str] = []
    seen_playlist_missing: set[str] = set()
    playlist_missing_by_folder: dict[str, list[str]] = {}
    for row in comparison_rows:
        if row.get("covered_by_local_or_rekordbox"):
            continue
        resource = str(row["resource"])
        if resource in seen_playlist_missing:
            continue
        seen_playlist_missing.add(resource)
        playlist_missing.append(resource)
        folder = str(row["playlist_missing_download_folder"])
        playlist_missing_by_folder.setdefault(folder, []).append(resource)
    playlist_missing_resources.write_text(
        "\n".join(playlist_missing) + ("\n" if playlist_missing else ""),
        encoding="utf-8",
    )

    playlist_download_results: list[dict[str, Any]] = []
    if args.apply_playlist_missing_downloads and playlist_missing_by_folder:
        for folder, resources in sorted(playlist_missing_by_folder.items(), key=lambda item: item[0].casefold()):
            playlist_download_results.extend(
                run_downloads(
                    resources,
                    config,
                    paths_config,
                    Path(args.tiddl_exe).resolve(),
                    reports_root,
                    timestamp,
                    folder,
                    f"tidal-playlist-missing-download-{normalize_text(folder).replace(' ', '-') or 'playlist'}",
                    track_quality,
                    dolby_atmos,
                )
            )
        local_tracks_after_playlist_download = scan_audio_files(paths_config, config)
        local_index_after_playlist_download = build_match_index(local_tracks_after_playlist_download)
        for row in comparison_rows:
            if row.get("covered_by_local_or_rekordbox"):
                row["local_status_after_playlist_download"] = row["local_status"]
                row["local_path_after_playlist_download"] = row["local_path"]
                row["covered_after_playlist_download"] = True
                continue
            track = TidalTrack(
                source="playlist",
                playlist=str(row["tidal_playlist"]),
                id=str(row["tidal_track_id"]),
                title=str(row["tidal_title"]),
                version="",
                artist=str(row["tidal_artist"]),
                artists=(str(row["tidal_artist"]),) if row.get("tidal_artist") else (),
                duration=int(row["tidal_duration"] or 0),
                isrc=str(row["tidal_isrc"] or ""),
                url=str(row["tidal_url"] or ""),
                added_at=str(row["tidal_added_at"] or ""),
            )
            after = local_matches_for_track(
                track,
                local_index_after_playlist_download,
                str(row["playlist_missing_download_folder"]),
            )
            row["playlist_missing_download_requested"] = str(row["resource"]) in seen_playlist_missing
            row["local_status_after_playlist_download"] = after["bucket"]
            row["local_path_after_playlist_download"] = after["path"]
            row["covered_after_playlist_download"] = after["bucket"] != "missing_from_processed_library_root_and_dancing"
        local_tracks = local_tracks_after_playlist_download
        local_index = local_index_after_playlist_download
        for summary in playlist_summary_rows:
            title = str(summary["tidal_playlist"])
            rows_for_playlist = [row for row in comparison_rows if row["tidal_playlist"] == title]
            summary["missing_from_local_and_rekordbox_after_playlist_download"] = sum(
                1 for row in rows_for_playlist if row.get("covered_after_playlist_download") is False
            )
    else:
        for row in comparison_rows:
            row["local_status_after_playlist_download"] = row["local_status"]
            row["local_path_after_playlist_download"] = row["local_path"]
            row["covered_after_playlist_download"] = row["covered_by_local_or_rekordbox"]
        for summary in playlist_summary_rows:
            summary["missing_from_local_and_rekordbox_after_playlist_download"] = summary[
                "missing_from_local_and_rekordbox"
            ]

    summary_fields = [
        "tidal_playlist",
        "tidal_uuid",
        "tidal_tracks_reported",
        "tidal_tracks_fetched",
        "tidal_last_updated",
        "tidal_url",
        "preferred_local_playlist",
        "solid_in_preferred_local",
        "review_in_preferred_local",
        "solid_elsewhere_processed_library_root_or_dancing",
        "review_elsewhere_processed_library_root_or_dancing",
        "missing_from_processed_library_root_and_dancing",
        "rekordbox_playlist",
        "rekordbox_local_rows",
        "rekordbox_streaming_rows_ignored",
        "rekordbox_solid_matches",
        "rekordbox_review_matches",
        "rekordbox_missing",
        "covered_by_local_or_rekordbox",
        "missing_from_local_and_rekordbox",
        "missing_from_local_and_rekordbox_after_playlist_download",
    ]
    comparison_fields = [
        "source",
        "tidal_playlist",
        "tidal_track_id",
        "resource",
        "tidal_title",
        "tidal_artist",
        "tidal_duration",
        "tidal_isrc",
        "tidal_added_at",
        "tidal_url",
        "local_status",
        "local_score",
        "local_path",
        "local_playlist",
        "local_source",
        "rekordbox_playlist",
        "rekordbox_status",
        "rekordbox_score",
        "rekordbox_path",
        "covered_by_local_or_rekordbox",
        "playlist_missing_download_folder",
        "playlist_missing_download_requested",
        "local_status_after_playlist_download",
        "local_path_after_playlist_download",
        "covered_after_playlist_download",
    ]
    tracklist_fields = [
        "tidal_playlist",
        "tidal_uuid",
        "tidal_track_id",
        "tidal_title",
        "tidal_artist",
        "tidal_duration",
        "tidal_isrc",
        "tidal_added_at",
        "tidal_url",
    ]
    liked_fields = [
        "liked_created_at",
        "tidal_track_id",
        "tidal_title",
        "tidal_artist",
        "tidal_duration",
        "tidal_isrc",
        "tidal_url",
        "resource",
        "status_before_download",
        "score_before_download",
        "local_path_before_download",
        "download_requested",
        "status_after_download",
        "local_path_after_download",
    ]

    write_csv(summary_csv, playlist_summary_rows, summary_fields)
    write_csv(comparison_csv, comparison_rows, comparison_fields)
    write_csv(tracklists_csv, tracklist_rows, tracklist_fields)
    write_csv(liked_csv, liked_rows, liked_fields)

    download_success = all(item["exit_code"] == 0 for item in download_results + playlist_download_results)
    payload: dict[str, Any] = {
        "success": download_success,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": (
            "apply_liked_and_playlist_missing_downloads"
            if args.apply_downloads and args.apply_playlist_missing_downloads
            else "apply_liked_downloads"
            if args.apply_downloads
            else "apply_playlist_missing_downloads"
            if args.apply_playlist_missing_downloads
            else "plan"
        ),
        "since": since_iso,
        "since_source": since_source,
        "liked_output_folder": str(Path(paths_config["playlistRoot"]) / config["likedOutputFolderName"]),
        "online_user_playlists_seen": len(online_playlists),
        "imported_playlists_checked": len(resolved_playlists),
        "unresolved_local_playlist_names": unresolved_local_playlist_names,
        "local_audio_files_indexed": len(local_tracks),
        "rekordbox_export": str(Path(args.rekordbox_export).resolve()) if args.rekordbox_export else "",
        "rekordbox_available": bool(rekordbox_export),
        "liked_tracks_seen_total": len(favorite_items),
        "liked_tracks_since": len(liked_tracks),
        "liked_missing_before_download": len(download_resources),
        "liked_download_chunks": download_results,
        "liked_missing_after_download": sum(
            1 for row in liked_rows if row["status_after_download"] == "missing_from_processed_library_root_and_dancing"
        ),
        "playlist_missing_before_download": len(playlist_missing),
        "playlist_missing_download_chunks": playlist_download_results,
        "playlist_missing_after_download": sum(
            int(summary["missing_from_local_and_rekordbox_after_playlist_download"])
            for summary in playlist_summary_rows
        ),
        "track_quality": track_quality,
        "dolby_atmos": dolby_atmos,
        "playlist_summary": playlist_summary_rows,
        "reports": {
            "summary_csv": str(summary_csv),
            "comparison_csv": str(comparison_csv),
            "tracklists_csv": str(tracklists_csv),
            "liked_csv": str(liked_csv),
            "liked_download_resources": str(resources_path),
            "playlist_missing_resources": str(playlist_missing_resources),
            "summary_json": str(summary_json),
        },
    }

    summary_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.update_state and payload["success"]:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        max_liked_created = max((track.added_at for track in liked_tracks), default=since_iso)
        state = {
            "version": 1,
            "lastSuccessfulImportAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            "lastLikedCreatedAt": max_liked_created,
            "lastSince": since_iso,
            "lastReportJson": str(summary_json),
            "lastLikedTracksSince": len(liked_tracks),
            "lastLikedMissingBeforeDownload": len(download_resources),
            "lastLikedMissingAfterDownload": payload["liked_missing_after_download"],
            "lastPlaylistMissingBeforeDownload": payload["playlist_missing_before_download"],
            "lastPlaylistMissingAfterDownload": payload["playlist_missing_after_download"],
            "lastTrackQuality": track_quality,
            "lastDolbyAtmos": dolby_atmos,
        }
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["state_updated"] = str(state_path)
    else:
        payload["state_updated"] = ""

    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--paths-config", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--reports-root", required=True)
    parser.add_argument("--state-path", required=True)
    parser.add_argument("--timestamp", default="")
    parser.add_argument("--since", default="")
    parser.add_argument("--rekordbox-export", default="")
    parser.add_argument("--tiddl-exe", required=True)
    parser.add_argument("--apply-downloads", action="store_true")
    parser.add_argument("--apply-playlist-missing-downloads", action="store_true")
    parser.add_argument("--track-quality", choices=["low", "normal", "high", "max"], default="")
    parser.add_argument("--dolby-atmos", choices=["none", "allow", "only"], default="")
    parser.add_argument("--update-state", action="store_true")
    return parser


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload = run(args)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload.get("success") else 1
    except Exception as exc:  # noqa: BLE001 - CLI reports operator-facing failure.
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
