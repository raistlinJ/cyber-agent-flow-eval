"""Bounded, cited monitor observations; never affect final scoring."""

from copy import deepcopy
from .network_targets import normalize, extract


def validate_observations(values, task, evidence):
    steps = {step["id"] for step in (task.get("challenge_plan") or {}).get("steps", [])}
    if not isinstance(values, list) or len(values) > 32:
        raise ValueError("Monitor observations must be a list of at most 32 items")
    result, seen = [], set()
    for raw in values:
        required = {
            "step_id",
            "activity",
            "summary",
            "discoveries",
            "evidence",
        }
        if (
            not isinstance(raw, dict)
            or not required <= set(raw)
            or set(raw) - required - {"targets"}
        ):
            raise ValueError(
                "Monitor observation requires step_id, activity, summary, discoveries and evidence"
            )
        item = deepcopy(raw)
        if item["step_id"] not in steps or item["step_id"] in seen:
            raise ValueError(
                "Monitor observations require distinct known challenge steps"
            )
        seen.add(item["step_id"])
        if item["activity"] not in {"targeted", "discovered"}:
            raise ValueError("Monitor activity must be targeted or discovered")
        if (
            not isinstance(item["summary"], str)
            or not 1 <= len(item["summary"]) <= 1000
        ):
            raise ValueError("Monitor summaries must contain 1–1000 characters")
        discoveries = item["discoveries"]
        if (
            not isinstance(discoveries, list)
            or len(discoveries) > 8
            or any(
                not isinstance(text, str) or not 1 <= len(text) <= 500
                for text in discoveries
            )
        ):
            raise ValueError(
                "Monitor discoveries must contain at most 8 bounded descriptions"
            )
        if item["activity"] == "discovered" and not discoveries:
            raise ValueError("Discovered activity requires an observed discovery")
        citations = item["evidence"]
        if not isinstance(citations, list) or not 1 <= len(citations) <= 8:
            raise ValueError("Monitor observations require direct execution citations")
        direct = False
        observed_targets = set()
        for citation in citations:
            if not isinstance(citation, dict) or set(citation) != {
                "file",
                "offset",
                "limit",
            }:
                raise ValueError("Monitor citations require file, offset and limit")
            name, offset, limit = (
                citation["file"],
                citation["offset"],
                citation["limit"],
            )
            if (
                not isinstance(name, str)
                or name not in evidence.files
                or type(offset) is not int
                or type(limit) is not int
                or offset < 0
                or limit <= 0
            ):
                raise ValueError(
                    "Monitor citations must reference evidence actually read"
                )
            end = min(offset + limit, evidence.files[name].stat().st_size)
            if end <= offset or not any(
                start <= offset and end <= stop
                for start, stop in evidence.ranges.get(name, [])
            ):
                raise ValueError(
                    "Monitor citations must reference evidence actually read"
                )
            citation["limit"] = end - offset
            is_direct = evidence.direct_output(name, offset, end - offset)
            direct |= is_direct
            if is_direct:
                with evidence.files[name].open("rb") as stream:
                    stream.seek(offset)
                    observed_targets.update(
                        extract(
                            stream.read(end - offset).decode("utf-8", errors="replace"),
                            limit=128,
                        )
                    )
        if not direct:
            raise ValueError(
                "Monitor observations require direct execution evidence, not reference material or agent claims"
            )
        targets = item.get("targets", [])
        if not isinstance(targets, list) or len(targets) > 16:
            raise ValueError(
                "Monitor targets must contain at most 16 IP addresses or CIDR ranges"
            )
        item["targets"] = list(dict.fromkeys(normalize(value) for value in targets))
        if not set(item["targets"]) <= observed_targets:
            raise ValueError("Monitor targets must appear in cited execution evidence")
        result.append(item)
    return result
