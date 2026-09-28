"""Install/stop a disposable, read-only HTTP fixture on corevm via QEMU GA.

Run the installer through `qm guest exec --pass-stdin 1` from the host. No files or
credentials from the real scenario are served. This is a lab fixture, not a
ScenarioForge-generated challenge or a hidden test dataset.
"""
import argparse
from pathlib import Path
import subprocess
import sys

UNIT = 'caf-eval-http-demo.service'
DIRECTORY = Path('/var/lib/caf-eval-http-demo')
SERVER = r'''
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys

PAGES = {
    '/': ('text/html', '<h1>Evaluation demo</h1><a href="/briefing.txt">Briefing</a> <a href="/archive/">Archive</a>'),
    '/briefing.txt': ('text/plain', 'First objective: FLAG{demo_entry}\n'),
    '/archive/': ('text/html', '<h1>Archive</h1><a href="/archive/note.txt">Read the note</a>'),
    '/archive/note.txt': ('text/plain', 'Second objective: FLAG{demo_archive}\n'),
}

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        page = PAGES.get(self.path)
        if page is None:
            self.send_error(404)
            return
        kind, text = page
        payload = text.encode()
        self.send_response(200)
        self.send_header('Content-Type', kind + '; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

if __name__ == '__main__':
    ThreadingHTTPServer((sys.argv[1], int(sys.argv[2])), Handler).serve_forever()
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', default='10.254.200.3', help='corevm HITL address')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--stop', action='store_true')
    args = parser.parse_args()
    if args.stop:
        subprocess.run(['systemctl', 'stop', UNIT], check=True)
        print('Stopped ' + UNIT)
        return
    if not 1 <= args.port <= 65535:
        parser.error('--port must be between 1 and 65535')
    DIRECTORY.mkdir(mode=0o755, exist_ok=True)
    if DIRECTORY.is_symlink():
        raise ValueError('Fixture directory must not be a symlink')
    DIRECTORY.chmod(0o755)
    script = DIRECTORY / 'server.py'
    if script.is_symlink():
        raise ValueError('Fixture script must not be a symlink')
    script.write_text(SERVER)
    script.chmod(0o644)
    subprocess.run(['systemctl', 'stop', UNIT], capture_output=True)
    subprocess.run(['systemctl', 'reset-failed', UNIT], capture_output=True)
    subprocess.run(['systemd-run', '--unit=' + UNIT,
                    '--property=DynamicUser=yes', '--property=NoNewPrivileges=yes',
                    '--property=RuntimeMaxSec=1h', '--property=KillMode=control-group',
                    '--', sys.executable, str(script), args.bind, str(args.port)], check=True)
    print(f'Demo requested at http://{args.bind}:{args.port}/; service stops after one hour.')
    print('Verify readiness with: systemctl status ' + UNIT)


if __name__ == '__main__':
    main()
