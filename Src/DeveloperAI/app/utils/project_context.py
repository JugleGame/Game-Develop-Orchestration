"""Render the game design into the stable prefix every CodeGen call shares.

Each ``create_script`` call is its own conversation with the model. Without
this, a script is written knowing only its own one-line feature description —
not the genre it belongs to, not the mechanics the rest of the game
implements, not that a sibling script exists at all. That isolation is a
structural cause of compile failures, because two files that must reference
each other are written by two conversations that never met.

**This text has to be byte-identical for every call in a game.** It is the
prompt-cache prefix on the server side: a value that varies per call (a
timestamp, an unsorted mapping, a growing list) turns every call into a cache
write instead of a read. So only fields fixed at Planning time go in, rendered
in a fixed order — the list of already-generated types is deliberately *not*
here, because it grows as the pass proceeds.
"""

from typing import Any

# Long enough to matter as a cache prefix, short enough not to crowd out the
# feature's own description. Mechanics are the part worth spending budget on;
# the overview is background.
_MAX_OVERVIEW_CHARS = 600
_MAX_MECHANICS = 12

# Fixed text, so it costs nothing after the first call in a game — it sits
# inside the cached prefix. Kept as one constant rather than assembled per call
# so the prefix stays byte-identical, which is what makes the cache hit.
_CODE_CONVENTIONS = """
Code conventions for this project:
- One public type per file; the file is named after it.
- Scripts live under Assets/Scripts/<Category>/, never directly in Assets/Scripts.
- Do not build the game's object graph at runtime. Prefabs and the scene are
  assembled separately, so a MonoBehaviour should expect to be placed on a
  GameObject already, and take its references through [SerializeField].
- Name types after game concepts. A type named after the spec that requested it
  (Spec001, Feature003) is rejected."""


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def build_project_context(design: dict[str, Any] | None, type_map: str = "") -> str:
    """Blueprint summary for the CodeGen system prefix. Empty when unplanned.

    Returning ``""`` is meaningful: the server only sets a cache breakpoint
    when there is context to cache, so an unplanned job simply runs without
    one rather than paying for a prefix too short to cache.

    ``type_map`` is the architecture pass's full list of the game's types. It
    belongs here rather than in ``existing_types`` because it is decided before
    the first file is written and does not change afterwards — which is what
    lets the *first* script know its siblings exist instead of being written
    blind, and keeps the prefix byte-identical across the pass.
    """

    if not design and not type_map.strip():
        return ""

    lines: list[str] = []
    design = design or {}

    genre = str(design.get("genre") or "").strip()
    if genre:
        lines.append(f"Genre: {genre}")

    art_style = str(design.get("art_style") or "").strip()
    if art_style:
        lines.append(f"Art style: {art_style}")

    platform = str(design.get("target_platform") or "").strip()
    if platform:
        lines.append(f"Target platform: {platform}")

    mechanics = [
        _clip(item, 200)
        for item in (design.get("core_mechanics") or [])[:_MAX_MECHANICS]
        if str(item).strip()
    ]
    if mechanics:
        lines.append("Core mechanics this game implements:")
        lines.extend(f"- {item}" for item in mechanics)

    overview = str(design.get("structure_overview") or "").strip()
    if overview:
        lines.append(f"World structure: {_clip(overview, _MAX_OVERVIEW_CHARS)}")

    if type_map.strip():
        lines.append("")
        lines.append(type_map.strip())

    if not lines:
        return ""

    lines.append(
        "\nEvery script below belongs to this one game. Keep naming, units and "
        "conventions consistent with the mechanics listed above, and reference the "
        "types listed above by their exact names rather than redefining them."
    )
    # The conventions this prefix has always claimed to carry but never did.
    # They belong here rather than in the server's system prompt because the
    # folder layout is per-game: the architecture pass chose these categories.
    lines.append(_CODE_CONVENTIONS)
    return "\n".join(lines)
