import json
import subprocess
import os
import sys
from pathlib import Path

import pytest
from cyber_agent_flow_eval.hints import HintController, metrics
from cyber_agent_flow_eval.storage import read_json, write_json
from cyber_agent_flow_eval.runner import launch
from cyber_agent_flow_eval.proxmox import ProxmoxBackend
from cyber_agent_flow_eval import guest_agent
from test_proxmox import GuestSimulator, config, ENGINE_SOURCE


def request(turn, results=None, final=None):
    return dict(sequence=turn, turn=turn, results=results or [], final_answer=final)


def test_facts_progress_reveal_and_answer_protection(tmp_path):
    fact = dict(id='credential', artifact='password', value='private-password', source_node='entry', evidence='Read /note.txt', requires=['entry'])
    metadata = dict(starting_facts=[dict(id='entry', value='public')], discoverable_facts=[fact])
    controller = HintController(tmp_path, metadata, dict(type='contains_all', expected=['SECRET-FLAG']))
    assert controller.respond(request(1))['hint'] is None
    first = controller.respond(request(2))
    assert first['hint'] and 'private-password' not in first['hint']
    assert controller.respond(request(2)) == first
    assert 'Read /note.txt' in controller.respond(request(4))['hint']
    assert 'private-password' in controller.respond(request(6))['hint']
    assert controller.respond(request(8))['hint'] is None
    result = metrics(tmp_path, True, True)
    assert result['hints_released'] == 3 and result['facts_revealed'] == 1
    assert result['assisted_success'] and not result['unassisted_success']
    assert len(result['assistance']['events']) == 3
    assert not result['assistance']['observed_fact_ids']


def test_observed_facts_are_never_revealed_again(tmp_path):
    controller = HintController(tmp_path, dict(discoverable_facts=[dict(id='fact', artifact='password', value='private', source_node='n', evidence='file')]), dict(type='contains_all', expected=['answer']))
    assert controller.respond(request(1, ['private']))['hint'] is None
    assert controller.respond(request(4))['hint'] is None
    assert read_json(tmp_path / 'assistance.json')['observed_fact_ids'] == ['fact']


def test_final_answer_and_no_plan(tmp_path):
    with pytest.raises(ValueError, match='no usable'):
        HintController(tmp_path, {}, dict(type='contains_all', expected=['answer']))
    with pytest.raises(ValueError, match='verifier answer'):
        HintController(tmp_path, {'progressive_hints': ['the answer is SECRET']}, dict(type='contains_all', expected=['SECRET']))
    c = HintController(tmp_path, {'progressive_hints': ['Inspect the page', 'unreleased private hint']}, dict(type='json_equals', expected={'token': 'SECRET'}))
    assert c.respond(request(1, final='{"token":"SECRET"}'))['hint'] is None
    assert c.respond(request(2, final='wrong'))['hint'] == 'Inspect the page'
    assert 'unreleased private hint' not in (tmp_path / 'assistance.json').read_text()


@pytest.mark.parametrize('remote', [False, True])
def test_worker_exchange_keeps_private_plan_on_host(tmp_path, config, remote):
    engine = tmp_path / 'engine'; engine.mkdir()
    source = ENGINE_SOURCE[:ENGINE_SOURCE.index('    async def chat(')] + '''    async def chat(self, prompt, cancel_event, progress_callback=None):
        hint = await progress_callback(dict(turn=1, results=[], final_answer='stuck'))
        self.messages.append({'role':'user', 'content':hint})
        self.messages.append({'role':'assistant', 'content':'recovered'})
'''
    (engine / 'mcp_client.py').write_text(source)
    directory = tmp_path / 'attempt'; directory.mkdir()
    engine_config = dict(path=str(engine), python=sys.executable)
    write_json(directory / 'input.json', dict(engine=engine_config, run_id='test', prompt='public task',
        tools=[], guidance='', server_command='unused', model=dict(url='http://localhost', provider='ollama', name='fake'),
        execution=dict(context_window=8192, max_turns=3, tool_timeout=5, network_policy={}, provide_progressive_hints=True)))
    write_json(directory / 'catalog.json', {})
    controller = HintController(directory, {'progressive_hints': ['Inspect the page', 'UNRELEASED']}, dict(type='contains_all', expected=['VERIFIER-SECRET']))
    if remote:
        class AsyncGuest(GuestSimulator):
            def call(self, vmid, op, **data):
                if op == 'start':
                    root = Path(data['path'])
                    self.process = subprocess.Popen([data['engine']['python'], str(root / 'cyber_agent_flow_eval/worker.py'), str(root / 'attempt')], cwd=engine, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return {}
                if op == 'status':
                    code = self.process.poll()
                    return {'SubState': 'running' if code is None else 'exited', 'ExecMainStatus': str(code), 'Result': 'success'}
                if op == 'stop':
                    if self.process.poll() is None:
                        self.process.kill(); self.process.wait()
                    return {'ActiveState': 'inactive'}
                return super().call(vmid, op, **data)
        config['poll_seconds'] = .05
        agent = AsyncGuest(config)
        result = ProxmoxBackend(config, engine_config, agent=agent).launch(directory, 10, hint_controller=controller)
        guest_root = Path(read_json(directory / 'transport.json')['path'])
        uploaded = b''.join(p.read_bytes() for p in guest_root.rglob('*') if p.is_file() and p.suffix in {'.json', '.jsonl'})
        assert b'UNRELEASED' not in uploaded and b'VERIFIER-SECRET' not in uploaded
    else:
        result = launch(directory, 10, hint_controller=controller)
    assert result['status'] == 'completed' and result['final_answer'] == 'recovered'
    assert len(controller.events) == 1
    assert 'UNRELEASED' not in (directory / 'input.json').read_text()


def test_guest_request_rejects_symlink(tmp_path):
    secret = tmp_path / 'secret.json'; secret.write_text('{"secret": true}')
    (tmp_path / 'hint-request.json').symlink_to(secret)
    with pytest.raises(OSError):
        guest_agent.dispatch(dict(op='hint_request', path=str(tmp_path)))


def test_numeric_verifier_answer_is_never_used_as_hint(tmp_path):
    with pytest.raises(ValueError, match='verifier answer'):
        HintController(tmp_path, {'progressive_hints': ['PIN: 123456']},
                       dict(type='json_equals', expected={'pin': 123456}))
