"""Unit tests for the deterministic Git repo naming rule (§03 Git MCP)."""

from app.utils.naming import build_repo_name, slugify


def test_slugify_lowercases_and_dashes_non_alnum_runs():
    assert slugify("Double Jump Hero!", max_length=32) == "double-jump-hero"


def test_slugify_truncates_and_drops_trailing_dash():
    assert slugify("a very very long game concept name", max_length=10) == "a-very-ver"


def test_slugify_blank_input_yields_empty_string():
    assert slugify("   ", max_length=10) == ""


def test_build_repo_name_joins_genre_slug_and_short_game_id():
    name = build_repo_name(
        genre="Platformer", prompt="Double Jump Hero!", game_id="3f9a1c2e-aaaa-bbbb"
    )
    assert name == "platformer-double-jump-hero-3f9a1c2e"


def test_build_repo_name_uses_short_game_id_as_is_when_already_short():
    name = build_repo_name(genre="rpg", prompt="a quest", game_id="g1")
    assert name == "rpg-a-quest-g1"


def test_build_repo_name_falls_back_for_blank_genre_and_prompt():
    name = build_repo_name(genre="", prompt="", game_id="g1")
    assert name == "game-prototype-g1"
