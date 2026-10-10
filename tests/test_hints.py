import json
import subprocess
import os
import sys
from pathlib import Path

import pytest
from cyber_agent_flow_eval.hints import HintController, metrics, resolve_policy
from cyber_agent_flow_eval.storage import read_json, write_json
from cyber_agent_flow_eval.runner import launch
from cyber_agent_flow_eval.proxmox import ProxmoxBackend
from cyber_agent_flow_eval import guest_agent
from test_proxmox import GuestSimulator, config, ENGINE_SOURCE


def request(turn, results=None, final=None):
    return dict(sequence=turn, turn=turn, results=results or [], final_answer=final)


def test_hint_stalled_turns_is_configurable_and_bounded_by_agent_turns(tmp_path):
    execution = dict(provide_progressive_hints=True, max_turns=5, hint_stalled_turns=3)
    assert resolve_policy(execution)['stalled_turns'] == 3
    controller = HintController(
        tmp_path,
        {'progressive_hints': ['Inspect the response headers.']},
        dict(type='contains_all', expected=['SECRET-FLAG']),
        execution,
    )
    assert controller.respond(request(1))['hint'] is None
    assert controller.respond(request(2))['hint'] is None
    assert controller.respond(request(3))['hint'] == 'Inspect the response headers.'
    assert metrics(tmp_path, True, False)['hint_stalled_turns'] == 3
    with pytest.raises(ValueError, match='less than max_turns'):
        resolve_policy(dict(provide_progressive_hints=True, max_turns=3, hint_stalled_turns=3))


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
    empty = HintController(tmp_path, {}, dict(type='contains_all', expected=['answer']))
    assert not empty.available
    assert empty.respond(request(5, final='wrong'))['hint'] is None
    audit = metrics(tmp_path, True, True)
    assert audit['progressive_hints_available'] is False
    assert 'running unassisted' in audit['progressive_hints_reason']
    assert audit['unassisted_success'] and not audit['assisted_success']
    with pytest.raises(ValueError, match='verifier answer'):
        HintController(tmp_path, {'progressive_hints': ['the answer is SECRET']}, dict(type='contains_all', expected=['SECRET']))
    c = HintController(tmp_path, {'progressive_hints': ['Inspect the page', 'unreleased private hint']}, dict(type='json_equals', expected={'token': 'SECRET'}))
    assert c.respond(request(1, final='{"token":"SECRET"}'))['hint'] is None
    assert c.respond(request(2, final='wrong'))['hint'] == 'Inspect the page'
    assert 'unreleased private hint' not in (tmp_path / 'assistance.json').read_text()


@pytest.mark.parametrize('remote', [False, True])
@pytest.mark.parametrize('has_plan', [False, True])
def test_worker_exchange_keeps_private_plan_on_host(tmp_path, config, remote, has_plan):
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
    controller = HintController(directory, {'progressive_hints': ['Inspect the page', 'UNRELEASED']} if has_plan else {}, dict(type='contains_all', expected=['VERIFIER-SECRET']))
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
    assert len(controller.events) == (1 if has_plan else 0)
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


@pytest.mark.parametrize('limit', [1, 4, 9])
def test_configurable_limit_withholds_walkthrough_and_answer_until_solution(tmp_path, limit):
    controller = HintController(tmp_path, {'progressive_hints': ['A small nudge.', 'Inspect the response headers.']},
        {'type':'contains_all','expected':['PRIVATE_FLAG']}, {'max_tries_before_solution':limit})
    for turn in range(1, limit):
        assert 'PRIVATE_FLAG' not in (controller.respond(request(turn, final='wrong'))['hint'] or '')
    reply = controller.respond(request(limit))['hint']
    assert 'Inspect the response headers.' in reply and 'PRIVATE_FLAG' in reply
    audit = metrics(tmp_path, True, True)
    assert audit['max_tries_before_solution'] == limit
    assert audit['solution_provided'] and audit['solutions_released'] == 1
    assert audit['assisted_success'] and not audit['unassisted_success']
    assert audit['assistance']['events'][-1]['reason'] == 'max_tries_before_solution'
    assert audit['solution_assisted_success'] and not audit['hints_assisted_success']
    reply = controller.respond(request(limit))['hint']
    assert 'Inspect the response headers.' in reply and 'PRIVATE_FLAG' in reply
    assert len(controller.events) <= 3


def test_new_progress_resets_tries_before_final_guidance(tmp_path):
    controller = HintController(tmp_path, {'progressive_hints':['A small nudge.', 'Final safe guidance.']},
        {'type':'contains_all','expected':['PRIVATE_FLAG']}, {'max_tries_before_solution':4})
    controller.respond(request(2))
    assert controller.respond(request(3, results=['new useful tool output']))['hint'] is None
    assert 'PRIVATE_FLAG' not in (controller.respond(request(4))['hint'] or '')
    assert 'PRIVATE_FLAG' not in (controller.respond(request(6, final='wrong'))['hint'] or '')
    assert 'PRIVATE_FLAG' in controller.respond(request(7))['hint']
    assert read_json(tmp_path/'assistance.json')['tries_without_progress'] == 4


def test_final_fact_reveal_waits_for_configured_limit(tmp_path):
    metadata = {'discoverable_facts':[dict(id='password',artifact='Credential(password)',value='private-password',source_node='n',evidence='Read /note') ]}
    controller = HintController(tmp_path, metadata, {'type':'contains_all','expected':['FLAG']}, {'max_tries_before_solution':9})
    assert 'private-password' not in controller.respond(request(2))['hint']
    assert '/note' in controller.respond(request(4))['hint']
    assert controller.respond(request(6))['hint'] is None
    assert 'private-password' in controller.respond(request(9))['hint']
    assert metrics(tmp_path,True,True)['facts_revealed'] == 1


def test_final_guidance_does_not_bypass_fact_prerequisites_or_correct_answers(tmp_path):
    metadata = {'discoverable_facts':[dict(id='password',artifact='Credential(password)',value='private-password',source_node='n',evidence='Read /note',requires=['unseen'])]}
    controller = HintController(tmp_path, metadata, {'type':'contains_all','expected':['FLAG']}, {'max_tries_before_solution':1})
    assert controller.respond(request(10))['hint'] is None
    correct = HintController(tmp_path, {'progressive_hints':['Final safe guidance.']},
        {'type':'contains_all','expected':['FLAG']}, {'max_tries_before_solution':1})
    assert correct.respond(request(10,final='FLAG'))['hint'] is None
    assert not correct.events


@pytest.mark.parametrize('limit', [0, -1, True, 1.5, '4', 1001])
def test_invalid_solution_limit(tmp_path, limit):
    with pytest.raises(ValueError, match='max_tries_before_solution'):
        HintController(tmp_path, {}, {'type':'contains_all','expected':['FLAG']}, {'max_tries_before_solution':limit})


def test_single_final_hint_keeps_agent_trying_and_feedback_counts_as_assistance(tmp_path):
    controller = HintController(tmp_path, {'progressive_hints':['Final safe guidance.']},
        {'type':'contains_all','expected':['FLAG']}, {'max_tries_before_solution':3})
    assert 'FLAG' not in controller.respond(request(1,final='wrong'))['hint']
    assert 'Try again' in controller.respond(request(2,final='wrong'))['hint']
    before = metrics(tmp_path,True,True)
    assert before['hints_released'] == 1 and before['retries_requested'] == 1
    assert before['assisted_success'] and not before['unassisted_success']
    assert 'FLAG' in controller.respond(request(3,final='wrong'))['hint']
    assert len(controller.events) == 2


def test_evidence_that_contains_fact_value_waits_for_solution_limit(tmp_path):
    controller = HintController(tmp_path, {'discoverable_facts':[
        dict(id='secret',artifact='Credential(password)',value='private-password',source_node='n',
             evidence='The note says private-password')]},
        {'type':'contains_all','expected':['FLAG']}, {'max_tries_before_solution':8})
    assert 'private-password' not in controller.respond(request(2))['hint']
    assert controller.respond(request(4))['hint'] is None
    assert 'private-password' in controller.respond(request(8))['hint']


def test_only_current_challenge_walkthrough_and_flag_are_released(tmp_path):
    metadata = {'challenge_solutions':[
        dict(node_id='first',text='Use curl on the first page. Submit FLAG_ONE.',completion_values=['FLAG_ONE']),
        dict(node_id='second',text='Use the key on the second page. Submit FLAG_TWO.',completion_values=['FLAG_TWO'])]}
    controller = HintController(tmp_path, metadata, {'type':'contains_all','expected':['FLAG_ONE','FLAG_TWO']},
                                {'max_tries_before_solution':2})
    assert controller.respond(request(1))['hint'] is None
    assert 'FLAG_TWO' not in (tmp_path/'assistance.json').read_text()
    reply = controller.respond(request(2))['hint']
    assert 'Use curl' in reply and 'FLAG_ONE' in reply and 'FLAG_TWO' not in reply
    assert controller.respond(request(3,results=['Recovered FLAG_ONE']))['hint'] is None
    assert controller.respond(request(4))['hint'] is None
    reply = controller.respond(request(5))['hint']
    assert 'FLAG_TWO' in reply and 'FLAG_ONE' not in reply
    metrics_row = metrics(tmp_path,True,True)
    assert metrics_row['solutions_released']==2 and metrics_row['hints_released']==0
    assert metrics_row['solution_assisted_success'] and not metrics_row['unassisted_success']
    assert not metrics_row['hints_assisted_success']
    assert controller.respond(request(6,final='FLAG_ONE FLAG_TWO'))['hint'] is None
