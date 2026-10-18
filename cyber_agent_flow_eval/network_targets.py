"""Literal IP/range extraction for observed tool targets; never resolves DNS."""

import ipaddress
import json
import re

_PATTERNS = [
    re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,3})?(?!\w|\.\w|/\d)"),
    re.compile(r"(?<![\w:])(?:[0-9a-fA-F]{0,4}:){2,}[0-9a-fA-F:.]*(?:/\d{1,3})?"),
]


def normalize(value):
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError("Target must be a literal IP address or CIDR range")
    try:
        return str(
            ipaddress.ip_network(value, strict=False)
            if "/" in value
            else ipaddress.ip_address(value)
        )
    except ValueError:
        raise ValueError("Target must be a literal IP address or CIDR range") from None


def extract(value, limit=16):
    """Only execution arguments/data; callers must not pass reasoning as activity."""

    def prune(item, depth=0):
        if depth > 5:
            return ""
        if isinstance(item, dict):
            return {
                str(key): prune(val, depth + 1)
                for key, val in list(item.items())[:64]
                if not any(
                    secret in str(key).lower().replace("_", "")
                    for secret in (
                        "password",
                        "apikey",
                        "token",
                        "authorization",
                        "secret",
                    )
                )
                and str(key).lower()
                not in ("headers", "body", "prompt", "reasoning", "guidance")
            }
        if isinstance(item, list):
            return [prune(part, depth + 1) for part in item[:64]]
        return item[:16000] if isinstance(item, str) else ""

    text = json.dumps(prune(value), ensure_ascii=False)[:24000]
    result = []
    for pattern in _PATTERNS:
        for match in pattern.finditer(text):
            try:
                target = normalize(match.group(0))
            except ValueError:
                continue
            if target not in result:
                result.append(target)
            if len(result) >= limit:
                return result
    return result


def overlaps(left, right):
    try:
        return ipaddress.ip_network(left, strict=False).overlaps(
            ipaddress.ip_network(right, strict=False)
        )
    except ValueError:
        return False
