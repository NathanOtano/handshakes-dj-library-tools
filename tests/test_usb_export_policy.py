import sys,unittest,xml.etree.ElementTree as ET
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from usb_export_policy import verify_bucket_coverage

class Coverage(unittest.TestCase):
    def test_car_presence_does_not_replace_bucket_and_chunks_cover_all(self):
        tree=ET.ElementTree(ET.fromstring('<DJ_PLAYLISTS><PLAYLISTS><NODE Name="ROOT" Type="0"><NODE Name="USB" Type="0"><NODE Name="DJ Bacs" Type="0"><NODE Name="Breaks" Type="0"><NODE Name="Tous (01)" Type="1"><TRACK Key="1"/></NODE><NODE Name="Tous (02)" Type="1"><TRACK Key="2"/></NODE></NODE></NODE></NODE></NODE></PLAYLISTS></DJ_PLAYLISTS>'))
        self.assertEqual([],verify_bucket_coverage(tree,{'1','2'}))
        self.assertIn('usb_dj_bucket_coverage_mismatch',verify_bucket_coverage(tree,{'1','2','3'}))
        tree.find('./PLAYLISTS/NODE/NODE/NODE').set('Name','car')
        self.assertIn('usb_dj_bucket_coverage_mismatch',verify_bucket_coverage(tree,{'1','2'}))

if __name__=='__main__':unittest.main()
