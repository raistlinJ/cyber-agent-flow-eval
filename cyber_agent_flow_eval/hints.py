"""Host-owned progressive assistance. Never upload this policy or its facts to CAF."""

import hashlib
import json
import time
from datetime import datetime, timezone
from .storage import write_json, read_json

POLICY = {
    "version": 2,
    "stalled_turns": 2,
    "max_hints": 3,
    "max_tries_before_solution": 6,
    "progress": "new declared fact in bounded tool output; otherwise new successful tool output",
}


def strings(value):
    if isinstance(value, str):
        yield value
    elif type(value) in (int, float, bool):
        yield json.dumps(value)
    elif isinstance(value, dict):
        for child in value.values():
            yield from strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from strings(child)


def resolve_policy(execution=None):
    policy = dict(POLICY)
    execution = execution or {}
    stalled = execution.get("hint_stalled_turns", policy["stalled_turns"])
    if type(stalled) is not int or not 1 <= stalled <= 1000:
        raise ValueError("hint_stalled_turns must be an integer from 1 to 1000")
    max_turns = execution.get("max_turns")
    if execution.get("provide_progressive_hints") and type(max_turns) is int and stalled >= max_turns:
        raise ValueError("hint_stalled_turns must be less than max_turns when progressive hints are enabled")
    policy["stalled_turns"] = stalled
    tries = (execution or {}).get(
        "max_tries_before_solution", policy["max_tries_before_solution"]
    )
    if type(tries) is not int or not 1 <= tries <= 1000:
        raise ValueError("max_tries_before_solution must be an integer from 1 to 1000")
    policy["max_tries_before_solution"] = tries
    return policy


class HintController:
    def __init__(
        self,
        directory,
        metadata,
        verifier,
        execution=None,
        *,
        task=None,
        judge=None,
        progress=None,
    ):
        self.directory, self.verifier = directory, verifier
        self.policy = resolve_policy(execution)
        self.last_observed_progress = 0
        self.tries_without_progress = 0
        self.started = time.monotonic()
        self.facts = metadata.get("discoverable_facts", [])
        self.known = {f["id"] for f in metadata.get("starting_facts", [])}
        self.observed, self.revealed, self.outputs = set(), set(), set()
        self.last_progress = 0
        self.response = None
        self.events = []
        self.feedback = []
        self.hints = []
        self.solutions = []
        self.completed_challenges = set()
        self.completed_steps = set()
        self.plan = metadata.get("challenge_plan") or (task or {}).get("challenge_plan")
        self.monitor = None
        self.monitor_error = None
        answers = (
            [
                c["private_reference"]
                for c in verifier["expected"]["criteria"]
                if c.get("private_reference")
            ]
            if verifier["type"] == "rubric"
            else list(strings(verifier.get("expected")))
        )

        def safe(text):
            return not any(answer and answer in text for answer in answers)

        scoped = bool(self.plan and any(s.get("hints") for s in self.plan["steps"]))
        for index, text in enumerate(
            [] if scoped else metadata.get("progressive_hints", [])
        ):
            if not isinstance(text, str) or not text.strip() or len(text) > 1500:
                raise ValueError(
                    "progressive_hints must contain nonempty strings up to 1500 characters"
                )
            if not safe(text):
                raise ValueError("A progressive hint contains a verifier answer")
            self.hints.append(
                dict(id=f"authored-{index + 1}", text=text, source="scenario_task")
            )
        for fact in self.facts:
            requirements = set(fact.get("requires", [])) | set(
                metadata.get("objective_requires", {}).get(fact.get("source_node"), [])
            )
            # A fact never requires itself to be revealed.
            requirements.discard(fact["id"])
            pointer = f"Look for the {fact['artifact']} needed for the objective."
            evidence = (
                f"For {fact['artifact']}, inspect this evidence: {fact['evidence']}"
            )
            reveal = f"Required fact supplied by the evaluator: {fact['artifact']} = {fact['value']}"
            for stage, text in enumerate((pointer, evidence, reveal), 1):
                if safe(text):
                    self.hints.append(
                        dict(
                            id=f"{fact['id']}-{stage}",
                            text=text,
                            source="scenario_fact",
                            fact_id=fact["id"],
                            stage=stage,
                            requires=sorted(requirements),
                            final_fact=stage == 3 or fact["value"] in text,
                            reveals_fact=stage == 3 or fact["value"] in text,
                        )
                    )
        if self.plan:
            for step in self.plan["steps"]:
                for index, text in enumerate(step.get("hints", [])):
                    if not safe(text):
                        raise ValueError("A challenge hint contains a verifier answer")
                    self.hints.append(
                        dict(
                            id=step["id"] + "-hint-" + str(index + 1),
                            text=text,
                            source="scenario_guide",
                            step_id=step["id"],
                            node_id=step["node_id"],
                        )
                    )
        solutions = metadata.get("challenge_solutions", [])
        if not isinstance(solutions, list):
            raise ValueError("challenge_solutions must be a list")
        for item in solutions:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("node_id"), str)
                or not item["node_id"]
                or not isinstance(item.get("text"), str)
                or not item["text"].strip()
                or len(item["text"]) > 32000
                or not isinstance(item.get("completion_values", []), list)
                or any(
                    not isinstance(value, str) or not value
                    for value in item.get("completion_values", [])
                )
            ):
                raise ValueError("Invalid challenge solution")
            if any(
                existing["node_id"] == item["node_id"] for existing in self.solutions
            ):
                raise ValueError("Duplicate challenge solution node")
            self.solutions.append(
                dict(
                    item,
                    id="solution-" + item["node_id"],
                    source="scenario_solution",
                    solution=True,
                )
            )
        # Older packages and the samples can still provide the reviewed task
        # procedure and exact verifier answer when no facilitator section exists.
        if (
            not self.solutions
            and self.hints
            and metadata.get("progressive_hints")
            and verifier["type"] != "rubric"
        ):
            walkthrough = metadata.get("task_prompt") or "\n".join(
                metadata["progressive_hints"]
            )
            self.solutions.append(
                dict(
                    id="solution-task",
                    node_id="task",
                    source="scenario_solution",
                    solution=True,
                    completion_values=[],
                    text="Solution walkthrough:\n"
                    + walkthrough
                    + "\nExact answer / flag to submit:\n"
                    + json.dumps(verifier["expected"], ensure_ascii=False),
                )
            )
        if self.plan:
            for step in self.plan["steps"]:
                if step.get("solution") and not any(
                    s["node_id"] == step["node_id"] for s in self.solutions
                ):
                    self.solutions.append(
                        dict(
                            id="solution-" + step["node_id"],
                            node_id=step["node_id"],
                            step_id=step["id"],
                            source="scenario_solution",
                            solution=True,
                            completion_values=[],
                            text=step["solution"],
                        )
                    )
        self.available = bool(self.hints or self.solutions)
        if (
            self.available
            and task
            and task.get("rubric")
            and judge
            and judge.get("enabled")
            and judge.get("monitor_progress", True)
        ):
            from .progress_monitor import ProgressMonitor

            self.monitor = ProgressMonitor(directory, task, judge, progress)
            self.policy["progress"] = "new satisfied rubric criterion supported by checkpoint execution evidence"
            self.policy["hint_scope"] = "first eligible unfinished challenge; max_hints applies per step when a plan exists"
        self.unavailable_reason = (
            None
            if self.available
            else "Task has no usable progressive_hints or discoverable_facts; running unassisted"
        )
        self.save()

    def save(self):
        write_json(
            self.directory / "assistance.json",
            dict(
                policy=self.policy,
                progress_monitor_enabled=self.monitor is not None,
                progress_monitor_error=self.monitor_error,
                completed_steps=sorted(self.completed_steps),
                tries_without_progress=self.tries_without_progress,
                available=self.available,
                unavailable_reason=self.unavailable_reason,
                events=self.events,
                retry_feedback=self.feedback,
                completed_challenges=sorted(self.completed_challenges),
                observed_fact_ids=sorted(self.observed),
                revealed_fact_ids=sorted(self.revealed),
            ),
        )

    def respond(self, request, timeout_seconds=None):
        if not isinstance(request, dict):
            raise ValueError("Invalid hint observation")
        turn = request.get("turn")
        if type(turn) is not int or turn < 1 or request.get("sequence") != turn:
            raise ValueError("Invalid hint sequence")
        if self.response and turn <= self.response["sequence"]:
            return self.response
        results = request.get("results", [])
        if (
            not isinstance(results, list)
            or len(results) > 2
            or any(not isinstance(r, str) or len(r) > 1200 for r in results)
        ):
            raise ValueError("Invalid hint evidence")
        monitored = None
        if self.monitor:
            monitored = self.monitor.check(request, timeout_seconds)
            if monitored is None:
                self.monitor_error = self.monitor.checks[-1].get("error")
                self.response = dict(sequence=turn, hint=None)
                self.save()
                return self.response
            self.monitor_error = None
            self.completed_steps = monitored["completed_steps"]
            if self.plan:
                self.completed_challenges = {
                    s["node_id"]
                    for s in self.plan["steps"]
                    if s["id"] in self.completed_steps
                }
            new_progress = monitored["new_progress"]
        else:
            new_progress = False
            for fact in self.facts:
                if fact["id"] not in self.known and any(
                    fact["value"] in result for result in results
                ):
                    self.known.add(fact["id"])
                    self.observed.add(fact["id"])
                    new_progress = True
            if not self.facts:
                for result in results:
                    digest = hashlib.sha256(result.encode()).hexdigest()
                    if result.strip() and digest not in self.outputs:
                        new_progress = True
                    self.outputs.add(digest)
            observed = "\n".join(results)
            for solution in self.solutions:
                values = solution.get("completion_values", [])
                if (
                    values
                    and solution["node_id"] not in self.completed_challenges
                    and all(value in observed for value in values)
                ):
                    self.completed_challenges.add(solution["node_id"])
                    new_progress = True
        if monitored and monitored["all_satisfied"]:
            final = request.get("final_answer")
            from .runner import verify

            hint = None
            if (
                final is not None
                and not request.get("final_truncated")
                and self.monitor.task.get("verification_mode") != "judge"
                and self.verifier["type"] != "rubric"
                and not verify(final, self.verifier)["passed"]
            ):
                hint = "The challenge actions are complete. Submit the observed values in the required final answer format, without extra fields or commentary."
                self.feedback.append(
                    dict(
                        turn=turn,
                        at=datetime.now(timezone.utc).isoformat(),
                        reason="incorrect_final_format_retry",
                        tries_without_progress=0,
                    )
                )
            self.response = dict(sequence=turn, hint=hint)
            self.save()
            return self.response
        if new_progress:
            self.last_progress = turn
            self.last_observed_progress = turn
        self.tries_without_progress = turn - self.last_observed_progress
        final = request.get("final_answer")
        failed_final = False
        if final is not None:
            from .runner import verify

            # Do not interrupt a correct final answer or judge truncated answers.
            if (monitored and monitored["all_satisfied"]) or (
                not monitored
                and (
                    self.verifier["type"] == "rubric"
                    or request.get("final_truncated")
                    or verify(final, self.verifier)["passed"]
                )
            ):
                self.response = dict(sequence=turn, hint=None)
                self.save()
                return self.response
            failed_final = True
        hint = None
        solution_due = (
            self.tries_without_progress >= self.policy["max_tries_before_solution"]
        )
        used = {e["id"] for e in self.events}
        current_step = (
            next(
                (
                    s
                    for s in self.plan["steps"]
                    if s["id"] not in self.completed_steps
                    and set(s["requires"]) <= self.completed_steps
                ),
                None,
            )
            if self.plan
            else None
        )
        current = next(
            (
                item
                for item in self.solutions
                if item["node_id"] not in self.completed_challenges
                and (
                    not self.plan
                    or current_step
                    and item["node_id"] == current_step["node_id"]
                )
            ),
            None,
        )
        candidate = (
            current if solution_due and current and current["id"] not in used else None
        )
        hint_count = sum(
            not event.get("solution")
            and (
                not self.plan
                or current_step
                and event.get("step_id") == current_step["id"]
            )
            for event in self.events
        )
        if (
            candidate is None
            and hint_count < self.policy["max_hints"]
            and (
                failed_final
                or turn - self.last_progress >= self.policy["stalled_turns"]
            )
        ):
            eligible = [
                item
                for item in self.hints
                if item["id"] not in used
                and item.get("fact_id") not in self.known
                and (
                    not self.plan
                    or current_step
                    and item.get("step_id") in (None, current_step["id"])
                )
                and set(item.get("requires", [])) <= self.known
                and (solution_due or not item.get("final_fact"))
            ]
            # For older fact-only plans, preserve a release for the final fact.
            if (
                not solution_due
                and not self.solutions
                and any(item.get("final_fact") for item in self.hints)
                and hint_count >= self.policy["max_hints"] - 1
            ):
                eligible = []
            candidate = (
                next((item for item in eligible if item.get("final_fact")), None)
                if solution_due
                else None
            )
            candidate = candidate or next(iter(eligible), None)
        if candidate:
            hint = candidate["text"]
            event = dict(
                candidate,
                turn=turn,
                at=datetime.now(timezone.utc).isoformat(),
                elapsed_seconds=time.monotonic() - self.started,
                tries_without_progress=self.tries_without_progress,
                reason=(
                    "max_tries_before_solution"
                    if candidate.get("solution")
                    or solution_due
                    and candidate.get("final_fact")
                    else (
                        "incorrect_final_answer"
                        if failed_final
                        else "no_observed_progress"
                    )
                ),
            )
            if current_step:
                event.update(
                    step_id=current_step["id"], node_id=current_step["node_id"]
                )
            self.events.append(event)
            if candidate.get("reveals_fact"):
                self.known.add(candidate["fact_id"])
                self.revealed.add(candidate["fact_id"])
            if candidate.get("solution"):
                for fact in self.facts:
                    if fact["value"] in hint and fact["id"] not in self.known:
                        self.known.add(fact["id"])
                        self.revealed.add(fact["id"])
            self.last_progress = turn
        if not hint and failed_final and not solution_due and self.available:
            # A bad final answer normally ends CAF's loop when the callback
            # returns None. Ask it to retry so it can reach the configured limit,
            # without disclosing a hint early or consuming a hint release.
            hint = "The task is not complete. Try again using the observed evidence and permitted tools."
            self.feedback.append(
                dict(
                    turn=turn,
                    at=datetime.now(timezone.utc).isoformat(),
                    reason="incorrect_final_answer_retry",
                    tries_without_progress=self.tries_without_progress,
                )
            )
        self.response = dict(sequence=turn, hint=hint)
        self.save()
        return self.response

    def poll(self, timeout_seconds=None):
        path = self.directory / "hint-request.json"
        if path.exists():
            write_json(
                self.directory / "hint-response.json",
                self.respond(read_json(path), timeout_seconds),
            )


def metrics(directory, enabled, success):
    path = directory / "assistance.json"
    audit = read_json(path) if path.exists() else {}
    events = audit.get("events", [])
    feedback = audit.get("retry_feedback", [])
    monitor_path = directory / "progress-monitor.json"
    monitor = read_json(monitor_path) if monitor_path.exists() else {}
    checks = monitor.get("checks", [])
    usages = []
    for check in checks:
        judge_path = directory / check.get("audit", "")
        if judge_path.is_file():
            usages.extend(
                call.get("usage", {}) for call in read_json(judge_path).get("calls", [])
            )

    def tokens(key):
        values = [u.get(key) for u in usages]
        return sum(values) if values and all(type(v) is int for v in values) else None

    return dict(
        progress_monitor_enabled=audit.get("progress_monitor_enabled", False),
        progress_monitor_checks=len(checks),
        progress_monitor_errors=sum(c["status"] == "error" for c in checks),
        progress_monitor_seconds=sum(c.get("seconds", 0) for c in checks),
        progress_monitor_calls=len(usages),
        progress_monitor_prompt_tokens=tokens("prompt_tokens"),
        progress_monitor_output_tokens=tokens("output_tokens"),
        progress_monitor=monitor,
        provide_progressive_hints=enabled,
        progressive_hints_available=(
            audit.get("available", True if events else None) if enabled else False
        ),
        progressive_hints_reason=audit.get("unavailable_reason") if enabled else None,
        hints_released=sum(not event.get("solution") for event in events),
        facts_revealed=len(audit.get("revealed_fact_ids", [])),
        max_tries_before_solution=(
            audit.get("policy", {}).get("max_tries_before_solution")
            if enabled
            else None
        ),
        hint_stalled_turns=(
            audit.get("policy", {}).get("stalled_turns") if enabled else None
        ),
        solutions_released=sum(bool(event.get("solution")) for event in events),
        retries_requested=len(feedback),
        solution_provided=any(event.get("solution") for event in events),
        assistance=audit,
        solution_assisted_success=success is True
        and any(event.get("solution") for event in events),
        hints_assisted_success=success is True
        and bool(events or feedback)
        and not any(event.get("solution") for event in events),
        assisted_success=success is True and bool(events or feedback),
        unassisted_success=success is True and not (events or feedback),
    )
