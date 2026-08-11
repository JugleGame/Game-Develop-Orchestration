"""Unit tests for the pipeline trace store.

The point of this table is that each row eventually carries a *label* — did it
compile, did QA pass. So these pin the two things that would quietly ruin the
data: counting a reused file as a fresh generation (inflating any success rate
computed later), and attaching a verdict to the wrong attempt.
"""

from typing import Any

from app.repository.trace_repository import TraceRepository

_CONTEXT = "Genre: platformer\nCore mechanics this game implements:\n- The player jumps."


def _script(feature_id: str, *, generated: bool = True, contents: str = "class A { }") -> dict:
    return {
        "feature_id": feature_id,
        "file": f"Assets/Scripts/{feature_id}.cs",
        "contents": contents,
        "class_name": "A",
        "prompt": f"Implement {feature_id}",
        "generated": generated,
    }


async def _record(session_factory, **kwargs: Any) -> list:
    async with session_factory() as session:
        return await TraceRepository(session).record_generation(**kwargs)


async def test_generation_is_recorded_with_its_inputs_and_output(session_factory):
    await _record(
        session_factory,
        game_id="g1",
        attempt=1,
        project_context=_CONTEXT,
        scripts=[_script("f-1")],
    )

    async with session_factory() as session:
        rows = await TraceRepository(session).list_for_game("g1")

    assert len(rows) == 1
    row = rows[0]
    assert row.feature_id == "f-1"
    assert row.attempt == 1
    assert row.prompt == "Implement f-1"
    assert row.project_context == _CONTEXT
    assert row.generated_source == "class A { }"
    # No verdict yet — that arrives from a later node.
    assert row.compile_ok is None
    assert row.qa_verdict is None


async def test_reused_scripts_are_not_recorded_as_generations(session_factory):
    """A retry regenerates only the implicated feature. Counting the carried-over
    files too would make every retry look like a full, mostly-successful pass."""

    await _record(
        session_factory,
        game_id="g1",
        attempt=2,
        project_context=_CONTEXT,
        scripts=[_script("f-1", generated=False), _script("f-2")],
    )

    async with session_factory() as session:
        rows = await TraceRepository(session).list_for_game("g1")

    assert [row.feature_id for row in rows] == ["f-2"]


async def test_recording_nothing_is_not_an_error(session_factory):
    rows = await _record(session_factory, game_id="g1", attempt=1, project_context="", scripts=[])

    assert rows == []


async def test_compile_result_labels_only_its_own_attempt(session_factory):
    """Attempt 1 failed and attempt 2 passed; both rows must keep their own
    verdict, or the failure/success pair stops being usable as an example."""

    await _record(
        session_factory, game_id="g1", attempt=1, project_context="", scripts=[_script("f-1")]
    )
    async with session_factory() as session:
        await TraceRepository(session).record_compile_result(
            game_id="g1",
            attempt=1,
            compile_ok=False,
            compile_errors=[{"file": "A.cs", "line": 3, "message": "CS1002"}],
        )

    await _record(
        session_factory, game_id="g1", attempt=2, project_context="", scripts=[_script("f-1")]
    )
    async with session_factory() as session:
        await TraceRepository(session).record_compile_result(
            game_id="g1", attempt=2, compile_ok=True, compile_errors=[]
        )

    async with session_factory() as session:
        rows = await TraceRepository(session).list_for_game("g1")

    assert [(row.attempt, row.compile_ok) for row in rows] == [(1, False), (2, True)]
    assert rows[0].compile_errors[0]["message"] == "CS1002"
    # An empty error list is stored as absence, not as an empty list.
    assert rows[1].compile_errors is None


async def test_qa_verdict_labels_every_row_of_the_attempt(session_factory):
    await _record(
        session_factory,
        game_id="g1",
        attempt=1,
        project_context="",
        scripts=[_script("f-1"), _script("f-2")],
    )

    async with session_factory() as session:
        labelled = await TraceRepository(session).record_qa_result(
            game_id="g1", attempt=1, verdict="FAIL", report={"message": "dash never triggers"}
        )

    assert labelled == 2
    async with session_factory() as session:
        rows = await TraceRepository(session).list_for_game("g1")
    assert all(row.qa_verdict == "FAIL" for row in rows)
    assert rows[0].qa_report["message"] == "dash never triggers"


async def test_verified_pairs_require_both_compile_and_qa_to_pass(session_factory):
    # record_compile_result/record_qa_result label every row of an attempt, so
    # two attempts (not two scripts in one attempt) are what separates a
    # verified row from an unverified one here.
    await _record(
        session_factory, game_id="g1", attempt=1, project_context="", scripts=[_script("f-1")]
    )
    async with session_factory() as session:
        repo = TraceRepository(session)
        await repo.record_compile_result(
            game_id="g1", attempt=1, compile_ok=True, compile_errors=None
        )
    async with session_factory() as session:
        # Compiled but QA rejected it - not a verified pair.
        await TraceRepository(session).record_qa_result(
            game_id="g1", attempt=1, verdict="FAIL", report=None
        )

    await _record(
        session_factory, game_id="g1", attempt=2, project_context="", scripts=[_script("f-2")]
    )
    async with session_factory() as session:
        repo = TraceRepository(session)
        await repo.record_compile_result(
            game_id="g1", attempt=2, compile_ok=True, compile_errors=None
        )
    async with session_factory() as session:
        await TraceRepository(session).record_qa_result(
            game_id="g1", attempt=2, verdict="PASS", report=None
        )

    async with session_factory() as session:
        verified = await TraceRepository(session).list_verified_pairs()

    assert [row.feature_id for row in verified] == ["f-2"]


async def test_before_after_pairs_match_attempt_one_failure_to_attempt_two_success(
    session_factory,
):
    await _record(
        session_factory, game_id="g1", attempt=1, project_context="", scripts=[_script("f-1")]
    )
    async with session_factory() as session:
        await TraceRepository(session).record_compile_result(
            game_id="g1", attempt=1, compile_ok=False, compile_errors=[{"message": "CS1002"}]
        )

    await _record(
        session_factory, game_id="g1", attempt=2, project_context="", scripts=[_script("f-1")]
    )
    async with session_factory() as session:
        repo = TraceRepository(session)
        await repo.record_compile_result(
            game_id="g1", attempt=2, compile_ok=True, compile_errors=None
        )
    async with session_factory() as session:
        await TraceRepository(session).record_qa_result(
            game_id="g1", attempt=2, verdict="PASS", report=None
        )

    async with session_factory() as session:
        pairs = await TraceRepository(session).list_before_after_pairs()

    assert len(pairs) == 1
    before, after = pairs[0]
    assert before.attempt == 1 and before.compile_ok is False
    assert after.attempt == 2 and after.compile_ok is True and after.qa_verdict == "PASS"


async def test_before_after_pairs_skip_unresolved_failures(session_factory):
    """A feature that failed attempt 1 but never got an attempt-2 success (still
    retrying, or escalated) must not show up as a fabricated example."""

    await _record(
        session_factory, game_id="g1", attempt=1, project_context="", scripts=[_script("f-1")]
    )
    async with session_factory() as session:
        await TraceRepository(session).record_compile_result(
            game_id="g1", attempt=1, compile_ok=False, compile_errors=[{"message": "CS1002"}]
        )

    async with session_factory() as session:
        pairs = await TraceRepository(session).list_before_after_pairs()

    assert pairs == []


async def test_traces_are_scoped_to_their_game(session_factory):
    await _record(
        session_factory, game_id="g1", attempt=1, project_context="", scripts=[_script("f-1")]
    )
    await _record(
        session_factory, game_id="g2", attempt=1, project_context="", scripts=[_script("f-1")]
    )

    async with session_factory() as session:
        repo = TraceRepository(session)
        await repo.record_compile_result(
            game_id="g1", attempt=1, compile_ok=True, compile_errors=None
        )
        g2_rows = await repo.list_for_game("g2")

    assert g2_rows[0].compile_ok is None, "one game's verdict must not label another's"
