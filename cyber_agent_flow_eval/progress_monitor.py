"""Separate host-side checkpoint judge; never supplies a final trial verdict."""

from copy import deepcopy
from pathlib import Path
import time

from .judge import JudgeError, judge_trial, Evidence
from .storage import write_json

MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024


def snapshot_local(source, destination):
    """Freeze only collected CAF evidence; no user-selected paths or commands."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    evidence = Evidence(source)
    total = 0
    for name, path in evidence.files.items():
        if name in {"worker-result.json", "assistance.json"} or name.startswith(
            "model_calls/"
        ):
            continue
        with path.open("rb") as stream:
            data = stream.read(MAX_SNAPSHOT_BYTES - total + 1)
        total += len(data)
        if total > MAX_SNAPSHOT_BYTES:
            raise JudgeError("Progress evidence exceeds the bounded snapshot limit")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return destination


class ProgressMonitor:
    def __init__(self, directory, task, config, progress=None):
        self.directory, self.task, self.config = Path(directory), task, deepcopy(config)
        self.progress = progress
        self.criteria = {}
        self.checks = []
        self.snapshot = lambda destination: snapshot_local(self.directory, destination)
        self.completed = set()
        self.plan = task.get("challenge_plan")

    def check(self, request, timeout_seconds=None):
        turn = request["turn"]
        check = dict(turn=turn, status="running")
        self.checks.append(check)
        started = time.monotonic()
        folder = self.directory / "progress-checks" / f"turn-{turn:04d}"
        check["audit"] = str(folder.relative_to(self.directory) / "judge.json")
        self.save()
        try:
            if len(self.checks) > self.config["progress_max_checks"]:
                raise JudgeError(
                    "Progress-check limit reached; no further automated assistance"
                )
            timeout = min(
                self.config["progress_timeout_seconds"],
                (
                    timeout_seconds
                    if timeout_seconds is not None
                    else self.config["progress_timeout_seconds"]
                ),
            )
            if timeout < 1:
                raise JudgeError(
                    "Insufficient remaining trial budget for a progress check"
                )
            folder.parent.mkdir(exist_ok=True)
            source = self.snapshot(folder)
            references = self.directory / "reference-material.json"
            if (
                references.is_file()
                and not (source / "reference-material.json").exists()
            ):
                import shutil

                shutil.copyfile(references, source / "reference-material.json")
            if not Evidence(source).trace_files:
                raise JudgeError(
                    "No execution transcript available for intermediate verification"
                )
            remaining = timeout - (time.monotonic() - started)
            if remaining < 1:
                raise JudgeError(
                    "Progress evidence transfer exhausted the checkpoint budget"
                )
            from .evidence_manifest import capture

            capture(source)
            config = dict(self.config, timeout_seconds=remaining)
            if self.progress:
                self.progress(f"turn {turn}: checking intermediate challenge evidence")
            verdict = judge_trial(
                config,
                source,
                self.task,
                request.get("final_answer"),
                None,
                progress=self.progress,
                purpose="progress",
            )
            new = False
            for finding in verdict["criteria"]:
                previous = self.criteria.get(finding["id"])
                # Reached milestones are cumulative, with the original citation
                # retained. Final scoring independently checks the complete trace.
                if not previous or previous["status"] != "satisfied":
                    self.criteria[finding["id"]] = dict(finding, audit=check["audit"])
                    new |= finding["status"] == "satisfied"
            essential = {
                c["id"]
                for c in self.task["rubric"]["criteria"]
                if c.get("essential", True)
            }
            satisfied = {
                key
                for key, finding in self.criteria.items()
                if finding["status"] == "satisfied"
            }
            if self.plan:
                for step in self.plan["steps"]:
                    required = essential.intersection(step["criterion_ids"]) or set(
                        step["criterion_ids"]
                    )
                    if required <= satisfied:
                        self.completed.add(step["id"])
            check.update(
                status="completed",
                criteria=verdict["criteria"],
                newly_satisfied=new,
                completed_steps=sorted(self.completed),
                all_satisfied=essential <= satisfied,
            )
            if self.progress:
                self.progress(
                    f'turn {turn}: {len(satisfied)} / {len(self.task["rubric"]["criteria"])} criteria observed complete'
                )
            return dict(
                new_progress=new,
                all_satisfied=essential <= satisfied,
                completed_steps=set(self.completed),
            )
        except (JudgeError, OSError, ValueError) as exc:
            check.update(status="error", error=str(exc))
            if self.progress:
                self.progress(f"turn {turn}: intermediate status unverified — {exc}")
            return None
        finally:
            check["seconds"] = time.monotonic() - started
            self.save()

    def save(self):
        steps = []
        for step in (self.plan or {}).get("steps", []):
            states = [
                self.criteria.get(c, {}).get("status", "unverified")
                for c in step["criterion_ids"]
            ]
            status = (
                "completed"
                if step["id"] in self.completed
                else (
                    "partial"
                    if "satisfied" in states
                    else "unmet" if all(s == "unmet" for s in states) else "unverified"
                )
            )
            steps.append(
                dict(
                    id=step["id"],
                    title=step["title"],
                    status=status,
                    eligible=set(step["requires"]) <= self.completed,
                    criterion_ids=step["criterion_ids"],
                )
            )
        write_json(
            self.directory / "progress-monitor.json",
            dict(
                version=1,
                role="progress-only; final judge scores independently",
                checks=self.checks,
                steps=steps,
                planned_criteria=len(self.task["rubric"]["criteria"]),
                criteria=self.criteria,
                completed_steps=sorted(self.completed),
                calibration="not human calibrated",
            ),
        )
