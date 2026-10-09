import json
import sys
from pathlib import Path
import pytest
from cyber_agent_flow_eval.reset import resolve, execute
from cyber_agent_flow_eval.runner import PreparationError
from cyber_agent_flow_eval.usage import collect, validate_pricing


def test_reset_is_frozen_and_records_failure(tmp_path):
    script = tmp_path / "reset.py"
    script.write_text("raise SystemExit(1)")
    cfg = resolve({"argv": [sys.executable, "reset.py"]}, tmp_path)
    with pytest.raises(PreparationError, match="reset failed"):
        execute(cfg, tmp_path)
    assert json.loads((tmp_path / "reset.json").read_text())["passed"] is False
    script.write_text("pass")
    with pytest.raises(PreparationError, match="script changed"):
        execute(cfg, tmp_path)


def test_reset_timeout_is_audited(tmp_path):
    script = tmp_path / "reset.py"
    script.write_text("import time\ntime.sleep(60)")
    cfg = resolve(
        {"argv": [sys.executable, "reset.py"], "timeout_seconds": 1}, tmp_path
    )
    with pytest.raises(PreparationError, match="timed out"):
        execute(cfg, tmp_path)
    assert json.loads((tmp_path / "reset.json").read_text())["timed_out"]


def test_pricing_preserves_unknown_and_uses_provider_tokens(tmp_path):
    rates = {"participant": {"input_per_million": 1, "output_per_million": 2}}
    assert collect(tmp_path, {}, rates)["participant_cost_usd"] is None
    calls = tmp_path / "model_calls"
    calls.mkdir()
    (calls / "call-000001.json").write_text(
        json.dumps(
            {
                "response": {
                    "raw": {"usage": {"prompt_tokens": 1000, "completion_tokens": 2000}}
                }
            }
        )
    )
    result = collect(tmp_path, {}, rates)
    assert (
        result["participant_usage_complete"] and result["participant_cost_usd"] == 0.005
    )
    assert result["judge_cost_usd"] is None
    (calls / "call-000002.json").write_text("unfinished response")
    assert collect(tmp_path, {}, rates)["participant_cost_usd"] is None
    with pytest.raises(ValueError):
        validate_pricing(
            {"participant": {"input_per_million": True, "output_per_million": 1}}
        )


def test_standalone_study_requires_reset_and_preserves_frozen_members(
    tmp_path, monkeypatch
):
    import yaml
    from cyber_agent_flow_eval import studies, runner
    from cyber_agent_flow_eval.storage import write_json

    experiments = [tmp_path / "one.yaml", tmp_path / "two.yaml"]
    for path in experiments:
        path.write_text("specification")
    study = tmp_path / "study.yaml"
    study.write_text(
        yaml.safe_dump(
            dict(
                version=1,
                id="heldout",
                baseline="baseline",
                experiments=[p.name for p in experiments],
            )
        )
    )
    spec = dict(
        model={"name": "fixed"},
        execution={"wall_seconds": 30},
        conditions=[{"id": "baseline", "tools": ["curl"]}],
        backend={"before_trial": []},
        tasks=[dict(id="inspect", family="configuration", split="test")],
    )
    monkeypatch.setattr(studies, "resolve", lambda path: dict(spec))
    with pytest.raises(ValueError, match="explicit reset"):
        studies.load(study)
    spec["reset"] = {"argv": ["reset"], "source_hashes": {}}
    calls = []

    def execute(path, dest, **options):
        calls.append(path.name)
        dest.mkdir()
        write_json(dest / "manifest.json", {"spec": {}})

    monkeypatch.setattr(runner, "run", execute)
    monkeypatch.setattr(
        studies,
        "summarize",
        lambda outputs, *args, **kwargs: dict(observed_trials=len(outputs)),
    )
    result = studies.run(study, tmp_path / "output", progress=lambda message: None)
    assert calls == ["one.yaml", "two.yaml"] and result["observed_trials"] == 2
    frozen = json.loads((tmp_path / "output/study.json").read_text())
    assert len(frozen["experiments"]) == 2 and frozen["experiments"][0]["spec_hash"]
    with pytest.raises(ValueError, match="already exists"):
        studies.run(study, tmp_path / "output", progress=lambda message: None)
