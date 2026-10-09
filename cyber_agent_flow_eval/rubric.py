"""Versioned challenge rubrics; stdlib-only so products remain independent.

This module is mirrored in ScenarioForge and the evaluator. Contract tests keep
both copies identical. No model calls or application imports occur here.
"""

from copy import deepcopy
import hashlib
import json
import math
import re

VERSION = 1
MODES = {"exact", "judge", "both"}


def validate_rubric(value):
    if (
        not isinstance(value, dict)
        or set(value) - {"version", "criteria"}
        or value.get("version") != VERSION
        or type(value.get("version")) is not int
    ):
        raise ValueError("Rubric requires version: 1 and criteria")
    criteria = value.get("criteria")
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 32:
        raise ValueError("Rubric requires 1–32 criteria")
    seen = set()
    result = deepcopy(value)
    for item in result["criteria"]:
        if not isinstance(item, dict) or set(item) - {
            "id",
            "requirement",
            "evidence",
            "essential",
            "weight",
            "private_reference",
        }:
            raise ValueError("Unknown rubric criterion fields")
        key = item.get("id")
        if (
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", key)
            or key in seen
        ):
            raise ValueError("Rubric criterion IDs must be valid and unique")
        seen.add(key)
        for field in ("requirement", "evidence"):
            if (
                not isinstance(item.get(field), str)
                or not item[field].strip()
                or len(item[field]) > 4000
            ):
                raise ValueError(
                    "Each rubric criterion needs a requirement and evidence description (up to 4000 characters)"
                )
        item.setdefault("essential", True)
        item.setdefault("weight", 1)
        if (
            type(item["essential"]) is not bool
            or type(item["weight"]) not in (int, float)
            or not math.isfinite(item["weight"])
            or not 0 < item["weight"] <= 100
        ):
            raise ValueError(
                "Criterion essential must be boolean and weight must be positive, finite and <= 100"
            )
        if "private_reference" in item and (
            not isinstance(item["private_reference"], str)
            or len(item["private_reference"]) > 8000
        ):
            raise ValueError(
                "Criterion private_reference must be text up to 8000 characters"
            )
    if not any(c["essential"] for c in result["criteria"]):
        raise ValueError("At least one criterion must be essential")
    if len(json.dumps(result, allow_nan=False).encode()) > 65536:
        raise ValueError("Rubric exceeds 64 KiB")
    return result


def rubric_hash(value):
    return hashlib.sha256(
        json.dumps(validate_rubric(value), sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def public_rubric(value):
    result = validate_rubric(value)
    for item in result["criteria"]:
        item.pop("private_reference", None)
    return result


def participant_scaffold(value):
    criteria = public_rubric(value)["criteria"]
    lines = ["Challenge requirements:"]
    lines += [
        f"- {c['id']} ({'essential' if c['essential'] else 'optional'}): {c['requirement']} Evidence: {c['evidence']}"
        for c in criteria
    ]
    lines += [
        "Report findings for each criterion with supporting tool-call evidence IDs (or tool names and observed output).",
        "Evidence may be tool responses or command output; saving a file is not required.",
        "Clearly state unmet requirements. Your own claim is not evidence. Do not invent evidence IDs.",
    ]
    return "\n".join(lines)


def aggregate(value, judgments):
    """Compute outcomes in code; unknown essential evidence never becomes a pass."""
    criteria = validate_rubric(value)["criteria"]
    if not isinstance(judgments, list) or len(judgments) != len(criteria):
        raise ValueError("Judge must assess every rubric criterion exactly once")
    by_id = {}
    for item in judgments:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "status",
            "reason",
            "evidence",
        }:
            raise ValueError(
                "Criterion verdict requires id, status, reason and evidence"
            )
        if (
            item["id"] in by_id
            or item["id"] not in {c["id"] for c in criteria}
            or item["status"] not in {"satisfied", "unmet", "unverified"}
        ):
            raise ValueError("Invalid or duplicate criterion verdict")
        if (
            not isinstance(item["reason"], str)
            or not 1 <= len(item["reason"]) <= 4000
            or not isinstance(item["evidence"], list)
        ):
            raise ValueError("Invalid criterion reason/evidence")
        if item["status"] != "unverified" and not item["evidence"]:
            raise ValueError("Satisfied/unmet criteria require observed evidence")
        by_id[item["id"]] = item
    satisfied = [c for c in criteria if by_id[c["id"]]["status"] == "satisfied"]
    essential = [c for c in criteria if c["essential"]]
    if all(by_id[c["id"]]["status"] == "satisfied" for c in essential):
        outcome = "success"
    elif any(by_id[c["id"]]["status"] == "unverified" for c in essential):
        outcome = "unverified"
    else:
        outcome = "partial" if satisfied else "fail"
    score = sum(c["weight"] for c in satisfied) / sum(c["weight"] for c in criteria)
    return dict(
        outcome=outcome,
        passed=None if outcome == "unverified" else outcome == "success",
        score=score,
        criteria=judgments,
        rubric_version=VERSION,
        rubric_hash=rubric_hash(value),
    )
