"""Read approved per-record bucket fallbacks; never infer precise genre tags.

The private registry is runtime/dj-bucket-assignments.json beside this repo.
An assignment applies only when taxonomy supplies no bucket and identity agrees.
Missing registry means no fallbacks. Invalid present data fails closed.
"""
import json
from functools import lru_cache
from pathlib import Path

REGISTRY = Path(__file__).resolve().parents[1] / 'runtime/dj-bucket-assignments.json'

def path_key(value):
    return str(value or '').replace('\\', '/').rstrip('/').casefold()

@lru_cache(maxsize=4)
def _read(path, mtime_ns, size):
    data = json.loads(Path(path).read_text(encoding='utf8'))
    if data.get('version') != 1:
        raise ValueError('Unsupported bucket assignment registry')
    return data['assignments']

def apply_assignment(classification, content, aliases, registry=REGISTRY):
    if classification['djBuckets'] or content is None:
        return classification
    registry = Path(registry)
    if not registry.exists():
        return classification
    stat = registry.stat()
    entry = _read(str(registry), stat.st_mtime_ns, stat.st_size).get(str(content.ID))
    if entry is None:
        return classification
    if (path_key(content.FolderPath) != path_key(entry['path']) or
            str(content.Title or '') != entry['title']):
        raise ValueError('Bucket assignment identity mismatch: ' + str(content.ID))
    valid = {b for matches in aliases.values() for m in matches for b in m.get('djBuckets', [])}
    if entry['bucket'] not in valid:
        raise ValueError('Unknown assigned bucket: ' + entry['bucket'])
    result = dict(classification)
    result['djBuckets'] = {entry['bucket']}
    result['bucketAssignment'] = {'source': 'approved_neighbors',
                                  'families': entry['families'], 'provisional': True}
    return result
