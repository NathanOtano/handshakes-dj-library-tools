import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from dj_ui_profile import usb_profile

class Profile(unittest.TestCase):
    def test_only_usb_subtree_and_ordered_memberships_are_exported(self):
        nodes=[dict(id='1',name='USB',parent='0',attribute=1,seq=1),dict(id='2',name='Rock',parent='1',attribute=0,seq=2),dict(id='3',name='Private',parent='0',attribute=0,seq=3)]
        result=usb_profile(nodes,{'2':[{'id':'9'},{'id':'8'}],'3':[{'id':'7'}]})
        self.assertEqual([['USB'],['USB','Rock']],[r['path'] for r in result])
        self.assertEqual([{'id':'9'},{'id':'8'}],result[1]['entries'])
        self.assertEqual('1',result[1]['parent_id'])

    def test_ambiguous_root_or_cycle_is_refused(self):
        with self.assertRaises(ValueError):usb_profile([], {})
        with self.assertRaises(ValueError):usb_profile([dict(id='1',name='USB',parent='1',attribute=1,seq=1)],{})

if __name__=='__main__':unittest.main()
