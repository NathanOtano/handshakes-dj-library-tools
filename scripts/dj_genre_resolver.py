#!/usr/bin/env python3
"""Build genre candidates for AutoTagger failures without mutating audio files."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import difflib
import hashlib
import json
import os
import re
import base64
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "dj-genre-resolver-v1"
METADATA_POLICY = {
    "genre": {
        "target": "preserve_existing_musical_genre",
        "resolverAction": "propose_only",
        "audioWriteAllowed": False,
    },
    "comment": {
        "target": "numeric_mixed_in_key_energy",
        "examples": ["3", "6", "8"],
        "overwriteAllowedWithoutMigrationReport": False,
    },
    "categories": {
        "target": "rekordbox_mytag_labels",
        "examples": [
            "SET_Dancing",
            "SET_Easy",
            "SET_Soft",
            "SET_Sleep",
            "MOM_Depart",
            "MOM_Montee",
            "MOM_Pic",
            "MOM_Tenue",
            "MOM_Sortie",
            "VIBE_Afro",
            "VIBE_Soulful",
            "TEXT_BassHeavy",
            "TEXT_Vocals",
        ],
        "oldNewReplacementAllowedAfterCopyDbVerification": True,
    },
}
VERSION_TOKENS = {
    "club",
    "clean",
    "dirty",
    "dub",
    "edit",
    "extended",
    "instrumental",
    "live",
    "master",
    "mix",
    "original",
    "radio",
    "remaster",
    "remastered",
    "remix",
    "version",
    "vocal",
}


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def sha1_text(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8", errors="replace")).hexdigest()


def compact(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = value.encode("ascii", "ignore").decode("ascii")
    value = value.casefold()
    value = value.replace("&", " and ")
    value = re.sub(r"\b(feat|featuring|ft)\.?\b", " feat ", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def compact_key(value: str) -> str:
    return re.sub(r"\s+", "", compact(value))


def title_without_versions(value: str) -> tuple[str, str]:
    version_parts: list[str] = []

    def capture(match: re.Match[str]) -> str:
        content = match.group(1).strip()
        if any(token in compact(content).split() for token in VERSION_TOKENS):
            version_parts.append(content)
            return " "
        return match.group(0)

    cleaned = re.sub(r"[\[(]([^\])]+)[\])]", capture, value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_")
    return cleaned or value, "; ".join(version_parts)


def parse_track_name(path_text: str) -> dict[str, Any]:
    stem = Path(path_text).stem
    if " - " in stem:
        artist_raw, title_raw = stem.split(" - ", 1)
    else:
        artist_raw, title_raw = "", stem
    title_clean, version_token = title_without_versions(title_raw)
    artists = [
        part.strip()
        for part in re.split(r"\s*(?:,|&|\+|\bx\b|\bfeat\.?\b|\bft\.?\b)\s*", artist_raw, flags=re.IGNORECASE)
        if part.strip()
    ]
    return {
        "filename": Path(path_text).name,
        "extension": Path(path_text).suffix.lower(),
        "artist_raw": artist_raw,
        "title_raw": title_raw,
        "title_clean": title_clean,
        "version_token": version_token,
        "featured_artists": "; ".join(artists[1:]) if len(artists) > 1 else "",
        "artist_count": len(artists),
        "normalized_artist_key": compact_key(artist_raw),
        "normalized_title_key": compact_key(title_clean),
        "has_non_ascii": any(ord(char) > 127 for char in stem),
        "has_version_token": bool(version_token) or any(token in compact(title_raw).split() for token in VERSION_TOKENS),
    }


def read_latest_state(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_m3u_path_filter(path: Path | None) -> set[str] | None:
    if path is None:
        return None
    allowed: set[str] = set()
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            item = line.strip()
            if not item or item.startswith("#"):
                continue
            allowed.add(item)
    return allowed


def latest_scope_m3u(reports_root: Path, scope: str) -> Path | None:
    if scope == "missing-existing-genre":
        candidates = [
            path
            for path in reports_root.glob("genre-resolver-*-missing-existing-genre.m3u")
            if "-true-unresolved-missing-existing-genre.m3u" not in path.name
        ]
    elif scope == "true-unresolved-missing-existing-genre":
        candidates = list(reports_root.glob("genre-resolver-*-true-unresolved-missing-existing-genre.m3u"))
    else:
        return None
    if not candidates:
        raise FileNotFoundError(f"No M3U found for worklist scope: {scope}")
    return max(candidates, key=lambda item: item.stat().st_mtime)


def read_attempts(path: Path) -> dict[str, list[dict[str, Any]]]:
    attempts: dict[str, list[dict[str, Any]]] = {}
    if not path.exists():
        return attempts
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("action") != "taggingProgress":
                continue
            status = event.get("status") or {}
            inner = status.get("status") or {}
            path_text = inner.get("path")
            platform = status.get("platform")
            if not path_text or not platform:
                continue
            attempts.setdefault(path_text, []).append(
                {
                    "ts": event.get("ts", ""),
                    "platform": platform,
                    "progress": status.get("progress"),
                    "status": inner.get("status", ""),
                    "message": inner.get("message") or "",
                    "accuracy": inner.get("accuracy"),
                    "usedShazam": bool(inner.get("usedShazam")),
                }
            )
    return attempts


def build_worklist(
    latest_rows: list[dict[str, str]],
    attempts_by_path: dict[str, list[dict[str, Any]]],
    limit: int,
    path_filter: set[str] | None = None,
) -> list[dict[str, Any]]:
    worklist: list[dict[str, Any]] = []
    emitted_paths: set[str] = set()

    def add_item(path_text: str, row: dict[str, str]) -> None:
        if not path_text or path_text in emitted_paths:
            return
        attempts = attempts_by_path.get(path_text, [])
        ok_platforms = sorted({attempt["platform"] for attempt in attempts if str(attempt.get("status", "")).casefold() == "ok"})
        seen_platforms = sorted({attempt["platform"] for attempt in attempts})
        has_prior_ok = len(ok_platforms) > 0
        parsed = parse_track_name(path_text)
        item = {
            "schema": SCHEMA_VERSION,
            "file_id": sha1_text(path_text),
            "path": path_text,
            "path_hash": sha1_text(path_text),
            "root_folder": root_folder(path_text),
            **parsed,
            "latest_status": row.get("status", ""),
            "latest_platform": row.get("platform", ""),
            "latest_message": row.get("message", ""),
            "latest_accuracy": row.get("accuracy", ""),
            "latest_ts": row.get("ts", ""),
            "seen_platform_count": len(seen_platforms),
            "seen_platforms": ";".join(seen_platforms),
            "ok_platforms": ";".join(ok_platforms),
            "has_prior_ok": has_prior_ok,
            "prior_ok_platforms": ";".join(ok_platforms),
            "no_platform_ok": not has_prior_ok,
            "masked_prior_ok": has_prior_ok,
            "needs_spotify_retry": (row.get("platform", "").casefold() == "spotify"),
            "existing_genre": "",
            "existing_style": "",
            "existing_comment": "",
            "existing_composer": "",
            "has_existing_genre": False,
            "has_existing_style": False,
            "has_existing_comment": False,
            "mixed_in_key_energy_candidate": "",
            "comment_migration_status": "not_checked",
        }
        worklist.append(item)
        emitted_paths.add(path_text)

    for row in latest_rows:
        path_text = row.get("path", "")
        if path_filter is not None:
            if path_text not in path_filter:
                continue
        elif (row.get("status") or "").casefold() != "error":
            continue
        add_item(path_text, row)
        if limit > 0 and len(worklist) >= limit:
            break

    if path_filter is not None and (limit <= 0 or len(worklist) < limit):
        for path_text in sorted(path_filter):
            if path_text in emitted_paths:
                continue
            add_item(
                path_text,
                {
                    "path": path_text,
                    "status": "input_m3u",
                    "platform": "input_m3u",
                    "message": "Path supplied by InputM3uPath and absent from latest AutoTagger state.",
                    "accuracy": "",
                    "ts": "",
                },
            )
            if limit > 0 and len(worklist) >= limit:
                break
    return worklist


def count_m3u_paths(path: Path) -> tuple[int, set[str]]:
    if not path.exists():
        return 0, set()
    paths: set[str] = set()
    with path.open("r", encoding="utf-8-sig", errors="replace") as handle:
        for line in handle:
            item = line.strip()
            if not item or item.startswith("#"):
                continue
            paths.add(item)
    return len(paths), paths


def source_integrity(AutoTagger_run: Path, latest_rows: list[dict[str, str]], attempts_by_path: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    latest_paths = {row.get("path", "") for row in latest_rows if row.get("path")}
    latest_ok = {row.get("path", "") for row in latest_rows if row.get("status", "").casefold() == "ok"}
    latest_error = {row.get("path", "") for row in latest_rows if row.get("status", "").casefold() == "error"}
    any_prior_ok = {
        path
        for path, attempts in attempts_by_path.items()
        if any(str(attempt.get("status", "")).casefold() == "ok" for attempt in attempts)
    }
    never_ok = latest_paths - any_prior_ok
    masked_prior_ok = latest_error & any_prior_ok
    success_count, success_paths = count_m3u_paths(AutoTagger_run / "derived-success-latest-state.m3u")
    failed_count, failed_paths = count_m3u_paths(AutoTagger_run / "derived-failed-latest-state.m3u")
    platform_attempts: dict[str, dict[str, int]] = {}
    used_shazam = 0
    latest_ok_low_accuracy = 0
    for attempts in attempts_by_path.values():
        for attempt in attempts:
            platform = attempt.get("platform", "")
            status = attempt.get("status", "")
            platform_attempts.setdefault(platform, {"ok": 0, "error": 0, "other": 0})
            if status in {"ok", "error"}:
                platform_attempts[platform][status] += 1
            else:
                platform_attempts[platform]["other"] += 1
            if attempt.get("usedShazam"):
                used_shazam += 1
    for row in latest_rows:
        if row.get("status", "").casefold() != "ok":
            continue
        try:
            accuracy = float(row.get("accuracy") or 0)
        except ValueError:
            accuracy = 0
        if accuracy < 1:
            latest_ok_low_accuracy += 1
    latest_error_messages: dict[str, int] = {}
    masked_deezer_ok_spotify_400 = 0
    for row in latest_rows:
        if row.get("status", "").casefold() != "error":
            continue
        message = row.get("message") or "(empty)"
        latest_error_messages[message] = latest_error_messages.get(message, 0) + 1
        path_text = row.get("path", "")
        ok_platforms = {
            attempt.get("platform", "")
            for attempt in attempts_by_path.get(path_text, [])
            if str(attempt.get("status", "")).casefold() == "ok"
        }
        if (
            "Deezer" in ok_platforms
            and row.get("platform", "").casefold() == "spotify"
            and "400" in (row.get("message") or "")
        ):
            masked_deezer_ok_spotify_400 += 1
    return {
        "latestRows": len(latest_rows),
        "latestDistinctPaths": len(latest_paths),
        "latestOk": len(latest_ok),
        "latestError": len(latest_error),
        "successM3uPaths": success_count,
        "failedM3uPaths": failed_count,
        "m3uOverlap": len(success_paths & failed_paths),
        "successM3uMatchesCsv": success_paths == latest_ok if success_paths else None,
        "failedM3uMatchesCsv": failed_paths == latest_error if failed_paths else None,
        "attemptEventCount": sum(len(value) for value in attempts_by_path.values()),
        "attemptDistinctPaths": len(attempts_by_path),
        "anyPriorOk": len(any_prior_ok),
        "neverOk": len(never_ok),
        "maskedPriorOk": len(masked_prior_ok),
        "maskedDeezerOkThenSpotify400": masked_deezer_ok_spotify_400,
        "latestOkLowAccuracy": latest_ok_low_accuracy,
        "usedShazamEvents": used_shazam,
        "platformAttempts": dict(sorted(platform_attempts.items())),
        "latestErrorMessages": dict(sorted(latest_error_messages.items(), key=lambda pair: (-pair[1], pair[0]))[:20]),
    }


def root_folder(path_text: str) -> str:
    parts = re.split(r"[\\/]+", path_text)
    try:
        idx = next(i for i, part in enumerate(parts) if part.casefold() == "processed_library_root")
        return parts[idx + 1] if idx + 1 < len(parts) else ""
    except StopIteration:
        return parts[-2] if len(parts) >= 2 else ""


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str] | None = None) -> None:
    ensure_dir(path.parent)
    if fieldnames is None:
        fields: list[str] = []
        for row in rows:
            for key in row:
                if key not in fields:
                    fields.append(key)
        fieldnames = fields
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def write_m3u(path: Path, paths: list[str]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write("#EXTM3U\n")
        for item in paths:
            handle.write(item + "\n")


def summarize_worklist(worklist: list[dict[str, Any]], integrity: dict[str, Any] | None = None) -> dict[str, Any]:
    by_latest_platform: dict[str, int] = {}
    by_latest_message: dict[str, int] = {}
    by_root_folder: dict[str, int] = {}
    for item in worklist:
        by_latest_platform[item["latest_platform"]] = by_latest_platform.get(item["latest_platform"], 0) + 1
        message = item["latest_message"] or "(empty)"
        by_latest_message[message] = by_latest_message.get(message, 0) + 1
        root = item["root_folder"] or "(unknown)"
        by_root_folder[root] = by_root_folder.get(root, 0) + 1
    summary = {
        "totalWorklist": len(worklist),
        "latestErrorsWithPriorOk": sum(1 for item in worklist if not item["no_platform_ok"]),
        "trueUnresolvedNoPlatformOk": sum(1 for item in worklist if item["no_platform_ok"]),
        "multipleArtists": sum(1 for item in worklist if int(item["artist_count"]) > 1),
        "versionToken": sum(1 for item in worklist if item["has_version_token"]),
        "nonAscii": sum(1 for item in worklist if item["has_non_ascii"]),
        "byLatestPlatform": dict(sorted(by_latest_platform.items(), key=lambda pair: (-pair[1], pair[0]))),
        "byLatestMessage": dict(sorted(by_latest_message.items(), key=lambda pair: (-pair[1], pair[0]))[:20]),
        "byRootFolder": dict(sorted(by_root_folder.items(), key=lambda pair: (-pair[1], pair[0]))[:20]),
    }
    if integrity is not None:
        summary["sourceIntegrity"] = integrity
    return summary


def run_ffprobe_tags(ffprobe: str, path_text: str) -> dict[str, str]:
    if not path_text or not Path(path_text).exists():
        return {"genre": "", "style": "", "comment": "", "composer": ""}
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format_tags=genre,GENRE,style,STYLE,comment,COMMENT,composer,COMPOSER,TCOM",
        "-of",
        "json",
        "--",
        path_text,
    ]
    completed = subprocess.run(command, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=45, check=False)
    if completed.returncode != 0:
        return {"genre": "", "style": "", "comment": "", "composer": ""}
    try:
        data = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {"genre": "", "style": "", "comment": "", "composer": ""}
    tags = (data.get("format") or {}).get("tags") or {}
    return {
        "genre": first_nonempty(tags, ["genre", "GENRE"]),
        "style": first_nonempty(tags, ["style", "STYLE"]),
        "comment": first_nonempty(tags, ["comment", "COMMENT"]),
        "composer": first_nonempty(tags, ["composer", "COMPOSER", "TCOM"]),
    }


def first_nonempty(mapping: dict[str, Any], keys: list[str]) -> str:
    for key in keys:
        value = mapping.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def enrich_existing_tags(worklist: list[dict[str, Any]], ffprobe: str | None) -> None:
    if not ffprobe:
        return
    for item in worklist:
        tags = run_ffprobe_tags(ffprobe, item["path"])
        item["existing_genre"] = tags["genre"]
        item["existing_style"] = tags["style"]
        item["existing_comment"] = tags["comment"]
        item["existing_composer"] = tags["composer"]
        item["has_existing_genre"] = bool(tags["genre"])
        item["has_existing_style"] = bool(tags["style"])
        item["has_existing_comment"] = bool(tags["comment"])
        item["mixed_in_key_energy_candidate"] = energy_candidate(tags["comment"]) or energy_candidate(tags["composer"])
        item["comment_migration_status"] = comment_migration_status(tags["comment"])


def energy_candidate(value: str) -> str:
    clean = str(value or "").strip()
    if re.fullmatch(r"(?:10|[1-9])", clean):
        return clean
    compact_stars = re.sub(r"\s+", "", clean)
    if compact_stars and len(compact_stars) <= 10 and all(char in {"*", "★"} for char in compact_stars):
        return str(len(compact_stars))
    return ""


def comment_migration_status(comment: str) -> str:
    if not str(comment or "").strip():
        return "blank_safe_for_energy"
    if energy_candidate(comment):
        return "already_numeric_or_star_energy"
    return "occupied_review_before_overwrite"


class CachedHttp:
    def __init__(self, cache_dir: Path, user_agent: str, source_configs: dict[str, dict[str, Any]]) -> None:
        self.cache_dir = cache_dir
        self.user_agent = user_agent
        self.source_configs = source_configs
        self.last_request_at: dict[str, float] = {}
        ensure_dir(cache_dir)

    def get_json(self, source_id: str, url: str, headers: dict[str, str] | None = None) -> tuple[dict[str, Any] | list[Any] | None, dict[str, Any]]:
        cache_path = self.cache_dir / source_id / f"{sha1_text(url)}.json"
        ensure_dir(cache_path.parent)
        if cache_path.exists():
            try:
                with cache_path.open("r", encoding="utf-8") as handle:
                    cached = json.load(handle)
                return cached.get("body"), {"status": "cache", "url": url, "httpStatus": cached.get("httpStatus", 200)}
            except (json.JSONDecodeError, OSError):
                pass

        self.wait_for_rate(source_id)
        request_headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        request = urllib.request.Request(url, headers=request_headers)
        started = time.time()
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                body_bytes = response.read()
                text = body_bytes.decode("utf-8", errors="replace")
                body = json.loads(text)
                http_status = int(response.status)
        except urllib.error.HTTPError as exc:
            return None, {"status": "http_error", "url": url, "httpStatus": exc.code, "error": str(exc)}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            return None, {"status": "error", "url": url, "error": str(exc)}
        finally:
            self.last_request_at[source_id] = time.time()

        payload = {
            "schema": SCHEMA_VERSION,
            "source": source_id,
            "url": url,
            "httpStatus": http_status,
            "fetchedAt": utc_now(),
            "elapsedMs": int((time.time() - started) * 1000),
            "body": body,
        }
        with cache_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        return body, {"status": "fetched", "url": url, "httpStatus": http_status}

    def wait_for_rate(self, source_id: str) -> None:
        config = self.source_configs.get(source_id, {})
        per_minute = float(config.get("rateLimitPerMinute") or 60)
        if per_minute <= 0:
            return
        min_interval = 60.0 / per_minute
        previous = self.last_request_at.get(source_id)
        if previous is None:
            return
        delay = min_interval - (time.time() - previous)
        if delay > 0:
            time.sleep(delay)


def source_config_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {source["id"]: source for source in config.get("sources", [])}


def enabled_sources(config: dict[str, Any], requested: list[str]) -> list[str]:
    sources = source_config_map(config)
    if requested:
        return [source for source in requested if source in sources]
    selected: list[str] = []
    for source in config.get("sources", []):
        if not source.get("enabled"):
            continue
        env_var = source.get("envVar") or ""
        if env_var and not os.environ.get(env_var):
            continue
        selected.append(source["id"])
    return selected


def similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, compact_key(left), compact_key(right)).ratio()


def split_artist_parts(value: str) -> list[str]:
    return [
        part.strip()
        for part in re.split(r"\s*(?:,|&|\+|\bx\b|\bfeat\.?\b|\bft\.?\b)\s*", value or "", flags=re.IGNORECASE)
        if part.strip()
    ]


def artist_similarity(raw_artist: str, matched_artist: str) -> float:
    scores = [similarity(raw_artist, matched_artist)]
    for part in split_artist_parts(raw_artist):
        scores.append(similarity(part, matched_artist))
    for part in split_artist_parts(matched_artist):
        scores.append(similarity(raw_artist, part))
    return max(scores) if scores else 0.0


def normalize_genres(raw_values: list[str], source_config: dict[str, Any]) -> list[str]:
    aliases = {compact(key): value for key, value in (source_config.get("genreAliases") or {}).items()}
    taxonomy = {compact(value): value for value in source_config.get("localTaxonomy", [])}
    normalized: list[str] = []
    for raw in raw_values:
        for part in re.split(r"[,;/|]+", raw or ""):
            item = compact(part)
            if not item:
                continue
            value = aliases.get(item) or taxonomy.get(item) or part.strip()
            if value and value not in normalized:
                normalized.append(value)
    return normalized


def candidate(
    item: dict[str, Any],
    source: str,
    matched_artist: str,
    matched_title: str,
    raw_genres: list[str],
    raw_styles: list[str],
    evidence: str,
    source_status: str,
    source_config: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    artist_score = artist_similarity(item["artist_raw"], matched_artist)
    title_score = max(similarity(item["title_clean"], matched_title), similarity(item["title_raw"], matched_title))
    identity_score = round((artist_score + title_score) / 2.0, 4)
    normalized = normalize_genres(raw_genres + raw_styles, source_config)
    source_weight = {
        "musicbrainz": 0.78,
        "listenbrainz": 0.82,
        "lastfm": 0.8,
        "discogs": 0.86,
        "spotify_direct": 0.72,
        "itunes": 0.58,
        "deezer": 0.55,
        "theaudiodb": 0.65,
        "existing_tag": 0.9,
    }.get(source, 0.5)
    confidence_score = round(min(1.0, identity_score * source_weight + (0.08 if normalized else 0.0)), 4)
    if confidence_score >= 0.78 and normalized:
        label = "high"
    elif confidence_score >= 0.58 and normalized:
        label = "medium"
    elif normalized:
        label = "low"
    else:
        label = "identity_only"
    payload = {
        "schema": SCHEMA_VERSION,
        "candidate_id": sha1_text(f"{item['file_id']}|{source}|{matched_artist}|{matched_title}|{evidence}"),
        "file_id": item["file_id"],
        "path": item["path"],
        "source_type": "official_api" if source != "existing_tag" else "local_tag",
        "source_name": source,
        "source_status": source_status,
        "matched_artist": matched_artist,
        "matched_title": matched_title,
        "artist_score": round(artist_score, 4),
        "title_score": round(title_score, 4),
        "identity_score": identity_score,
        "raw_genres": ";".join(raw_genres),
        "raw_styles": ";".join(raw_styles),
        "normalized_genres": ";".join(normalized),
        "normalized_styles": "",
        "evidence_id_or_url": evidence,
        "candidate_confidence": confidence_score,
        "candidate_confidence_label": label,
    }
    if extra:
        payload.update(extra)
    return payload


def resolve_itunes(item: dict[str, Any], http: CachedHttp, source_config: dict[str, Any], max_candidates: int) -> list[dict[str, Any]]:
    term = f"{item['artist_raw']} {item['title_clean']}".strip()
    params = {"term": term, "media": "music", "entity": "song", "limit": str(max_candidates)}
    url = "https://itunes.apple.com/search?" + urllib.parse.urlencode(params)
    data, meta = http.get_json("itunes", url)
    candidates: list[dict[str, Any]] = []
    if not isinstance(data, dict):
        return candidates
    for result in data.get("results", [])[:max_candidates]:
        genre = result.get("primaryGenreName") or ""
        candidates.append(
            candidate(
                item,
                "itunes",
                result.get("artistName", ""),
                result.get("trackName", ""),
                [genre] if genre else [],
                [],
                result.get("trackViewUrl") or meta.get("url", ""),
                meta.get("status", ""),
                source_config,
                {"collection": result.get("collectionName", ""), "release_year": str(result.get("releaseDate", ""))[:4]},
            )
        )
    return candidates


def resolve_deezer(item: dict[str, Any], http: CachedHttp, source_config: dict[str, Any], max_candidates: int) -> list[dict[str, Any]]:
    query = f'artist:"{item["artist_raw"]}" track:"{item["title_clean"]}"'
    url = "https://api.deezer.com/search/track?" + urllib.parse.urlencode({"q": query, "limit": str(max_candidates)})
    data, meta = http.get_json("deezer", url)
    candidates: list[dict[str, Any]] = []
    if not isinstance(data, dict):
        return candidates
    for result in data.get("data", [])[:max_candidates]:
        artist = (result.get("artist") or {}).get("name", "")
        title = result.get("title_short") or result.get("title") or ""
        genres: list[str] = []
        album_id = (result.get("album") or {}).get("id")
        if album_id:
            album_url = f"https://api.deezer.com/album/{album_id}"
            album_data, _ = http.get_json("deezer", album_url)
            if isinstance(album_data, dict):
                genre_data = ((album_data.get("genres") or {}).get("data") or [])
                genres = [entry.get("name", "") for entry in genre_data if entry.get("name")]
        candidates.append(
            candidate(
                item,
                "deezer",
                artist,
                title,
                genres,
                [],
                result.get("link") or meta.get("url", ""),
                meta.get("status", ""),
                source_config,
                {"album": (result.get("album") or {}).get("title", ""), "duration_s": result.get("duration")},
            )
        )
    return candidates


def spotify_access_token(runtime_root: Path) -> str:
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        client_id, client_secret = spotify_credentials_from_AutoTagger_config()
    if not client_id or not client_secret:
        return ""
    token_cache = runtime_root / "spotify-token-cache.json"
    now = time.time()
    if token_cache.exists():
        try:
            cached = load_json(token_cache)
            if cached.get("access_token") and float(cached.get("expires_at", 0)) > now + 60:
                return str(cached["access_token"])
        except (ValueError, OSError):
            pass
    auth = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("utf-8"),
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - official HTTPS API.
        payload = json.loads(response.read().decode("utf-8"))
    token = str(payload.get("access_token") or "")
    if not token:
        return ""
    ensure_dir(token_cache.parent)
    with token_cache.open("w", encoding="utf-8") as handle:
        json.dump(
            {
                "access_token": token,
                "token_type": payload.get("token_type", "Bearer"),
                "expires_at": now + int(payload.get("expires_in") or 3600),
                "storedAt": utc_now(),
                "note": "Bearer token only; client id and secret are never cached here.",
            },
            handle,
            ensure_ascii=False,
            indent=2,
        )
    return token


def spotify_credentials_from_AutoTagger_config() -> tuple[str, str]:
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return "", ""
    settings_path = Path(appdata) / "AutoTagger" / "AutoTagger" / "config" / "settings.json"
    if not settings_path.exists():
        return "", ""
    try:
        settings = load_json(settings_path)
    except (ValueError, OSError):
        return "", ""

    def get_path(root: Any, parts: list[str]) -> str:
        cursor = root
        for part in parts:
            if not isinstance(cursor, dict) or part not in cursor:
                return ""
            cursor = cursor[part]
        return cursor.strip() if isinstance(cursor, str) else ""

    paths = [
        ["ui", "autoTaggerConfig", "spotify"],
        ["ui", "autoTaggerProfiles", "0", "config", "spotify"],
        ["ui", "audioFeatures"],
    ]

    for path_parts in paths:
        cursor: Any = settings
        for part in path_parts:
            if isinstance(cursor, list) and part.isdigit():
                index = int(part)
                cursor = cursor[index] if index < len(cursor) else {}
            elif isinstance(cursor, dict):
                cursor = cursor.get(part, {})
            else:
                cursor = {}
        if not isinstance(cursor, dict):
            continue
        client_id = get_path(cursor, ["clientId"]) or get_path(cursor, ["spotifyClientId"])
        client_secret = get_path(cursor, ["clientSecret"]) or get_path(cursor, ["spotifyClientSecret"])
        if client_id and client_secret:
            return client_id, client_secret
    return "", ""


def spotify_query_variants(item: dict[str, Any]) -> list[tuple[str, str]]:
    artist_raw = item["artist_raw"]
    title_clean = item["title_clean"]
    title_raw = item["title_raw"]
    lead_artist = artist_raw.split(",")[0].split("&")[0].strip()
    variants = [
        ("field_track_artist", f'track:"{title_clean}" artist:"{artist_raw}"'),
        ("field_track_lead_artist", f'track:"{title_clean}" artist:"{lead_artist}"'),
        ("plain_clean", f"{artist_raw} {title_clean}"),
        ("plain_lead_artist", f"{lead_artist} {title_clean}"),
        ("plain_raw_title", f"{artist_raw} {title_raw}"),
    ]
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for name, query in variants:
        query = re.sub(r"\s+", " ", query).strip()
        if not query or query in seen:
            continue
        seen.add(query)
        out.append((name, query))
    return out


def spotify_artist_genres(
    artist_ids: list[str],
    http: CachedHttp,
    token: str,
) -> dict[str, list[str]]:
    genres_by_artist: dict[str, list[str]] = {}
    unique_ids = []
    for artist_id in artist_ids:
        if artist_id and artist_id not in unique_ids:
            unique_ids.append(artist_id)
    for start in range(0, len(unique_ids), 50):
        chunk = unique_ids[start : start + 50]
        url = "https://api.spotify.com/v1/artists?" + urllib.parse.urlencode({"ids": ",".join(chunk)})
        data, _ = http.get_json("spotify_direct", url, headers={"Authorization": f"Bearer {token}"})
        if not isinstance(data, dict):
            continue
        for artist in data.get("artists", []) or []:
            if not isinstance(artist, dict):
                continue
            artist_id = str(artist.get("id") or "")
            if artist_id:
                genres_by_artist[artist_id] = [str(value) for value in artist.get("genres", []) if value]
    return genres_by_artist


def resolve_spotify_direct(
    item: dict[str, Any],
    http: CachedHttp,
    source_config: dict[str, Any],
    max_candidates: int,
    runtime_root: Path,
) -> list[dict[str, Any]]:
    token = spotify_access_token(runtime_root)
    if not token:
        return []
    market = os.environ.get("SPOTIFY_MARKET", "FR").strip() or "FR"
    candidates: list[dict[str, Any]] = []
    seen_track_ids: set[str] = set()
    for strategy, query in spotify_query_variants(item):
        params = {"q": query, "type": "track", "limit": str(max_candidates), "market": market}
        url = "https://api.spotify.com/v1/search?" + urllib.parse.urlencode(params)
        data, meta = http.get_json("spotify_direct", url, headers={"Authorization": f"Bearer {token}"})
        if not isinstance(data, dict):
            continue
        tracks = ((data.get("tracks") or {}).get("items") or [])[:max_candidates]
        artist_ids_for_batch: list[str] = []
        for result in tracks:
            for entry in result.get("artists") or []:
                artist_id = entry.get("id", "")
                if artist_id:
                    artist_ids_for_batch.append(artist_id)
        genres_by_artist = spotify_artist_genres(artist_ids_for_batch, http, token)
        for result in tracks:
            track_id = str(result.get("id") or "")
            if not track_id or track_id in seen_track_ids:
                continue
            seen_track_ids.add(track_id)
            artists = result.get("artists") or []
            artist_names = [entry.get("name", "") for entry in artists if entry.get("name")]
            artist_ids = [entry.get("id", "") for entry in artists if entry.get("id")]
            artist_genres: list[str] = []
            for artist_id in artist_ids:
                for genre in genres_by_artist.get(artist_id, []):
                    if genre not in artist_genres:
                        artist_genres.append(genre)
            album = result.get("album") or {}
            external_urls = result.get("external_urls") or {}
            external_ids = result.get("external_ids") or {}
            candidates.append(
                candidate(
                    item,
                    "spotify_direct",
                    ", ".join(artist_names),
                    result.get("name", ""),
                    artist_genres[:12],
                    [],
                    external_urls.get("spotify") or meta.get("url", ""),
                    meta.get("status", ""),
                    source_config,
                    {
                        "spotify_id": track_id,
                        "spotify_query_strategy": strategy,
                        "spotify_query": query,
                        "spotify_market": market,
                        "album": album.get("name", ""),
                        "release_year": str(album.get("release_date", ""))[:4],
                        "popularity": result.get("popularity"),
                        "isrc": external_ids.get("isrc", ""),
                        "source_policy_note": "Spotify artist genres are catalog metadata hints; resolver proposes only and does not train models or write audio tags.",
                    },
                )
            )
        if len(candidates) >= max_candidates:
            break
    return candidates[:max_candidates]


def resolve_musicbrainz(item: dict[str, Any], http: CachedHttp, source_config: dict[str, Any], max_candidates: int) -> list[dict[str, Any]]:
    query = f'artist:"{item["artist_raw"]}" AND recording:"{item["title_clean"]}"'
    url = "https://musicbrainz.org/ws/2/recording?" + urllib.parse.urlencode({"query": query, "fmt": "json", "limit": str(max_candidates)})
    data, meta = http.get_json("musicbrainz", url)
    candidates: list[dict[str, Any]] = []
    if not isinstance(data, dict):
        return candidates
    for result in data.get("recordings", [])[:max_candidates]:
        title = result.get("title", "")
        artist = " ".join(
            credit.get("name", "")
            for credit in result.get("artist-credit", [])
            if isinstance(credit, dict) and credit.get("name")
        ).strip()
        mbid = result.get("id", "")
        genres: list[str] = []
        tags: list[str] = []
        if mbid:
            lookup_url = f"https://musicbrainz.org/ws/2/recording/{mbid}?" + urllib.parse.urlencode({"inc": "genres+tags+artist-credits+isrcs", "fmt": "json"})
            lookup_data, _ = http.get_json("musicbrainz", lookup_url)
            if isinstance(lookup_data, dict):
                genres = [entry.get("name", "") for entry in lookup_data.get("genres", []) if entry.get("name")]
                tags = [entry.get("name", "") for entry in lookup_data.get("tags", []) if entry.get("name")]
        candidates.append(
            candidate(
                item,
                "musicbrainz",
                artist,
                title,
                genres,
                tags[:6],
                f"https://musicbrainz.org/recording/{mbid}" if mbid else meta.get("url", ""),
                meta.get("status", ""),
                source_config,
                {"recording_mbid": mbid, "musicbrainz_score": result.get("score")},
            )
        )
        if mbid:
            candidates.extend(resolve_listenbrainz_by_mbid(item, http, source_config, mbid, artist, title))
    return candidates


def resolve_listenbrainz_by_mbid(
    item: dict[str, Any],
    http: CachedHttp,
    source_config: dict[str, Any],
    mbid: str,
    artist: str,
    title: str,
) -> list[dict[str, Any]]:
    params = {"recording_mbids": mbid, "inc": "artist tag release"}
    url = "https://api.listenbrainz.org/1/metadata/recording/?" + urllib.parse.urlencode(params)
    data, meta = http.get_json("listenbrainz", url)
    if not isinstance(data, dict):
        return []
    payload = data.get(mbid) or {}
    tag_payload = payload.get("tag") or {}
    raw_tags: list[str] = []
    for bucket in ("recording", "release_group", "artist"):
        for entry in tag_payload.get(bucket, []) or []:
            tag = entry.get("tag")
            if tag:
                raw_tags.append(tag)
    if not raw_tags:
        return []
    return [
        candidate(
            item,
            "listenbrainz",
            artist,
            title,
            raw_tags[:12],
            [],
            f"listenbrainz:recording:{mbid}",
            meta.get("status", ""),
            source_config,
            {"recording_mbid": mbid},
        )
    ]


def resolve_lastfm(item: dict[str, Any], http: CachedHttp, source_config: dict[str, Any], max_candidates: int) -> list[dict[str, Any]]:
    api_key = os.environ.get("LASTFM_API_KEY", "")
    if not api_key:
        return []
    params = {
        "method": "track.getTopTags",
        "artist": item["artist_raw"],
        "track": item["title_clean"],
        "api_key": api_key,
        "format": "json",
    }
    url = "https://ws.audioscrobbler.com/2.0/?" + urllib.parse.urlencode(params)
    data, meta = http.get_json("lastfm", url)
    if not isinstance(data, dict):
        return []
    tags = (data.get("toptags") or {}).get("tag") or []
    raw = [entry.get("name", "") for entry in tags[: max_candidates * 2] if entry.get("name")]
    if not raw:
        return []
    return [
        candidate(
            item,
            "lastfm",
            item["artist_raw"],
            item["title_clean"],
            raw,
            [],
            meta.get("url", ""),
            meta.get("status", ""),
            source_config,
        )
    ]


def resolve_discogs(item: dict[str, Any], http: CachedHttp, source_config: dict[str, Any], max_candidates: int) -> list[dict[str, Any]]:
    token = os.environ.get("DISCOGS_TOKEN", "")
    if not token:
        return []
    query = f"{item['artist_raw']} {item['title_clean']}".strip()
    params = {"q": query, "type": "release", "per_page": str(max_candidates)}
    url = "https://api.discogs.com/database/search?" + urllib.parse.urlencode(params)
    data, meta = http.get_json("discogs", url, headers={"Authorization": f"Discogs token={token}"})
    candidates: list[dict[str, Any]] = []
    if not isinstance(data, dict):
        return candidates
    for result in data.get("results", [])[:max_candidates]:
        title = result.get("title", "")
        artist, track_title = split_discogs_title(title)
        candidates.append(
            candidate(
                item,
                "discogs",
                artist,
                track_title,
                result.get("genre") or [],
                result.get("style") or [],
                result.get("uri") or meta.get("url", ""),
                meta.get("status", ""),
                source_config,
                {"year": result.get("year"), "country": result.get("country")},
            )
        )
    return candidates


def split_discogs_title(value: str) -> tuple[str, str]:
    if " - " in value:
        return tuple(value.split(" - ", 1))  # type: ignore[return-value]
    return "", value


def resolve_theaudiodb(item: dict[str, Any], http: CachedHttp, source_config: dict[str, Any], max_candidates: int) -> list[dict[str, Any]]:
    api_key = os.environ.get("THEAUDIODB_API_KEY", "")
    if not api_key:
        return []
    base = f"https://www.theaudiodb.com/api/v1/json/{urllib.parse.quote(api_key)}/searchtrack.php"
    url = base + "?" + urllib.parse.urlencode({"s": item["artist_raw"], "t": item["title_clean"]})
    data, meta = http.get_json("theaudiodb", url)
    candidates: list[dict[str, Any]] = []
    if not isinstance(data, dict):
        return candidates
    for result in (data.get("track") or [])[:max_candidates]:
        raw_genres = [result.get("strGenre") or ""]
        raw_styles = [result.get("strStyle") or "", result.get("strMood") or ""]
        candidates.append(
            candidate(
                item,
                "theaudiodb",
                result.get("strArtist", ""),
                result.get("strTrack", ""),
                [value for value in raw_genres if value],
                [value for value in raw_styles if value],
                meta.get("url", ""),
                meta.get("status", ""),
                source_config,
                {"album": result.get("strAlbum", ""), "year": result.get("intYearReleased", "")},
            )
        )
    return candidates


def resolve_candidates(
    worklist: list[dict[str, Any]],
    source_config: dict[str, Any],
    runtime_root: Path,
    requested_sources: list[str],
    max_candidates: int,
) -> list[dict[str, Any]]:
    source_map = source_config_map(source_config)
    sources = enabled_sources(source_config, requested_sources)
    http = CachedHttp(runtime_root / "api-cache", source_config.get("userAgent", "DJ_Library genre resolver"), source_map)
    all_candidates: list[dict[str, Any]] = []
    for item in worklist:
        if item.get("has_existing_genre"):
            all_candidates.append(
                candidate(
                    item,
                    "existing_tag",
                    item["artist_raw"],
                    item["title_clean"],
                    [item.get("existing_genre", "")],
                    [item.get("existing_style", "")],
                    "local-audio-tag",
                    "local",
                    source_config,
                )
            )
        for source in sources:
            try:
                if source == "itunes":
                    all_candidates.extend(resolve_itunes(item, http, source_config, max_candidates))
                elif source == "deezer":
                    all_candidates.extend(resolve_deezer(item, http, source_config, max_candidates))
                elif source == "spotify_direct":
                    all_candidates.extend(resolve_spotify_direct(item, http, source_config, max_candidates, runtime_root))
                elif source == "musicbrainz":
                    all_candidates.extend(resolve_musicbrainz(item, http, source_config, max_candidates))
                elif source == "lastfm":
                    all_candidates.extend(resolve_lastfm(item, http, source_config, max_candidates))
                elif source == "discogs":
                    all_candidates.extend(resolve_discogs(item, http, source_config, max_candidates))
                elif source == "theaudiodb":
                    all_candidates.extend(resolve_theaudiodb(item, http, source_config, max_candidates))
            except Exception as exc:  # noqa: BLE001 - report source failure per item.
                all_candidates.append(
                    {
                        "schema": SCHEMA_VERSION,
                        "candidate_id": sha1_text(f"{item['file_id']}|{source}|error|{exc}"),
                        "file_id": item["file_id"],
                        "path": item["path"],
                        "source_type": "official_api",
                        "source_name": source,
                        "source_status": "resolver_error",
                        "error": str(exc),
                        "matched_artist": "",
                        "matched_title": "",
                        "artist_score": 0,
                        "title_score": 0,
                        "identity_score": 0,
                        "raw_genres": "",
                        "raw_styles": "",
                        "normalized_genres": "",
                        "candidate_confidence": 0,
                        "candidate_confidence_label": "error",
                    }
                )
    return all_candidates


def propose_resolutions(worklist: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_file: dict[str, list[dict[str, Any]]] = {}
    for item in candidates:
        by_file.setdefault(item["file_id"], []).append(item)

    proposals: list[dict[str, Any]] = []
    for item in worklist:
        file_candidates = by_file.get(item["file_id"], [])
        usable = [cand for cand in file_candidates if cand.get("normalized_genres") and float(cand.get("identity_score") or 0) >= 0.55]
        usable.sort(key=lambda cand: (float(cand.get("candidate_confidence") or 0), float(cand.get("identity_score") or 0)), reverse=True)
        if usable:
            best = usable[0]
            decision_state = "proposed"
            confidence = best.get("candidate_confidence_label", "low")
            selected = best.get("normalized_genres", "")
            review_reason = ""
            if confidence in {"low", "identity_only"}:
                decision_state = "manual_review"
                review_reason = "low confidence candidate"
        elif item.get("has_existing_genre"):
            best = {}
            decision_state = "existing_tag_only"
            confidence = "medium"
            selected = item.get("existing_genre", "")
            review_reason = "existing genre tag found but no external confirmation"
        else:
            best = {}
            decision_state = "manual_review"
            confidence = "none"
            selected = ""
            review_reason = "no genre candidate"

        if item.get("no_platform_ok") and confidence not in {"high", "medium"}:
            decision_state = "manual_review"
            review_reason = (review_reason + "; " if review_reason else "") + "no AutoTagger platform ok"
        if int(item.get("artist_count") or 0) > 2 and confidence not in {"high", "medium"}:
            decision_state = "manual_review"
            review_reason = (review_reason + "; " if review_reason else "") + "multiple artists"

        proposals.append(
            {
                "file_id": item["file_id"],
                "path": item["path"],
                "artist_raw": item["artist_raw"],
                "title_raw": item["title_raw"],
                "title_clean": item["title_clean"],
                "selected_genres": selected,
                "genre_count": len([value for value in str(selected).split(";") if value]),
                "selected_styles": "",
                "style_count": 0,
                "selected_source": best.get("source_name", "local" if selected else ""),
                "resolution_basis": best.get("source_name", "existing_tag" if selected else "none"),
                "confidence_score": best.get("candidate_confidence", ""),
                "confidence_label": confidence,
                "decision_state": decision_state,
                "review_reason": review_reason,
                "ok_platforms": item.get("ok_platforms", ""),
                "latest_platform": item.get("latest_platform", ""),
                "latest_message": item.get("latest_message", ""),
                "existing_genre": item.get("existing_genre", ""),
                "existing_style": item.get("existing_style", ""),
                "existing_comment": item.get("existing_comment", ""),
                "existing_composer": item.get("existing_composer", ""),
                "mixed_in_key_energy_candidate": item.get("mixed_in_key_energy_candidate", ""),
                "comment_migration_status": item.get("comment_migration_status", "not_checked"),
                "has_prior_ok": item.get("has_prior_ok", False),
                "prior_ok_platforms": item.get("prior_ok_platforms", ""),
                "masked_prior_ok": item.get("masked_prior_ok", False),
                "missed_genre_reason": review_reason if not selected else "",
                "genre_apply_action": "preserve_existing_genre; resolver_proposes_only",
                "comment_apply_action": "reserve_for_numeric_mixed_in_key_energy; do_not_overwrite_without_migration_report",
                "category_apply_target": "rekordbox_mytag_labels",
                "recommended_category_labels": "",
                "category_label_status": "not_inferred_by_genre_resolver",
                "audio_mutation_allowed": False,
                "apply_status": "not_implemented",
            }
        )
    return proposals


def summarize_resolution(
    worklist: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    integrity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    by_source: dict[str, int] = {}
    by_state: dict[str, int] = {}
    by_confidence: dict[str, int] = {}
    by_comment_migration_status: dict[str, int] = {}
    for cand in candidates:
        by_source[cand.get("source_name", "")] = by_source.get(cand.get("source_name", ""), 0) + 1
    for proposal in proposals:
        by_state[proposal["decision_state"]] = by_state.get(proposal["decision_state"], 0) + 1
        by_confidence[proposal["confidence_label"]] = by_confidence.get(proposal["confidence_label"], 0) + 1
    for item in worklist:
        status = item.get("comment_migration_status", "not_checked")
        by_comment_migration_status[status] = by_comment_migration_status.get(status, 0) + 1
    return {
        **summarize_worklist(worklist, integrity),
        "candidateCount": len(candidates),
        "candidateBySource": dict(sorted(by_source.items(), key=lambda pair: (-pair[1], pair[0]))),
        "proposalByState": dict(sorted(by_state.items(), key=lambda pair: (-pair[1], pair[0]))),
        "proposalByConfidence": dict(sorted(by_confidence.items(), key=lambda pair: (-pair[1], pair[0]))),
        "proposedCount": sum(1 for item in proposals if item["decision_state"] == "proposed"),
        "manualReviewCount": sum(1 for item in proposals if item["decision_state"] == "manual_review"),
        "existingGenreCount": sum(1 for item in worklist if item.get("has_existing_genre")),
        "existingStyleCount": sum(1 for item in worklist if item.get("has_existing_style")),
        "existingCommentCount": sum(1 for item in worklist if item.get("has_existing_comment")),
        "mixedInKeyEnergyCandidateCount": sum(1 for item in worklist if item.get("mixed_in_key_energy_candidate")),
        "commentMigrationByStatus": dict(sorted(by_comment_migration_status.items(), key=lambda pair: (-pair[1], pair[0]))),
        "audioMutationAllowedCount": sum(1 for item in proposals if str(item.get("audio_mutation_allowed")).casefold() == "true"),
    }


def audio_readiness(repo_root: Path, ffprobe: str | None) -> dict[str, Any]:
    checks: dict[str, Any] = {
        "ffprobe": {"available": bool(ffprobe), "path": ffprobe or ""},
        "fpcalc": {"available": bool(find_command("fpcalc")), "path": find_command("fpcalc") or ""},
        "pythonPackages": {},
        "recommendation": {
            "availableNow": "Chromaprint/fpcalc is useful for identity or near-duplicate detection, not genre by orchestration.",
            "lightweightPilot": "Use ffmpeg/ffprobe metadata plus optional low-level spectral features only as clustering hints, not genre truth.",
            "strongerPilot": "Essentia TensorFlow models or CLAP/music embedding models are better for timbre/instrument similarity, but need a separate readiness and licensing gate.",
        },
    }
    for module in ("librosa", "essentia", "numpy", "sklearn", "torch", "laion_clap"):
        checks["pythonPackages"][module] = module_available(module)
    return checks


def find_command(name: str) -> str:
    paths = os.environ.get("PATH", "").split(os.pathsep)
    candidates = [name]
    if os.name == "nt" and not name.lower().endswith(".exe"):
        candidates.append(name + ".exe")
    for folder in paths:
        for candidate_name in candidates:
            path = Path(folder) / candidate_name
            if path.exists():
                return str(path)
    return ""


def module_available(name: str) -> bool:
    completed = subprocess.run([sys.executable, "-c", f"import {name}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return completed.returncode == 0


def report_paths(reports_root: Path, timestamp: str) -> dict[str, Path]:
    prefix = f"genre-resolver-{timestamp}"
    return {
        "worklistCsv": reports_root / f"{prefix}-worklist.csv",
        "attemptsCsv": reports_root / f"{prefix}-attempts.csv",
        "candidatesJsonl": reports_root / f"{prefix}-candidates.jsonl",
        "proposalsCsv": reports_root / f"{prefix}-proposals.csv",
        "manualReviewM3u": reports_root / f"{prefix}-manual-review.m3u",
        "missingGenreM3u": reports_root / f"{prefix}-missing-existing-genre.m3u",
        "trueUnresolvedMissingGenreM3u": reports_root / f"{prefix}-true-unresolved-missing-existing-genre.m3u",
        "summaryJson": reports_root / f"{prefix}-summary.json",
    }


def flatten_attempts(worklist: list[dict[str, Any]], attempts_by_path: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    file_by_path = {item["path"]: item["file_id"] for item in worklist}
    rows: list[dict[str, Any]] = []
    for path_text, attempts in attempts_by_path.items():
        if path_text not in file_by_path:
            continue
        for index, attempt in enumerate(attempts, start=1):
            rows.append({"file_id": file_by_path[path_text], "path": path_text, "attempt_order": index, **attempt})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--source-config", required=True)
    parser.add_argument("--AutoTagger-run", required=True)
    parser.add_argument("--reports-root", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--mode", choices=["worklist", "resolve", "verify", "audioreadiness"], default="worklist")
    parser.add_argument("--input-m3u", default="")
    parser.add_argument(
        "--worklist-scope",
        choices=["all-errors", "missing-existing-genre", "true-unresolved-missing-existing-genre"],
        default="all-errors",
    )
    parser.add_argument("--source", action="append", default=[])
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--max-candidates-per-source", type=int, default=5)
    parser.add_argument("--verify-audio-tags", action="store_true")
    parser.add_argument("--ffprobe", default="")
    args = parser.parse_args()

    repo_root = Path(args.repo_root)
    _ = load_json(Path(args.config))
    source_config = load_json(Path(args.source_config))
    AutoTagger_run = Path(args.AutoTagger_run)
    reports_root = Path(args.reports_root)
    runtime_root = Path(args.runtime_root)
    ensure_dir(reports_root)
    ensure_dir(runtime_root)

    if args.mode == "audioreadiness":
        payload = {
            "success": True,
            "schema": SCHEMA_VERSION,
            "mode": args.mode,
            "generatedAt": utc_now(),
            "audioReadiness": audio_readiness(repo_root, args.ffprobe or find_command("ffprobe")),
        }
        print_json(payload)
        return 0

    latest_csv = AutoTagger_run / "derived-latest-state-by-path.csv"
    events_jsonl = AutoTagger_run / "AutoTagger-events.jsonl"
    if not latest_csv.exists():
        raise FileNotFoundError(latest_csv)

    latest_rows = read_latest_state(latest_csv)
    attempts_by_path = read_attempts(events_jsonl)
    integrity = source_integrity(AutoTagger_run, latest_rows, attempts_by_path)
    selected_input_m3u = Path(args.input_m3u) if args.input_m3u else latest_scope_m3u(reports_root, args.worklist_scope)
    if args.mode == "resolve" and selected_input_m3u is None and args.limit <= 0:
        raise ValueError(
            "Unbounded Resolve refused. Pass --input-m3u, --limit, or --worklist-scope missing-existing-genre / true-unresolved-missing-existing-genre."
        )
    path_filter = read_m3u_path_filter(selected_input_m3u) if selected_input_m3u is not None else None
    worklist = build_worklist(latest_rows, attempts_by_path, args.limit, path_filter)
    if args.verify_audio_tags:
        enrich_existing_tags(worklist, args.ffprobe or find_command("ffprobe"))

    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    paths = report_paths(reports_root, timestamp)
    attempts = flatten_attempts(worklist, attempts_by_path)
    write_csv(paths["worklistCsv"], worklist)
    write_csv(paths["attemptsCsv"], attempts)

    candidates: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    if args.mode == "resolve":
        candidates = resolve_candidates(worklist, source_config, runtime_root, args.source, args.max_candidates_per_source)
        proposals = propose_resolutions(worklist, candidates)
        write_jsonl(paths["candidatesJsonl"], candidates)
        write_csv(paths["proposalsCsv"], proposals)
        write_m3u(paths["manualReviewM3u"], [item["path"] for item in proposals if item["decision_state"] == "manual_review"])
        summary = summarize_resolution(worklist, candidates, proposals, integrity)
    else:
        summary = summarize_worklist(worklist, integrity)
        if args.mode == "verify":
            missing_genre = [item for item in worklist if not item.get("has_existing_genre")]
            true_unresolved_missing_genre = [item for item in missing_genre if item.get("no_platform_ok")]
            masked_prior_ok_with_genre = [
                item for item in worklist if item.get("masked_prior_ok") and item.get("has_existing_genre")
            ]
            summary["existingGenreCount"] = sum(1 for item in worklist if item.get("has_existing_genre"))
            summary["existingStyleCount"] = sum(1 for item in worklist if item.get("has_existing_style"))
            summary["existingCommentCount"] = sum(1 for item in worklist if item.get("has_existing_comment"))
            summary["mixedInKeyEnergyCandidateCount"] = sum(1 for item in worklist if item.get("mixed_in_key_energy_candidate"))
            summary["commentMigrationByStatus"] = dict(
                sorted(
                    {
                        status: sum(1 for item in worklist if item.get("comment_migration_status") == status)
                        for status in {item.get("comment_migration_status", "not_checked") for item in worklist}
                    }.items(),
                    key=lambda pair: (-pair[1], pair[0]),
                )
            )
            summary["missingExistingGenreCount"] = len(missing_genre)
            summary["trueUnresolvedMissingExistingGenreCount"] = len(true_unresolved_missing_genre)
            summary["maskedPriorOkWithExistingGenreCount"] = len(masked_prior_ok_with_genre)
            summary["verifyAudioTags"] = bool(args.verify_audio_tags)
            write_m3u(paths["missingGenreM3u"], [item["path"] for item in missing_genre])
            write_m3u(paths["trueUnresolvedMissingGenreM3u"], [item["path"] for item in true_unresolved_missing_genre])

    summary_payload = {
        "success": True,
        "schema": SCHEMA_VERSION,
        "mode": args.mode,
        "generatedAt": utc_now(),
        "AutoTaggerRunPath": str(AutoTagger_run),
        "worklistScope": args.worklist_scope,
        "inputM3uPath": str(selected_input_m3u) if selected_input_m3u is not None else "",
        "inputM3uPathCount": len(path_filter) if path_filter is not None else None,
        "limit": args.limit,
        "sources": enabled_sources(source_config, args.source) if args.mode == "resolve" else [],
        "summary": summary,
        "reports": {key: str(value) for key, value in paths.items() if value.exists()},
        "runtimeRoot": str(runtime_root),
        "metadataPolicy": METADATA_POLICY,
        "mutation": {"audioFilesMutated": False, "tagsWritten": False, "applyModeImplemented": False},
    }
    with paths["summaryJson"].open("w", encoding="utf-8") as handle:
        json.dump(summary_payload, handle, ensure_ascii=False, indent=2)
    print_json(summary_payload)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001 - top-level CLI report.
        print_json({"success": False, "schema": SCHEMA_VERSION, "error": str(exc), "generatedAt": utc_now()})
        raise SystemExit(1)
