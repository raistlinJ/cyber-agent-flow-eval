"""Proxmox host backend: QEMU guest-agent exec and bounded binary transfers."""
from contextlib import contextmanager
from contextvars import ContextVar
import base64
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
import zipfile

from . import guest_agent
from .storage import read_json, write_json

CHUNK = 16 * 1024  # Base64 output stays well below QGA's captured-output limit.
UPLOAD_CHUNK = 512 * 1024  # Base64 JSON stays below qm --pass-stdin's 1 MiB limit.


_authorization = ContextVar('proxmox_authorization', default=None)


@contextmanager
def authorized_operations(check):
    """Optional caller-owned dispatch guard; trusted standalone CLI stays supported."""
    token = _authorization.set(check)
    try:
        yield
    finally:
        _authorization.reset(token)


class GuestAgent:
    def __init__(self, config, *, authorize=None):
        self.authorize = authorize if authorize is not None else _authorization.get()
        self.config = config
        self.script = Path(guest_agent.__file__).read_text()

    def qm(self, args, *, input_data=None):
        if self.authorize:
            self.authorize(args)
        try:
            result = subprocess.run(['qm', *map(str, args)], capture_output=True, text=True,
                                    input=input_data, timeout=self.config['command_timeout'])
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f'Proxmox qm unavailable or timed out: {exc}') from exc
        if result.returncode:
            raise ValueError(f'Proxmox qm failed: {result.stderr[-2000:]}')
        try:
            return json.loads(result.stdout)
        except ValueError as exc:
            raise ValueError('Proxmox qm returned invalid JSON') from exc

    def call(self, vmid, op, *, timeout=None, **data):
        timeout = timeout or self.config['command_timeout']
        deadline = time.monotonic() + timeout
        payload = json.dumps(dict(data, op=op))
        if op == 'write':
            if len(payload.encode()) > 1024 * 1024:
                raise ValueError('Upload RPC exceeds qm stdin limit')
            # A completed small write normally needs one qm process instead of
            # exec plus exec-status. If still running, poll its PID; never resend.
            result = self.qm(['guest', 'exec', vmid, '--pass-stdin', '1', '--synchronous', '1',
                              '--timeout', str(min(5, max(1, int(timeout) // 2))), '--',
                              self.config['guest_python'], '-c', self.script], input_data=payload)
        else:
            result = self.qm(['guest', 'exec', vmid, '--synchronous', '0', '--',
                              self.config['guest_python'], '-c', self.script, payload])
        if not result.get('exited') and type(result.get('pid')) is not int:
            raise ValueError('Guest agent did not return an execution PID')
        status = result
        while True:
            if status.get('exited'):
                if status.get('out-truncated') or status.get('err-truncated'):
                    raise ValueError('Guest agent truncated command output')
                if status.get('exitcode') != 0:
                    raise ValueError(f'Guest operation {op} failed: {status.get("out-data", "")} {status.get("err-data", "")}')
                try:
                    return json.loads(status.get('out-data', ''))
                except ValueError as exc:
                    raise ValueError(f'Guest operation {op} returned invalid JSON') from exc
            if time.monotonic() >= deadline:
                raise ValueError(f'Guest operation {op} timed out; guest PID {result["pid"]}')
            if status is not result:
                time.sleep(min(self.config['poll_seconds'], max(0, deadline - time.monotonic())))
            status = self.qm(['guest', 'exec-status', vmid, result['pid']])

    def put(self, vmid, path, content):
        if len(content) > self.config['max_transfer_bytes']:
            raise ValueError('Upload exceeds max_transfer_bytes')
        for offset in range(0, max(1, len(content)), UPLOAD_CHUNK):
            self.call(vmid, 'write', path=path, offset=offset,
                      content=base64.b64encode(content[offset:offset + UPLOAD_CHUNK]).decode())
        identity = self.call(vmid, 'stat', path=path, limit=self.config['max_transfer_bytes'])
        if identity != {'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}:
            raise ValueError('Guest upload checksum mismatch')

    def get(self, vmid, path):
        identity = self.call(vmid, 'stat', path=path, limit=self.config['max_transfer_bytes'])
        size = identity['size']
        if type(size) is not int or not 0 <= size <= self.config['max_transfer_bytes']:
            raise ValueError('Download exceeds max_transfer_bytes')
        content = bytearray()
        for offset in range(0, size, CHUNK):
            block = self.call(vmid, 'read', path=path, offset=offset, length=min(CHUNK, size - offset))
            decoded = base64.b64decode(block['content'], validate=True)
            if len(decoded) != min(CHUNK, size - offset):
                raise ValueError('Guest download was incomplete')
            content.extend(decoded)
        if hashlib.sha256(content).hexdigest() != identity['sha256']:
            raise ValueError('Guest download checksum mismatch; file changed during transfer')
        return bytes(content)


def unpack(content, destination, limit, allowed):
    """Extract only regular, relative, expected paths; validate before writing."""
    destination = Path(destination)
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = set()
        entries = archive.infolist()
        if sum(e.file_size for e in entries) > limit:
            raise ValueError('Expanded archive exceeds max_transfer_bytes')
        for entry in entries:
            name = entry.filename
            path = PurePosixPath(name)
            mode = (entry.external_attr >> 16) & 0o170000
            if (not name or path.is_absolute() or '..' in path.parts or '\\' in name
                    or path.as_posix() != name or name in names or mode not in {0, 0o100000}
                    or entry.is_dir() or not allowed(name)):
                raise ValueError(f'Unexpected or unsafe archive member: {name}')
            target = destination / name
            if target.exists() or target.is_symlink() or not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError(f'Archive would overwrite or escape destination: {name}')
            names.add(name)
        for entry in entries:
            path = destination / entry.filename
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as stream:
                stream.write(archive.read(entry))


class ProxmoxBackend:
    def __init__(self, config, engine, *, agent=None):
        self.config, self.engine = config, engine
        self.agent = agent or GuestAgent(config)
        self.vmid = config['participant_vmid']

    def lock(self):
        from .runner import lease
        return lease(Path('/var/lock') / f'cyber-agent-flow-eval-vm-{self.vmid}.lock')

    def identities(self):
        return self.agent.call(self.vmid, 'probe', engine=self.engine, user=self.config['user'])

    def stop(self, record):
        from .runner import CleanupError
        try:
            return self.agent.call(record.get('vmid', self.vmid), 'stop', unit=record['unit'])
        except Exception as exc:
            raise CleanupError(f'Cannot confirm guest trial cleanup for {record["unit"]}: {exc}. '
                               'Restore guest-agent access and resume before starting another experiment.') from exc

    def recover(self, output):
        # Previous host death can leave a guest worker alive until RuntimeMaxSec.
        for path in Path(output).glob('trials/*/attempt-*/transport.json'):
            record = read_json(path)
            if not record.get('stopped'):
                self.stop(record)
                record['stopped'] = True
                write_json(path, record)
            if not record.get('collected'):
                self.collect(path.parent, record)
                record['collected'] = True
                write_json(path, record)
        for path in Path(output).glob('trials/*/attempt-*/hook-*.json'):
            record = read_json(path)
            if not record.get('stopped'):
                self.stop(record)
                record['stopped'] = True
                write_json(path, record)

    def before_trial(self, directory):
        from .runner import PreparationError
        for number, hook in enumerate(self.config['before_trial']):
            record = {'vmid': hook['vmid'], 'unit': 'caf-eval-hook-' + uuid.uuid4().hex, 'stopped': False,
                      'argv': hook['argv'], 'started_at': datetime.now(timezone.utc).isoformat()}
            path = directory / f'hook-{number:03d}.json'
            write_json(path, record)
            try:
                result = self.agent.call(hook['vmid'], 'hook', timeout=hook['timeout_seconds'] + 30,
                                         unit=record['unit'], seconds=hook['timeout_seconds'], argv=hook['argv'],
                                         **{k: hook[k] for k in ('user', 'cwd', 'environment_file') if k in hook})
                (directory / f'hook-{number:03d}.log').write_bytes(self.agent.get(hook['vmid'], result['log_path']))
                self.agent.call(hook['vmid'], 'unlink', path=result['log_path'])
                if result['exitcode']:
                    raise ValueError(f'before_trial hook {number} failed; see hook log')
            except Exception as exc:
                raise PreparationError(f'before_trial hook {number} failed: {exc}') from exc
            finally:
                self.stop(record)
                record['stopped'] = True
                write_json(path, record)

    def collect(self, directory, record):
        vmid = record.get('vmid', self.vmid)
        result = self.agent.call(vmid, 'pack', path=record['path'] + '/attempt',
                                 limit=self.config['max_transfer_bytes'])
        try:
            content = self.agent.get(vmid, result['path'])
            # Collection is atomic at directory granularity and can be retried.
            destination = directory / 'guest-output'
            if destination.exists():
                return
            with tempfile.TemporaryDirectory(prefix='.collect-', dir=directory) as temporary:
                unpack(content, temporary, self.config['max_transfer_bytes'],
                       lambda n: n in guest_agent.OUTPUT_FILES or n.startswith(('model_calls/', 'runs/')))
                Path(temporary).rename(destination)
        finally:
            self.agent.call(vmid, 'unlink', path=result['path'])

    def launch(self, directory, seconds):
        token = uuid.uuid4().hex
        record = {'vmid': self.vmid, 'unit': 'caf-eval-' + token,
                  'path': self.config['workspace'].rstrip('/') + '/' + token, 'stopped': False,
                  'started_at': datetime.now(timezone.utc).isoformat()}
        record['argv'] = [self.engine['python'], record['path'] + '/cyber_agent_flow_eval/worker.py', record['path'] + '/attempt']
        def checkpoint(phase, **fields):
            if record.get('phase') != phase:
                record.setdefault('phase_events', []).append(dict(phase=phase, at=datetime.now(timezone.utc).isoformat()))
                record['phase_events'] = record['phase_events'][-20:]
            record.update(fields, phase=phase, updated_at=datetime.now(timezone.utc).isoformat())
            try:
                write_json(directory / 'transport.json', record)
            except OSError:
                if phase == 'preparing':
                    raise  # Initial recovery journal is mandatory before mutation.
                # Optional progress writes must never prevent worker cleanup.
        # Journal before the first mutating RPC. An interrupted start is recoverable.
        checkpoint('preparing')
        self.agent.call(self.vmid, 'mkdir', path=record['path'], user=self.config['user'])
        package = Path(__file__).resolve().parent
        uploads = [(record['path'] + '/cyber_agent_flow_eval/' + name, (package / name).read_bytes())
                   for name in ('__init__.py', 'worker.py', 'execution_service.py', 'engine.py', 'storage.py')]
        uploads += [(record['path'] + '/attempt/' + name, (directory / name).read_bytes()) for name in ('input.json', 'catalog.json')]
        checkpoint('uploading', files_uploaded=0, files_total=len(uploads), bytes_uploaded=0,
                   bytes_total=sum(len(content) for _, content in uploads))
        for path, content in uploads:
            self.agent.put(self.vmid, path, content)
            checkpoint('uploading', files_uploaded=record['files_uploaded'] + 1,
                       bytes_uploaded=record['bytes_uploaded'] + len(content))
        started = time.monotonic()
        status = {}
        try:
            checkpoint('starting', execution_started_at=datetime.now(timezone.utc).isoformat())
            self.agent.call(self.vmid, 'start', path=record['path'], unit=record['unit'], seconds=seconds,
                            user=self.config['user'], engine=self.engine,
                            environment_file=self.config.get('environment_file'))
            checkpoint('executing')
            deadline = started + seconds + 15
            while True:
                status = self.agent.call(self.vmid, 'status', unit=record['unit'])
                checkpoint('executing', service_status=status)
                if status.get('SubState') in {'exited', 'failed', 'dead'} or status.get('LoadState') == 'not-found':
                    break
                if time.monotonic() >= deadline:
                    status['Result'] = 'timeout'
                    break
                time.sleep(self.config['poll_seconds'])
        finally:
            checkpoint('stopping')
            self.stop(record)
            record.update(stopped=True, service_status=status, execution_seconds=time.monotonic() - started)
            checkpoint('collecting')
            self.collect(directory, record)
            checkpoint('collected', collected=True)
        result_path = directory / 'guest-output/result.json'
        if status.get('Result') == 'timeout':
            result = {'status': 'timeout', 'final_answer': None, 'errors': ['Guest wall-clock budget exceeded']}
        elif result_path.exists() and status.get('ExecMainStatus') == '0':
            result = read_json(result_path)
        else:
            result = {'status': 'error', 'final_answer': None, 'errors': ['Guest worker failed; see guest-output/worker.log']}
        result['execution_seconds'] = record['execution_seconds']
        return result

    def fetch_suite(self, guest_path, output):
        from .backends import guest_path as validate_path
        from .scenarioforge import FILES, load_suite
        validate_path(guest_path, 'suite ZIP path')
        if 'app_vmid' not in self.config:
            raise ValueError('fetch-suite requires backend.app_vmid')
        output = Path(output).resolve()
        if output.exists():
            raise ValueError('Suite output already exists; choose a new directory')
        content = self.agent.get(self.config['app_vmid'], guest_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.suite-', dir=output.parent) as temporary:
            unpack(content, temporary, self.config['max_transfer_bytes'], lambda n: n in FILES or n == 'manifest.json')
            tasks, snapshot = load_suite(temporary)
            Path(temporary).rename(output)
        return {'output': str(output), 'tasks': len(tasks), 'package_hash': snapshot['package_hash']}
