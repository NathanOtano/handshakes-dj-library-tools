import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from usb_playlist_limits import bpm_groups,split_playlists

class PlaylistLimits(unittest.TestCase):
 def test_decades_preserve_every_track_including_unknown_tempo(self):
  tracks={'a':70,'b':140,'c':128,'d':0};groups,removed=bpm_groups(tracks)
  self.assertEqual(set(tracks),set().union(*(set(v) for v in groups.values())))
  self.assertIn('BPM inconnu',groups)
  self.assertFalse('070-079' in groups and '140-149' in groups)
 def test_chunks_keep_order_and_unique_members(self):
  ids=[str(i) for i in range(2001)];parts=split_playlists({('USB','Example'):ids+['0']})
  self.assertEqual([1000,1000,1],[len(x) for x in parts.values()])
  self.assertEqual(ids,[i for values in parts.values() for i in values])

if __name__=='__main__':unittest.main()
