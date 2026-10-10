import base64
from contextlib import nullcontext
import io
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import zipfile

import pytest
import yaml

from cyber_agent_flow_eval import backends, guest_agent
from cyber_agent_flow_eval.backends import resolve_backend
from cyber_agent_flow_eval.engine import runtime_identity
from cyber_agent_flow_eval.proxmox import CHUNK, UPLOAD_CHUNK, GuestAgent, ProxmoxBackend, unpack
from cyber_agent_flow_eval.runner import CleanupError, PreparationError, run, source_identity
from cyber_agent_flow_eval.spec import resolve
from cyber_agent_flow_eval.storage import read_json, write_json
from test_experiments import specification


@pytest.fixture
def config(tmp_path):
    return resolve_backend({'type': 'proxmox', 'participant_vmid': 9403, 'app_vmid': 9402,
                            'user': pwd.getpwuid(os.getuid()).pw_name,
                            'workspace': str(tmp_path / 'guest workspace')})


class GuestSimulator(GuestAgent):
    """Use real binary transfer/helper code; emulate only Linux service lifecycle."""
    def __init__(self, config):
        super().__init__(config)
        self.calls = []
        self.services = {}
        self.fail_stop = False
        self.timeout = False

    def call(self, vmid, op, **data):
        self.calls.append((vmid, op, data))
        if op == 'probe':
            return {'engine': source_identity(data['engine']['path'])['engine'],
                    'runtime': runtime_identity(data['engine'])}
        if op == 'start':
            root = Path(data['path'])
            directory = root / 'attempt'
            env = dict(os.environ, CAF_RUN_BASE_DIR=str(directory), CAF_TOOLS_CONFIG_PATH=str(directory / 'catalog.json'))
            with (directory / 'worker.log').open('w') as log:
                result = subprocess.run([data['engine']['python'], str(root / 'cyber_agent_flow_eval/worker.py'), str(directory)],
                                        env=env, cwd=data['engine']['path'], stdout=log, stderr=log, timeout=10)
            self.services[data['unit']] = {'LoadState': 'loaded', 'SubState': 'failed' if self.timeout else 'exited',
                                           'Result': 'timeout' if self.timeout else 'success',
                                           'ExecMainStatus': str(result.returncode)}
            if self.timeout:
                (directory / 'result.json').unlink(missing_ok=True)
            return {}
        if op == 'status':
            return self.services[data['unit']]
        if op == 'stop':
            if self.fail_stop:
                raise ValueError('guest disconnected')
            return {'ActiveState': 'inactive'}
        if op == 'hook':
            log = Path(self.config['workspace']).parent / 'hook.log'
            log.write_text('restored known state\n')
            return {'exitcode': 0, 'log_path': str(log)}
        if op == 'route_targets':
            return {'changed': data['networks'], 'gateway': '10.254.200.1',
                    'interface': 'ens18', 'networks': data['networks']}
        return guest_agent.dispatch(dict(data, op=op))


ENGINE_SOURCE = '''
class Client:
    def chat(self):
        return {'content': '{"flags":["FLAG{observed}"]}'}
class MCPSession:
    def __init__(self, *, allowed_tools=None, guidance_text=None, reveal_network_policy=True, **kwargs):
        self.callback = kwargs['event_callback']
        self.messages = [{'role': 'system', 'content': 'test'}]
        self._client = Client()
        self._exit_stack = None
    async def start(self): pass
    async def stop(self): pass
    async def chat(self, prompt, cancel_event):
        self.callback({'type': 'tool_result', 'result': 'FLAG{observed}'})
        self.messages.append({'role': 'assistant', 'content': self._client.chat()['content']})
'''


@pytest.fixture
def remote_spec(specification, config, tmp_path, monkeypatch):
    engine = tmp_path / 'guest CAF with spaces'
    engine.mkdir()
    (engine / 'mcp_client.py').write_text(ENGINE_SOURCE)
    for name in ('mcp_kali.py', 'session_logger.py'):
        (engine / name).write_text('# fixture engine\n')
    raw = yaml.safe_load(specification.read_text())
    raw['engine'] = {'path': str(engine), 'python': sys.executable}
    raw['backend'] = config
    raw['tasks'][0]['verifier'] = {'type': 'flags_found', 'expected': {'hidden-objective': 'FLAG{observed}', 'unfound': 'FLAG{private}'}}
    specification.write_text(yaml.safe_dump(raw))
    agent = GuestSimulator(config)
    backend = ProxmoxBackend(config, raw['engine'], agent=agent)
    monkeypatch.setattr(backends, 'create_backend', lambda spec: backend)
    monkeypatch.setattr(backend, 'lock', nullcontext)
    return specification, backend, agent


def test_remote_run_stages_public_data_collects_scores_and_resumes(remote_spec, tmp_path):
    path, backend, agent = remote_spec
    output = tmp_path / 'results'
    rows = run(path, output)
    assert len(rows) == 2
    assert all(r['status'] == 'completed' and r['score'] == 0.5 and r['flags_observed'] == 1 for r in rows)
    assert read_json(output / 'manifest.json')['source_hashes']['engine']
    transport = read_json(next(output.glob('trials/*/attempt-*/transport.json')))
    assert transport['phase'] == 'collected' and transport['files_uploaded'] == transport['files_total'] == 7
    assert transport['bytes_uploaded'] == transport['bytes_total'] > 0
    assert [event['phase'] for event in transport['phase_events']] == [
        'preparing', 'uploading', 'starting', 'executing', 'stopping', 'collecting', 'collected']
    assert transport['execution_started_at'] <= transport['updated_at']
    uploads = [base64.b64decode(data['content']) for _, op, data in agent.calls if op == 'write']
    assert b'FLAG{private}' not in b''.join(uploads)
    assert b'hidden-objective' not in b''.join(uploads)
    for row in rows:
        directory = output / row['attempt_path']
        assert (directory / 'guest-output/model_calls/call-000001.json').is_file()
        assert read_json(directory / 'transport.json')['stopped']
        assert read_json(directory / 'evaluation.json')['evidence'][0] == 'guest-output/result.json'
    count = len([1 for _, op, _ in agent.calls if op == 'start'])
    run(path, output, resume=True)
    assert len([1 for _, op, _ in agent.calls if op == 'start']) == count


def test_guest_timeout_retains_progress_without_final_answer(remote_spec, tmp_path):
    path, backend, agent = remote_spec
    agent.timeout = True
    rows = run(path, tmp_path / 'results')
    assert all(r['status'] == 'timeout' and r['verified_success'] is None and r['flags_observed'] == 1 for r in rows)


def test_unconfirmed_cleanup_aborts_remaining_trials(remote_spec, tmp_path):
    path, backend, agent = remote_spec
    agent.fail_stop = True
    with pytest.raises(CleanupError, match='Cannot confirm'):
        run(path, tmp_path / 'results')
    assert len([1 for _, op, _ in agent.calls if op == 'start']) == 1
    record = read_json(tmp_path / 'results/trials/trial-000001/attempt-0001/transport.json')
    assert record['stopped'] is False
    # Recovery stops the previous guest service before a retry starts.
    agent.fail_stop = False
    run(path, tmp_path / 'results', resume=True, retry_failed=True)
    assert read_json(tmp_path / 'results/trials/trial-000001/attempt-0001/transport.json')['stopped']


def test_binary_chunk_transfer_and_integrity(config, tmp_path):
    agent = GuestSimulator(config)
    content = os.urandom(UPLOAD_CHUNK * 3 + 7)
    path = str(tmp_path / "file with ' quotes and spaces")
    agent.put(9403, path, content)
    assert agent.get(9403, path) == content
    assert len([1 for _, op, _ in agent.calls if op == 'write']) == 4


def test_download_rejects_changed_file(config, tmp_path, monkeypatch):
    agent = GuestSimulator(config)
    path = tmp_path / 'data'
    path.write_bytes(b'abc')
    original = agent.call
    def tamper(vmid, op, **data):
        result = original(vmid, op, **data)
        if op == 'read':
            result['content'] = base64.b64encode(b'bad').decode()
        return result
    monkeypatch.setattr(agent, 'call', tamper)
    with pytest.raises(ValueError, match='checksum mismatch'):
        agent.get(9403, str(path))


def test_qm_uses_argv_async_polling_and_checks_truncation(config, monkeypatch):
    agent = GuestAgent(config)
    commands = []
    responses = iter([{'pid': 12}, {'exited': False}, {'exited': True, 'exitcode': 0, 'out-data': '{"ok": true}'}])
    def execute(argv, **kwargs):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(next(responses)), '')
    monkeypatch.setattr(subprocess, 'run', execute)
    monkeypatch.setattr('time.sleep', lambda seconds: None)
    assert agent.call(9403, 'stat', path='/a b') == {'ok': True}
    assert commands[0][:7] == ['qm', 'guest', 'exec', '9403', '--synchronous', '0', '--']
    assert commands[1] == ['qm', 'guest', 'exec-status', '9403', '12']
    responses = iter([{'pid': 13}, {'exited': True, 'exitcode': 0, 'out-truncated': True}])
    with pytest.raises(ValueError, match='truncated'):
        agent.call(9403, 'stat', path='/x')


@pytest.mark.parametrize('size', [0, 3213005])
def test_stdin_upload_runs_real_helper_with_bounded_input_and_fewer_dispatches(config, tmp_path, monkeypatch, size):
    original_run = subprocess.run
    commands, guards, results = [], [], {}
    agent = GuestAgent(config, authorize=lambda args: guards.append(list(args)))
    def execute(argv, **kwargs):
        commands.append(argv)
        if argv[2] == 'exec':
            command = argv[argv.index('--') + 1:]
            payload = kwargs.get('input')
            if payload is not None:
                assert '--pass-stdin' in argv and '--synchronous' in argv
                assert len(payload.encode()) < 1024 * 1024
                assert all(len(arg.encode()) < 128 * 1024 for arg in argv)
                assert '"content"' not in ' '.join(argv)  # File bytes are not argv.
            result = original_run(command, input=payload, text=True, capture_output=True)
            status = dict(exited=True, exitcode=result.returncode, **{'out-data': result.stdout, 'err-data': result.stderr})
            if payload is not None:
                reply = status
            else:
                results[17] = status
                reply = {'pid': 17}
        else:
            reply = results[int(argv[-1])]
        return subprocess.CompletedProcess(argv, 0, json.dumps(reply), '')
    monkeypatch.setattr(subprocess, 'run', execute)
    destination = tmp_path / "binary ' file"
    content = os.urandom(size)
    agent.put(9403, str(destination), content)
    assert destination.read_bytes() == content
    writes = max(1, (size + UPLOAD_CHUNK - 1) // UPLOAD_CHUNK)
    assert len(commands) == writes + 2  # One completed exec/write, then stat + status.
    assert len(guards) == len(commands)
    assert len([cmd for cmd in commands if '--pass-stdin' in cmd]) == writes


def test_upload_waits_for_existing_pid_without_replaying_write(config, monkeypatch):
    commands = []
    replies = iter([{'pid': 12}, {'exited': False}, {'exited': True, 'exitcode': 0, 'out-data': '{}'}])
    def execute(argv, **kwargs):
        commands.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(next(replies)), '')
    monkeypatch.setattr(subprocess, 'run', execute)
    monkeypatch.setattr('time.sleep', lambda seconds: None)
    assert GuestAgent(config).call(9403, 'write', path='/x', offset=0, content='YQ==') == {}
    assert [cmd[2] for cmd in commands] == ['exec', 'exec-status', 'exec-status']


@pytest.mark.parametrize('result,match', [
    ({'exited': True, 'exitcode': 1, 'out-data': '{"error":"disk full"}'}, 'disk full'),
    ({'exited': True, 'exitcode': 0, 'out-truncated': True}, 'truncated'),
    ({'exited': True, 'exitcode': 0, 'out-data': 'bad'}, 'invalid JSON'),
    ({'pid': True}, 'PID')])
def test_upload_rejects_failed_or_ambiguous_completion(config, monkeypatch, result, match):
    monkeypatch.setattr(subprocess, 'run', lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, json.dumps(result), ''))
    with pytest.raises(ValueError, match=match):
        GuestAgent(config).call(9403, 'write', path='/x', offset=0, content='YQ==')


def test_upload_revocation_stops_before_next_chunk(config, monkeypatch):
    dispatched = []
    def authorize(args):
        if dispatched:
            raise PermissionError('Revoked')
    def execute(argv, **kwargs):
        dispatched.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps({'exited': True, 'exitcode': 0, 'out-data': '{}'}), '')
    monkeypatch.setattr(subprocess, 'run', execute)
    with pytest.raises(PermissionError, match='Revoked'):
        GuestAgent(config, authorize=authorize).put(9403, '/x', b'a' * (UPLOAD_CHUNK + 1))
    assert len(dispatched) == 1


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'a/../../escape', 'a\\b', 'manifest.json/../escape'])
def test_archive_traversal_rejected(tmp_path, name):
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w') as archive:
        archive.writestr(name, 'data')
    with pytest.raises(ValueError, match='unsafe'):
        unpack(content.getvalue(), tmp_path / 'out', 10000, lambda n: True)


def test_fetch_validates_suite_before_publishing(config, tmp_path):
    fixture = Path(__file__).parent / 'fixtures/scenarioforge_discovery_suite'
    archive = tmp_path / 'export.zip'
    with zipfile.ZipFile(archive, 'w') as bundle:
        for path in fixture.rglob('*'):
            if path.is_file():
                bundle.write(path, path.relative_to(fixture))
    agent = GuestSimulator(config)
    backend = ProxmoxBackend(config, {}, agent=agent)
    result = backend.fetch_suite(str(archive), tmp_path / 'suite')
    assert result['tasks'] == 1
    assert all(vmid == 9402 for vmid, _, _ in agent.calls)
    with zipfile.ZipFile(archive, 'a') as bundle:
        bundle.writestr('unapproved.txt', 'no')
    with pytest.raises(ValueError, match='unsafe'):
        backend.fetch_suite(str(archive), tmp_path / 'invalid')
    assert not (tmp_path / 'invalid').exists()


@pytest.mark.parametrize('kind', ['macos', 'linux', 'windows'])
def test_placeholders_fail_explicitly(kind):
    with pytest.raises(ValueError, match='placeholder'):
        resolve_backend({'type': kind})


def test_plan_resolves_guest_paths_without_local_checkout(specification, config):
    raw = yaml.safe_load(specification.read_text())
    raw['backend'] = config
    raw['engine'] = {'path': '/only/in/guest/caf', 'python': '/only/in/guest/venv/bin/python'}
    specification.write_text(yaml.safe_dump(raw))
    assert resolve(specification)['engine'] == raw['engine']


def test_hooks_run_before_worker(remote_spec, tmp_path):
    path, backend, agent = remote_spec
    backend.config['before_trial'] = [{'vmid': 9401, 'argv': ['/opt/lab/reset'], 'timeout_seconds': 60}]
    run(path, tmp_path / 'results')
    ops = [op for _, op, _ in agent.calls]
    assert ops.index('hook') < ops.index('start')
    assert (tmp_path / 'results/trials/trial-000001/attempt-0001/hook-000.log').read_text().startswith('restored')


def test_route_allowed_targets_runs_before_worker(remote_spec, tmp_path):
    path, backend, agent = remote_spec
    backend.config['route_allowed_targets'] = True
    backend.target_networks = ['172.17.230.0/24']
    run(path, tmp_path / 'results')
    ops = [op for _, op, _ in agent.calls]
    assert ops.index('route_targets') < ops.index('start')
    audit = read_json(tmp_path / 'results/trials/trial-000001/attempt-0001/target-routes.json')
    assert audit['changed'] == ['172.17.230.0/24']


def test_hook_failure_prevents_all_worker_launches(remote_spec, tmp_path, monkeypatch):
    path, backend, agent = remote_spec
    backend.config['before_trial'] = [{'vmid': 9401, 'argv': ['/opt/lab/reset'], 'timeout_seconds': 60}]
    original = agent.call
    def fail(vmid, op, **data):
        result = original(vmid, op, **data)
        if op == 'hook':
            result['exitcode'] = 1
        return result
    monkeypatch.setattr(agent, 'call', fail)
    with pytest.raises(PreparationError):
        run(path, tmp_path / 'results')
    assert not any(op == 'start' for _, op, _ in agent.calls)
    assert read_json(tmp_path / 'results/trials/trial-000001/attempt-0001/attempt.json')['status'] == 'preparation_failed'


def test_failed_collection_can_be_retried_without_restarting_worker(remote_spec, tmp_path, monkeypatch):
    path, backend, agent = remote_spec
    original = backend.collect
    attempts = 0
    def fail_once(directory, record):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError('interrupted download')
        return original(directory, record)
    monkeypatch.setattr(backend, 'collect', fail_once)
    output = tmp_path / 'results'
    rows = run(path, output)
    assert rows[0]['status'] == 'error'
    record = read_json(output / 'trials/trial-000001/attempt-0001/transport.json')
    assert record['stopped'] and not record.get('collected')
    rows = run(path, output, resume=True)
    assert rows[0]['flags_observed'] == 1
    assert len([1 for _, op, _ in agent.calls if op == 'start']) == 2


def test_import_suite_preserves_absolute_guest_paths(specification, config, tmp_path):
    from cyber_agent_flow_eval.scenarioforge import import_config
    raw = yaml.safe_load(specification.read_text())
    raw['backend'] = config
    raw['engine'] = {'path': '/guest/CAF', 'python': '/guest/CAF/venv/bin/python'}
    raw['execution']['network_policy'] = {'allow': ['*'], 'disallow': []}
    specification.write_text(yaml.safe_dump(raw))
    output = tmp_path / 'nested/study.yaml'
    import_config(Path(__file__).parent / 'fixtures/scenarioforge_discovery_suite', specification, output)
    assert resolve(output)['engine'] == raw['engine']


def test_recover_cli_does_not_require_matching_engine_sources(remote_spec, tmp_path):
    from cyber_agent_flow_eval.__main__ import main
    path, backend, agent = remote_spec
    agent.fail_stop = True
    output = tmp_path / 'results'
    with pytest.raises(CleanupError):
        run(path, output)
    # A changed engine blocks resume but must not block stopping an old service.
    engine = Path(backend.engine['path']) / 'mcp_client.py'
    engine.write_text(engine.read_text() + '\n# engine changed\n')
    agent.fail_stop = False
    assert main(['recover', '--config', str(path), '--output', str(output)]) == 0
    record = read_json(output / 'trials/trial-000001/attempt-0001/transport.json')
    assert record['stopped'] and record['collected']
    assert len([1 for _, op, _ in agent.calls if op == 'start']) == 1


def test_archive_symlink_and_expansion_limits(tmp_path):
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w') as archive:
        entry = zipfile.ZipInfo('model_calls/link')
        entry.external_attr = 0o120777 << 16
        archive.writestr(entry, '/etc/passwd')
    with pytest.raises(ValueError, match='unsafe'):
        unpack(content.getvalue(), tmp_path, 10000, lambda n: True)
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('data', 'a' * 1000)
    with pytest.raises(ValueError, match='Expanded'):
        unpack(content.getvalue(), tmp_path, 100, lambda n: True)


def test_guest_service_uses_user_and_cgroup_deadline(config, tmp_path, monkeypatch):
    monkeypatch.setattr(guest_agent, 'CONTROL_DIR', tmp_path / 'controls')
    root = tmp_path / 'run'
    (root / 'attempt').mkdir(parents=True)
    for name in ('input.json', 'catalog.json'):
        (root / 'attempt' / name).write_text('{}')
    captured = []
    monkeypatch.setattr(guest_agent, 'command', lambda argv, **kw: captured.append(argv))
    guest_agent.dispatch({'op': 'start', 'path': str(root), 'unit': 'caf-eval-test', 'seconds': 12,
                          'user': config['user'], 'engine': {'path': '/opt/CAF with spaces', 'python': '/opt/venv/bin/python'},
                          'environment_file': '/etc/caf.env'})
    argv = captured[0]
    assert '--property=RuntimeMaxSec=12' in argv
    assert '--property=KillMode=control-group' in argv
    assert '--property=ExitType=cgroup' in argv
    assert '--property=EnvironmentFile=/etc/caf.env' in argv
    assert '--property=User=' + config['user'] in argv
    assert '--property=WorkingDirectory=/opt/CAF with spaces' in argv


def test_guest_target_routes_override_local_docker_network(monkeypatch):
    calls = []
    def command(argv, **kwargs):
        calls.append(argv)
        if argv == ['ip', '-j', '-4', 'route', 'show', 'default']:
            return json.dumps([
                {'gateway': '192.168.20.2', 'dev': 'ens20', 'metric': 100},
                {'gateway': '10.254.200.1', 'dev': 'ens18', 'metric': 2000},
            ])
        if argv[:7] == ['ip', '-j', '-4', 'route', 'show', 'exact', '172.17.230.0/24']:
            return json.dumps([{'dst': '172.17.0.0/16', 'dev': 'docker0'}])
        if argv[:6] == ['ip', '-j', '-4', 'route', 'get', '172.17.230.1']:
            return json.dumps([{'dst': '172.17.230.1', 'gateway': '10.254.200.1', 'dev': 'ens18'}])
        return ''
    monkeypatch.setattr(guest_agent, 'command', command)
    result = guest_agent.dispatch({'op': 'route_targets', 'networks': ['172.17.230.0/24']})
    assert result == {'changed': ['172.17.230.0/24'], 'gateway': '10.254.200.1',
                      'interface': 'ens18', 'networks': ['172.17.230.0/24']}
    assert ['ip', '-4', 'route', 'replace', '172.17.230.0/24', 'via', '10.254.200.1',
            'dev', 'ens18', 'metric', '2000'] in calls


def test_delayed_start_cannot_revive_cancelled_service(config, tmp_path, monkeypatch):
    monkeypatch.setattr(guest_agent, 'CONTROL_DIR', tmp_path / 'controls')
    monkeypatch.setattr(guest_agent, 'service_status', lambda unit: {'LoadState': 'not-found', 'ActiveState': 'inactive'})
    guest_agent.dispatch({'op': 'stop', 'unit': 'caf-eval-race'})
    root = tmp_path / 'run'
    (root / 'attempt').mkdir(parents=True)
    for name in ('input.json', 'catalog.json'):
        (root / 'attempt' / name).write_text('{}')
    monkeypatch.setattr(guest_agent, 'command', lambda *a, **kw: pytest.fail('Cancelled service started'))
    with pytest.raises(ValueError, match='cancelled'):
        guest_agent.dispatch({'op': 'start', 'path': str(root), 'unit': 'caf-eval-race', 'seconds': 10,
                              'user': config['user'], 'engine': {'path': '/opt/caf', 'python': '/opt/caf/venv/bin/python'}})


def test_optional_progress_write_failure_does_not_skip_worker_stop(remote_spec, tmp_path, monkeypatch):
    from cyber_agent_flow_eval import proxmox
    path, backend, agent = remote_spec
    original = proxmox.write_json
    failures = []
    def write(target, record):
        if record.get('phase') == 'stopping':
            failures.append(str(target))
            raise OSError('Progress journal temporarily unavailable')
        original(target, record)
    monkeypatch.setattr(proxmox, 'write_json', write)
    rows = run(path, tmp_path / 'results')
    assert failures and all(row['status'] == 'completed' for row in rows)
    assert sum(op == 'stop' for _, op, _ in agent.calls) >= len(rows)


@pytest.mark.parametrize('operation',['stat','stop'])
def test_slow_dispatch_still_polls_acknowledged_guest_operation(config,monkeypatch,operation):
    import time
    clock=[0]
    monkeypatch.setattr(time,'monotonic',lambda:clock[0])
    calls=[]
    agent=GuestAgent(config)
    def qm(args,**kwargs):
        calls.append(args)
        if args[1]=='exec':
            clock[0]+=config['command_timeout']+5
            return {'pid':1947}
        return {'exited':True,'exitcode':0,'out-data':json.dumps({'ok':True})}
    monkeypatch.setattr(agent,'qm',qm)
    assert agent.call(453833,operation)=={'ok':True}
    assert [call[1] for call in calls]==['exec','exec-status']


def test_pending_guest_operation_still_has_bounded_poll_deadline(config,monkeypatch):
    import time
    clock=[0]
    monkeypatch.setattr(time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(time,'sleep',lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    calls=[]
    agent=GuestAgent(config)
    def qm(args,**kwargs):
        calls.append(args)
        if args[1]=='exec':
            clock[0]+=40
            return {'pid':1947}
        clock[0]+=2
        return {'exited':False}
    monkeypatch.setattr(agent,'qm',qm)
    with pytest.raises(ValueError,match='stop timed out; guest PID 1947'):
        agent.call(453833,'stop',timeout=5)
    assert len([args for args in calls if args[1]=='exec'])==1
    assert len(calls)<=5


def test_guest_preflight_stops_only_recorded_units_and_blocks_unknown_active_jobs(tmp_path,monkeypatch):
    monkeypatch.setattr(guest_agent,'CONTROL_DIR',tmp_path/'control')
    commands=[]
    monkeypatch.setattr(guest_agent,'service_status',lambda unit:{'LoadState':'not-found','ActiveState':'inactive'})
    def command(argv,**kwargs):
        commands.append(argv)
        return 'caf-orchestrator-bbbb.service loaded active running foreign job\n'
    monkeypatch.setattr(guest_agent,'command',command)
    with pytest.raises(ValueError,match='active CAF services'):
        guest_agent._dispatch({'op':'preflight','units':['caf-orchestrator-aaaa']})
    assert (tmp_path/'control/caf-orchestrator-aaaa.control').read_text()=='cancelled\n'
    assert not (tmp_path/'control/caf-orchestrator-bbbb.control').exists()
    monkeypatch.setattr(guest_agent,'command',lambda *a,**k:'')
    assert guest_agent._dispatch({'op':'preflight','units':[]})['ready']


def test_preflight_recovers_reset_hooks_without_stopping_other_services(tmp_path, monkeypatch):
    monkeypatch.setattr(guest_agent, 'CONTROL_DIR', tmp_path/'controls')
    units=['caf-eval-hook-'+'a'*32,'caf-eval-'+'b'*32,'caf-orchestrator-'+'c'*32]
    stopped=[]
    states={unit:'active' for unit in units}
    def status(unit):
        return dict(LoadState='loaded',ActiveState=states[unit],SubState='running' if states[unit]=='active' else 'dead')
    def command(argv, **kwargs):
        if argv[:2]==['systemctl','stop']:
            assert argv[2] in units
            stopped.append(argv[2])
            states[argv[2]]='inactive'
        return ''
    monkeypatch.setattr(guest_agent,'service_status',status)
    monkeypatch.setattr(guest_agent,'command',command)
    result=guest_agent.dispatch(dict(op='preflight',units=units))
    assert result['ready'] and stopped==units
    assert all((tmp_path/'controls'/(unit+'.control')).read_text()=='cancelled\n' for unit in units)
    assert 'preflight' in guest_agent.SUPPORTED_OPERATIONS


@pytest.mark.parametrize('unit',['ssh.service','caf-eval-hook-not-a-token','caf-eval-hook-../escape','caf-eval-hook-abcd.service'])
def test_preflight_rejects_unowned_or_malformed_hook_units(unit, monkeypatch):
    monkeypatch.setattr(guest_agent,'command',lambda *a,**kw:pytest.fail('Invalid cleanup touched guest services'))
    with pytest.raises(ValueError,match='Invalid recorded cleanup units'):
        guest_agent.dispatch(dict(op='preflight',units=[unit]))
