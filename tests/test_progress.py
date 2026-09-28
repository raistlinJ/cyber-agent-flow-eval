import json

from cyber_agent_flow_eval.progress import record_progress
from cyber_agent_flow_eval.storage import read_json, write_json


def test_progress_uses_observed_outputs_not_prompt_or_tool_arguments(tmp_path):
    verifier = {'type': 'flags_found', 'expected': {'a': 'FLAG{a}', 'b': 'FLAG{b}'}}
    events = [
        {'type': 'tool_call', '_eval_elapsed_seconds': 1, 'args': 'FLAG{b}'},
        {'type': 'tool_result', '_eval_elapsed_seconds': 12, 'result': 'found FLAG{a}'},
        {'type': 'tool_result', '_eval_elapsed_seconds': 15, 'result': 'FLAG{a}'},
    ]
    (tmp_path / 'events.jsonl').write_text('\n'.join(json.dumps(e) for e in events) + '\n{"type":')
    (tmp_path / 'model_calls').mkdir()
    write_json(tmp_path / 'model_calls/call-000001.json', {'observed_seconds': 1, 'request': 'FLAG{b}'})
    result = record_progress(tmp_path, verifier, [10, 20])
    assert result == {'flags_observed': 1, 'progress_score': .5, 'time_to_first_flag_seconds': 12}
    progress = read_json(tmp_path / 'progress.json')
    assert progress['checkpoints'] == [{'seconds': 10, 'flags_observed': 0}, {'seconds': 20, 'flags_observed': 1}]
    assert progress['unreadable_records'] == 1
    assert 'FLAG{' not in (tmp_path / 'progress.json').read_text()


def test_missing_telemetry_is_not_scored_as_zero(tmp_path):
    result = record_progress(tmp_path, {'type': 'flags_found', 'expected': {'a': 'FLAG{a}'}}, [10])
    assert result['flags_observed'] is None
    assert result['progress_score'] is None
    assert read_json(tmp_path / 'progress.json')['telemetry_available'] is False
