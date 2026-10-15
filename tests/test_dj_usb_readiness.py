import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from audit_dj_usb_readiness import check_coverage

class Readiness(unittest.TestCase):
    def test_missing_bucket_or_stale_usb_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'a.aif';p.write_bytes(b'a')
            tracks={'1':{'path':str(p)}}
            lists={('USB','DJ Bacs','Breaks','Tous'):['1']}
            self.assertIn('selected_tracks_without_bucket',check_coverage(tracks,{'1':{'djBuckets':set()}},{'Breaks':{'1'}},lists,3)['errors'])
            self.assertIn('usb_bucket_membership_mismatch:Breaks',check_coverage(tracks,{'1':{'djBuckets':{'Breaks'}}},{'Breaks':{'1'}},{('USB','car','Bass'):['1']},2)['errors'])

    def test_chunks_preserve_bucket_and_capacity_and_missing_file(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/'a.aif';p.write_bytes(b'a');ids=[str(i) for i in range(1001)]
            tracks={i:{'path':str(p)} for i in ids};classes={i:{'djBuckets':{'Breaks'}} for i in ids};roots={'Breaks':set(ids)}
            lists={('USB','DJ Bacs','Breaks','Tous (01)'):ids[:1000],('USB','DJ Bacs','Breaks','Tous (02)'):ids[1000:]}
            self.assertEqual('pass',check_coverage(tracks,classes,roots,lists,3)['status'])
            self.assertIn('usb_elements_above_limit',check_coverage(tracks,classes,roots,lists,999)['errors'])
            p.unlink();self.assertEqual(1001,len(check_coverage(tracks,classes,roots,lists,3)['missingFiles']))

if __name__=='__main__':unittest.main()
