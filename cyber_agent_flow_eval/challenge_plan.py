"""Private, versioned step-to-rubric mapping; stdlib-only and mirrored in evaluator."""

from copy import deepcopy
import json


def validate_plan(value, rubric):
    if (
        not isinstance(value, dict)
        or set(value) - {"version", "steps", "source"}
        or type(value.get("version")) is not int
        or value["version"] != 1
    ):
        raise ValueError("Challenge plan requires version 1 and steps")
    steps = value.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 32:
        raise ValueError("Challenge plan requires 1–32 steps")
    if not isinstance(rubric, dict) or not isinstance(rubric.get("criteria"), list):
        raise ValueError("Challenge plan requires a challenge rubric")
    criteria = {c["id"] for c in rubric["criteria"]}
    ids, mapped = set(), set()
    result = deepcopy(value)
    for step in result["steps"]:
        if not isinstance(step, dict) or set(step) - {
            "id",
            "node_id",
            "title",
            "criterion_ids",
            "requires",
            "hints",
            "solution",
        }:
            raise ValueError("Unknown challenge step fields")
        for key in ("id", "node_id", "title"):
            if not isinstance(step.get(key), str) or not 1 <= len(step[key]) <= 200:
                raise ValueError("Challenge step requires id, node_id and title")
        if step["id"] in ids:
            raise ValueError("Duplicate challenge step ID")
        ids.add(step["id"])
        refs = step.get("criterion_ids")
        if (
            not isinstance(refs, list)
            or not refs
            or any(
                not isinstance(c, str) or c not in criteria or c in mapped for c in refs
            )
            or len(set(refs)) != len(refs)
        ):
            raise ValueError(
                "Challenge criterion IDs must exist in the rubric and belong to one step"
            )
        mapped.update(refs)
        for key in ("requires", "hints"):
            step.setdefault(key, [])
            if not isinstance(step[key], list) or any(
                not isinstance(v, str) or not v.strip() for v in step[key]
            ):
                raise ValueError("Challenge requires and hints must be string lists")
        if len(step["hints"]) > 16 or any(len(h) > 1500 for h in step["hints"]):
            raise ValueError("Challenge hints exceed the hint limits")
        if (
            not isinstance(step.get("solution", ""), str)
            or len(step.get("solution", "")) > 16000
        ):
            raise ValueError("Challenge solution must be text up to 16000 characters")
    if mapped != criteria:
        raise ValueError("Challenge plan must map every rubric criterion")
    completed = set()
    for step in result["steps"]:
        if len(set(step["requires"])) != len(step["requires"]) or any(
            ref not in completed for ref in step["requires"]
        ):
            raise ValueError(
                "Challenge prerequisites must reference earlier steps; cycles are not allowed"
            )
        completed.add(step["id"])
    if "source" in result and (
        not isinstance(result["source"], dict)
        or len(json.dumps(result["source"])) > 4000
    ):
        raise ValueError("Challenge source must be a bounded provenance object")
    if len(json.dumps(result).encode()) > 65536:
        raise ValueError("Challenge plan exceeds 64 KiB")
    return result
