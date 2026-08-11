"""Every model a tool server can pick must have a price here.

The model choice lives in three separate processes — ``STRATEGIC_MODEL``,
``QA_MODEL``, ``UNITY_CODEGEN_MODEL`` — while the price table lives in the
orchestrator. Nothing connects them at runtime: an unpriced model still runs,
still burns tokens, and is accounted as $0 through ``unpriced_models``. The
pipeline keeps working and only the bill goes quiet, which is the kind of
failure nobody reports.

This test is the connection. It reads the defaults out of the server sources
rather than restating them, so changing a default in one of those files either
updates this test's input or fails it.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import pytest

from app.utils.usage import _PRICES_PER_MTOK, _SCHEDULED_PRICE_CHANGES, estimate_cost_usd

MCP_SERVERS = Path(__file__).resolve().parents[3] / "McpServers"

# MODEL = os.getenv("STRATEGIC_MODEL", "claude-opus-5")
# self._model = model or os.getenv("UNITY_CODEGEN_MODEL", "claude-opus-5")
_MODEL_DEFAULT = re.compile(
    r"os\.getenv\(\s*\"(?P<env>[A-Z_]*MODEL)\"\s*,\s*\"(?P<model>claude-[\w.\-]+)\"\s*\)"
)

MODEL_SOURCES = (
    MCP_SERVERS / "strategic" / "planner.py",
    MCP_SERVERS / "qa" / "judge.py",
    MCP_SERVERS / "unity" / "codegen.py",
)


def _declared_defaults() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in MODEL_SOURCES:
        for match in _MODEL_DEFAULT.finditer(path.read_text(encoding="utf-8")):
            found[match.group("env")] = match.group("model")
    return found


def test_every_model_source_is_readable() -> None:
    """A renamed module would make the coverage test vacuously pass."""

    for path in MODEL_SOURCES:
        assert path.is_file(), f"expected a model default in {path}"


def test_all_three_servers_declare_a_default() -> None:
    defaults = _declared_defaults()
    assert set(defaults) == {"STRATEGIC_MODEL", "QA_MODEL", "UNITY_CODEGEN_MODEL"}, (
        f"could not find every server's model default; parsed {defaults}"
    )


@pytest.mark.parametrize(
    "env_var,model", sorted(_declared_defaults().items()), ids=lambda v: str(v)
)
def test_default_model_is_priced(env_var: str, model: str) -> None:
    assert model in _PRICES_PER_MTOK, (
        f"{env_var} defaults to {model!r}, which has no entry in _PRICES_PER_MTOK. "
        f"Its spend would be reported as $0 and hidden in 'unpriced_models'."
    )


@pytest.mark.parametrize("model,change", sorted(_SCHEDULED_PRICE_CHANGES.items()))
def test_scheduled_price_change_has_not_silently_passed(
    model: str, change: tuple[str, tuple[float, float]]
) -> None:
    """Introductory pricing expires; the table must be updated when it does."""

    effective_from, new_price = change
    if date.today() < date.fromisoformat(effective_from):
        return
    assert _PRICES_PER_MTOK[model] == new_price, (
        f"{model} introductory pricing ended {effective_from}; "
        f"_PRICES_PER_MTOK still says {_PRICES_PER_MTOK[model]}, expected {new_price}."
    )


def test_unpriced_model_is_distinct_from_free() -> None:
    """``None`` means 'not counted'; ``0.0`` would mean 'genuinely free'."""

    usage = {"input_tokens": 1000, "output_tokens": 1000}
    assert estimate_cost_usd("some-unreleased-model", usage) is None
    assert estimate_cost_usd("claude-opus-5", usage) == pytest.approx(0.03)
