import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from conftest import CAF_ROOT, ENGINE

from cyber_agent_flow_eval.runner import lease, run, verify
from cyber_agent_flow_eval.spec import resolve, schedule
from cyber_agent_flow_eval.storage import read_json, write_json

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def specification(tmp_path):
    raw = yaml.safe_load((ROOT / 'configs/experiments/example.yaml').read_text())
    raw['engine'] = dict(ENGINE)
    raw['execution']['target_lock'] = str(tmp_path / 'target.lock')
    raw['repetitions'] = 1
    for condition in raw['conditions']:
        condition['catalog'] = str(ROOT / 'configs/experiments/empty-tools.json')
        if 'guidance_files' in condition:
            condition['guidance_files'] = [str(ROOT / 'configs/experiments/inventory-guide.md')]
    path = tmp_path / 'suite.yaml'
    path.write_text(yaml.safe_dump(raw))
    return path


def successful_worker(directory, seconds):
    participant = read_json(directory / 'input.json')
    assert 'verifier' not in participant
    assert 'expected' not in participant
    assert participant['tools'] == []
    assert seconds > 0
    result = {'status': 'completed', 'final_answer': '{"services":[{"host":"192.0.2.10","port":80,"protocol":"tcp"}]}'}
    write_json(directory / 'result.json', result)
    write_json(directory / 'messages.json', [{'role': 'assistant', 'content': result['final_answer']}])
    return result


def test_schedule_and_frozen_guidance(specification):
    spec = resolve(specification)
    assert schedule(spec) == schedule(copy.deepcopy(spec))
    assert len(schedule(spec)) == 2
    assert {t['pair_id'] for t in schedule(spec)} == {'inventory-001:0'}
    assert spec['conditions'][0]['guidance_snapshot'] == []
    assert spec['conditions'][1]['guidance_snapshot'][0]['text']
    assert spec['conditions'][0]['artifact_hash'] != spec['conditions'][1]['artifact_hash']


@pytest.mark.parametrize('change', ['unknown', 'duplicate', 'budget', 'seed'])
def test_invalid_spec_rejected(specification, change):
    raw = yaml.safe_load(specification.read_text())
    if change == 'unknown':
        raw['executon'] = {}
    elif change == 'duplicate':
        raw['tasks'].append(raw['tasks'][0])
    elif change == 'budget':
        raw['execution']['wall_seconds'] = 0
    else:
        raw['model']['seed'] = 42
    specification.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError):
        resolve(specification)


def test_dataset_resume_and_change_detection(specification, tmp_path):
    output = tmp_path / 'output'
    rows = run(specification, output, launcher=successful_worker)
    assert len(rows) == 2 and all(r['verified_success'] for r in rows)
    assert (output / 'dataset.csv').read_text().count('\n') == 3
    assert len((output / 'dataset.jsonl').read_text().splitlines()) == 2
    def unexpected(*args):
        pytest.fail('Completed attempt reran')
    assert len(run(specification, output, resume=True, launcher=unexpected)) == 2
    raw = yaml.safe_load(specification.read_text())
    raw['tasks'][0]['prompt'] += ' Changed'
    specification.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValueError, match='changed'):
        run(specification, output, resume=True, launcher=unexpected)


def test_failed_attempt_preserved_on_retry(specification, tmp_path):
    output = tmp_path / 'output'
    def timeout(*args):
        return {'status': 'timeout', 'final_answer': None}
    rows = run(specification, output, launcher=timeout)
    assert all(r['verified_success'] is None for r in rows)
    rows = run(specification, output, resume=True, retry_failed=True, launcher=successful_worker)
    assert len(rows) == 4
    assert sum(r['status'] == 'timeout' for r in rows) == 2
    assert sum(r['verified_success'] is True for r in rows) == 2


def test_interrupted_attempt_retained(specification, tmp_path):
    output = tmp_path / 'output'
    run(specification, output, launcher=successful_worker)
    path = output / 'trials/trial-000001/attempt-0001/attempt.json'
    row = read_json(path)
    row['status'] = 'running'
    write_json(path, row)
    rows = run(specification, output, resume=True, launcher=successful_worker)
    assert len(rows) == 3
    assert read_json(path)['status'] == 'interrupted'
    assert read_json(path)['verified_success'] is None


def test_lock_and_verification(tmp_path):
    with lease(tmp_path / 'target.lock'):
        with pytest.raises(ValueError, match='locked'):
            with lease(tmp_path / 'target.lock'):
                pass
    assert not verify('```json\n{}\n```', {'type': 'json_equals', 'expected': {}})['passed']
    assert not verify('{"x":true}', {'type': 'json_equals', 'expected': {'x': 1}})['passed']
    assert verify('{"x":1}', {'type': 'json_equals', 'expected': {'x': 1}})['passed']


@pytest.mark.parametrize('relocate_engine', [False, True])
def test_real_worker_with_fake_provider(specification, tmp_path, relocate_engine):
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def reply(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def do_GET(self):
            self.reply({'data': [{'id': 'test'}]})
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.reply({'choices': [{'message': {'role': 'assistant', 'content':
                        '{"services":[{"host":"192.0.2.10","port":80,"protocol":"tcp"}]}'}}],
                        'usage': {'prompt_tokens': 20, 'completion_tokens': 10}})
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        raw = yaml.safe_load(specification.read_text())
        if relocate_engine:
            import shutil
            selected = tmp_path / 'different CAF checkout with spaces'
            selected.mkdir()
            for file in [*CAF_ROOT.glob('*.py'), CAF_ROOT / 'kali_tools.json']:
                shutil.copy2(file, selected / file.name)
            client = selected / 'mcp_client.py'
            client.write_text(client.read_text().replace('You are a network security assistant.',
                                                       'SELECTED_EXTERNAL_ENGINE. You are a network security assistant.'))
            raw['engine']['path'] = str(selected)
        raw['model'] = {'provider': 'openai', 'url': f'http://127.0.0.1:{server.server_port}', 'name': 'test'}
        raw['execution']['network_policy'] = {'allow': ['10.88.0.0/16'], 'disallow': ['10.88.99.0/24']}
        specification.write_text(yaml.safe_dump(raw))
        original_catalog = (CAF_ROOT / 'kali_tools.json').read_bytes()
        rows = run(specification, tmp_path / 'output')
        assert all(r['status'] == 'completed' and r['verified_success'] for r in rows), rows
        assert len(requests) == 2
        assert ('SELECTED_EXTERNAL_ENGINE' in json.dumps(requests)) == relocate_engine
        assert read_json(tmp_path / 'output/manifest.json')['spec']['engine'] == raw['engine']
        assert '10.88.' not in json.dumps(requests)
        assert 'Allowed targets:' not in json.dumps(requests)
        assert all('tools' not in request for request in requests)
        assert (CAF_ROOT / 'kali_tools.json').read_bytes() == original_catalog
        for row in rows:
            directory = tmp_path / 'output' / row['attempt_path']
            checkpoint = read_json(directory / 'checkpoint.json')
            assert all(m['role'] == 'system' for m in checkpoint)
            assert bool('Copy observations faithfully' in checkpoint[0]['content']) == (row['condition_id'] == 'guidance')
            calls = list((directory / 'model_calls').glob('call-*.json'))
            assert len(calls) == 1
            assert read_json(calls[0])['response']['raw']['usage']['prompt_tokens'] == 20
    finally:
        server.shutdown(); server.server_close(); thread.join()




def test_duplicate_yaml_keys_rejected(tmp_path):
    path = tmp_path / 'duplicate.yaml'
    path.write_text('version: 1\nversion: 1\n')
    with pytest.raises(ValueError, match='Duplicate YAML key'):
        resolve(path)


def test_wall_timeout_terminates_worker(tmp_path, monkeypatch):
    import subprocess
    import sys
    from cyber_agent_flow_eval import runner
    actual_popen = subprocess.Popen
    processes = []
    def sleeping_worker(*args, **kwargs):
        process = actual_popen([sys.executable, '-c', 'import time; time.sleep(60)'], **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(runner.subprocess, 'Popen', sleeping_worker)
    write_json(tmp_path / 'input.json', {'engine': dict(ENGINE)})
    result = runner.launch(tmp_path, 1)
    assert result['status'] == 'timeout'
    assert processes[0].poll() is not None


@pytest.mark.asyncio
@pytest.mark.parametrize('event,status', [('dangerous_tool_approval', 'interaction_required'), ('tool_timeout_decision', 'interaction_required'), ('error', 'error')])
async def test_service_propagates_engine_events(tmp_path, monkeypatch, event, status):
    from cyber_agent_flow_eval import execution_service
    class Session:
        def __init__(self, **kwargs):
            self.callback = kwargs['event_callback']
            self.messages = [{'role': 'system', 'content': 'Initial'}]
            self._client = object()
            self._exit_stack = None
            self.timeout_killed = False
        def resolve_tool_timeout_decision(self, action):
            assert action == 'kill'
            self.timeout_killed = True
        async def start(self):
            pass
        async def chat(self, prompt, cancel_event):
            self.callback({'type': event, 'message': 'test'})
            if event in {'dangerous_tool_approval', 'tool_timeout_decision'}:
                assert cancel_event.is_set()
            if event == 'tool_timeout_decision':
                assert self.timeout_killed
        async def stop(self):
            pass
    monkeypatch.setattr(execution_service, 'load_session', lambda engine: Session)
    config = {'engine': dict(ENGINE), 'model': {'url': 'unused', 'provider': 'ollama_direct', 'name': 'test'},
              'execution': {'context_window': 1024, 'max_turns': 1, 'tool_timeout': 1,
                            'network_policy': {'allow': [], 'disallow': []}},
              'server_command': 'unused', 'run_id': 'test', 'tools': [], 'guidance': '', 'prompt': 'task'}
    result = await execution_service.execute(config, tmp_path)
    assert result['status'] == status
    assert result['final_answer'] == ''




def test_discovery_flag_scoring_without_private_objective_mapping():
    definition = {'type': 'flags_found', 'expected': {'objective-1': 'FLAG{a}', 'objective-2': 'FLAG{b}'}}
    assert verify('{"flags":["FLAG{b}","FLAG{a}"]}', definition)['passed']
    partial = verify('{"flags":["FLAG{b}"]}', definition)
    assert partial['score'] == .5 and not partial['passed']
    assert 'FLAG{' not in json.dumps(partial)
    for answer in ('{"flags":["FLAG{a}","FLAG{a}","FLAG{b}"]}',
                   '{"flags":["FLAG{a}","FLAG{b}","fake"]}', '{"flags":{}}', '[]'):
        assert not verify(answer, definition)['passed']
