"""Explicit local adapter to an installed audit and verified native exporter.

Uses private operator settings; no bundled key, collection, model or player API.
Audit writes snapshots/reports only; prepare writes local verification/preview;
sync is the sole live-target operation and delegates the exporter's own guards.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')

def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def run(command):
    print('Étape locale : ' + Path(str(command[1])).name, flush=True)
    subprocess.run([str(a) for a in command], check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

def closed_apps():
    if sys.platform != 'win32':
        raise ValueError('Cet adaptateur natif est prévu pour Windows')
    code = "@(Get-Process rekordbox,rekordboxAgent,rbinit,rbcloudagent,chkdsk -ErrorAction SilentlyContinue).Count"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', code], capture_output=True, text=True, check=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if int(result.stdout.strip()):
        raise ValueError('Fermer Rekordbox et attendre la fin des réparations du volume')

def hashes(source):
    names = ('master.db', 'master.db-wal', 'master.db-shm', 'masterPlaylists6.xml', 'automixPlaylist6.xml')
    return {n: sha(source/n) for n in names if (source/n).is_file()}

def perform(action, settings_path):
    s = read(settings_path)
    runtime = Path(s['runtime']);runtime.mkdir(parents=True, exist_ok=True)
    source = Path(s['sourceRoot'])
    closed_apps()
    if action == 'audit':
        before = hashes(source)
        if 'master.db' not in before:
            raise ValueError('Collection source introuvable')
        snapshot = runtime / ('snapshot-' + str(time.time_ns()))
        snapshot.mkdir()
        for name, expected in before.items():
            shutil.copy2(source/name, snapshot/name)
            if sha(snapshot/name) != expected:
                raise ValueError('Copie de collection incohérente')
        if hashes(source) != before:
            raise ValueError('Collection modifiée pendant la copie')
        report = runtime/'audit.json'
        run([s['python'], Path(s['libraryRepo'])/'scripts/audit_dj_usb_readiness.py', '--master', snapshot/'master.db', '--db-dir', snapshot, '--taxonomy', s['taxonomy'], '--output', report])
        if hashes(source) != before:
            raise ValueError('Collection modifiée pendant la vérification')
        save(runtime/'source-audit.json', dict(master_hashes=before))
        save(runtime/'snapshot.json', dict(master=str(snapshot/'master.db')))
        print('Bibliothèque vérifiée sans modification des originaux.')
        return
    audit = read(runtime/'source-audit.json')
    if hashes(source) != audit['master_hashes']:
        raise ValueError('La collection a changé : relancer la vérification')
    manifest_path = runtime/'export-input/prepare.json'
    exporter = Path(s['exporterRepo'])/'scripts'
    if action == 'prepare':
        snapshot = Path(read(runtime/'snapshot.json')['master'])
        if sha(snapshot) != audit['master_hashes']['master.db']:
            raise ValueError('La copie source a changé : relancer la vérification')
        run([s['python'], Path(__file__).with_name('dj_ui_profile.py'), '--master', snapshot, '--output', runtime/'profile.json'])
        run([s['python'], exporter/'prepare_usb_export_xml.py', '--master', snapshot, '--profile-audit', runtime/'profile.json', '--analysis-root', source/'share', '--output', runtime/'export-input'])
    manifest = read(manifest_path)
    if manifest['source_sha256'] != audit['master_hashes']['master.db']:
        raise ValueError('L’export local ne correspond plus à la collection. Reconstruire le lot avant de synchroniser.')
    base = [s['python'], exporter/'deploy_usb_export.py', '--manifest', manifest_path, '--stage', s['stage'], '--key-source', s['keySource'], '--run', runtime/'deployment', '--source-audit', runtime/'source-audit.json']
    if action == 'prepare':
        run([s['python'], exporter/'verify_usb_export_stage.py', '--manifest', manifest_path, '--stage', s['stage'], '--key-source', s['keySource'], '--pdb-source', s['pdbSource']])
        for volume in s['requiredRoots']:
            if not Path(volume).is_dir():
                raise ValueError('Clé requise absente : ' + volume)
        run(base)
    elif action == 'sync':
        run(base + ['--apply'])
    else:
        raise ValueError('Opération inconnue')

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('audit', 'prepare', 'sync'))
    p.add_argument('--settings', type=Path, required=True)
    a = p.parse_args()
    try:
        perform(a.action, a.settings)
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        message = str(error) if not isinstance(error, subprocess.CalledProcessError) else 'La préparation native a refusé ce lot (code %s). Consulter le journal.' % error.returncode
        print('DJ_UI_ERROR: ' + message, file=sys.stderr)
        raise SystemExit(1)
