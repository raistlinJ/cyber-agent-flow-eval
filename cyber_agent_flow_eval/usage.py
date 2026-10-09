"""Provider-reported usage and explicitly priced costs; unknown is never zero."""

from pathlib import Path
import math
from .storage import read_json


def validate_pricing(value):
    if not isinstance(value, dict) or set(value) - {
        "participant",
        "judge",
        "generation_cost_usd",
    }:
        raise ValueError("Pricing supports participant, judge and generation_cost_usd")
    for key, rates in value.items():
        values = (
            [rates]
            if key == "generation_cost_usd"
            else (
                list(rates.values())
                if isinstance(rates, dict)
                and set(rates) == {"input_per_million", "output_per_million"}
                else [None]
            )
        )
        if any(
            type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values
        ):
            raise ValueError("Prices must be finite nonnegative USD values")
    return value


def cost(tokens_in, tokens_out, rates):
    if tokens_in is None or tokens_out is None or rates is None:
        return None
    return (
        tokens_in * rates["input_per_million"]
        + tokens_out * rates["output_per_million"]
    ) / 1_000_000


def collect(directory, row, pricing):
    directory = Path(directory)
    root = (
        directory / "guest-output"
        if (directory / "guest-output").is_dir()
        else directory
    )
    calls = sorted((root / "model_calls").glob("call-*.json"))
    inputs = []
    outputs = []
    for path in calls:
        try:
            response = read_json(path).get("response", {})
        except (ValueError, OSError, AttributeError):
            response = {}
        if not isinstance(response, dict):
            response = {}
        raw = response.get("raw", response)
        if not isinstance(raw, dict):
            raw = {}
        usage = raw.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        a = usage.get(
            "prompt_tokens", usage.get("input_tokens", raw.get("prompt_eval_count"))
        )
        b = usage.get(
            "completion_tokens", usage.get("output_tokens", raw.get("eval_count"))
        )
        inputs.append(a if type(a) is int and a >= 0 else None)
        outputs.append(b if type(b) is int and b >= 0 else None)
    complete = bool(calls) and all(v is not None for v in inputs + outputs)
    tokens_in = sum(inputs) if complete else None
    tokens_out = sum(outputs) if complete else None
    return dict(
        participant_prompt_tokens=tokens_in,
        participant_output_tokens=tokens_out,
        participant_usage_complete=complete,
        participant_cost_usd=cost(tokens_in, tokens_out, pricing.get("participant")),
        judge_cost_usd=cost(
            row.get("judge_prompt_tokens"),
            row.get("judge_output_tokens"),
            pricing.get("judge"),
        ),
        usage_scope="recorded CAF model calls; nested tool/provider calls may be unavailable",
    )
