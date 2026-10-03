"""Small stdlib-only RPC helper executed inside Linux guests through QEMU GA.

This module never receives task verifiers. It is sent as Python source, not
installed as a persistent guest daemon. Commands are argv arrays, never a shell.
"""
import ast
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import stat
import subprocess
import sys
import tempfile
import zipfile


OUTPUT_FILES = {'result.json', 'messages.json', 'checkpoint.json', 'events.jsonl', 'worker.log'}
CONTROL_DIR = Path('/run/cyber-agent-flow-eval')
MAINTENANCE_LOCK = Path('/run/caf-application-maintenance.lock')
MAINTENANCE_PENDING = Path('/var/lib/caf-application-maintenance.pending')
RPC_INPUT_LIMIT = 1024 * 1024
# Host startup checks this contract before it dispatches work to a VM. Helpers
# are sent from the host; no persistent guest-side evaluator install is needed.
SUPPORTED_OPERATIONS = frozenset({
    'hint_request', 'hint_reply', 'probe', 'mkdir', 'write', 'stat', 'read',
    'unlink', 'start', 'preflight', 'status', 'stop', 'pack', 'hook',
})


def command(argv, timeout=20, allow_failure=False):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if result.returncode and not allow_failure:
        raise ValueError(f'Guest command failed ({result.returncode}): {result.stderr[-2000:]}')
    return result.stdout


def file_identity(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(65536), b''):
            h.update(chunk)
    return {'size': path.stat().st_size, 'sha256': h.hexdigest()}


def service_status(unit):
    output = command(['systemctl', 'show', unit, '--property=LoadState,ActiveState,SubState,Result,ExecMainStatus'],
                     allow_failure=True)
    result = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
    if not result.get('LoadState') or not result.get('ActiveState'):
        raise ValueError('Cannot inspect guest systemd service state')
    return result


@contextmanager
def service_control(data):
    # QGA operations can outlive a disconnected host command. Serialize start
    # and stop, with a cancellation tombstone so a delayed start cannot revive
    # a trial after the host has already confirmed its stop.
    CONTROL_DIR.mkdir(mode=0o700, exist_ok=True)
    path = CONTROL_DIR / (data['unit'] + '.control')
    with path.open('a+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield stream


def _dispatch(data):
    op = data['op']
    if op == 'hint_request':
        path = Path(data['path']) / 'hint-request.json'
        if not path.exists():
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError('Hint request must be a regular file')
            content = stream.read(4001)
        if len(content) > 4000:
            raise ValueError('Hint request too large')
        return json.loads(content)
    if op == 'hint_reply':
        root = Path(data['path'])
        # Atomic root-owned reply; only released assistance reaches the worker.
        fd, temporary = tempfile.mkstemp(prefix='.hint-', dir=root)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(data['response'], stream)
            os.chmod(temporary, 0o644)
            os.replace(temporary, root / 'hint-response.json')
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return {'ok': True}
    if op == 'probe':
        root = Path(data['engine']['path'])
        for name in ('mcp_client.py', 'mcp_kali.py', 'session_logger.py'):
            if not (root / name).is_file():
                raise ValueError(f'Missing CAF file: {name}')
        try:
            tree = ast.parse((root / 'mcp_client.py').read_text())
            session = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'MCPSession')
            init = next(n for n in session.body if isinstance(n, ast.FunctionDef) and n.name == '__init__')
        except (SyntaxError, StopIteration) as exc:
            raise ValueError(f'Cannot find a compatible MCPSession in guest engine.path={root}; '
                             'check the configured cyber-agent-flow checkout inside the participant VM') from exc
        missing = {'allowed_tools', 'guidance_text', 'reveal_network_policy'} - {a.arg for a in [*init.args.args, *init.args.kwonlyargs]}
        if missing:
            raise ValueError(f'CAF engine lacks evaluation controls at guest engine.path={root}: '
                             f'MCPSession.__init__ is missing {", ".join(sorted(missing))}. '
                             'Update this cyber-agent-flow checkout inside the participant VM, or correct engine.path '
                             'and engine.python in the host runtime config. Updating the host orchestrator/evaluator '
                             'does not update the guest engine.')
        files = [*root.glob('*.py'), *root.glob('requirements*.txt')]
        source = {str(p.relative_to(root)): p.read_text() for p in sorted(files)}
        runtime_script = '''import sys, json
from importlib.metadata import version, PackageNotFoundError
packages = {}
for name in ['mcp', 'ollama', 'requests', 'PyYAML', 'psutil']:
    try: packages[name] = version(name)
    except PackageNotFoundError: packages[name] = None
print(json.dumps({'python': sys.version, 'executable': sys.executable, 'dependencies': packages}))
'''
        runtime = json.loads(command([data['engine']['python'], '-c', runtime_script]))
        if (root / '.git').exists():
            git_args = ['git', '-c', 'safe.directory=' + str(root), '-c', 'core.fsmonitor=false', '-C', str(root)]
            runtime['engine_revision'] = command([*git_args, 'rev-parse', 'HEAD']).strip()
            runtime['engine_modified'] = bool(command([*git_args, 'status', '--porcelain', '--untracked-files=no']).strip())
        if data.get('user'):
            pwd.getpwnam(data['user'])
        systemd = command(['systemctl', 'show', '--property=Version', '--value']).strip()
        match = re.search(r'\d+', systemd)
        if not match or int(match.group()) < 250:
            raise ValueError('Guest systemd 250+ is required for cgroup lifetime tracking')
        return {'engine': hashlib.sha256(json.dumps(source, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                'runtime': runtime}
    if op == 'mkdir':
        path = Path(data['path'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o755)
        path.mkdir(mode=0o755)  # Exclusive UUID workspace, never reuse a previous trial.
        path.chmod(0o755)
        attempt = path / 'attempt'
        attempt.mkdir(mode=0o700)
        user = pwd.getpwnam(data['user'])
        os.chown(attempt, user.pw_uid, user.pw_gid)
        (path / 'cyber_agent_flow_eval').mkdir()
        (path / 'cyber_agent_flow_eval').chmod(0o755)
        return {}
    if op == 'write':
        path = Path(data['path'])
        payload = base64.b64decode(data['content'], validate=True)
        mode = 'xb' if data['offset'] == 0 else 'r+b'
        with path.open(mode) as stream:
            stream.seek(data['offset'])
            stream.write(payload)
        path.chmod(0o644)
        return {}
    if op == 'stat':
        path = Path(data['path'])
        if not path.is_file() or path.is_symlink():
            raise ValueError('Transfer requires a regular file')
        if path.stat().st_size > data['limit']:
            raise ValueError('File exceeds max_transfer_bytes')
        return file_identity(path)
    if op == 'read':
        with Path(data['path']).open('rb') as stream:
            stream.seek(data['offset'])
            return {'content': base64.b64encode(stream.read(data['length'])).decode()}
    if op == 'unlink':
        Path(data['path']).unlink(missing_ok=True)
        return {}
    if op == 'start':
        user = pwd.getpwnam(data['user'])
        attempt = Path(data['path']) / 'attempt'
        for name in ('input.json', 'catalog.json'):
            os.chown(attempt / name, user.pw_uid, user.pw_gid)
            os.chmod(attempt / name, 0o600)
        # systemd owns the entire tool process tree even if the host disconnects.
        argv = ['systemd-run', '--quiet', '--unit=' + data['unit'],
                '--property=Type=exec', '--property=ExitType=cgroup', '--property=RemainAfterExit=yes',
                '--property=RuntimeMaxSec=' + str(data['seconds']),
                '--property=TimeoutStopSec=5', '--property=KillMode=control-group',
                '--property=User=' + data['user'],
                '--property=WorkingDirectory=' + data['engine']['path'],
                '--property=StandardOutput=append:' + str(attempt / 'worker.log'),
                '--property=StandardError=append:' + str(attempt / 'worker.log'),
                '--setenv=HOME=' + user.pw_dir,
                '--setenv=CAF_RUN_BASE_DIR=' + str(attempt),
                '--setenv=CAF_TOOLS_CONFIG_PATH=' + str(attempt / 'catalog.json')]
        if data.get('environment_file'):
            argv.append('--property=EnvironmentFile=' + data['environment_file'])
        # Escape systemd specifiers in caller-controlled property/env values.
        argv = [v.replace('%', '%%') for v in argv]
        argv += ['--', data['engine']['python'], str(Path(data['path']) / 'cyber_agent_flow_eval/worker.py'), str(attempt)]
        with service_control(data) as control:
            control.seek(0)
            if control.read():
                raise ValueError('Guest trial was cancelled before service start')
            command(argv)
        return {}
    if op == 'preflight':
        units = data.get('units', [])
        if not isinstance(units, list) or len(units) > 128 or any(not isinstance(unit, str) or not re.fullmatch(r'caf-(?:eval(?:-hook)?|orchestrator)-[a-f0-9]+', unit) for unit in units):
            raise ValueError('Invalid recorded cleanup units')
        stopped = []
        for unit in units:
            state = _dispatch({'op':'stop', 'unit':unit})
            stopped.append({'unit':unit, 'state':state.get('ActiveState')})
        output = command(['systemctl', 'list-units', '--all', '--plain', '--no-legend', '--no-pager',
                          'caf-eval-*', 'caf-orchestrator-*'])
        remaining = []
        for line in output.splitlines():
            fields = line.split()
            if len(fields) >= 4 and fields[0].startswith(('caf-eval-', 'caf-orchestrator-')) and fields[2] not in ('inactive', 'failed'):
                remaining.append({'unit':fields[0], 'active':fields[2], 'sub':fields[3]})
        if remaining:
            raise ValueError('VM has active CAF services not confirmed stopped: ' + json.dumps(remaining))
        return {'ready':True, 'stopped':stopped}
    if op == 'status':
        return service_status(data['unit'])
    if op == 'stop':
        with service_control(data) as control:
            control.write('cancelled\n')
            control.flush()
            os.fsync(control.fileno())
            status = service_status(data['unit'])
            if status['LoadState'] != 'not-found':
                command(['systemctl', 'stop', data['unit']], timeout=15)
                status = service_status(data['unit'])
            if status.get('ActiveState') not in {'inactive', 'failed'}:
                raise ValueError(f'Guest service is not stopped: {status}')
        return status
    if op == 'pack':
        root = Path(data['path'])
        files = []
        for path in root.rglob('*'):
            relative = path.relative_to(root).as_posix()
            selected = relative in OUTPUT_FILES or (path.parts[len(root.parts)] in {'model_calls', 'runs'})
            if not selected or path.is_dir():
                continue
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('Guest output must contain only regular files within the trial')
            files.append(path)
        if sum(p.stat().st_size for p in files) > data['limit']:
            raise ValueError('Guest output exceeds max_transfer_bytes')
        fd, archive = tempfile.mkstemp(prefix='caf-eval-output-', suffix='.zip')
        os.close(fd)
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
            for path in files:
                bundle.write(path, path.relative_to(root).as_posix())
        return {'path': archive}
    if op == 'hook':
        # Keep potentially large hook output out of QGA's captured-output buffer.
        if data.get('log_path'):
            path = data['log_path']
            with open(path, 'x'):
                pass
        else:
            fd, path = tempfile.mkstemp(prefix='caf-eval-hook-', suffix='.log')
            os.close(fd)
        argv = ['systemd-run', '--quiet', '--unit=' + data['unit'],
                '--property=Type=exec', '--property=ExitType=cgroup', '--property=RemainAfterExit=yes',
                '--property=RuntimeMaxSec=' + str(data['seconds']),
                '--property=TimeoutStopSec=5', '--property=KillMode=control-group',
                '--property=StandardOutput=append:' + path,
                '--property=StandardError=append:' + path]
        for key, prop in [('user', 'User'), ('cwd', 'WorkingDirectory'), ('environment_file', 'EnvironmentFile')]:
            if data.get(key):
                argv.append('--property=' + prop + '=' + data[key].replace('%', '%%'))
        if data.get('user'):
            argv.append('--setenv=HOME=' + pwd.getpwnam(data['user']).pw_dir)
        argv.extend(['--', *data['argv']])
        with service_control(data) as control:
            control.seek(0)
            if control.read():
                raise ValueError('Guest hook was cancelled before service start')
            command(argv)
        import time
        deadline = time.monotonic() + data['seconds'] + 15
        while time.monotonic() < deadline:
            status = service_status(data['unit'])
            if status.get('SubState') in {'exited', 'failed', 'dead'}:
                code = int(status.get('ExecMainStatus', 1)) if status.get('Result') == 'success' else 1
                return {'exitcode': code, 'log_path': path}
            time.sleep(0.2)
        raise ValueError('Guest hook exceeded its deadline')
    raise ValueError(f'Unknown guest operation: {op}')


def dispatch(data):
    # Serialize launches with offline application maintenance, including after a
    # host disconnect. Existing units are checked by the maintenance helper.
    if data.get('op') not in ('start', 'hook'):
        return _dispatch(data)
    with MAINTENANCE_LOCK.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Application maintenance is active; retry after it completes') from None
        if MAINTENANCE_PENDING.exists():
            raise ValueError('Application maintenance needs recovery before experiment launch')
        return _dispatch(data)


if __name__ == '__main__':
    try:
        # Upload requests arrive on stdin to avoid OS argument-size limits.
        request = sys.argv[1] if len(sys.argv) > 1 else sys.stdin.read(RPC_INPUT_LIMIT + 1)
        if len(request.encode()) > RPC_INPUT_LIMIT:
            raise ValueError('Guest RPC exceeds input limit')
        print(json.dumps(dispatch(json.loads(request))))
    except Exception as exc:
        print(json.dumps({'error': f'{type(exc).__name__}: {exc}'}))
        sys.exit(1)
