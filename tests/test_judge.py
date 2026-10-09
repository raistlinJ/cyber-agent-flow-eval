import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import pytest
from cyber_agent_flow_eval.judge import Evidence, JudgeError, judge_trial, resolve_judge, _completion
from cyber_agent_flow_eval.storage import read_json, write_json


@pytest.fixture
def endpoint():
    state={'requests':[],'bad':None}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            state['requests'].append((self.path,body,self.headers.get('Authorization')))
            if state['bad']=='http':
                self.send_response(503);self.end_headers();return
            if state['bad']=='json':
                self.send_response(200);self.end_headers();self.wfile.write(b'not JSON');return
            messages=body['messages']
            if len(messages)==2:
                plan=json.loads(messages[1]['content'])
                filename=next(iter(plan['required_trace_files']),'worker-result.json')
                action={'action':'read_evidence','file':filename,'offset':0,'limit':6000}
            else:
                filename=json.loads(messages[-1]['content'])['tool_result']['file']
                action={'action':'verdict','passed':True,'score':1,'reason':'Observed the expected final response.', 'evidence':[filename]}
            if state['bad']=='path':action={'action':'read_evidence','file':'../../secret'}
            if state['bad']=='verdict':action={'action':'verdict','passed':'yes','score':1,'reason':'Done','evidence':[]}
            if state['bad']=='loop':action={'action':'read_evidence','file':'worker-result.json'}
            if state['bad']=='skip_trace':
                if len(messages)==2:action={'action':'read_evidence','file':'worker-result.json'}
                else:action['evidence']=['worker-result.json']
            content=json.dumps(action)
            result={'message':{'content':content},'prompt_eval_count':20,'eval_count':10} if self.path.endswith('/api/chat') else {
                'choices':[{'message':{'content':content}}],'usage':{'prompt_tokens':20,'completion_tokens':10}}
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(json.dumps(result).encode())
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    state['url']=f'http://127.0.0.1:{server.server_port}'
    try:yield state
    finally:server.shutdown();server.server_close();thread.join()


@pytest.mark.parametrize('provider',['openai','ollama_direct','litellm'])
def test_judge_agent_reads_evidence_and_records_model_usage(tmp_path,endpoint,monkeypatch,provider):
    monkeypatch.setenv('TEST_JUDGE_KEY','private-test-key')
    config=resolve_judge({'enabled':True,'model':{'provider':provider,'url':endpoint['url'],'name':'judge-model','api_key_env':'TEST_JUDGE_KEY'}})
    write_json(tmp_path/'worker-result.json',{'final_answer':'observed-token'})
    task={'prompt':'Recover the token.','verifier':{'type':'contains_all','expected':['observed-token']}}
    verdict=judge_trial(config,tmp_path,task,'observed-token',{'passed':True})
    assert verdict['passed']
    audit=read_json(tmp_path/'judge.json')
    assert audit['status']=='completed' and len(audit['calls'])==2
    assert audit['calls'][0]['usage']=={'prompt_tokens':20,'output_tokens':10}
    assert 'private-test-key' not in json.dumps(audit)
    assert endpoint['requests'][0][2]=='Bearer private-test-key'
    assert endpoint['requests'][0][1]['model']=='judge-model'
    assert endpoint['requests'][0][0]==('/api/chat' if provider=='ollama_direct' else '/v1/chat/completions')


@pytest.mark.parametrize('failure',['http','json','path','verdict','loop'])
def test_judge_errors_are_saved_without_success_fallback(tmp_path,endpoint,failure):
    endpoint['bad']=failure
    config=resolve_judge({'enabled':True,'model':{'provider':'openai','url':endpoint['url'],'name':'judge'},'max_turns':2})
    write_json(tmp_path/'worker-result.json',{'final_answer':'wrong'})
    with pytest.raises(JudgeError):
        judge_trial(config,tmp_path,{'prompt':'Task','verifier':{'type':'contains_all','expected':['answer']}},'wrong',{'passed':False})
    assert read_json(tmp_path/'judge.json')['status']=='error'


def test_evidence_tool_rejects_arbitrary_paths_and_symlinks(tmp_path):
    write_json(tmp_path/'worker-result.json',{'final_answer':'value'})
    (tmp_path/'messages.json').symlink_to('/etc/passwd')
    evidence=Evidence(tmp_path)
    assert 'messages.json' not in evidence.files
    with pytest.raises(JudgeError):evidence.call({'action':'read_evidence','file':'/etc/passwd'})
    with pytest.raises(JudgeError):evidence.call({'action':'read_evidence','file':'worker-result.json','limit':100000})


@pytest.mark.parametrize('trace',['events.jsonl','messages.json','runs/trial-1/transcript.md','runs/trial-1/tool_calls/001_curl.json','model_calls/call-000001.json','worker.log'])
def test_judge_reads_execution_logs_before_verdict(tmp_path,endpoint,trace):
    output=tmp_path/'guest-output'
    log=output/trace
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps({'tool':'curl','exit_code':0,'result':'observed-token'}))
    write_json(tmp_path/'worker-result.json',{'final_answer':'observed-token'})
    config=resolve_judge({'enabled':True,'model':{'provider':'openai','url':endpoint['url'],'name':'judge'}})
    verdict=judge_trial(config,tmp_path,{'prompt':'Fetch the token','verifier':{'type':'contains_all','expected':['observed-token']}},'observed-token',{'passed':True})
    assert verdict['evidence']==[trace]
    audit=read_json(tmp_path/'judge.json')
    assert audit['execution_trace_reviewed'] and audit['evidence_files_read']==[trace]
    assert audit['evidence_warning'] is None
    assert 'observed-token' in json.loads(audit['messages'][3]['content'])['tool_result']['content']


def test_judge_cannot_skip_collected_execution_logs(tmp_path,endpoint):
    endpoint['bad']='skip_trace'
    (tmp_path/'events.jsonl').write_text('{"type":"tool_result","result":"actual output"}\n')
    write_json(tmp_path/'worker-result.json',{'final_answer':'claim'})
    config=resolve_judge({'enabled':True,'model':{'provider':'openai','url':endpoint['url'],'name':'judge'}})
    with pytest.raises(JudgeError,match='execution log it read'):
        judge_trial(config,tmp_path,{'prompt':'Task','verifier':{'type':'contains_all','expected':['claim']}},'claim',{'passed':True})
    audit=read_json(tmp_path/'judge.json')
    assert not audit['execution_trace_reviewed'] and audit['status']=='error'


def test_empty_logs_are_reported_and_eof_does_not_count_as_evidence(tmp_path,endpoint):
    (tmp_path/'events.jsonl').write_text('')
    (tmp_path/'worker.log').write_text('')
    write_json(tmp_path/'worker-result.json',{'final_answer':'claim'})
    config=resolve_judge({'enabled':True,'model':{'provider':'openai','url':endpoint['url'],'name':'judge'}})
    judge_trial(config,tmp_path,{'prompt':'Task','verifier':{'type':'contains_all','expected':['claim']}},'claim',{'passed':True})
    audit=read_json(tmp_path/'judge.json')
    assert audit['evidence_warning'] and not audit['execution_trace_reviewed']
    evidence=Evidence(tmp_path)
    evidence.call({'action':'read_evidence','file':'worker-result.json','offset':(tmp_path/'worker-result.json').stat().st_size})
    assert not evidence.read


def test_native_tool_records_are_required_and_large_outputs_can_be_paged(tmp_path):
    run=tmp_path/'guest-output/runs/trial-1'
    (run/'tool_calls').mkdir(parents=True)
    tool=run/'tool_calls/001_curl.json'
    tool.write_text('{"tool":"curl","result":"truncated"}')
    (tmp_path/'guest-output/events.jsonl').write_text('{"type":"status"}')
    artifact=run/'artifacts/001_curl_output.txt'
    artifact.parent.mkdir()
    artifact.write_text('x'*2_100_000+'observed-token')
    evidence=Evidence(tmp_path)
    assert evidence.required_trace_files==['runs/trial-1/tool_calls/001_curl.json']
    result=evidence.call({'action':'read_evidence','file':'runs/trial-1/artifacts/001_curl_output.txt','offset':2_100_000,'limit':100})
    assert result['content']=='observed-token' and not result['more']


def test_official_openai_request_supports_reasoning_models(monkeypatch):
    requests=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,limit):return json.dumps({'choices':[{'message':{'content':'{}'}}]}).encode()
    def open_request(request,**kwargs):
        requests.append(request)
        return Response()
    monkeypatch.setattr('urllib.request.urlopen',open_request)
    config=resolve_judge({'enabled':True,'model':{'provider':'openai','url':'https://api.openai.com/v1','name':'o3'}})
    _completion(config,[{'role':'user','content':'Return JSON.'}],10)
    body=json.loads(requests[0].data)
    assert body['max_completion_tokens']==2048
    assert 'max_tokens' not in body and 'temperature' not in body
    assert requests[0].full_url=='https://api.openai.com/v1/chat/completions'


@pytest.mark.parametrize('change',[{'enabled':'yes'},{'max_turns':0},{'timeout_seconds':False},{'max_tokens':20000},{'model':{'provider':'openai','url':'http://user:secret@host','name':'judge'}}])
def test_invalid_judge_config(change):
    value={'enabled':True,'model':{'provider':'openai','url':'http://127.0.0.1:11434/v1','name':'judge'},**change}
    with pytest.raises(ValueError):resolve_judge(value)
