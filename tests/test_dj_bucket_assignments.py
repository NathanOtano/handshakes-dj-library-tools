import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from dj_bucket_assignments import apply_assignment

class Assignments(unittest.TestCase):
    def test_identity_guard_and_no_genre_invention(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'registry.json'
            p.write_text(json.dumps({'version': 1, 'assignments': {'7': {
                'path': 'C:/DJ_Music/a.aif', 'title': 'A', 'bucket': 'Breaks', 'families': 2}}}))
            c = SimpleNamespace(ID='7', FolderPath='C:\\DJ_Music\\a.aif', Title='A')
            cl = {'djBuckets': set(), 'strictGenres': set(), 'subgenres': set()}
            ix = {'break': [{'djBuckets': ['Breaks']}]}
            out = apply_assignment(cl, c, ix, p)
            self.assertEqual(out['djBuckets'], {'Breaks'})
            self.assertFalse(out['strictGenres'])
            self.assertFalse(cl['djBuckets'])
            c.FolderPath = 'C:/DJ_Music/other.aif'
            with self.assertRaisesRegex(ValueError, 'identity mismatch'):
                apply_assignment(cl, c, ix, p)

    def test_supported_genre_wins_and_missing_registry_is_optional(self):
        c = SimpleNamespace(ID='7', FolderPath='C:/DJ_Music/a.aif', Title='A')
        cl = {'djBuckets': {'House'}}
        self.assertIs(apply_assignment(cl, c, {}, 'missing.json'), cl)
        cl = {'djBuckets': set()}
        self.assertIs(apply_assignment(cl, c, {}, 'missing.json'), cl)

if __name__ == '__main__':
    unittest.main()
