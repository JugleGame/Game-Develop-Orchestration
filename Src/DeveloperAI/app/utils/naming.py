"""Deterministic Git repo naming: ``{genre}-{prompt-slug}-{gameId}`` (§03 Git MCP)."""

import re


def slugify(text: str, *, max_length: int) -> str:
    """Lowercase, dash-separated slug. Empty input yields an empty string."""

    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_length].rstrip("-")


def build_repo_name(*, genre: str, prompt: str, game_id: str) -> str:
    genre_slug = slugify(genre, max_length=20) or "game"
    prompt_slug = slugify(prompt, max_length=32) or "prototype"
    game_id_suffix = game_id[:8] if len(game_id) > 8 else game_id
    return f"{genre_slug}-{prompt_slug}-{game_id_suffix}"
