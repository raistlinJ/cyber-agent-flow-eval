import asyncio
import json
from pathlib import Path

import pytest

from cyber_agent_flow_eval.execution_service import ActivityBudget, execute
from cyber_agent_flow_eval.storage import write_json


def test_active_budget_excludes_wait_and_resumes_accounting():
    clock = [0.0]
    budget = ActivityBudget(10, clock=lambda: clock[0])
    clock[0] = 3
    budget.pause()
    clock[0] = 603
    assert budget.active_seconds == 3
    budget.resume()
    clock[0] = 605
    assert budget.active_seconds == 5
    assert budget.wait_seconds == 600


@pytest.mark.parametrize("monitor_only", [True, False])
@pytest.mark.asyncio
async def test_worker_waits_longer_than_active_budget_for_assistance(tmp_path, monkeypatch, monitor_only):
    from cyber_agent_flow_eval import execution_service
    class Session:
        def __init__(self, **kwargs):
            self.messages = []
            self._client = object()
            self._exit_stack = None
        async def start(self): pass
        async def stop(self): pass
        async def chat(self, prompt, cancel_event, progress_callback=None):
            assert await progress_callback(dict(turn=1, results=[])) == 'Try next'
            self.messages.append(dict(role='assistant', content='observed'))
    monkeypatch.setattr(execution_service, 'load_session', lambda engine: Session)
    async def host_reply():
        while not (tmp_path / 'hint-request.json').exists():
            await asyncio.sleep(.01)
        await asyncio.sleep(.25)
        write_json(tmp_path / 'hint-response.json', dict(sequence=1, hint='Try next'))
    reply = asyncio.create_task(host_reply())
    result = await execute(dict(engine={}, model=dict(url='unused', provider='openai', name='test'),
        execution=dict(context_window=1024, max_turns=2, tool_timeout=1,
            network_policy=dict(allow=[], disallow=[]), wall_seconds=.15,
            provide_progressive_hints=not monitor_only, monitor_progress=monitor_only, assistance_timeout_seconds=1),
        server_command='unused', run_id='test', tools=[], guidance='', prompt='task'), tmp_path)
    await reply
    assert result['status'] == 'completed'
    assert result['assistance_wait_seconds'] >= .25
    assert result['participant_active_seconds'] < .15


@pytest.mark.asyncio
async def test_worker_active_timeout_still_stops_trial(tmp_path, monkeypatch):
    from cyber_agent_flow_eval import execution_service
    class Session:
        def __init__(self, **kwargs):
            self.messages=[]
            self._client=object()
            self._exit_stack=None
            self.stopped=False
        async def start(self): pass
        async def stop(self): self.stopped=True
        async def chat(self, prompt, cancel_event): await asyncio.sleep(10)
    monkeypatch.setattr(execution_service, 'load_session', lambda engine: Session)
    result = await execute(dict(engine={}, model=dict(url='unused', provider='openai', name='test'),
        execution=dict(context_window=1024, max_turns=2, tool_timeout=1,
            network_policy=dict(allow=[], disallow=[]), wall_seconds=.05),
        server_command='unused', run_id='test', tools=[], guidance='', prompt='task'), tmp_path)
    assert result['status'] == 'timeout'
    assert 'active-time budget exceeded' in result['errors'][0]


def test_rubric_judge_citation_clips_only_eof_not_unread_bytes(tmp_path, monkeypatch):
    from cyber_agent_flow_eval import judge
    path = 'runs/t/tool_calls/001_shell.json'
    write_json(tmp_path / path, dict(tool='shell', result='proof'))
    size = (tmp_path / path).stat().st_size
    actions = iter([
        dict(action='read_evidence', file=path, offset=0, limit=6000),
        dict(action='verdict', criteria=[dict(id='one', status='satisfied', reason='Observed proof',
                                            evidence=[dict(file=path, offset=0, limit=6000)])]),
    ])
    monkeypatch.setattr(judge, '_completion', lambda *args, **kwargs: (json.dumps(next(actions)), {}))
    rubric=dict(version=1, criteria=[dict(id='one', requirement='Read proof', evidence='Tool output')])
    result = judge.judge_trial(judge.resolve_judge(dict(enabled=True,
        model=dict(provider='openai', url='http://fixture/v1', name='judge'))),
        tmp_path, dict(prompt='Read proof', rubric=rubric), '', None)
    assert result['passed']
    assert result['criteria'][0]['evidence'][0]['limit'] == size


def test_defer_stalled_solution_advances_independent_step_without_unlocking_dependencies(tmp_path, monkeypatch):
    from cyber_agent_flow_eval.hints import HintController
    from cyber_agent_flow_eval.judge import resolve_judge
    rubric=dict(version=1, criteria=[dict(id=f'c{i}', requirement='Read proof', evidence='Tool output') for i in range(3)])
    plan=dict(version=1, steps=[dict(id=f's{i}', node_id=f'n{i}', title=f'Challenge {i}',
        criterion_ids=[f'c{i}'], requires=['s0'] if i==1 else [], hints=[], solution=f'Walkthrough {i}') for i in range(3)])
    task=dict(prompt='Read proof', rubric=rubric, challenge_plan=plan, verification_mode='judge')
    c=HintController(tmp_path, dict(challenge_plan=plan), dict(type='rubric', expected=rubric),
        dict(max_tries_before_solution=1, hint_stalled_turns=1), task=task,
        judge=resolve_judge(dict(enabled=True, model=dict(provider='openai', url='http://fixture/v1', name='judge'))))
    monkeypatch.setattr(c.monitor, 'check', lambda *args: dict(new_progress=False, all_satisfied=False, completed_steps=set()))
    def request(turn): return dict(sequence=turn, turn=turn, results=[])
    assert c.respond(request(1))['hint'] == 'Walkthrough 0'
    c.respond(request(2))
    assert c.respond(request(3))['hint'] == 'Walkthrough 2'
    assert c.deferred_steps == {'s0'}
    assert not c.completed_steps
    assert not any(e.get('node_id')=='n1' for e in c.events)
    audit=json.loads((tmp_path/'progress-monitor.json').read_text())
    assert audit['steps'][0]['status']=='deferred'
    assert audit['steps'][1]['eligible'] is False

def test_judge_requests_action_schema_and_falls_back_only_for_capability_rejection(monkeypatch):
    import io
    import urllib.error
    from cyber_agent_flow_eval.judge import _completion, resolve_judge
    requests=[]
    class Response:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def read(self,limit): return b'{"choices":[{"message":{"content":"{}"}}]}'
    def open_request(request,**kwargs):
        body=json.loads(request.data)
        requests.append(body)
        if len(requests)==1:
            schema=body['response_format']['json_schema']['schema']
            assert schema['required']==['action']
            assert 'verdict' in schema['properties']['action']['enum']
            raise urllib.error.HTTPError(request.full_url,400,'Unsupported',{},io.BytesIO(b'{"error":"response_format json_schema is not supported"}'))
        return Response()
    monkeypatch.setattr('urllib.request.urlopen',open_request)
    config=resolve_judge(dict(enabled=True,model=dict(provider='openai',url='http://fixture/v1',name='judge')))
    assert _completion(config,[dict(role='user',content='Return JSON')],10)[0]=='{}'
    assert requests[1]['response_format']=={'type':'json_object'}


def test_judge_does_not_retry_unrelated_validation_errors(monkeypatch):
    import io
    import urllib.error
    from cyber_agent_flow_eval.judge import _completion, resolve_judge, JudgeError
    requests=[]
    def open_request(request,**kwargs):
        requests.append(request)
        raise urllib.error.HTTPError(request.full_url,400,'Bad model',{},io.BytesIO(b'{"error":"model does not exist"}'))
    monkeypatch.setattr('urllib.request.urlopen',open_request)
    config=resolve_judge(dict(enabled=True,model=dict(provider='openai',url='http://fixture/v1',name='judge')))
    with pytest.raises(JudgeError,match='HTTP 400'):
        _completion(config,[dict(role='user',content='Return JSON')],10)
    assert len(requests)==1


def test_judge_can_cite_initial_native_outputs_without_extra_read_turns(tmp_path, monkeypatch):
    from cyber_agent_flow_eval import judge
    path='runs/t/tool_calls/001_shell.json'
    write_json(tmp_path/path,dict(tool='shell',result='observed proof'))
    def completion(config,messages,timeout,**kwargs):
        outputs=json.loads(messages[1]['content'])['initial_execution_evidence']
        assert outputs[0]['file']==path and 'observed proof' in outputs[0]['content']
        citation={key:outputs[0][key] for key in ('file','offset','limit')}
        return json.dumps(dict(action='verdict',criteria=[dict(id='one',status='satisfied',reason='Observed proof',evidence=[citation])])),{}
    monkeypatch.setattr(judge,'_completion',completion)
    rubric=dict(version=1,criteria=[dict(id='one',requirement='Read proof',evidence='Tool output')])
    result=judge.judge_trial(judge.resolve_judge(dict(enabled=True,model=dict(provider='openai',url='http://fixture/v1',name='judge'))),tmp_path,dict(prompt='Read proof',rubric=rubric),'',None)
    assert result['passed']
    assert len(json.loads((tmp_path/'judge.json').read_text())['calls'])==1


def test_judge_retries_invalid_stream_once_without_streaming(tmp_path, monkeypatch):
    from cyber_agent_flow_eval import judge
    path='runs/t/tool_calls/001_shell.json'
    write_json(tmp_path/path,dict(tool='shell',result='observed proof'))
    calls=[]
    def completion(config,messages,timeout,on_delta=None):
        calls.append(on_delta is not None)
        if on_delta is not None:
            raise judge.JudgeError('Judge endpoint returned an invalid model response')
        size=(tmp_path/path).stat().st_size
        return json.dumps(dict(action='verdict',criteria=[dict(
            id='one',status='satisfied',reason='Observed proof',
            evidence=[dict(file=path,offset=0,limit=size)])])),{}
    monkeypatch.setattr(judge,'_completion',completion)
    rubric=dict(version=1,criteria=[dict(
        id='one',requirement='Read proof',evidence='Tool output')])
    result=judge.judge_trial(judge.resolve_judge(dict(
        enabled=True,model=dict(provider='openai',url='http://fixture/v1',name='judge'))),
        tmp_path,dict(prompt='Read proof',rubric=rubric),'',None)
    assert result['passed']
    assert calls==[True,False]
    audit=json.loads((tmp_path/'judge.json').read_text())
    assert audit['calls'][0]['transport_retry']=='non-streaming'


def test_transcript_citation_requires_matching_native_output_in_cited_range(tmp_path):
    from cyber_agent_flow_eval.judge import Evidence
    path='runs/t/transcript.md'
    native='runs/t/tool_calls/001_shell.json'
    write_json(tmp_path/native,dict(tool='shell',args={'args':'cat proof'},exit_code=0,result='observed proof'))
    claim='Agent says observed proof.\n'
    block='#### 🔧 Tool Call [today]: `shell`\n\n**Args:** `{"args": "cat proof"}`  \n**Duration:** 1ms  \n**Exit code:** 0  \n\n```\nobserved proof\n```\n'
    (tmp_path/path).write_text(claim+block)
    evidence=Evidence(tmp_path)
    assert not evidence.direct_output(path,0,len(claim.encode()))
    assert evidence.direct_output(path,len(claim.encode()),len(block.encode()))
    (tmp_path/path).write_text(claim+block.replace('observed proof','invented proof'))
    evidence=Evidence(tmp_path)
    assert not evidence.direct_output(path,len(claim.encode()),len(block.encode()))

@pytest.mark.parametrize('remote',[False,True])
def test_host_and_guest_allow_assistance_wait_longer_than_participant_limit(tmp_path, remote):
    import os
    import subprocess
    import sys
    import time
    import pwd
    from cyber_agent_flow_eval.backends import resolve_backend
    from cyber_agent_flow_eval.proxmox import ProxmoxBackend
    from cyber_agent_flow_eval.runner import launch
    from cyber_agent_flow_eval.hints import HintController
    from test_proxmox import GuestSimulator, ENGINE_SOURCE
    engine=tmp_path/'engine'
    engine.mkdir()
    source=ENGINE_SOURCE[:ENGINE_SOURCE.index('    async def chat(')]+'''    async def chat(self, prompt, cancel_event, progress_callback=None):
        hint = await progress_callback(dict(turn=1, results=[], final_answer='stuck'))
        self.messages.append({'role':'assistant', 'content':'observed'})
'''
    (engine/'mcp_client.py').write_text(source)
    directory=tmp_path/'attempt'
    directory.mkdir()
    engine_config=dict(path=str(engine),python=sys.executable)
    write_json(directory/'input.json',dict(engine=engine_config,run_id='test',prompt='task',tools=[],guidance='',server_command='unused',
        model=dict(url='unused',provider='openai',name='fake'),execution=dict(context_window=1024,max_turns=3,tool_timeout=1,
        network_policy=dict(allow=[],disallow=[]),wall_seconds=1,provide_progressive_hints=True,assistance_timeout_seconds=3)))
    write_json(directory/'catalog.json',{})
    class SlowController(HintController):
        def respond(self, request, timeout_seconds=None):
            time.sleep(1.2)
            return super().respond(request, timeout_seconds)
    controller=SlowController(directory,dict(progressive_hints=['Inspect the proof']),dict(type='contains_all',expected=['PRIVATE']))
    if remote:
        cfg=resolve_backend(dict(type='proxmox',participant_vmid=9403,app_vmid=9402,user=pwd.getpwuid(os.getuid()).pw_name,workspace=str(tmp_path/'guest')))
        cfg['poll_seconds']=.01
        class AsyncGuest(GuestSimulator):
            def call(self,vmid,op,**data):
                if op=='start':
                    assert data['seconds']>1
                    root=Path(data['path'])
                    self.process=subprocess.Popen([data['engine']['python'],str(root/'cyber_agent_flow_eval/worker.py'),str(root/'attempt')],cwd=engine,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                    return {}
                if op=='status':
                    code=self.process.poll()
                    return dict(SubState='running' if code is None else 'exited',ExecMainStatus=str(code),Result='success')
                if op=='stop':
                    if self.process.poll() is None:
                        self.process.kill()
                        self.process.wait()
                    return dict(ActiveState='inactive')
                return super().call(vmid,op,**data)
        result=ProxmoxBackend(cfg,engine_config,agent=AsyncGuest(cfg)).launch(directory,1,hint_controller=controller)
    else:
        result=launch(directory,1,hint_controller=controller)
    assert result['status']=='completed'
    assert result['assistance_wait_seconds']>=1.2
    assert result['participant_active_seconds']<1
