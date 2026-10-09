import base64
import json
import pytest
from cyber_agent_flow_eval.guest_agent import transcript_chunk
from cyber_agent_flow_eval.live_transcript import LiveTranscript
from cyber_agent_flow_eval.execution_service import RecordingClient, returned_reasoning


def test_chunks_preserve_utf8_partial_lines_and_finish_without_duplicates(tmp_path):
    attempt=tmp_path/'trials/trial-1/attempt-0001'
    attempt.mkdir(parents=True)
    (attempt/'attempt.json').write_text(json.dumps({'trial_id':'trial-1','attempt':1,'condition_id':'baseline'}))
    transcript=LiveTranscript(attempt)
    data=(json.dumps({'type':'prompt','text':'Read café'},ensure_ascii=False)+'\n'+json.dumps({'type':'tool_result','result':'observed'})+'\n').encode()
    split=data.index(b'\xc3')+1
    transcript.accept({'offset':0,'content':base64.b64encode(data[:split]).decode()})
    assert not transcript.path.exists()
    transcript.accept({'offset':split,'content':base64.b64encode(data[split:split+10]).decode()})
    output=attempt/'events.jsonl';output.write_bytes(data)
    transcript.finish(output)
    rows=[json.loads(line) for line in transcript.path.read_text().splitlines()]
    assert len(rows)==2 and rows[0]['event']['text']=='Read café'
    assert rows[0]['trial_id']=='trial-1' and rows[1]['condition_id']=='baseline'
    assert transcript.offset==len(data)
    transcript.finish(output)
    assert len(transcript.path.read_text().splitlines())==2


def test_guest_live_chunk_is_bounded_missing_safe_and_rejects_symlinks(tmp_path):
    assert transcript_chunk(tmp_path,0)=={'offset':0,'content':''}
    path=tmp_path/'events.jsonl';path.write_bytes(b'x'*40000)
    chunk=transcript_chunk(tmp_path,12)
    assert chunk['offset']==12 and len(base64.b64decode(chunk['content']))==16384
    path.unlink();path.symlink_to('/etc/passwd')
    with pytest.raises(OSError):transcript_chunk(tmp_path,0)
    with pytest.raises(ValueError):transcript_chunk(tmp_path,-1)


@pytest.mark.parametrize('response',[
    {'message':{'thinking':'Returned explanation','content':'Answer'}},
    {'message':{'content':'Answer'},'raw':{'choices':[{'message':{'reasoning_content':'Returned explanation'}}]}},
    {'raw':{'content':[{'type':'thinking','thinking':'Returned explanation'}]}},
])
def test_recording_client_emits_only_provider_returned_reasoning(tmp_path,response):
    class Client:
        def chat(self):return response
    events=[]
    recorder=RecordingClient(Client(),tmp_path/'model_calls',emit=events.append)
    assert recorder.chat()==response
    assert events==[{'type':'reasoning','text':'Returned explanation','model_call':1}]
    assert returned_reasoning({'message':{'content':'Ordinary answer'}})==''
    assert returned_reasoning({'message':{'content':'Ordinary answer'},'raw':{'choices':123}})==''


def test_live_preview_clips_large_events_and_recovers_after_oversized_line(tmp_path):
    transcript=LiveTranscript(tmp_path)
    transcript.feed((json.dumps({'type':'response','text':'x'*60000})+'\n').encode())
    transcript.feed(b'x'*300000)
    transcript.feed(b'not JSON\n'+b'{"type":"status","message":"Recovered"}\n')
    records=[json.loads(line)['event'] for line in transcript.path.read_text().splitlines()]
    assert records[0]['truncated'] and len(records[0]['text'])==8000
    assert 'oversized' in records[1]['message'] and records[-1]['message']=='Recovered'
