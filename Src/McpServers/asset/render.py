"""Prompt classification and deterministic seeding for 2D asset generation.

The actual pixels come from PixelLab (``pixellab_client.py``) — this module
only decides *what* to ask for: which ``AssetKind`` a prompt describes, what
material a tile/prop is made of (so PixelLab's forced palette matches), and a
seed derived from the game/feature/prompt so a rejected asset can be
regenerated with the same inputs.

Keyword matching rather than an LLM: this runs on every asset, must be
deterministic for reproducibility, and the vocabulary is small and closed.
"""

from __future__ import annotations

import hashlib
import random
import re
from typing import Literal

from .style import ArtStyle

AssetKind = Literal["character", "monster", "tile", "prop", "ui_panel", "ui_button", "icon"]

# Prompt keyword → asset kind. First match wins, so order matters: the more
# specific UI terms are checked before the generic world terms.
#
# "character" and "monster" split what used to be one bucket (measured,
# prompt-eval 2026-08-02 round 6): a humanoid needs headroom a square canvas
# doesn't give it, but the same tall ratio distorts a round creature like a
# slime. "enemy" is deliberately in "monster", not "character" — a
# non-humanoid antagonist is the common case this project draws.
_KIND_KEYWORDS: tuple[tuple[AssetKind, tuple[str, ...]], ...] = (
    ("ui_button", ("button", "버튼", "cta")),
    ("ui_panel", ("panel", "hud", "menu", "dialog", "inventory", "패널", "메뉴")),
    ("icon", ("icon", "cursor", "marker", "badge", "아이콘")),
    (
        "tile",
        (
            "tile",
            "terrain",
            "ground",
            "floor",
            "grass",
            "water",
            "sand",
            "road",
            "타일",
            "지형",
            "바닥",
        ),
    ),
    (
        "monster",
        (
            "monster",
            "creature",
            "enemy",
            "beast",
            "slime",
            "boss",
            "villain",
            "몬스터",
            "괴물",
            "슬라임",
            "적군",
            "적",
        ),
    ),
    (
        "character",
        ("character", "player", "npc", "hero", "캐릭터", "플레이어"),
    ),
    (
        "prop",
        ("tree", "rock", "bush", "chest", "barrel", "prop", "object", "나무", "바위", "상자"),
    ),
)

# Prompt keyword → material name (see style._MATERIALS). This is what makes a
# "grass terrain tile" green rather than whatever the game's base hue is.
_MATERIAL_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("water", ("water", "ocean", "sea", "lake", "river", "물", "바다", "강")),
    ("lava", ("lava", "magma", "volcan", "용암")),
    ("snow", ("snow", "ice", "frozen", "눈", "얼음")),
    ("sand", ("sand", "desert", "beach", "dune", "모래", "사막")),
    ("stone", ("stone", "rock", "cliff", "granite", "cobble", "돌", "바위", "암석")),
    ("metal", ("metal", "steel", "iron", "금속", "철")),
    ("dirt", ("dirt", "mud", "soil", "earth", "흙", "진흙")),
    ("wood", ("wood", "log", "plank", "barrel", "chest", "나무판", "통나무", "상자")),
    ("foliage", ("tree", "bush", "shrub", "leaf", "leaves", "forest", "oak", "pine", "나무", "숲")),
    ("grass", ("grass", "meadow", "lawn", "field", "풀", "잔디", "초원")),
)


def classify(prompt: str) -> AssetKind:
    """Infer what to draw from the feature description.

    Keyword matching rather than an LLM: this runs on every asset, must be
    deterministic for reproducibility, and the vocabulary is small and closed.
    """

    lowered = prompt.lower()
    words = lowered.split()
    for kind, keywords in _KIND_KEYWORDS:
        if any(_matches(keyword, lowered, words) for keyword in keywords):
            return kind
    return "prop"


def _matches(keyword: str, lowered: str, words: list[str]) -> bool:
    """Match English keywords as words and preserve Korean phrase matching.

    Short English keywords such as ``cta`` occur inside unrelated words such
    as ``rectangular``. Treating them as substrings can send terrain prompts
    down the UI branch. Korean phrases still use substring matching, except
    the one-syllable ``적`` keyword, which must remain a standalone word so
    ordinary words such as ``도적`` do not become monsters.
    """

    if keyword.isascii():
        return re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", lowered) is not None
    if len(keyword) == 1:
        return keyword in words
    return keyword in lowered


# Only terrain and props take their colour from a material. Characters use the
# game's identity ramp and UI uses the palette's UI roles, so reporting a
# material for those would be false metadata in the provenance record.
_MATERIAL_KINDS = frozenset({"tile", "prop"})


def material_for(prompt: str, kind: AssetKind) -> str | None:
    """The material a sprite is made of, or None if it is not material-driven."""

    if kind not in _MATERIAL_KINDS:
        return None
    lowered = prompt.lower()
    for material, keywords in _MATERIAL_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return material
    return "foliage" if kind == "prop" else "grass"


def rng_for(style: ArtStyle, feature_id: str, prompt: str) -> random.Random:
    """A PRNG that depends only on the game, the feature, and the prompt.

    Same inputs → same seed, forever. That is what lets a rejected asset be
    regenerated with the same PixelLab seed.
    """

    digest = hashlib.sha256(f"{style.seed}|{feature_id}|{prompt}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


__all__ = ["AssetKind", "classify", "material_for", "rng_for"]
