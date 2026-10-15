"""Read-only release gate: every selected local recording needs a real DJ bucket.

Run against an app-closed DB snapshot. The optional output is a report only.
No inferred genres, fallback creation, library writes or USB writes occur here.
"""
import argparse
import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def check_coverage(tracks, classifications, roots, usb_lists, folders, max_elements=1000):
    selected = set().union(*(set(v) for v in usb_lists.values())) if usb_lists else set()
    errors = []
    if not selected:
        errors.append('empty_usb_selection')
    unknown = selected - tracks.keys()
    if unknown:
        errors.append('unknown_selected_ids')
    unresolved, stale, missing_files = [], [], []
    provisional = 0
    for cid in sorted(selected - unknown):
        row, cl = tracks[cid], classifications[cid]
        if not Path(row['path']).is_file():
            missing_files.append(cid)
        wanted = cl['djBuckets']
        if not wanted:
            unresolved.append(cid)
        if any(cid not in roots.get(b, set()) for b in wanted):
            stale.append(cid)
        provisional += bool(cl.get('bucketAssignment'))
    if unresolved: errors.append('selected_tracks_without_bucket')
    if stale: errors.append('derived_buckets_need_refresh')
    if missing_files: errors.append('selected_audio_missing')
    too_large = [list(p) for p, ids in usb_lists.items() if len(ids) > 1000]
    if too_large: errors.append('playlist_above_1000')
    nodes = len(usb_lists) + folders
    if nodes > max_elements: errors.append('usb_elements_above_limit')
    # Every source DJ bucket must survive the selected USB view/chunks as a set.
    projected = defaultdict(set)
    for path, members in usb_lists.items():
        if len(path) == 4 and path[:2] == ('USB', 'DJ Bacs') and re.fullmatch(r'Tous(?: \(\d+\))?', path[-1]):
            projected[path[2]].update(members)
    for b, members in roots.items():
        if set(members) & selected != projected[b]:
            errors.append('usb_bucket_membership_mismatch:' + b)
    return dict(status='pass' if not errors else 'fail', tracks=len(selected),
                playlists=len(usb_lists), folders=folders, elements=nodes,
                maxPlaylistTracks=max(map(len, usb_lists.values()), default=0),
                provisional=provisional, unbucketed=unresolved, staleBuckets=stale,
                missingFiles=missing_files, errors=errors)


def audit(master, db_dir, taxonomy):
    from pyrekordbox import Rekordbox6Database
    from pyrekordbox.db6 import tables
    from rekordbox_genre_taxonomy_audit import make_taxonomy_index, classify_terms, TERM_SPLIT_RE, clean_genre_part
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    before = digest(master)
    db = Rekordbox6Database(path=master, db_dir=db_dir)
    try:
        config = json.loads(taxonomy.read_text(encoding='utf8'))
        aliases = make_taxonomy_index(config)['aliasToMatches']
        tracks, classes = {}, {}
        for c in db.query(tables.DjmdContent):
            cid = str(c.ID)
            tracks[cid] = dict(path=c.FolderPath or '', title=c.Title or '')
            terms = [clean_genre_part(t) for t in TERM_SPLIT_RE.split(c.Genre.Name if c.Genre else '') if clean_genre_part(t)]
            classes[cid] = classify_terms(terms, aliases, c)
        nodes = {str(p.ID): p for p in db.query(tables.DjmdPlaylist)}
        def path(pid):
            parts, seen = [], set()
            while pid and pid not in ('0', 'root'):
                if pid in seen: raise ValueError('playlist_cycle')
                seen.add(pid); p = nodes[pid]; parts.insert(0, p.Name); pid = str(p.ParentID or '')
            return tuple(parts)
        members = defaultdict(list)
        for e in db.query(tables.DjmdSongPlaylist).order_by(tables.DjmdSongPlaylist.TrackNo):
            members[str(e.PlaylistID)].append(str(e.ContentID))
        roots, usb, folders = {}, {}, 0
        for pid, p in nodes.items():
            parts = path(pid)
            if int(p.Attribute) == 0 and len(parts) == 3 and parts[0] == 'DJ Bacs' and parts[-1] == 'Tous':
                roots[parts[1]] = set(members[pid])
            if parts and parts[0] == 'USB':
                if int(p.Attribute) == 0: usb[parts] = members[pid]
                else: folders += 1
        result = check_coverage(tracks, classes, roots, usb, folders)
    finally:
        db.close(); db.engine.dispose()
    if digest(master) != before: raise ValueError('audit_changed_snapshot')
    result.update(observedAt=datetime.now(timezone.utc).isoformat(), masterSha256=before,
                  taxonomySha256=digest(taxonomy), sourceWrites=False, audioWrites=False,
                  proofScope='Selected USB scope only; intentionally excluded sampler/streaming entries are outside this gate. Precise genre truth and hardware playback are separate.')
    return result


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--master', type=Path, required=True)
    p.add_argument('--db-dir', type=Path, required=True)
    p.add_argument('--taxonomy', type=Path, default=Path(__file__).resolve().parents[1]/'config/dj-genre-taxonomy.json')
    p.add_argument('--output', type=Path)
    a = p.parse_args(); result = audit(a.master, a.db_dir, a.taxonomy)
    if a.output: a.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(result, ensure_ascii=True))
    raise SystemExit(result['status'] != 'pass')
