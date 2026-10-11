import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from cyber_agent_flow_eval.judge import _completion, resolve_judge, JudgeError
from cyber_agent_flow_eval.judge_stream import EventLog, completion_response


@pytest.mark.parametrize('provider',['openai','ollama_direct'])
def test_live_delta_is_saved_before_generation_finishes(tmp_path,provider):
    released, early = threading.Event(), threading.Event()
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200)
            self.send_header('Content-Type','application/x-ndjson' if provider=='ollama_direct' else 'text/event-stream')
            self.end_headers()
            def send(data):
                raw=json.dumps(data).encode()
                self.wfile.write(raw+b'\n' if provider=='ollama_direct' else b'data: '+raw+b'\n\n')
                self.wfile.flush()
            first={'message':{'content':'{"action":','thinking':'Checking.'},'done':False} if provider=='ollama_direct' else {'choices':[{'delta':{'content':'{"action":','reasoning_content':'Checking.'}}]}
            send(first)
            released.wait(3)
            last={'message':{'content':'"verdict"}'},'done':True,'prompt_eval_count':7,'eval_count':3} if provider=='ollama_direct' else {'choices':[{'delta':{'content':'"verdict"}'},'finish_reason':'stop'}],'usage':{'prompt_tokens':7,'completion_tokens':3}}
            send(last)
            if provider!='ollama_direct':
                self.wfile.write(b'data: [DONE]\n\n');self.wfile.flush()
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    log=EventLog(tmp_path/'events.jsonl')
    def delta(channel,text):
        log.delta(channel,text);log.flush()
        if channel=='response' and text:
            assert 'Checking.' in log.path.read_text()
            assert '{\"action\":' in log.path.read_text().replace('\\"','"')
            early.set();released.set()
    try:
        cfg=resolve_judge({'enabled':True,'model':{'provider':provider,'url':f'http://127.0.0.1:{server.server_port}','name':'test'}})
        text,usage=_completion(cfg,[{'role':'user','content':'Synthetic JSON.'}],5,on_delta=delta)
        assert early.is_set() and json.loads(text)=={'action':'verdict'}
        assert usage=={'prompt_tokens':7,'output_tokens':3}
        assert requests[0]['stream'] is True
        assert log.path.stat().st_mode & 0o777 == 0o600
    finally:
        released.set();server.shutdown();server.server_close();thread.join()


@pytest.mark.parametrize('payload',[
    b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n',
    b'data: {"error":{"message":"private provider detail"}}\n\n',
    b'data: not-json\n\n',
])
def test_incomplete_and_failed_streams_never_become_verdicts(payload):
    response=io.BytesIO(payload);response.headers={'Content-Type':'text/event-stream'}
    with pytest.raises(ValueError):
        completion_response(response,False,lambda *a:None,__import__('time').monotonic()+1)


def test_stream_heartbeats_do_not_extend_total_deadline():
    response=io.BytesIO(b': heartbeat\n');response.headers={'Content-Type':'text/event-stream'}
    with pytest.raises(TimeoutError):
        completion_response(response,False,None,__import__('time').monotonic()-1)


def test_server_ignoring_stream_is_supported():
    response=io.BytesIO(b'{"choices":[{"message":{"content":"{}"}}]}');response.headers={'Content-Type':'application/json'}
    deltas=[]
    assert completion_response(response,False,lambda *a:deltas.append(a),__import__('time').monotonic()+1)[0]=='{}'
    assert ('response','{}') in deltas


def test_optional_output_failure_does_not_fail_a_review(tmp_path):
    log=EventLog(tmp_path/'missing/events.jsonl')
    log.delta('response','{}');log.flush();log.emit('status',text='Done')
    assert log.disabled
