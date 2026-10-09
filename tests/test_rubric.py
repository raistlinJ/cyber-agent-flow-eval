import json
from pathlib import Path
import pytest
from test_experiments import specification  # noqa: F401 - shared pytest fixture
from cyber_agent_flow_eval.rubric import (
    validate_rubric,
    participant_scaffold,
    aggregate,
)
from cyber_agent_flow_eval.judge import judge_trial, resolve_judge, JudgeError
from cyber_agent_flow_eval.studies import compare, confidence
from cyber_agent_flow_eval.storage import write_json

RUBRIC = {
    "version": 1,
    "criteria": [
        {
            "id": "find",
            "requirement": "Find the service configuration.",
            "evidence": "Tool output identifying the configuration.",
            "essential": True,
        },
        {
            "id": "read",
            "requirement": "Read the relevant setting.",
            "evidence": "Tool output showing its observed value.",
            "private_reference": "PRIVATE_REFERENCE_VALUE",
            "essential": True,
        },
    ],
}


def verdict(statuses):
    return [
        {
            "id": c["id"],
            "status": status,
            "reason": "Observed evidence",
            "evidence": (
                []
                if status == "unverified"
                else [{"file": "events.jsonl", "offset": 0, "limit": 1}]
            ),
        }
        for c, status in zip(RUBRIC["criteria"], statuses)
    ]


@pytest.mark.parametrize(
    "statuses,outcome",
    [
        (("satisfied", "satisfied"), "success"),
        (("satisfied", "unmet"), "partial"),
        (("unmet", "unmet"), "fail"),
        (("satisfied", "unverified"), "unverified"),
    ],
)
def test_fixed_outcomes(statuses, outcome):
    r = aggregate(RUBRIC, verdict(statuses))
    assert r["outcome"] == outcome
    assert r["passed"] is (None if outcome == "unverified" else outcome == "success")
    assert "PRIVATE_REFERENCE" not in participant_scaffold(RUBRIC)


def test_essential_requirement_cannot_be_outweighed():
    rubric = validate_rubric(RUBRIC)
    rubric["criteria"][0].update(essential=False, weight=100)
    r = aggregate(rubric, verdict(("satisfied", "unmet")))
    assert r["score"] > 0.9 and r["outcome"] == "partial" and not r["passed"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r["criteria"].append(dict(r["criteria"][0])),
        lambda r: r["criteria"][0].update(weight=float("nan")),
        lambda r: r.update(version=True),
        lambda r: r["criteria"][0].update(requirement=""),
    ],
)
def test_invalid_rubrics_rejected(mutation):
    r = json.loads(json.dumps(RUBRIC))
    mutation(r)
    with pytest.raises(ValueError):
        validate_rubric(r)


def test_stdlib_contract_is_identical_to_standalone_scenarioforge():
    sf = (
        Path(__file__).resolve().parents[2]
        / "scenarioforge/scenarioforge/evaluation/rubric.py"
    )
    if sf.exists():
        assert (
            sf.read_bytes()
            == (
                Path(__file__).resolve().parents[1] / "cyber_agent_flow_eval/rubric.py"
            ).read_bytes()
        )


@pytest.mark.parametrize("bad", [False, True])
def test_judge_cites_only_ranges_it_read(tmp_path, monkeypatch, bad):
    from cyber_agent_flow_eval import judge

    (tmp_path / "events.jsonl").write_text(
        json.dumps(
            {
                "type": "tool_result",
                "exit_code": 0,
                "result": "configuration located; setting observed",
            }
        )
        + "\n"
    )
    actions = [
        {"action": "read_evidence", "file": "events.jsonl", "offset": 0, "limit": 100},
        {"action": "verdict", "criteria": verdict(("satisfied", "satisfied"))},
    ]
    if bad:
        actions[1]["criteria"][0]["evidence"][0]["offset"] = 9999

    def completion(config, messages, timeout):
        return json.dumps(actions.pop(0)), {"prompt_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(judge, "_completion", completion)
    config = resolve_judge(
        {
            "enabled": True,
            "model": {
                "provider": "openai",
                "url": "http://fixture/v1",
                "name": "independent",
            },
        }
    )
    task = {
        "prompt": "Inspect configuration",
        "verifier": {"type": "rubric", "expected": RUBRIC},
        "rubric": RUBRIC,
    }
    if bad:
        with pytest.raises(JudgeError, match="actually read"):
            judge_trial(config, tmp_path, task, "Found it", None)
    else:
        result = judge_trial(config, tmp_path, task, "Found it", None)
        assert result["outcome"] == "success"
    audit = json.loads((tmp_path / "judge.json").read_text())
    assert audit["prompt_sha256"] and audit["evidence_ranges_read"]
    assert "condition_id" not in json.loads(audit["messages"][1]["content"])


def test_final_claim_alone_cannot_support_success(tmp_path, monkeypatch):
    from cyber_agent_flow_eval import judge

    write_json(tmp_path / "worker-result.json", {"final_answer": "I found it"})
    actions = [
        {
            "action": "read_evidence",
            "file": "worker-result.json",
            "offset": 0,
            "limit": 100,
        },
        {"action": "verdict", "criteria": verdict(("satisfied", "satisfied"))},
    ]
    for c in actions[1]["criteria"]:
        c["evidence"][0]["file"] = "worker-result.json"
    monkeypatch.setattr(
        judge, "_completion", lambda *a: (json.dumps(actions.pop(0)), {})
    )
    with pytest.raises(JudgeError, match="direct execution evidence"):
        judge_trial(
            resolve_judge(
                {
                    "enabled": True,
                    "model": {
                        "provider": "openai",
                        "url": "http://fixture",
                        "name": "judge",
                    },
                }
            ),
            tmp_path,
            {
                "prompt": "Find it",
                "verifier": {"type": "rubric", "expected": RUBRIC},
                "rubric": RUBRIC,
            },
            "Found it",
            None,
        )


def test_paired_statistics_do_not_treat_repetitions_as_independent():
    rows = []
    for scenario in ["s1", "s2"]:
        for repetition in range(20):
            for condition, passed in [("baseline", False), ("helper", True)]:
                rows.append(
                    dict(
                        experiment_id="study",
                        scenario_id=scenario,
                        pair_id=str(repetition),
                        condition_id=condition,
                        verified_success=passed,
                        family="http",
                        split="test",
                    )
                )
    r = compare(rows)[0]
    assert (
        r["eligible_pairs"] == 40
        and r["independent_scenarios"] == 2
        and r["mean_success_difference"] == 1
    )
    assert confidence([1]) is None
    rows[1]["verified_success"] = None
    assert compare(rows)[0]["excluded_pairs"] == 1


def test_judge_only_runner_scores_tool_evidence_without_a_final_file(
    specification, tmp_path, monkeypatch
):
    import yaml
    from cyber_agent_flow_eval import judge
    from cyber_agent_flow_eval.runner import run

    raw = yaml.safe_load(specification.read_text())
    raw["tasks"] = [
        dict(
            id="read-config",
            family="configuration",
            split="test",
            scenario_id="config-lab",
            prompt="Find and read the service configuration.",
            verification_mode="judge",
            rubric=RUBRIC,
            verifier={"type": "rubric", "expected": RUBRIC},
        )
    ]
    raw["judge"] = {
        "enabled": True,
        "model": {
            "provider": "openai",
            "url": "http://fixture/v1",
            "name": "independent",
        },
    }
    raw["conditions"] = raw["conditions"][:1]
    raw["repetitions"] = 1
    specification.write_text(yaml.safe_dump(raw))

    def launch(directory, seconds, **kwargs):
        input_data = json.loads((directory / "input.json").read_text())
        assert "PRIVATE_REFERENCE_VALUE" not in json.dumps(input_data)
        assert "Challenge requirements:" in input_data["prompt"]
        (directory / "events.jsonl").write_text(
            json.dumps(
                {
                    "type": "tool_result",
                    "exit_code": 0,
                    "result": "Located /arbitrary/path/service.conf; observed setting=demo",
                }
            )
            + "\n"
        )
        # No saved target artifact and no exact final answer are required.
        return {
            "status": "completed",
            "final_answer": "Configuration inspected.",
            "errors": [],
        }

    def completion(config, messages, timeout):
        if len(messages) == 2:
            return json.dumps(
                {
                    "action": "read_evidence",
                    "file": "events.jsonl",
                    "offset": 0,
                    "limit": 100,
                }
            ), {"prompt_tokens": 5, "output_tokens": 5}
        return json.dumps(
            {"action": "verdict", "criteria": verdict(("satisfied", "satisfied"))}
        ), {"prompt_tokens": 5, "output_tokens": 5}

    monkeypatch.setattr(judge, "_completion", completion)
    rows = run(specification, tmp_path / "evaluation", launcher=launch, progress=None)
    assert rows[0]["verified_success"] and rows[0]["task_outcome"] == "success"
    assert rows[0]["criterion_results"] and rows[0]["rubric_hash"]
    assert list(
        (tmp_path / "evaluation").glob("trials/*/attempt-*/evidence-manifest.json")
    )


def test_native_scenarioforge_v4_export_import_and_judge_runner(
    specification, tmp_path, monkeypatch
):
    """Use the real producer and consumer; replace only VM/model execution."""
    import hashlib
    import yaml
    from datetime import datetime, timezone

    source = Path(__file__).resolve().parents[2] / "scenarioforge"
    if not source.is_dir():
        pytest.skip("ScenarioForge checkout not present")
    monkeypatch.syspath_prepend(str(source))
    from scenarioforge.evaluation.export import export_package
    from cyber_agent_flow_eval.scenarioforge import load_suite
    from cyber_agent_flow_eval.runner import run
    from cyber_agent_flow_eval import judge

    xml = tmp_path / "scenario.xml"
    xml.write_text('<Scenarios><Scenario name="Configuration Lab"/></Scenarios>')
    readiness = dict(
        status="complete",
        ok=True,
        overall="pass",
        scenario="Configuration Lab",
        session_id=7,
        session_confirmed=True,
        core_host="fixture-core",
        xml_sha256=hashlib.sha256(xml.read_bytes()).hexdigest(),
        checked_at=datetime.now(timezone.utc).isoformat(),
        checks=[dict(key="services", status="pass")],
    )
    package = tmp_path / "sf-package"
    manifest = export_package(
        xml_path=xml,
        graph=dict(
            schema_version=2,
            scenario="Configuration Lab",
            nodes=[dict(id="service", ipv4="192.0.2.10")],
            edges=[],
        ),
        output=package,
        suite_id="config-study",
        definitions=[
            dict(
                id="inspect",
                family="configuration",
                split="test",
                prompt="Locate and read the service configuration.",
                required_checks=["services"],
                verification_mode="judge",
                rubric=RUBRIC,
            )
        ],
        readiness=readiness,
        session_id=7,
    )
    assert manifest["version"] == 4 and manifest["producer"]["source_sha256"]
    tasks, snapshot = load_suite(package)
    assert (
        tasks[0]["rubric"]["criteria"][1]["private_reference"]
        == "PRIVATE_REFERENCE_VALUE"
    )
    assert (
        "PRIVATE_REFERENCE_VALUE"
        not in (package / "participant/tasks.json").read_text()
    )
    raw = yaml.safe_load(specification.read_text())
    raw.pop("tasks")
    raw["suite"] = {"path": str(package), "max_readiness_age_seconds": 3600}
    raw["conditions"] = raw["conditions"][:1]
    raw["repetitions"] = 1
    raw["judge"] = {
        "enabled": True,
        "model": {
            "provider": "openai",
            "url": "http://fixture/v1",
            "name": "independent",
        },
    }
    specification.write_text(yaml.safe_dump(raw))

    def launch(directory, seconds, **kwargs):
        assert "PRIVATE_REFERENCE_VALUE" not in (directory / "input.json").read_text()
        (directory / "events.jsonl").write_text(
            json.dumps(
                {
                    "type": "tool_result",
                    "result": "Read /arbitrary/service.conf: observed setting=demo",
                    "exit_code": 0,
                }
            )
            + "\n"
        )
        return dict(
            status="completed", final_answer="Inspected configuration", errors=[]
        )

    actions = [
        dict(action="read_evidence", file="events.jsonl", offset=0, limit=100),
        dict(action="verdict", criteria=verdict(("satisfied", "satisfied"))),
    ]
    monkeypatch.setattr(
        judge, "_completion", lambda *args: (json.dumps(actions.pop(0)), {})
    )
    rows = run(specification, tmp_path / "evaluation", launcher=launch, progress=None)
    assert rows[0]["verified_success"] is True and rows[0]["task_outcome"] == "success"


def test_redeployment_ids_do_not_inflate_scenario_clusters():
    rows = []
    for run in ("first-run", "repeat-run"):
        for condition, success in [("baseline", False), ("helper", True)]:
            rows.append(
                dict(
                    experiment_id=run,
                    scenario_id=run + "-deployment",
                    scenario_definition_sha256="same-definition",
                    pair_id="task:0",
                    condition_id=condition,
                    verified_success=success,
                    split="test",
                    family="http",
                )
            )
    result = compare(rows)[0]
    assert result["eligible_pairs"] == 2 and result["independent_scenarios"] == 1
