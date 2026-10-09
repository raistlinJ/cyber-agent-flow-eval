"""Actual ScenarioForge import/export and host scoring; VM/model calls are doubles."""

import hashlib
import json
from pathlib import Path
import runpy
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone

import pytest
import yaml
from test_experiments import specification  # noqa: F401
from cyber_agent_flow_eval import judge
from cyber_agent_flow_eval.runner import run
from cyber_agent_flow_eval.scenarioforge import load_suite, require_ready

ROOT = Path(__file__).resolve().parents[2]
BUNDLES = ROOT / "cyber-agent-flow-orchestrator/ScenarioForge-Bundles"
NAMES = [
    "01-http-service-token.zip",
    "02-linked-web-flags.zip",
    "03-json-manifest-token.zip",
    "04-file-download-path-traversal.zip",
    "05-path-traversal-generated-flag.zip",
]


@pytest.mark.parametrize("name", NAMES)
@pytest.mark.parametrize("outcome", ["success", "partial", "format", "exact"])
def test_native_bundle_and_evidence_scoring(
    specification,  # noqa: F811 - pytest fixture injection
    tmp_path,
    monkeypatch,
    name,
    outcome,
):
    source = ROOT / "scenarioforge"
    if not source.is_dir() or not BUNDLES.is_dir():
        pytest.skip("Sibling sample / ScenarioForge checkouts not present")
    monkeypatch.syspath_prepend(str(source))
    from scenarioforge.evaluation.export import export_package

    importer = runpy.run_path(str(source / "webapp/reproduction_bundle.py"))[
        "import_scenario_file"
    ]
    path = BUNDLES / name
    imported = importer(str(path), str(tmp_path / "import"))
    assert imported.bundled_artifact_sources == imported.total_artifact_sources > 0
    xml = Path(imported.xml_path)
    root = ET.parse(xml)
    scene = root.find("Scenario").get("name")
    task = json.loads(root.find(".//FlowState").text)["evaluation_tasks"][0]
    with zipfile.ZipFile(path) as archive:
        assert json.loads(archive.read("evaluation-tasks.json")) == [task]
        assert json.loads(archive.read("evaluation-rubric.json")) == task["rubric"]
        assert json.loads(archive.read("experiment-profile.json"))["judge_required"]
        guide = archive.read("participant-guide.md").decode()
    expected = task["verifier"]["expected"]
    secrets = expected["flags"] if "flags" in expected else list(expected.values())
    assert all(
        secret not in task["prompt"] and secret not in guide for secret in secrets
    )
    assert task["split"] == "development" and task["verification_mode"] == "both"
    assert task["progressive_hints"] and task["required_checks"] == [
        "containers",
        "services",
        "ports",
    ]
    if outcome == "exact":
        task["verification_mode"] = "exact"
    readiness = dict(
        status="complete",
        ok=True,
        overall="pass",
        scenario=scene,
        session_id=1,
        session_confirmed=True,
        core_host="fixture-core",
        xml_sha256=hashlib.sha256(xml.read_bytes()).hexdigest(),
        checked_at=datetime.now(timezone.utc).isoformat(),
        checks=[dict(key=k, status="pass") for k in task["required_checks"]],
    )
    package = tmp_path / "package"
    manifest = export_package(
        xml_path=xml,
        graph=dict(
            schema_version=2,
            scenario=scene,
            nodes=[dict(id="web", ipv4="192.0.2.10")],
            edges=[],
        ),
        output=package,
        suite_id="sample-contract",
        definitions=[task],
        readiness=readiness,
        session_id=1,
    )
    assert manifest["version"] == 4
    tasks, snapshot = load_suite(package)
    require_ready(snapshot, 3600)
    assert tasks[0]["rubric"] == task["rubric"]
    public = (package / "participant/tasks.json").read_text()
    assert all(secret not in public for secret in secrets)
    assert "For JSON-only tasks, do not add evidence fields" in public
    raw = yaml.safe_load(specification.read_text())
    raw.pop("tasks")
    raw["suite"] = dict(path=str(package), max_readiness_age_seconds=3600)
    raw["conditions"] = raw["conditions"][:1]
    raw["repetitions"] = 1
    raw["judge"] = dict(
        enabled=outcome != "exact",
        model=dict(provider="openai", url="http://fixture/v1", name="independent"),
    )
    specification.write_text(yaml.safe_dump(raw))

    def launch(directory, seconds, **kwargs):
        assert all(
            secret not in (directory / "input.json").read_text() for secret in secrets
        )
        (directory / "events.jsonl").write_text(
            json.dumps(
                dict(
                    type="tool_result",
                    tool="curl",
                    exit_code=0,
                    result="Observed HTTP response: " + json.dumps(expected),
                )
            )
            + "\n"
        )
        return dict(
            status="completed",
            final_answer=(
                json.dumps(expected) if outcome not in {"partial", "format"} else "{}"
            ),
            errors=[],
        )

    def completion(config, messages, timeout):
        if len(messages) == 2:
            return (
                json.dumps(
                    dict(
                        action="read_evidence", file="events.jsonl", offset=0, limit=100
                    )
                ),
                {},
            )
        criteria = [
            dict(
                id=c["id"],
                status=(
                    "satisfied"
                    if outcome in {"success", "format"} or i == 0
                    else "unmet"
                ),
                reason="Fixture execution output inspected",
                evidence=[dict(file="events.jsonl", offset=0, limit=1)],
            )
            for i, c in enumerate(task["rubric"]["criteria"])
        ]
        return json.dumps(dict(action="verdict", criteria=criteria)), {}

    monkeypatch.setattr(judge, "_completion", completion)
    row = run(specification, tmp_path / "evaluation", launcher=launch, progress=None)[0]
    if outcome == "partial":
        assert row["verified_success"] is False and row["task_outcome"] == "partial"
        assert 0 < row["score"] < 1
    elif outcome == "format":
        assert row["verified_success"] is False and row["task_outcome"] == "partial"
        assert row["score"] == 1
    else:
        assert row["verified_success"] is True and row["task_outcome"] == "success"
    assert row["verification_mode"] == ("exact" if outcome == "exact" else "both")
