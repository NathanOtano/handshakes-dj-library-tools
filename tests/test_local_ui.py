import http.client
import json
from pathlib import Path
import re
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from dj_local_ui import Jobs, make_server

WORKER = r'''
import json,sys,time,subprocess
from pathlib import Path
root=Path(sys.argv[1]);name=sys.argv[2];mode=sys.argv[3]
if mode=='stale':raise SystemExit(0)
if mode=='slow':time.sleep(.6)
if mode=='fail':raise SystemExit(7)
if mode=='tree':
    child="from pathlib import Path;import sys,time;root=Path(sys.argv[1]);[(root.joinpath('heartbeat').write_text(str(i)),time.sleep(.05)) for i in range(50)]"
    subprocess.Popen([sys.executable,'-c',child,str(root)],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    time.sleep(5)
if mode=='stop':
    deadline=time.monotonic()+10
    while not (root/'STOP').exists() and time.monotonic()<deadline:time.sleep(.03)
    raise SystemExit(9)
if name=='audit':data=dict(status='fail' if mode=='bad-audit' else 'pass',tracks=120,unbucketed=['7'] if mode=='bad-audit' else [],elements=43,errors=['selected_tracks_without_bucket'] if mode=='bad-audit' else [])
elif name=='prepare':data=dict(status='preview',identity=dict(label='TEST',fs='FAT32',free=10737418240,serial='TEMPORARY'),counts=dict(new=2,replace=1,reuse=117),backup_bytes=2000)
else:
    (root/'copied-to-fixture.txt').write_text('fixture only')
    data=dict(status='pass',copied_files=3)
(root/(name+'.json')).write_text(json.dumps(data),encoding='utf8')
if mode=='bad-audit':raise SystemExit(1)
'''

def fixture(root, modes=None):
    root=Path(root);root.mkdir(parents=True, exist_ok=True)
    modes=modes or {}
    (root/'worker.py').write_text(WORKER,encoding='utf8')
    config=dict(version=1,libraryLabel='Bibliothèque de test',targetLabel='Clé temporaire TEST',runtime=str(root/'runtime'),sealFiles=[],tasks={})
    for name in ('audit','prepare','sync'):
        config['tasks'][name]=dict(commands=[[sys.executable,str(root/'worker.py'),str(root),name,modes.get(name,'ok')]],cwd=str(root),report=str(root/(name+'.json')))
    config['tasks']['sync']['stopFile']=str(root/'STOP')
    path=root/'config.json';path.write_text(json.dumps(config),encoding='utf8')
    return path

def wait(jobs):
    deadline=time.monotonic()+8
    while jobs.snapshot()['busy'] and time.monotonic()<deadline:time.sleep(.02)
    if jobs.snapshot()['busy']:raise AssertionError('Worker did not stop')
    return jobs.snapshot()

class LocalUI(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.path=fixture(self.root)
        self.jobs=Jobs(self.path)

    def tearDown(self):
        if self.jobs.snapshot()['busy']:
            self.jobs.stop();wait(self.jobs)
        self.temp.cleanup()

    def prepare(self):
        self.jobs.start('audit');self.assertIsNone(wait(self.jobs)['error'])
        self.jobs.start('prepare');state=wait(self.jobs);self.assertIsNone(state['error'])
        return state['planToken']

    def test_nominal_three_operations_and_single_use_confirmation(self):
        token=self.prepare()
        self.assertFalse((self.root/'copied-to-fixture.txt').exists())
        self.jobs.start('sync',token);state=wait(self.jobs)
        self.assertEqual('pass',state['reports']['sync']['status'])
        self.assertTrue((self.root/'copied-to-fixture.txt').exists())
        with self.assertRaises(ValueError):self.jobs.start('sync',token)

    def test_prepare_requires_successful_audit(self):
        with self.assertRaises(ValueError):self.jobs.start('prepare')

    def test_unknown_or_unconfirmed_operation_is_refused(self):
        for name in ('shell','../sync','sync'):
            with self.assertRaises(ValueError):self.jobs.start(name)

    def test_changed_plan_is_refused(self):
        token=self.prepare()
        (self.root/'prepare.json').write_text('{}')
        with self.assertRaises(ValueError):self.jobs.start('sync',token)
        self.assertFalse((self.root/'copied-to-fixture.txt').exists())

    def test_changed_config_is_refused(self):
        token=self.prepare()
        self.path.write_text(self.path.read_text()+' ')
        with self.assertRaises(ValueError):self.jobs.start('sync',token)

    def test_new_audit_invalidates_preview(self):
        self.prepare();self.jobs.start('audit');state=wait(self.jobs)
        self.assertIsNone(state['planToken']);self.assertNotIn('prepare',state['reports'])

    def test_restart_shows_history_but_requires_a_fresh_audit(self):
        self.jobs.start('audit');wait(self.jobs);self.jobs=Jobs(self.path)
        self.assertEqual(120,self.jobs.snapshot()['reports']['audit']['tracks'])
        self.assertFalse(self.jobs.snapshot()['auditFresh'])
        with self.assertRaises(ValueError):self.jobs.start('prepare')

    def test_parallel_jobs_are_refused(self):
        self.jobs=Jobs(fixture(self.root,{'audit':'slow'}))
        self.jobs.start('audit')
        with self.assertRaises(ValueError):self.jobs.start('audit')
        wait(self.jobs)

    def test_stale_report_does_not_pass(self):
        self.jobs.start('audit');wait(self.jobs)
        self.jobs=Jobs(fixture(self.root,{'audit':'stale'}))
        self.jobs.start('audit');state=wait(self.jobs)
        self.assertIsNotNone(state['error']);self.assertNotIn('audit',state['reports'])

    def test_command_failure_releases_busy_state(self):
        self.jobs=Jobs(fixture(self.root,{'audit':'fail'}));self.jobs.start('audit')
        state=wait(self.jobs);self.assertIsNotNone(state['error']);self.assertFalse(state['busy'])

    def test_bad_audit_keeps_bounded_issues_and_blocks_prepare(self):
        self.jobs=Jobs(fixture(self.root,{'audit':'bad-audit'}));self.jobs.start('audit')
        state=wait(self.jobs);self.assertEqual(1,state['reports']['audit']['unbucketedCount'])
        with self.assertRaises(ValueError):self.jobs.start('prepare')

    def test_cooperative_stop_on_fixture_copy(self):
        self.jobs=Jobs(fixture(self.root,{'sync':'stop'}));token=self.prepare()
        self.jobs.start('sync',token);time.sleep(.1);self.jobs.stop();state=wait(self.jobs)
        self.assertTrue((self.root/'STOP').exists());self.assertEqual('Arrêté',state['phase'])
        self.assertFalse((self.root/'copied-to-fixture.txt').exists())

    def test_report_summary_limits_rows(self):
        data=Jobs.summary(dict(status='fail',errors=list(range(2000)),private_value='not exposed'))
        self.assertEqual(30,len(data['errors']));self.assertEqual(2000,data['errorsCount'])
        self.assertNotIn('private_value',data)

    def test_local_cancellation_stops_owned_descendants(self):
        self.jobs=Jobs(fixture(self.root,{'audit':'tree'}));self.jobs.start('audit')
        deadline=time.monotonic()+3
        while not (self.root/'heartbeat').exists() and time.monotonic()<deadline:time.sleep(.02)
        self.assertTrue((self.root/'heartbeat').exists())
        self.jobs.stop();wait(self.jobs)
        before=(self.root/'heartbeat').read_text();time.sleep(.25)
        self.assertEqual(before,(self.root/'heartbeat').read_text())

class HTTPUI(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.server=make_server(fixture(self.temp.name),0)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.port=self.server.server_port
        self.origin='http://127.0.0.1:'+str(self.port)
        status,html=self.request('GET','/')
        self.assertEqual(200,status)
        self.token=re.search('name="dj-session" content="([^"]+)"',html).group(1)

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join()
        if self.server.jobs.snapshot()['busy']:wait(self.server.jobs)
        self.temp.cleanup()

    def request(self,method,path,body=None,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        conn.request(method,path,body,headers or {});r=conn.getresponse();data=r.read().decode();status=r.status;conn.close();return status,data

    def test_foreign_origin_and_missing_session_are_refused(self):
        body=json.dumps(dict(task='audit'))
        self.assertEqual(403,self.request('POST','/api/start',body,{'Origin':'https://example.com','X-DJ-Token':self.token})[0])
        self.assertEqual(403,self.request('POST','/api/start',body,{'Origin':self.origin})[0])

    def test_bad_host_and_traversal_are_refused(self):
        self.assertEqual(403,self.request('GET','/',headers={'Host':'evil.example'})[0])
        self.assertEqual(404,self.request('GET','/../config/local-ui.json')[0])

    def test_port_cannot_be_shared_with_another_server(self):
        with self.assertRaises(OSError):make_server(fixture(Path(self.temp.name)/'second'),self.port)

    def test_browser_cannot_supply_commands_or_paths(self):
        headers={'Origin':self.origin,'X-DJ-Token':self.token}
        self.assertEqual(409,self.request('POST','/api/start',json.dumps(dict(task='audit',command=['whoami'])),headers)[0])
        self.assertFalse(self.server.jobs.snapshot()['busy'])

    def test_real_http_audit_and_state(self):
        headers={'Origin':self.origin,'X-DJ-Token':self.token}
        self.assertEqual(200,self.request('POST','/api/start',json.dumps(dict(task='audit')),headers)[0])
        wait(self.server.jobs)
        status,body=self.request('GET','/api/state');self.assertEqual(200,status)
        self.assertEqual(120,json.loads(body)['reports']['audit']['tracks'])

if __name__=='__main__':unittest.main()
