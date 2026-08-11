"""Report LLM token usage back to the orchestrator.

A tool server is the only process that sees an Anthropic response, so it is the
only place the token counts exist. Returning them in the tool result is what
lets the orchestrator attribute spend to the job that caused it
(``app/utils/usage.py`` on that side folds them into ``game_jobs.cost_usd``).

Servers that do no LLM work simply never call this and are accounted as zero.
"""

from __future__ import annotations

from typing import Any

__all__ = ["usage_of", "merge_usage"]


def usage_of(response: Any, model: str) -> dict[str, Any]:
    """Extract a plain, JSON-safe usage object from an Anthropic response.

    Reads defensively: a missing or renamed field must not fail the tool call
    over bookkeeping.
    """

    raw = getattr(response, "usage", None)
    return {
        "model": model,
        "input_tokens": _count(raw, "input_tokens"),
        "output_tokens": _count(raw, "output_tokens"),
        "cache_read_input_tokens": _count(raw, "cache_read_input_tokens"),
        "cache_creation_input_tokens": _count(raw, "cache_creation_input_tokens"),
    }


def merge_usage(*entries: dict[str, Any] | None) -> dict[str, Any]:
    """Sum several usage objects from the same tool call into one.

    The model name is taken from the first entry that has one; a single tool
    call does not mix models today, and reporting one name keeps the payload
    pricable on the orchestrator side.
    """

    total: dict[str, Any] = {
        "model": "",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    for entry in entries:
        if not entry:
            continue
        if not total["model"]:
            total["model"] = entry.get("model", "")
        for key in tuple(total):
            if key == "model":
                continue
            total[key] += _as_int(entry.get(key))
    return total


def _count(raw: Any, field: str) -> int:
    return _as_int(getattr(raw, field, 0))


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))
