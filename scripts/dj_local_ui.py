"""Loopback-only UI for explicitly configured trusted DJ commands. No shell or scheduling."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]

def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))

def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')
    os.replace(temp, path)

class Jobs:
    def __init__(self, config_path):
        self.config_path = Path(config_path).resolve()
        self.config = read_json(self.config_path)
        if self.config.get('version') != 1 or set(self.config['tasks']) != {'audit', 'prepare', 'sync'}:
            raise ValueError('Configuration locale invalide')
        for name, task in self.config['tasks'].items():
            if not isinstance(task['commands'], list) or any(not isinstance(c, list) or not c or any(not isinstance(a, str) for a in c) for c in task['commands']):
                raise ValueError('Arguments de commande invalides : ' + name)
        self.runtime = self.path(self.config['runtime'])
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.state = dict(busy=False, task=None, phase='Prêt', reports={}, error=None, planToken=None, auditFresh=False)
        audit_report = self.path(self.config['tasks']['audit']['report'])
        if audit_report.is_file():
            try:
                self.state['reports']['audit'] = self.summary(read_json(audit_report))
            except (OSError, ValueError, TypeError):
                pass
        self.seal = {}
        self.process = None
        self.stop_requested = False

    def path(self, value):
        p = Path(value)
        return p.resolve() if p.is_absolute() else (ROOT / p).resolve()

    def snapshot(self):
        with self.lock:
            result = json.loads(json.dumps(self.state))
            result.update(libraryLabel=self.config.get('libraryLabel', 'Bibliothèque'), targetLabel=self.config.get('targetLabel', 'Clé USB'), configured={n: bool(t['commands']) for n, t in self.config['tasks'].items()})
            if self.state['busy'] and self.state['task'] == 'sync':
                try:
                    progress = read_json(self.path(self.config['tasks']['sync']['report']))
                    result['progress'] = {k: progress[k] for k in ('phase', 'completed_files', 'total_files', 'copied_bytes') if k in progress}
                except (OSError, ValueError):
                    pass
            return result

    def start(self, task_name, confirmation=None):
        with self.lock:
            if task_name not in self.config['tasks']:
                raise ValueError('Opération inconnue')
            if self.state['busy']:
                raise ValueError('Une opération est déjà en cours')
            task = self.config['tasks'][task_name]
            if not task['commands']:
                raise ValueError('Commande à configurer localement')
            if task_name == 'prepare' and (not self.state['auditFresh'] or self.state['reports'].get('audit', {}).get('status') != 'pass'):
                raise ValueError('Vérifier la bibliothèque avant de préparer')
            if task_name == 'sync':
                if not confirmation or not secrets.compare_digest(confirmation, self.state.get('planToken') or ''):
                    raise ValueError('Confirmer l’aperçu courant avant copie')
                if not self.seal or any(not Path(p).is_file() or digest(p) != h for p, h in self.seal.items()):
                    self.state['planToken'] = None
                    raise ValueError('L’aperçu ou ses sources ont changé : préparer à nouveau')
            self.state['planToken'] = None
            self.seal = {}
            self.stop_requested = False
            self.state.update(busy=True, task=task_name, phase='Démarrage', error=None)
            self.state['reports'].pop(task_name, None)
            if task_name == 'audit':
                self.state['auditFresh'] = False
                self.state['reports'].pop('prepare', None)
                self.state['reports'].pop('sync', None)
            if task.get('stopFile') and self.path(task['stopFile']).exists():
                self.state.update(busy=False, error='Un arrêt précédent est présent : vérifier le lot avant reprise')
                raise ValueError(self.state['error'])
            threading.Thread(target=self._run, args=(task_name,), daemon=True).start()

    def _run(self, name):
        task = self.config['tasks'][name]
        report = self.path(task['report'])
        started = time.time_ns()
        try:
            before_stat = report.stat().st_mtime_ns if report.is_file() else None
            with (self.runtime / (name + '.log')).open('w', encoding='utf8') as log:
                for number, command in enumerate(task['commands'], 1):
                    with self.lock:
                        if self.stop_requested:
                            raise InterruptedError('Arrêt demandé')
                        self.state['phase'] = 'Étape %d / %d' % (number, len(task['commands']))
                        env = dict(os.environ, PYTHONIOENCODING='utf-8')
                        self.process = subprocess.Popen(command, cwd=self.path(task['cwd']), env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), start_new_session=os.name != 'nt')
                        self.state['pid'] = self.process.pid
                    exit_code = self.process.wait()
                    with self.lock:
                        self.process = None
                    if self.stop_requested:
                        raise InterruptedError('Arrêt demandé')
                    if exit_code:
                        if name == 'audit' and report.is_file() and report.stat().st_mtime_ns != before_stat:
                            data = read_json(report)
                            if data.get('status') == 'fail':
                                self.state['reports']['audit'] = self.summary(data)
                        log.flush()
                        with (self.runtime / (name + '.log')).open('rb') as diagnostic:
                            diagnostic.seek(max(0, diagnostic.seek(0, 2) - 4000))
                            tail = diagnostic.read().decode('utf8', errors='replace')
                        messages = [line.split('DJ_UI_ERROR: ', 1)[1][:500] for line in tail.splitlines() if line.startswith('DJ_UI_ERROR: ')]
                        raise ValueError(messages[-1] if messages else 'La commande a refusé l’opération (code %d). Consulter les détails.' % exit_code)
            data = read_json(report)
            expected = 'preview' if name == 'prepare' else 'pass'
            if data.get('status') != expected:
                raise ValueError('Le rapport ne valide pas cette opération')
            if report.stat().st_mtime_ns == before_stat or report.stat().st_mtime_ns < started - 2_000_000_000:
                raise ValueError('Rapport périmé : aucun nouveau résultat')
            if name == 'prepare' and (not isinstance(data.get('identity'), dict) or not isinstance(data.get('counts'), dict)):
                raise ValueError('L’aperçu doit identifier la clé et les changements')
            with self.lock:
                self.state['reports'][name] = self.summary(data)
                if name == 'audit':
                    self.state['auditFresh'] = True
                if name == 'prepare':
                    paths = [report, self.config_path] + [self.path(p) for p in self.config.get('sealFiles', [])]
                    self.seal = {str(p): digest(p) for p in paths}
                    self.state['planToken'] = secrets.token_urlsafe(24)
                self.state['phase'] = 'Copie vérifiée' if name == 'sync' else 'Terminé'
        except Exception as exc:
            with self.lock:
                self.state.update(error=str(exc), phase='Arrêté' if self.stop_requested else 'À vérifier', planToken=None)
        finally:
            with self.lock:
                self.process = None
                self.state.update(busy=False, pid=None)
                write_json(self.runtime / 'ui-status.json', self.snapshot())

    @staticmethod
    def summary(data):
        allowed = ('status', 'tracks', 'playlists', 'folders', 'elements', 'maxPlaylistTracks', 'provisional', 'unbucketed', 'missingFiles', 'errors', 'counts', 'identity', 'required_free_bytes', 'backup_bytes', 'copied_files', 'reused_audio', 'verified_metadata', 'warning')
        result = {k: data[k] for k in allowed if k in data}
        for field in ('errors', 'unbucketed', 'missingFiles'):
            if isinstance(result.get(field), list):
                result[field + 'Count'] = len(result[field])
                result[field] = result[field][:30]
        if 'identity' in result:
            result['identity'] = {k: v for k, v in result['identity'].items() if k in ('label', 'fs', 'free', 'serial', 'style', 'volume')}
        return result

    def stop(self):
        with self.lock:
            if not self.state['busy']:
                return
            self.stop_requested = True
            task = self.config['tasks'][self.state['task']]
            if self.state['task'] == 'sync':
                if not task.get('stopFile'):
                    self.stop_requested = False
                    raise ValueError('Cet exporteur ne propose pas d’arrêt coopératif')
                self.path(task['stopFile']).touch()
            elif self.process:
                if self.process.poll() is None:
                    if os.name == 'nt':
                        subprocess.run(['taskkill.exe', '/PID', str(self.process.pid), '/T', '/F'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    else:
                        os.killpg(self.process.pid, signal.SIGTERM)
            self.state['phase'] = 'Arrêt demandé'

def make_server(config, port=0):
    jobs = Jobs(config)
    token = secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, body, content_type='application/json; charset=utf-8'):
            data = body.encode('utf8') if isinstance(body, str) else body
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(data)

        def host_ok(self):
            return self.headers.get('Host') == '127.0.0.1:' + str(self.server.server_port)

        def do_GET(self):
            if not self.host_ok():
                return self.send(403, '{}')
            if self.path == '/api/state':
                return self.send(200, json.dumps(jobs.snapshot(), ensure_ascii=False))
            if self.path == '/api/log':
                name = jobs.state.get('task')
                path = jobs.runtime / ((name or 'audit') + '.log')
                tail = ''
                if path.is_file():
                    with path.open('rb') as f:
                        f.seek(max(0, path.stat().st_size - 12000))
                        tail = f.read().decode('utf8', errors='replace')
                return self.send(200, json.dumps({'text': tail}))
            paths = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css', '/favicon.svg': 'favicon.svg'}
            if self.path not in paths:
                return self.send(404, '{}')
            name = paths[self.path]
            body = (ROOT/'local_ui'/name).read_text(encoding='utf8').replace('__SESSION__', token)
            mime = {'html': 'text/html', 'js': 'application/javascript', 'css': 'text/css', 'svg': 'image/svg+xml'}[name.split('.')[-1]]
            self.send(200, body, mime + '; charset=utf-8')

        def do_POST(self):
            origin = 'http://127.0.0.1:' + str(self.server.server_port)
            if not self.host_ok() or self.headers.get('Origin') != origin or not secrets.compare_digest(self.headers.get('X-DJ-Token', ''), token):
                return self.send(403, json.dumps({'error': 'Origine ou session refusée'}))
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 4096:
                    raise ValueError('Requête invalide')
                data = json.loads(self.rfile.read(size))
                if self.path == '/api/start':
                    if set(data) - {'task', 'confirmation'}:
                        raise ValueError('Paramètres non autorisés')
                    jobs.start(data['task'], data.get('confirmation'))
                elif self.path == '/api/stop':
                    jobs.stop()
                elif self.path == '/api/shutdown':
                    if jobs.snapshot()['busy']:
                        raise ValueError('Attendre la fin ou demander un arrêt contrôlé')
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                else:
                    return self.send(404, '{}')
                self.send(200, json.dumps(jobs.snapshot(), ensure_ascii=False))
            except (ValueError, KeyError, TypeError, OSError) as exc:
                self.send(409, json.dumps({'error': str(exc)}, ensure_ascii=False))
    class LocalServer(ThreadingHTTPServer):
        allow_reuse_address = False
        def server_bind(self):
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            super().server_bind()
    server = LocalServer(('127.0.0.1', port), Handler)
    server.jobs = jobs
    return server

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--port', type=int, default=0)
    args = parser.parse_args()
    server = make_server(args.config, args.port)
    instance = (server.jobs.runtime/'server.lock').open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            instance.seek(0, 2)
            if instance.tell() == 0:
                instance.write(b'1');instance.flush()
            instance.seek(0)
            msvcrt.locking(instance.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(instance, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        server.server_close();instance.close()
        raise ValueError('Une interface utilise déjà cette configuration')
    write_json(server.jobs.runtime/'server.json', dict(pid=os.getpid(), url='http://127.0.0.1:%d/' % server.server_port, config=str(args.config.resolve())))
    print('http://127.0.0.1:%d/' % server.server_port, flush=True)
    try:
        server.serve_forever()
    finally:
        if server.jobs.snapshot()['busy']:
            server.jobs.stop()
            while server.jobs.snapshot()['busy']:
                time.sleep(.1)
        server.server_close()
        instance.close()

if __name__ == '__main__':
    main()
