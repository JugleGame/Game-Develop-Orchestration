"""Per-job LLM usage and cost accounting, plus PixelLab's image quota.

The orchestrator never calls Anthropic itself — the MCP tool servers do. What it
*does* own is the single point every tool response passes through
(``BaseToolClient.call_tool``), so usage is harvested there and attributed to the
job that is running.

Attribution uses a :class:`~contextvars.ContextVar` rather than threading a
counter through ``GraphState``: each job runs in its own asyncio task
(``GameService._launch``), and a context variable set at the top of that task is
naturally isolated from every other job without any node having to know it
exists. Nodes stay pure ``state -> dict`` functions, which is the rule
``Doc/설계/CLAUDE.md`` puts on LangGraph nodes.

A tool server opts in simply by returning a ``usage`` object in its result;
servers that do no LLM work return nothing and are accounted as zero. The
asset server is the one exception in unit: PixelLab bills images against a
monthly quota rather than tokens, so it reports ``imagesGenerated`` and that
is counted separately from ``cost_usd``.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from typing import Any

# USD per million tokens, (input, output). Cache reads bill at 0.1x input and
# 5-minute cache writes at 1.25x input, applied below.
#
# Every model a tool server can select must appear here. The selection lives in
# three other processes (STRATEGIC_MODEL / QA_MODEL / UNITY_CODEGEN_MODEL) and
# nothing links them at runtime — an absent model is silently billed as $0 via
# ``unpriced_models``. ``tests/unit/test_pricing_coverage.py`` is that link.
_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.00, 50.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    # Introductory pricing, in effect through 2026-08-31; the list price is
    # (3.00, 15.00). Billing the list price here overstates QA spend by 50%,
    # so the switch-over date is recorded rather than left to memory.
    "claude-sonnet-5": (2.00, 10.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

# Models whose price changes on a known date. Surfaced as a test failure rather
# than a silent mispricing once the date passes.
_SCHEDULED_PRICE_CHANGES: dict[str, tuple[str, tuple[float, float]]] = {
    "claude-sonnet-5": ("2026-08-31", (3.00, 15.00)),
}

_CACHE_READ_MULTIPLIER = 0.10
_CACHE_WRITE_MULTIPLIER = 1.25

_UNKNOWN_MODEL_PRICE = (0.0, 0.0)


@dataclass
class UsageTotals:
    """Running totals for one job."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_usd: float = 0.0
    # Images the asset server generated for this job. Not a dollar figure and
    # deliberately not folded into ``cost_usd``: PixelLab bills against a
    # monthly image quota, so what depletes is the count, and the marginal
    # dollar cost of one more image is zero until that quota runs out.
    images_generated: int = 0
    # Models that produced tokens but have no entry in the price table; their
    # tokens are counted, their cost is not. Surfaced so a silently-zero bill is
    # visibly attributed rather than looking like free usage.
    unpriced_models: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["cost_usd"] = round(self.cost_usd, 6)
        payload["unpriced_models"] = sorted(self.unpriced_models or [])
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> UsageTotals:
        """Rebuild totals previously produced by :meth:`to_dict`.

        Needed because a job is not one continuous run: ApprovalGate suspends
        the graph, and the resume executes as a second run. Without restoring
        what was already spent, the second run would start from zero and
        overwrite the first run's bill.
        """

        if not payload:
            return cls()
        models = payload.get("unpriced_models")
        return cls(
            calls=_as_int(payload.get("calls")),
            input_tokens=_as_int(payload.get("input_tokens")),
            output_tokens=_as_int(payload.get("output_tokens")),
            cache_read_input_tokens=_as_int(payload.get("cache_read_input_tokens")),
            cache_creation_input_tokens=_as_int(payload.get("cache_creation_input_tokens")),
            cost_usd=float(payload.get("cost_usd") or 0.0),
            images_generated=_as_int(payload.get("images_generated")),
            unpriced_models=list(models) if isinstance(models, list) and models else None,
        )


_CURRENT: ContextVar[UsageTotals | None] = ContextVar("job_usage", default=None)


def estimate_cost_usd(model: str, usage: Mapping[str, Any]) -> float | None:
    """Cost of one call, or ``None`` when ``model`` has no published price here.

    ``None`` is deliberately distinct from ``0.0``: an unknown model means "not
    counted", not "free".
    """

    price = _PRICES_PER_MTOK.get(model)
    if price is None:
        return None

    input_price, output_price = price
    per_token_in = input_price / 1_000_000
    per_token_out = output_price / 1_000_000

    return (
        _as_int(usage.get("input_tokens")) * per_token_in
        + _as_int(usage.get("output_tokens")) * per_token_out
        + _as_int(usage.get("cache_read_input_tokens")) * per_token_in * _CACHE_READ_MULTIPLIER
        + _as_int(usage.get("cache_creation_input_tokens"))
        * per_token_in
        * _CACHE_WRITE_MULTIPLIER
    )


@contextmanager
def track(initial: Mapping[str, Any] | None = None) -> Iterator[UsageTotals]:
    """Accumulate usage recorded anywhere inside this context.

    ``initial`` continues a job's running total across a suspend/resume rather
    than restarting it at zero.
    """

    totals = UsageTotals.from_dict(initial)
    token = _CURRENT.set(totals)
    try:
        yield totals
    finally:
        _CURRENT.reset(token)


def current() -> UsageTotals | None:
    """Totals for the job running in this context, if any."""

    return _CURRENT.get()


def record(payload: Any) -> None:
    """Fold one tool result's ``usage`` object into the current totals.

    Accepts the whole tool response and ignores anything that does not carry a
    usable ``usage`` mapping, so callers need no knowledge of which servers
    report usage.
    """

    totals = _CURRENT.get()
    if totals is None or not isinstance(payload, Mapping):
        return

    # Image generation is reported on its own field, not inside ``usage``: it
    # is a quota count, not tokens, and the asset server has no tokens to
    # report alongside it.
    totals.images_generated += _as_int(payload.get("imagesGenerated"))

    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return

    totals.calls += 1
    totals.input_tokens += _as_int(usage.get("input_tokens"))
    totals.output_tokens += _as_int(usage.get("output_tokens"))
    totals.cache_read_input_tokens += _as_int(usage.get("cache_read_input_tokens"))
    totals.cache_creation_input_tokens += _as_int(usage.get("cache_creation_input_tokens"))

    model = str(usage.get("model") or payload.get("model") or "")
    cost = estimate_cost_usd(model, usage)
    if cost is None:
        if totals.unpriced_models is None:
            totals.unpriced_models = []
        if model and model not in totals.unpriced_models:
            totals.unpriced_models.append(model)
        return
    totals.cost_usd += cost


def _as_int(value: Any) -> int:
    """Coerce a reported token count to a non-negative int.

    Tool servers are separate processes; a malformed or missing field must not
    take a job down over bookkeeping.
    """

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, int(value))
