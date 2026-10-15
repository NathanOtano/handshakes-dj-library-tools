"""Windows launcher: hidden local server, then the user's browser. No autostart."""
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser

ROOT=Path(__file__).resolve().parents[1]

def launch(open_browser=True):
    config=ROOT/'config/local-ui.json'
    if not config.is_file():raise ValueError('Configurer config/local-ui.json avant de lancer l’interface.')
    settings=json.loads(config.read_text(encoding='utf-8-sig'))
    runtime=Path(settings['runtime'])
    if not runtime.is_absolute():runtime=ROOT/runtime
    runtime.mkdir(parents=True,exist_ok=True)
    receipt=runtime/'server.json'
    if receipt.is_file():
        try:
            old=json.loads(receipt.read_text(encoding='utf8'))
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(old['url']+'api/state',timeout=1) as response:state=json.load(response)
            if state.get('libraryLabel')==settings.get('libraryLabel') and state.get('targetLabel')==settings.get('targetLabel'):
                if open_browser:webbrowser.open(old['url'])
                return old
        except (OSError,ValueError,KeyError):pass
    with (runtime/'launcher.log').open('ab') as log:
        process=subprocess.Popen([sys.executable,str(ROOT/'scripts/dj_local_ui.py'),'--config',str(config)],cwd=ROOT,stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        if process.poll() is not None:raise ValueError('Le serveur n’a pas démarré. Consulter launcher.log.')
        if receipt.is_file():
            current=json.loads(receipt.read_text(encoding='utf8'))
            if current['pid']==process.pid:
                if open_browser:webbrowser.open(current['url'])
                return current
        time.sleep(.1)
    process.terminate()
    raise ValueError('Le démarrage a dépassé le délai ; aucun travail DJ n’a été lancé.')

if __name__=='__main__':
    try:launch('--no-open' not in sys.argv)
    except Exception as error:
        import tkinter.messagebox
        tkinter.messagebox.showerror('Handshakes DJ',str(error))
