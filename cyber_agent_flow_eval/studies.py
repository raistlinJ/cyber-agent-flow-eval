"""Reproducible multi-scenario studies and paired, scenario-clustered statistics."""

from collections import defaultdict
from pathlib import Path
import json
import math
import random
import statistics
import yaml
from .storage import read_json, write_json
from .spec import StrictLoader, resolve, digest
from .reporting import results


def confidence(values, *, seed=0, draws=2000):
    if len(values) < 2 or len(set(values)) < 2:
        return None
    rng = random.Random(seed)
    estimates = sorted(
        statistics.mean(rng.choices(values, k=len(values))) for _ in range(draws)
    )
    return [
        estimates[int(draws * 0.025)],
        estimates[min(draws - 1, int(draws * 0.975))],
    ]


def compare(rows, baseline="baseline", *, seed=0):
    pairs = defaultdict(dict)
    for row in rows:
        key = (
            row.get("study_run_id", row.get("experiment_id")),
            row.get("scenario_definition_sha256", row.get("scenario_id")),
            row.get(
                "pair_id", row.get("task_id", "") + ":" + str(row.get("repetition", 0))
            ),
        )
        pairs[key][row["condition_id"]] = row
    candidates = sorted({r["condition_id"] for r in rows} - {baseline})
    comparisons = []
    for candidate in candidates:
        scenarios = defaultdict(list)
        scores = defaultdict(list)
        unassisted = defaultdict(list)
        eligible = 0
        missing = 0
        for key, pair in pairs.items():
            a, b = pair.get(baseline), pair.get(candidate)
            if (
                not a
                or not b
                or type(a.get("verified_success")) is not bool
                or type(b.get("verified_success")) is not bool
            ):
                missing += 1
                continue
            if a.get("split") != b.get("split") or a.get("family") != b.get("family"):
                raise ValueError(
                    "A paired comparison must use the same split and family"
                )
            eligible += 1
            scenarios[key[1]].append(
                int(b["verified_success"]) - int(a["verified_success"])
            )
            score = lambda r: (
                r.get("score")
                if type(r.get("score")) in (float, int) and math.isfinite(r["score"])
                else int(r["verified_success"])
            )
            scores[key[1]].append(score(b) - score(a))
            autonomous = lambda r: int(
                r["verified_success"] and r.get("assistance_level", "none") == "none"
            )
            unassisted[key[1]].append(autonomous(b) - autonomous(a))
        units = [statistics.mean(v) for v in scenarios.values()]
        comparisons.append(
            dict(
                baseline=baseline,
                condition=candidate,
                eligible_pairs=eligible,
                excluded_pairs=missing,
                independent_scenarios=len(units),
                mean_success_difference=statistics.mean(units) if units else None,
                confidence_interval_95=confidence(units, seed=seed),
                mean_score_difference=(
                    statistics.mean([statistics.mean(v) for v in scores.values()])
                    if scores
                    else None
                ),
                score_confidence_interval_95=confidence(
                    [statistics.mean(v) for v in scores.values()], seed=seed
                ),
                mean_unassisted_success_difference=(
                    statistics.mean([statistics.mean(v) for v in unassisted.values()])
                    if unassisted
                    else None
                ),
                method="equal scenario weights; percentile bootstrap over scenario clusters, 2000 draws",
                limitation=(
                    "Too few scenario clusters or no observed variation; bootstrap uncertainty is not estimable"
                    if len(units) < 2 or len(set(units)) < 2
                    else (
                        "Few scenario clusters; interval is exploratory"
                        if len(units) < 10
                        else None
                    )
                ),
            )
        )
    return comparisons


def summarize(outputs, baseline="baseline", *, seed=0, enforce_holdout=True):
    reports = []
    provenance = None
    rows = []
    family_splits = defaultdict(set)
    seen = set()
    for output in outputs:
        output = Path(output).resolve()
        if str(output) in seen:
            raise ValueError("Duplicate evaluation in study")
        seen.add(str(output))
        report = results(output)
        manifest = read_json(output / "manifest.json")
        current_provenance = {
            k: manifest.get(k)
            for k in ("source_hash", "engine_runtime", "dependencies")
        }
        if provenance is not None and current_provenance != provenance:
            raise ValueError(
                "Study evaluations used different engine/evaluator sources or dependencies; compare them in separate studies"
            )
        provenance = current_provenance
        reports.append(
            dict(
                output=str(output),
                experiment_id=report["experiment_id"],
                spec_hash=report["spec_hash"],
                planned_trials=report["planned_trials"],
                observed_trials=len(report["attempts"]),
            )
        )
        for task in manifest["spec"]["tasks"]:
            family_splits[task["family"]].add(task["split"])
        for row in report["attempts"]:
            rows.append(dict(row, study_run_id=str(output)))
    overlap = {
        key: sorted(value) for key, value in family_splits.items() if len(value) > 1
    }
    if enforce_holdout and overlap:
        raise ValueError(
            "Scenario families cross held-out splits: "
            + json.dumps(overlap, sort_keys=True)
        )
    groups = defaultdict(list)
    for row in rows:
        groups[(row["split"], row["family"])].append(row)
    planned = sum(r["planned_trials"] for r in reports)
    return dict(
        version=1,
        baseline=baseline,
        bootstrap_seed=seed,
        unit="scenario cluster; repetitions are paired within clusters",
        runs=reports,
        planned_trials=planned,
        observed_trials=len(rows),
        evaluable_trials=sum(type(r.get("verified_success")) is bool for r in rows),
        unverified_trials=sum(r.get("verified_success") is None for r in rows),
        unstarted_trials=planned - len(rows),
        human_calibration="not performed",
        family_split_overlap=overlap,
        comparisons=compare(rows, baseline, seed=seed),
        by_split={
            split: compare(
                [r for r in rows if r["split"] == split], baseline, seed=seed
            )
            for split in ("development", "validation", "test")
        },
        by_family=[
            dict(split=k[0], family=k[1], comparisons=compare(v, baseline, seed=seed))
            for k, v in sorted(groups.items())
        ],
    )


def load(path):
    path = Path(path).resolve()
    value = yaml.load(path.read_text(), Loader=StrictLoader)
    if (
        not isinstance(value, dict)
        or set(value)
        - {
            "version",
            "id",
            "experiments",
            "baseline",
            "bootstrap_seed",
            "enforce_holdout",
        }
        or value.get("version") != 1
    ):
        raise ValueError("Study requires version 1, id and experiments")
    from .spec import identifier

    identifier(value.get("id"))
    if not isinstance(value.get("experiments"), list) or not value["experiments"]:
        raise ValueError("Study needs experiment YAML paths")
    experiments = [
        (path.parent / Path(p)).resolve()
        for p in value["experiments"]
        if isinstance(p, str)
    ]
    if len(experiments) != len(value["experiments"]) or len(set(experiments)) != len(
        experiments
    ):
        raise ValueError("Invalid/duplicate experiment paths")
    if (
        type(value.get("bootstrap_seed", 0)) is not int
        or type(value.get("enforce_holdout", True)) is not bool
    ):
        raise ValueError("Invalid study controls")
    specs = [resolve(p) for p in experiments]
    families = defaultdict(set)
    common = None
    for spec in specs:
        signature = dict(
            model=spec["model"],
            judge=spec.get("judge"),
            budgets={
                k: spec["execution"].get(k)
                for k in (
                    "wall_seconds",
                    "max_turns",
                    "tool_timeout",
                    "context_window",
                    "provide_progressive_hints",
                    "max_tries_before_solution",
                    "auto_approve_dangerous",
                )
            },
            conditions=[
                {k: c.get(k) for k in ("id", "tools", "provide_progressive_hints")}
                for c in spec["conditions"]
            ],
        )
        if common is not None and signature != common:
            raise ValueError(
                "Study members must share models, judges, budgets and condition selections"
            )
        common = signature
        if not spec.get("reset") and not spec["backend"].get("before_trial"):
            raise ValueError(
                "Standalone studies require an explicit reset command or guest before_trial reset/readiness hooks"
            )
        if value.get("baseline", "baseline") not in {
            c["id"] for c in spec["conditions"]
        }:
            raise ValueError("Baseline must exist in every study member")
        for task in spec["tasks"]:
            families[task["family"]].add(task["split"])
    if value.get("enforce_holdout", True) and any(
        len(v) > 1 for v in families.values()
    ):
        raise ValueError("A family cannot cross development/validation/test splits")
    return value, experiments, specs


def run(path, output, *, resume=False, progress=print):
    from .runner import lease

    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with lease(output / ".study.lock"):
        return _execute(path, output, resume=resume, progress=progress)


def _execute(path, output, *, resume=False, progress=print):
    from .runner import run as execute

    value, experiments, specs = load(path)
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    identity = dict(
        study=value,
        experiments=[
            dict(path=str(p), spec_hash=digest(s)) for p, s in zip(experiments, specs)
        ],
    )
    record = output / "study.json"
    if record.exists():
        if not resume or read_json(record) != identity:
            raise ValueError(
                "Study changed or already exists; use resume for unchanged studies"
            )
    else:
        write_json(record, identity)
    outputs = []
    for index, experiment in enumerate(experiments):
        dest = output / f"experiment-{index+1:04d}"
        outputs.append(dest)
        progress(f"Study experiment {index+1}/{len(experiments)}: {experiment.name}")
        execute(
            experiment,
            dest,
            resume=resume and (dest / "manifest.json").exists(),
            progress=progress,
        )
    report = summarize(
        outputs,
        value.get("baseline", "baseline"),
        seed=value.get("bootstrap_seed", 0),
        enforce_holdout=value.get("enforce_holdout", True),
    )
    write_json(output / "study-summary.json", report)
    return report
