#!/usr/bin/env python3
"""Audit a Rekordbox library against the local DJ genre taxonomy."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from pyrekordbox import Rekordbox6Database
from pyrekordbox.db6 import DjmdContent
from pyrekordbox.db6.tables import DjmdGenre

STREAMING_PREFIXES = ("tidal:", "qobuz:", "beatport:", "beatsource:", "soundcloud:")
TERM_SPLIT_RE = re.compile(r"\s*(?:;|\||,|\u2022|\n|\r|\t|\s/\s)\s*")


def now_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    ensure_dir(path.parent)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def path_from_rekordbox(value: str | None) -> str:
    if not value:
        return ""
    raw = str(value).strip()
    parsed = urlparse(raw)
    if parsed.scheme == "file":
        decoded = unquote(parsed.path or "")
        if parsed.netloc and parsed.netloc.lower() not in {"", "localhost"}:
            decoded = f"//{parsed.netloc}{decoded}"
        if re.match(r"^/[A-Za-z]:", decoded):
            decoded = decoded[1:]
        return decoded.replace("/", "\\")
    return unquote(raw).replace("/", "\\")


def is_streaming_path(value: str | None) -> bool:
    return normalize_key(value).startswith(STREAMING_PREFIXES)


def normalize_text(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def normalize_key(value: str | None) -> str:
    text = normalize_text(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("&", " and ")
    text = text.replace("+", " and ")
    text = re.sub(r"['`´’]", "", text)
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip().casefold()


def display_join(values: set[str] | list[str]) -> str:
    return " | ".join(sorted(values, key=lambda item: item.casefold()))


def get_text_field(row: Any, field: str) -> str:
    if not hasattr(row, field):
        return ""
    value = getattr(row, field)
    if hasattr(value, "Name"):
        return normalize_text(str(getattr(value, "Name") or ""))
    return normalize_text(str(value or ""))


def clean_genre_part(value: str) -> str:
    value = normalize_text(value)
    if not value:
        return ""
    value = re.sub(r"^<DjmdGenre\([^)]*Name=", "", value)
    value = value.rstrip(")>")
    value = normalize_text(value)
    if "(" in value:
        before_parenthesis = normalize_text(value.split("(", 1)[0])
        if before_parenthesis:
            return before_parenthesis
    return value


def build_genre_by_id(db: Rekordbox6Database) -> dict[str, str]:
    return {str(row.ID): normalize_text(str(row.Name or "")) for row in db.query(DjmdGenre).all()}


def content_genre_terms(row: DjmdContent, genre_by_id: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    fields: dict[str, str] = {}
    for field in ("GenreName", "SrcGenre", "Genre", "Label", "SrcLabel", "Comments"):
        value = get_text_field(row, field)
        if value:
            fields[field] = value

    genre_id = get_text_field(row, "GenreID")
    if genre_id and genre_id in genre_by_id:
        fields["GenreID"] = genre_by_id[genre_id]

    terms: list[str] = []
    seen: set[str] = set()
    for value in fields.values():
        for part in TERM_SPLIT_RE.split(value):
            part = clean_genre_part(part)
            if not part:
                continue
            key = normalize_key(part)
            if not key or key in seen:
                continue
            seen.add(key)
            terms.append(part)
    return terms, fields


def make_taxonomy_index(config: dict[str, Any]) -> dict[str, Any]:
    alias_to_matches: dict[str, list[dict[str, Any]]] = defaultdict(list)
    canonical_aliases: dict[str, set[str]] = defaultdict(set)

    for item in config["djBuckets"]:
        name = item["name"]
        for alias in [name, *item.get("aliases", [])]:
            key = normalize_key(alias)
            alias_to_matches[key].append({"kind": "djBucket", "name": name, "djBuckets": [name]})
            canonical_aliases[name].add(alias)

    for item in config.get("djSubBuckets", []):
        name = item["name"]
        parent = item["parent"]
        for alias in [name, *item.get("aliases", [])]:
            key = normalize_key(alias)
            alias_to_matches[key].append(
                {
                    "kind": "djSubBucket",
                    "name": name,
                    "djBuckets": [parent],
                    "djSubBucket": name,
                    "djSubBucketParent": parent,
                }
            )
            canonical_aliases[name].add(alias)

    for item in config["strictGenres"]:
        name = item["name"]
        for alias in [name, *item.get("aliases", [])]:
            key = normalize_key(alias)
            alias_to_matches[key].append(
                {
                    "kind": "strictGenre",
                    "name": name,
                    "strictGenre": name,
                    "djBuckets": list(item.get("djBuckets", [])),
                }
            )
            canonical_aliases[name].add(alias)

    for item in config["subgenres"]:
        name = item["name"]
        for alias in [name, *item.get("aliases", [])]:
            key = normalize_key(alias)
            alias_to_matches[key].append(
                {
                    "kind": "subgenre",
                    "name": name,
                    "subgenre": name,
                    "strictGenre": item["parent"],
                    "djBuckets": list(item.get("djBuckets", [])),
                }
            )
            canonical_aliases[name].add(alias)

    return {"aliasToMatches": alias_to_matches, "canonicalAliases": canonical_aliases}


def classify_terms(
    terms: list[str],
    alias_to_matches: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    dj_buckets: set[str] = set()
    dj_sub_buckets: set[tuple[str, str]] = set()
    strict_genres: set[str] = set()
    subgenres: set[str] = set()
    matched_aliases: set[str] = set()
    unmatched_terms: list[str] = []

    for term in terms:
        key = normalize_key(term)
        matches = alias_to_matches.get(key, [])
        if not matches:
            unmatched_terms.append(term)
            continue
        matched_aliases.add(term)
        for match in matches:
            dj_buckets.update(match.get("djBuckets", []))
            if match.get("djSubBucket"):
                dj_sub_buckets.add((match["djSubBucketParent"], match["djSubBucket"]))
            if match.get("strictGenre"):
                strict_genres.add(match["strictGenre"])
            if match.get("subgenre"):
                subgenres.add(match["subgenre"])

    return {
        "djBuckets": dj_buckets,
        "djSubBuckets": dj_sub_buckets,
        "strictGenres": strict_genres,
        "subgenres": subgenres,
        "matchedAliases": matched_aliases,
        "unmatchedTerms": unmatched_terms,
    }


def bpm_sort_key(row: DjmdContent) -> tuple[int, int, str, int]:
    bpm = int(getattr(row, "BPM", 0) or 0)
    missing_bpm = 1 if bpm <= 0 else 0
    return (missing_bpm, bpm, str(getattr(row, "Title", "") or "").casefold(), int(getattr(row, "ID", 0) or 0))


def track_row(content: DjmdContent, playlist_path: str, layer: str) -> dict[str, Any]:
    bpm_raw = int(getattr(content, "BPM", 0) or 0)
    return {
        "playlist_path": playlist_path,
        "layer": layer,
        "content_id": str(getattr(content, "ID", "")),
        "artist": str(getattr(getattr(content, "Artist", None), "Name", "") or getattr(content, "ArtistName", "") or ""),
        "title": str(getattr(content, "Title", "") or ""),
        "bpm": round(bpm_raw / 100, 2),
        "bpm_raw": bpm_raw,
        "path": path_from_rekordbox(str(getattr(content, "FolderPath", "") or "")),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    master = Path(args.master).resolve()
    config = read_json(Path(args.taxonomy).resolve())
    reports_root = Path(args.reports_root).resolve()
    stamp = now_stamp()
    prefix = f"rekordbox-genre-taxonomy-audit-{stamp}"

    index = make_taxonomy_index(config)
    alias_to_matches = index["aliasToMatches"]
    roots = config["playlistRoots"]
    subgenre_min = int(args.subgenre_min_tracks or config["defaults"]["subgenreMinTracks"])

    db = Rekordbox6Database(path=str(master), db_dir=str(master.parent))
    try:
        genre_by_id = build_genre_by_id(db)
        contents = list(db.query(DjmdContent).all())
        local_contents = [
            row for row in contents if not is_streaming_path(str(getattr(row, "FolderPath", "") or ""))
        ]

        playlist_members: dict[str, dict[str, Any]] = {}
        track_classifications: list[dict[str, Any]] = []
        term_counter: Counter[str] = Counter()
        unmatched_counter: Counter[str] = Counter()
        no_match_rows: list[dict[str, Any]] = []

        for content in local_contents:
            terms, source_fields = content_genre_terms(content, genre_by_id)
            for term in terms:
                term_counter[term] += 1
            classification = classify_terms(terms, alias_to_matches)
            for term in classification["unmatchedTerms"]:
                unmatched_counter[term] += 1

            content_id = str(getattr(content, "ID", ""))
            track_classifications.append(
                {
                    "content_id": content_id,
                    "artist": str(getattr(getattr(content, "Artist", None), "Name", "") or getattr(content, "ArtistName", "") or ""),
                    "title": str(getattr(content, "Title", "") or ""),
                    "genre_terms": display_join(terms),
                    "matched_aliases": display_join(classification["matchedAliases"]),
                    "dj_buckets": display_join(classification["djBuckets"]),
                    "dj_sub_buckets": display_join([f"{parent} > {name}" for parent, name in classification["djSubBuckets"]]),
                    "strict_genres": display_join(classification["strictGenres"]),
                    "subgenres": display_join(classification["subgenres"]),
                    "unmatched_terms": display_join(classification["unmatchedTerms"]),
                    "path": path_from_rekordbox(str(getattr(content, "FolderPath", "") or "")),
                }
            )

            if not classification["djBuckets"] and not classification["strictGenres"] and not classification["subgenres"]:
                no_match_rows.append(track_classifications[-1])

            for bucket in classification["djBuckets"]:
                playlist_path = f"{roots['djBuckets']}/{bucket}"
                playlist_members.setdefault(playlist_path, {"layer": "djBucket", "contents": []})["contents"].append(content)

            for parent, sub_bucket in classification["djSubBuckets"]:
                playlist_path = f"{roots['djBuckets']}/{parent}/{sub_bucket}"
                playlist_members.setdefault(playlist_path, {"layer": "djSubBucket", "contents": []})["contents"].append(content)

            for strict in classification["strictGenres"]:
                playlist_path = f"{roots['musicologicalGenres']}/{strict}"
                playlist_members.setdefault(playlist_path, {"layer": "strictGenre", "contents": []})["contents"].append(content)

            for subgenre in classification["subgenres"]:
                parent = ""
                for item in config["subgenres"]:
                    if item["name"] == subgenre:
                        parent = item["parent"]
                        break
                playlist_path = f"{roots['subgenres']}/{parent}/{subgenre}" if parent else f"{roots['subgenres']}/{subgenre}"
                playlist_members.setdefault(playlist_path, {"layer": "subgenre", "contents": []})["contents"].append(content)

        playlist_rows: list[dict[str, Any]] = []
        track_membership_rows: list[dict[str, Any]] = []
        suppressed_subgenre_rows: list[dict[str, Any]] = []

        for playlist_path, info in sorted(playlist_members.items(), key=lambda item: item[0].casefold()):
            contents_for_playlist = list({str(getattr(row, "ID", "")): row for row in info["contents"]}.values())
            layer = info["layer"]
            if layer == "subgenre" and len(contents_for_playlist) < subgenre_min:
                suppressed_subgenre_rows.append(
                    {
                        "playlist_path": playlist_path,
                        "layer": layer,
                        "track_count": len(contents_for_playlist),
                        "reason": f"below_subgenre_min_tracks:{subgenre_min}",
                    }
                )
                continue

            sorted_contents = sorted(contents_for_playlist, key=bpm_sort_key)
            playlist_rows.append(
                {
                    "playlist_path": playlist_path,
                    "layer": layer,
                    "track_count": len(sorted_contents),
                    "sort": "bpm_only",
                    "status": "candidate",
                }
            )
            for row in sorted_contents:
                track_membership_rows.append(track_row(row, playlist_path, layer))

        top_terms_rows = [
            {"term": term, "count": count, "normalized": normalize_key(term)}
            for term, count in term_counter.most_common(250)
        ]
        unmatched_rows = [
            {"term": term, "count": count, "normalized": normalize_key(term)}
            for term, count in unmatched_counter.most_common(250)
        ]

        paths = {
            "summary": reports_root / f"{prefix}-summary.json",
            "playlists": reports_root / f"{prefix}-playlists.csv",
            "memberships": reports_root / f"{prefix}-memberships.csv",
            "trackClassifications": reports_root / f"{prefix}-tracks.csv",
            "unmatchedTerms": reports_root / f"{prefix}-unmatched-terms.csv",
            "topTerms": reports_root / f"{prefix}-top-terms.csv",
            "suppressedSubgenres": reports_root / f"{prefix}-suppressed-subgenres.csv",
            "noMatchTracks": reports_root / f"{prefix}-no-match-tracks.csv",
        }

        write_csv(paths["playlists"], playlist_rows, ["playlist_path", "layer", "track_count", "sort", "status"])
        write_csv(
            paths["memberships"],
            track_membership_rows,
            ["playlist_path", "layer", "content_id", "artist", "title", "bpm", "bpm_raw", "path"],
        )
        write_csv(
            paths["trackClassifications"],
            track_classifications,
            ["content_id", "artist", "title", "genre_terms", "matched_aliases", "dj_buckets", "dj_sub_buckets", "strict_genres", "subgenres", "unmatched_terms", "path"],
        )
        write_csv(paths["unmatchedTerms"], unmatched_rows, ["term", "count", "normalized"])
        write_csv(paths["topTerms"], top_terms_rows, ["term", "count", "normalized"])
        write_csv(paths["suppressedSubgenres"], suppressed_subgenre_rows, ["playlist_path", "layer", "track_count", "reason"])
        write_csv(paths["noMatchTracks"], no_match_rows, ["content_id", "artist", "title", "genre_terms", "matched_aliases", "dj_buckets", "dj_sub_buckets", "strict_genres", "subgenres", "unmatched_terms", "path"])

        summary = {
            "schema": "rekordbox-genre-taxonomy-audit-v1",
            "generatedAt": now_iso(),
            "mode": "audit",
            "master": str(master),
            "taxonomy": str(Path(args.taxonomy).resolve()),
            "subgenreMinTracks": subgenre_min,
            "localTrackCount": len(local_contents),
            "playlistCandidateCount": len(playlist_rows),
            "membershipCandidateCount": len(track_membership_rows),
            "djBucketPlaylistCount": sum(1 for row in playlist_rows if row["layer"] == "djBucket"),
            "djSubBucketPlaylistCount": sum(1 for row in playlist_rows if row["layer"] == "djSubBucket"),
            "strictGenrePlaylistCount": sum(1 for row in playlist_rows if row["layer"] == "strictGenre"),
            "subgenrePlaylistCount": sum(1 for row in playlist_rows if row["layer"] == "subgenre"),
            "suppressedSubgenreCount": len(suppressed_subgenre_rows),
            "noMatchTrackCount": len(no_match_rows),
            "uniqueGenreTermCount": len(term_counter),
            "uniqueUnmatchedTermCount": len(unmatched_counter),
            "topUnmatchedTerms": unmatched_rows[:25],
            "topPlaylists": sorted(playlist_rows, key=lambda row: int(row["track_count"]), reverse=True)[:30],
            "reports": {key: str(value) for key, value in paths.items()},
        }
        write_json(paths["summary"], summary)
        return summary
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit Rekordbox genre taxonomy candidates.")
    parser.add_argument("--master", required=True)
    parser.add_argument("--taxonomy", default="config/dj-genre-taxonomy.json")
    parser.add_argument("--reports-root", default="reports")
    parser.add_argument("--subgenre-min-tracks", type=int, default=0)
    return parser


def main(argv: list[str]) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
