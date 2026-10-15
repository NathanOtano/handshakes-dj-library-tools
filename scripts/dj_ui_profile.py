"""Extract the current USB playlist tree from a closed-app snapshot, report only."""
import argparse
import json
from pathlib import Path

def usb_profile(nodes, entries):
    byid={str(n['id']):n for n in nodes}
    def path(pid):
        parts=[];seen=set()
        while pid and pid not in ('0','root'):
            if pid in seen or pid not in byid:raise ValueError('Invalid playlist parent tree')
            seen.add(pid);n=byid[pid];parts.insert(0,n['name']);pid=str(n['parent'] or '')
        return parts
    result=[]
    for pid,n in byid.items():
        parts=path(pid)
        if parts and parts[0]=='USB':
            result.append(dict(id=pid,name=n['name'],parent_id=str(n['parent'] or ''),attribute=n['attribute'],seq=n['seq'],path=parts,entries=entries.get(pid,[])))
    if len([n for n in result if n['path']==['USB']])!=1:raise ValueError('USB root ambiguous')
    return result

def extract(master,output):
    from pyrekordbox import Rekordbox6Database
    from sqlalchemy import text,bindparam
    db=Rekordbox6Database(path=master,db_dir=master.parent)
    try:
        nodes=[dict(id=str(p['ID']),name=p['Name'],parent=str(p['ParentID'] or ''),attribute=int(p['Attribute']),seq=p['Seq']) for p in db.session.execute(text('select ID,Name,ParentID,Attribute,Seq from djmdPlaylist')).mappings()]
        selected=[p['id'] for p in usb_profile(nodes,{})]
        entries={}
        query=text('select PlaylistID,ContentID,TrackNo from djmdSongPlaylist where PlaylistID in :ids order by PlaylistID,TrackNo').bindparams(bindparam('ids',expanding=True))
        for offset in range(0,len(selected),400):
            for e in db.session.execute(query,dict(ids=selected[offset:offset+400])).mappings():
                entries.setdefault(str(e['PlaylistID']),[]).append(dict(id=str(e['ContentID'])))
        result=dict(usb_profile=usb_profile(nodes,entries))
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    finally:db.close()

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--master',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();extract(a.master,a.output)
