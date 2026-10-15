"""Empirical native USB profile: final-audio-path hashing and dense ID checks.

Integration helpers for a snapshot-based exporter, not an in-place USB writer.
Unknown runtime profiles fail closed; opaque fields remain uninterpreted.
"""
import struct
from pathlib import PurePosixPath


def verify_native_profile(db, raw):
    """Fail closed on the empirical AERO export profile; never repair in a verifier."""
    errors = []
    tracks = db.tracks
    ids = sorted(t.id for t in tracks)
    if not ids or ids != list(range(1, len(ids) + 1)):
        errors.append('native_counter_requires_dense_ids')
    rows = db.row_locations(19)
    if len(rows) != 1:
        errors.append('native_runtime_row_count')
    else:
        marker, count, reserved = struct.unpack_from('<3I', raw, rows[0])
        if (marker, count, reserved) != (0x280, len(ids), 0):
            errors.append('native_runtime_counter_or_profile')
    seen = set()
    for t in tracks:
        if str(PurePosixPath(t.analyze_path).parent) != analysis_directory(t.file_path):
            errors.append('native_analysis_directory:' + str(t.id))
        if t.analyze_path.casefold() in seen:
            errors.append('native_analysis_collision:' + str(t.id))
        seen.add(t.analyze_path.casefold())
    for table in db.tables:
        if int(table.type) not in (0, 7): continue
        # This builder deliberately retires these indexes. Any remaining entry
        # belongs to a different profile and must be investigated, not ignored.
        off = table.first_page * db.page_size
        if struct.unpack_from('<H', raw, off + 0x38)[0] != 0:
            errors.append('native_retired_index_not_empty:' + str(int(table.type)))
    return errors

def analysis_directory(audio_path):
    if not audio_path.startswith('/Contents/') or '..' in audio_path.split('/') or ':' in audio_path:
        raise ValueError('Unsafe audio path')
    value = 0
    for (unit,) in struct.iter_unpack('<H', audio_path.encode('utf-16-le')):
        value = (((value * 0x5bc9 + unit) & 0xffffffff) * 0x93b5 + unit) & 0xffffffff
    value %= 200003
    bucket = ((value & 1) | ((value >> 1) & 2) | ((value >> 4) & 4) |
              ((value >> 4) & 8) | ((value >> 5) & 16) |
              ((value >> 8) & 32) | ((value >> 10) & 64))
    return f'/PIONEER/USBANLZ/P{bucket:03X}/{value:08X}'

def allocate_analysis_path(audio_path, reserved):
    directory = analysis_directory(audio_path)
    for index in range(10000):
        base = f'{directory}/ANLZ{index:04d}.DAT'
        if base.casefold() not in reserved:
            reserved.add(base.casefold())
            return base
    raise ValueError('Native analysis collision namespace exhausted')

def synchronize_dense_counter(editor):
    db = editor.db
    ids = sorted(t.id for t in db.tracks)
    if not ids or ids != list(range(1, len(ids) + 1)):
        raise ValueError('Counter profile requires dense track IDs')
    rows = db.row_locations(19)
    if len(rows) != 1:
        raise ValueError('Counter profile requires one runtime row')
    row = rows[0]
    marker, old, unknown = struct.unpack_from('<3I', editor._buf, row)
    if marker != 0x280 or unknown != 0:
        raise ValueError('Uncalibrated native runtime profile')
    if old != len(ids):
        editor._put(row + 4, '<I', len(ids))
        editor._mark_table_touched(19, row // db.page_size)
    return {'before': old, 'after': len(ids)}
