"""Zero is explicit no-timeout, including streams, assistance and guest backstops."""

import io
import json
from types import SimpleNamespace
import pytest
import yaml
from cyber_agent_flow_eval import judge
from cyber_agent_flow_eval.execution_service import ActivityBudget
from cyber_agent_flow_eval.judge_stream import completion_response
from cyber_agent_flow_eval.spec import resolve
from cyber_agent_flow_eval.storage import read_json, write_json
from test_experiments import specification
from test_proxmox import remote_spec, config
from test_progress_monitor import controller, observation, completion_for


def cfg():
    return judge.resolve_judge(
        dict(
            enabled=True,
            timeout_seconds=0,
            progress_timeout_seconds=0,
            model=dict(provider="openai", url="http://judge.invalid/v1", name="judge"),
        )
    )


def test_resolve_unlimited_limits_and_reject_negative_or_boolean(specification):
    raw = yaml.safe_load(specification.read_text())
    raw["execution"].update(wall_seconds=0, tool_timeout=0)
    specification.write_text(yaml.safe_dump(raw))
    value = resolve(specification)
    assert (
        value["execution"]["wall_seconds"] == 0
        and value["execution"]["tool_timeout"] == 0
    )
    assert cfg()["timeout_seconds"] == cfg()["progress_timeout_seconds"] == 0
    for field in ("timeout_seconds", "progress_timeout_seconds"):
        for invalid in (-1, False):
            settings = dict(cfg())
            settings[field] = invalid
            with pytest.raises(ValueError):
                judge.resolve_judge(settings)


def test_judge_no_timeout_uses_unbounded_http_and_stream(monkeypatch):
    calls = []

    class Response(io.BytesIO):
        headers = {"Content-Type": "application/json"}

    def open_request(request, timeout, context):
        calls.append(timeout)
        return Response(
            json.dumps(
                dict(choices=[dict(message=dict(content='{"action":"verdict"}'))])
            ).encode()
        )

    monkeypatch.setattr(judge.urllib.request, "urlopen", open_request)
    assert judge._completion(cfg(), [], None)[0] == '{"action":"verdict"}'
    assert calls == [None]
    streamed = Response(
        b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    )
    streamed.headers = {"Content-Type": "text/event-stream"}
    timeouts = []
    streamed.fp = SimpleNamespace(
        raw=SimpleNamespace(_sock=SimpleNamespace(settimeout=timeouts.append))
    )
    assert completion_response(streamed, False, lambda *a: None, None)[0] == "ok"
    assert timeouts and all(value is None for value in timeouts)


def test_final_judge_and_monitor_have_no_hidden_elapsed_budget(tmp_path, monkeypatch):
    (tmp_path / "events.jsonl").write_text('{"type":"tool_result","result":"proof"}\n')
    (tmp_path / "worker-result.json").write_text('{"final_answer":"proof"}')
    calls = []

    def completion(config, messages, timeout, **kwargs):
        calls.append(timeout)
        value = (
            dict(action="read_evidence", file="events.jsonl", offset=0, limit=6000)
            if len(messages) == 2
            else dict(
                action="verdict",
                passed=True,
                score=1,
                reason="Observed output",
                evidence=["events.jsonl"],
            )
        )
        return json.dumps(value), {}

    monkeypatch.setattr(judge, "_completion", completion)
    assert judge.judge_trial(
        cfg(),
        tmp_path,
        dict(
            prompt="Read proof", verifier=dict(type="contains_all", expected=["proof"])
        ),
        "proof",
        None,
    )["passed"]
    assert calls == [None, None]
    c = controller(tmp_path)
    c.monitor.config["progress_timeout_seconds"] = 0
    base = completion_for({"entry": "satisfied", "target": "unverified"})

    def monitor_completion(config, messages, timeout, **kwargs):
        assert timeout is None
        return base(config, messages, timeout, **kwargs)

    monkeypatch.setattr(judge, "_completion", monitor_completion)
    assert c.respond(observation(1))["hint"] is None
    assert c.completed_steps == {"first"}


@pytest.mark.asyncio
async def test_unlimited_activity_budget_survives_large_elapsed_time():
    clock = [0.0]
    budget = ActivityBudget(0, clock=lambda: clock[0])

    async def operation():
        clock[0] = 10**12
        return "finished"

    assert await budget.run(operation()) == "finished"


def test_remote_worker_no_timeout_disables_systemd_backstop(remote_spec, tmp_path):
    from cyber_agent_flow_eval.runner import run

    path, backend, agent = remote_spec
    raw = yaml.safe_load(path.read_text())
    raw["execution"].update(wall_seconds=0, tool_timeout=0)
    path.write_text(yaml.safe_dump(raw))
    rows = run(path, tmp_path / "results")
    assert all(row["status"] == "completed" for row in rows)
    starts = [data for _, op, data in agent.calls if op == "start"]
    assert starts and all(data["seconds"] == 0 for data in starts)


def test_unlimited_network_failure_is_not_reported_as_budget_expiry(monkeypatch):
    def unavailable(*args,**kwargs):
        raise TimeoutError('OS-level connection failure')
    monkeypatch.setattr(judge.urllib.request,'urlopen',unavailable)
    with pytest.raises(judge.JudgeError,match='despite no application deadline'):
        judge._completion(cfg(),[],None)
