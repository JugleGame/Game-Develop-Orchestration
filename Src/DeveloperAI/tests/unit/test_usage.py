"""Unit tests for per-job LLM usage accounting.

The accumulator is what turns "a tool server called Anthropic" into a number on
``game_jobs.cost_usd``, so the properties pinned here are: attribution is
per-context (never leaks between jobs), a malformed report cannot take a job
down, and an unpriced model is visibly unpriced rather than silently free.
"""

import asyncio

from app.utils import usage

_OPUS_CALL = {
    "usage": {
        "model": "claude-opus-5",
        "input_tokens": 1_000_000,
        "output_tokens": 1_000_000,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
}


def test_records_tokens_and_prices_them():
    with usage.track() as totals:
        usage.record(_OPUS_CALL)

    assert totals.calls == 1
    assert totals.input_tokens == 1_000_000
    assert totals.output_tokens == 1_000_000
    # 1M input at $5 + 1M output at $25.
    assert totals.cost_usd == 30.0


def test_cache_reads_and_writes_are_priced_off_the_input_rate():
    with usage.track() as totals:
        usage.record(
            {
                "usage": {
                    "model": "claude-opus-5",
                    "cache_read_input_tokens": 1_000_000,
                    "cache_creation_input_tokens": 1_000_000,
                }
            }
        )

    # $5 * 0.1 for the read + $5 * 1.25 for the 5-minute write.
    assert totals.cost_usd == 0.5 + 6.25


def test_totals_accumulate_across_calls():
    with usage.track() as totals:
        usage.record(_OPUS_CALL)
        usage.record(_OPUS_CALL)

    assert totals.calls == 2
    assert totals.cost_usd == 60.0


def test_unpriced_model_counts_tokens_but_is_flagged_not_billed():
    """An unknown model must not look free — that would understate the bill."""

    with usage.track() as totals:
        usage.record({"usage": {"model": "some-future-model", "output_tokens": 1_000_000}})

    assert totals.output_tokens == 1_000_000
    assert totals.cost_usd == 0.0
    assert totals.to_dict()["unpriced_models"] == ["some-future-model"]


def test_responses_without_usage_are_ignored():
    """Most tool servers do no LLM work; their results must be accounted zero."""

    with usage.track() as totals:
        usage.record({"file": "Assets/Scripts/Foo.cs"})
        usage.record(None)
        usage.record("not a mapping")

    assert totals.calls == 0
    assert totals.cost_usd == 0.0


def test_generated_images_are_counted_apart_from_dollars():
    """PixelLab bills a monthly image quota, not tokens.

    The count must survive a suspend/resume the same way spend does, and must
    not be folded into ``cost_usd``: one more image costs nothing until the
    quota runs out.
    """

    with usage.track() as first_run:
        usage.record({"assetPath": "a.png", "imagesGenerated": 1})
        usage.record({"assetPath": "b.png", "imagesGenerated": 2})

    assert first_run.images_generated == 3
    assert first_run.cost_usd == 0.0
    assert first_run.calls == 0

    with usage.track(first_run.to_dict()) as resumed:
        usage.record({"assetPath": "c.png", "imagesGenerated": 1})

    assert resumed.images_generated == 4


def test_malformed_token_counts_do_not_raise():
    """A tool server is a separate process; bad bookkeeping must not kill a job."""

    with usage.track() as totals:
        usage.record(
            {"usage": {"model": "claude-opus-5", "input_tokens": None, "output_tokens": "lots"}}
        )

    assert totals.calls == 1
    assert totals.input_tokens == 0
    assert totals.output_tokens == 0


def test_recording_outside_a_scope_is_a_no_op():
    usage.record(_OPUS_CALL)  # must not raise

    assert usage.current() is None


def test_resuming_a_job_continues_its_tally_instead_of_restarting_it():
    """ApprovalGate splits a job into two runs; the bill must survive the seam.

    Regression: the second run used to start from zero and overwrite the
    planning run's spend, so every approved game under-reported its cost.
    """

    with usage.track() as first_run:
        usage.record(_OPUS_CALL)
    assert first_run.cost_usd == 30.0

    with usage.track(first_run.to_dict()) as second_run:
        usage.record(_OPUS_CALL)

    assert second_run.calls == 2
    assert second_run.cost_usd == 60.0
    assert second_run.input_tokens == 2_000_000


def test_seeding_from_an_absent_total_starts_clean():
    with usage.track(None) as totals:
        assert totals.calls == 0
        assert totals.cost_usd == 0.0


async def test_concurrent_jobs_do_not_share_totals():
    """Each job runs in its own task; a ContextVar must keep the bills apart."""

    async def run_job(calls: int) -> float:
        with usage.track() as totals:
            for _ in range(calls):
                usage.record(_OPUS_CALL)
                await asyncio.sleep(0)  # force interleaving
            return totals.cost_usd

    first, second = await asyncio.gather(run_job(1), run_job(3))

    assert first == 30.0
    assert second == 90.0
