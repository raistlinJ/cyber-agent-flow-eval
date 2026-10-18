"""Cited progress observations describe activity without awarding completion."""

from copy import deepcopy
import json
import pytest
from cyber_agent_flow_eval import judge
from cyber_agent_flow_eval.monitor_observations import validate_observations
from cyber_agent_flow_eval.storage import read_json
from test_progress_monitor import TASK, controller, observation, completion_for


def evidence_at(root):
    (root / "events.jsonl").write_text(
        json.dumps(
            dict(
                type="tool_result", tool="curl", result="HTTP 200; banner is available"
            )
        )
        + "\n"
    )
    (root / "reference-material.json").write_text('{"answer":"private guide"}')
    evidence = judge.Evidence(root)
    evidence.call(
        dict(action="read_evidence", file="events.jsonl", offset=0, limit=6000)
    )
    evidence.call(
        dict(
            action="read_evidence", file="reference-material.json", offset=0, limit=6000
        )
    )
    return evidence


def observed():
    return dict(
        step_id="first",
        activity="discovered",
        summary="Read the service banner.",
        discoveries=["The service responds with HTTP 200."],
        evidence=[dict(file="events.jsonl", offset=0, limit=6000)],
    )


def test_observation_requires_known_step_and_read_direct_execution(tmp_path):
    evidence = evidence_at(tmp_path)
    item = observed()
    validated = validate_observations([item], TASK, evidence)
    assert (
        validated[0]["evidence"][0]["limit"]
        == (tmp_path / "events.jsonl").stat().st_size
    )
    assert item["evidence"][0]["limit"] == 6000
    for mutation, match in [
        ({"step_id": "unknown"}, "known"),
        ({"discoveries": []}, "discovery"),
        ({"activity": "solved"}, "activity"),
        (
            {"evidence": [dict(file="reference-material.json", offset=0, limit=6000)]},
            "direct execution",
        ),
        (
            {"evidence": [dict(file="events.jsonl", offset=1000, limit=1)]},
            "actually read",
        ),
        ({"evidence": []}, "citations"),
        ({"discoveries": ["x" * 501]}, "bounded"),
    ]:
        bad = dict(item, **mutation)
        with pytest.raises(ValueError, match=match):
            validate_observations([bad], TASK, evidence)
    with pytest.raises(ValueError, match="distinct"):
        validate_observations([item, item], TASK, evidence)
    evidence.ranges = {}
    with pytest.raises(ValueError, match="actually read"):
        validate_observations([item], TASK, evidence)


def test_agent_claim_is_not_a_discovery(tmp_path):
    (tmp_path / "events.jsonl").write_text('{"type":"assistant","text":"I found it"}\n')
    evidence = judge.Evidence(tmp_path)
    evidence.call(
        dict(action="read_evidence", file="events.jsonl", offset=0, limit=6000)
    )
    with pytest.raises(ValueError, match="direct execution"):
        validate_observations([observed()], TASK, evidence)


def test_monitor_persists_cited_discovery_without_solving_step(tmp_path, monkeypatch):
    c = controller(tmp_path)
    evidence_at(tmp_path)
    base = completion_for({"entry": "unverified", "target": "unverified"})
    turn = [1]

    def completion(cfg, messages, timeout, **kwargs):
        body, usage = base(cfg, messages, timeout, **kwargs)
        if len(messages) > 2:
            value = json.loads(body)
            value["observations"] = [observed()]
            if turn[0] == 2:
                value["observations"][0]["discoveries"] = [
                    "The banner includes a version."
                ]
            body = json.dumps(value)
        return body, usage

    monkeypatch.setattr(judge, "_completion", completion)
    c.respond(observation(1))
    turn[0] = 2
    c.respond(observation(2))
    saved = read_json(tmp_path / "progress-monitor.json")
    assert saved["version"] == 2 and saved["updated_at"]
    assert saved["targeted_steps"] == ["first"] and saved["completed_steps"] == []
    entry = saved["observations"]["first"]
    assert entry["turn"] == 2 and len(entry["discoveries"]) == 2
    assert (tmp_path / entry["audit"]).is_file() and entry["evidence"][0][
        "file"
    ] == "events.jsonl"
    assert all(check["observations"] for check in saved["checks"])
    assert not (tmp_path / "judge.json").exists()


def test_monitor_does_not_release_guidance_when_hints_are_off(tmp_path, monkeypatch):
    from cyber_agent_flow_eval.hints import HintController
    from cyber_agent_flow_eval.judge import resolve_judge
    from test_progress_monitor import PLAN

    evidence_at(tmp_path)
    cfg = resolve_judge(
        dict(
            enabled=True,
            model=dict(provider="openai", url="http://fixture/v1", name="monitor"),
        )
    )
    c = HintController(
        tmp_path,
        {},
        TASK["verifier"],
        dict(max_tries_before_solution=1),
        task=TASK,
        judge=cfg,
        release_hints=False,
    )
    monkeypatch.setattr(
        judge,
        "_completion",
        completion_for({"entry": "satisfied", "target": "unverified"}),
    )
    assert c.respond(observation(5))["hint"] is None
    assert c.completed_steps == {"first"} and not c.events and not c.feedback
    assert (
        read_json(tmp_path / "progress-monitor.json")["checks"][0]["status"]
        == "completed"
    )


def test_monitor_targets_must_be_literal_and_present_in_cited_tool_evidence(tmp_path):
    evidence=evidence_at(tmp_path)
    (tmp_path/'events.jsonl').write_text('{"type":"tool_result","result":"Connected to 10.0.0.3:80"}\n')
    evidence=judge.Evidence(tmp_path)
    evidence.call(dict(action='read_evidence',file='events.jsonl',offset=0,limit=6000))
    item=observed();item['targets']=['10.0.0.3']
    assert validate_observations([item],TASK,evidence)[0]['targets']==['10.0.0.3']
    for targets in [['10.0.0.99'],['example.com'],['999.1.2.3'],[False],['10.0.0.3']*17]:
        with pytest.raises(ValueError):validate_observations([dict(item,targets=targets)],TASK,evidence)


def test_ip_extraction_handles_urls_ranges_ipv6_and_ignores_secret_fields():
    from cyber_agent_flow_eval.network_targets import extract,overlaps
    value=extract(dict(url='http://10.0.0.3/path',args='nmap 10.0.0.20/24; curl http://[2001:db8::2]/',api_key='192.0.2.99',password='192.0.2.8'))
    assert value==['10.0.0.3','10.0.0.0/24','2001:db8::2']
    assert extract('http://999.0.0.3/ malformed 10.0.0.3/999')==[]
    assert overlaps('10.0.0.0/24','10.0.0.3') and not overlaps('10.0.0.0/24','2001:db8::2')
