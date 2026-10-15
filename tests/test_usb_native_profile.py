import sys
import unittest
import struct
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import usb_native_profile as profile

class NativeProfile(unittest.TestCase):
    def test_verifier_refuses_old_directory_stale_counter_and_index(self):
        raw=bytearray(8192);struct.pack_into('<3I',raw,40,0x280,1,0)
        track=SimpleNamespace(id=1,file_path='/Contents/Rock/Example Artist - Example Track.aif',analyze_path='/PIONEER/USBANLZ/P011/00005F11/ANLZ0000.DAT')
        db=SimpleNamespace(tracks=[track],row_locations=lambda t:[40],page_size=4096,tables=[SimpleNamespace(type=0,first_page=1)])
        self.assertEqual([],profile.verify_native_profile(db,raw))
        track.analyze_path='/PIONEER/USBANLZ/P000/00000000/ANLZ0000.DAT'
        struct.pack_into('<I',raw,44,8269);struct.pack_into('<H',raw,4096+0x38,2)
        result=profile.verify_native_profile(db,raw)
        self.assertIn('native_runtime_counter_or_profile',result)
        self.assertIn('native_analysis_directory:1',result)
        self.assertIn('native_retired_index_not_empty:0',result)

    def test_verified_path_and_guard(self):
        self.assertEqual(profile.analysis_directory('/Contents/Rock/Example Artist - Example Track.aif'),
                         '/PIONEER/USBANLZ/P011/00005F11')
        with self.assertRaises(ValueError):
            profile.analysis_directory('/Contents/../unsafe.aif')

    def test_collision_never_overwrites_an_existing_analysis(self):
        directory = '/PIONEER/USBANLZ/P001/00000001'
        reserved = {(directory + '/ANLZ0000.DAT').casefold()}
        with patch.object(profile, 'analysis_directory', return_value=directory):
            self.assertEqual(profile.allocate_analysis_path('/Contents/Test/A.aif', reserved), directory+'/ANLZ0001.DAT')
            self.assertEqual(profile.allocate_analysis_path('/Contents/Test/B.aif', reserved), directory+'/ANLZ0002.DAT')

    def test_counter_refuses_sparse_or_uncalibrated_profiles(self):
        db = SimpleNamespace(tracks=[SimpleNamespace(id=1), SimpleNamespace(id=3)])
        with self.assertRaisesRegex(ValueError, 'dense'):
            profile.synchronize_dense_counter(SimpleNamespace(db=db))
        db = SimpleNamespace(tracks=[SimpleNamespace(id=1)], row_locations=lambda table:[40], page_size=4096)
        editor = SimpleNamespace(db=db, _buf=bytearray(80))
        with self.assertRaisesRegex(ValueError, 'Uncalibrated'):
            profile.synchronize_dense_counter(editor)

    def test_counter_changes_only_calibrated_value_and_marks_page(self):
        db = SimpleNamespace(tracks=[SimpleNamespace(id=1)], row_locations=lambda table:[40], page_size=4096)
        editor = SimpleNamespace(db=db, _buf=bytearray(80))
        struct.pack_into('<3I',editor._buf,40,0x280,3,0)
        old=bytes(editor._buf);marked=[]
        editor._put=lambda off,fmt,value:struct.pack_into(fmt,editor._buf,off,value)
        editor._mark_table_touched=lambda table,page:marked.append((table,page))
        self.assertEqual(profile.synchronize_dense_counter(editor),{'before':3,'after':1})
        self.assertEqual(marked,[(19,0)])
        self.assertEqual(bytes(editor._buf[:44])+bytes(editor._buf[48:]),old[:44]+old[48:])

if __name__ == '__main__':
    unittest.main()
