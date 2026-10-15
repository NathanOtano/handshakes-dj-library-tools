"""Pure playlist projections. No audio or database writes."""
from collections import defaultdict
import math

def bpm_groups(bpm_by_id, step=10, remove_redundant=True):
    groups = defaultdict(set)
    for cid, bpm in bpm_by_id.items():
        values = {bpm} if bpm > 0 else set()
        if 60 <= bpm <= 110:
            values.add(bpm * 2)
        if 140 <= bpm <= 220:
            values.add(bpm / 2)
        if not values:
            groups['BPM inconnu'].add(cid)
        for value in values:
            low = math.floor(value / step) * step
            groups[f'{low:03d}-{low + step - 1:03d}'].add(cid)
    kept, removed, covered = {}, [], set()
    def preference(label):
        if label == 'BPM inconnu':
            return (-1, 0, label)
        low = int(label.split('-')[0])
        return (0 if 100 <= low < 160 else 1, abs(low + step / 2 - 130), label)
    for label in sorted(groups, key=preference):
        ids = groups[label]
        if remove_redundant and ids <= covered:
            removed.append(dict(label=label, ids=sorted(ids), covered_by=[k for k, v in kept.items() if ids & set(v)]))
        else:
            kept[label] = sorted(ids, key=lambda i: (bpm_by_id[i], i))
            covered.update(ids)
    assert covered == set(bpm_by_id)
    # Each alternative above came from the exact half/double predicate, never a
    # rounded BPM or merely similar name. A partially redundant range stays whole.
    return dict(sorted(kept.items())), removed

def split_playlists(plan, limit=1000):
    out = {}
    for path, ids in plan.items():
        ids = list(dict.fromkeys(ids))
        if not ids:
            continue
        if len(ids) <= limit:
            out[path] = ids
        else:
            parts = []
            for offset in range(0, len(ids), limit):
                key = (*path[:-1], path[-1] + f' ({offset // limit + 1:02d})')
                out[key] = ids[offset:offset + limit]
                parts.extend(out[key])
            assert parts == ids
    return out
