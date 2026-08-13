"""Deterministic PixelLab prompt composition.

The host owns the asset intent. This module only removes instructions already
represented by PixelLab's structured fields and appends the minimum framing
needed for each asset kind. It never calls a model or invents subject details.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .render import AssetKind


_SPACE = re.compile(r"\s+")
_SEPARATOR = re.compile(r"\s*[,;|]\s*")

# These clauses duplicate API fields set by server._pixellab_style_params or
# the request payload. Matching whole clauses keeps subject phrases intact.
_STRUCTURED_CLAUSES = frozenset(
    {
        "pixel art",
        "2d pixel art",
        "transparent background",
        "no background",
        "side view",
        "low top-down view",
        "high top-down view",
        "black outline",
        "single color black outline",
        "single color outline",
        "selective outline",
        "lineless",
        "flat shading",
        "medium shading",
        "medium detail",
        "highly detailed",
    }
)

_SIZE_CLAUSE = re.compile(r"\d{2,3}\s*[x×]\s*\d{2,3}(?:\s*(?:px|pixels?))?", re.IGNORECASE)

_FRAMING: dict[AssetKind, str] = {
    "character": "full body centered, connected readable silhouette",
    "monster": "single centered creature, fully visible, connected readable silhouette",
    "tile": "edge-to-edge tile, repeatable boundaries, consistent projection",
    "prop": "single centered isolated object, connected silhouette, visible support",
    "icon": "single centered item, connected readable silhouette",
    "ui_button": "text-free button with a clean border",
    "ui_panel": "text-free panel with a clean border",
}

_COMPOSITION_QUESTIONS: dict[AssetKind, str] = {
    "character": "What pose, facing direction, and equipment define the silhouette?",
    "monster": "What anatomy, posture, and carried elements define the silhouette?",
    "tile": "What projection, repeat axis, walkable edge, and transition behavior are required?",
    "prop": "What dominant mass, support/contact structure, and viewing angle are required?",
    "icon": "What complete item silhouette, fill ratio, and border treatment are required?",
    "ui_button": "What dimensions, states, border, and content slots are required?",
    "ui_panel": "What dimensions, hierarchy, border, and content regions are required?",
}


@dataclass(frozen=True)
class PromptPlan:
    """A composed prompt plus observable character-count metadata."""

    prompt: str
    original_characters: int
    composed_characters: int
    removed_structured_clauses: tuple[str, ...]

    def metadata(self) -> dict[str, object]:
        return {
            "originalCharacters": self.original_characters,
            "composedCharacters": self.composed_characters,
            "characterDelta": self.composed_characters - self.original_characters,
            "removedStructuredClauses": list(self.removed_structured_clauses),
        }


def normalize_items(values: list[str] | None) -> tuple[str, ...]:
    """Normalize ordered prompt details without changing their meaning."""

    result: list[str] = []
    for value in values or []:
        normalized = _SPACE.sub(" ", value).strip(" .")
        if normalized and normalized.casefold() not in {item.casefold() for item in result}:
            result.append(normalized)
    return tuple(result)


def prepare(
    kind: AssetKind,
    *,
    subject: str = "",
    purpose: str = "",
    composition: str = "",
    must_have: list[str] | None = None,
    avoid: list[str] | None = None,
    art_style: str = "",
    is_revision: bool = False,
    preserve: list[str] | None = None,
    change: list[str] | None = None,
) -> dict[str, object]:
    """Return deterministic intake questions and a host-ready prompt.

    The function does not invent visual content. It only orders answers so
    required structures lead, feedback is explicit, and exclusions remain a
    last-resort tail instead of dominating the subject.
    """

    subject = _SPACE.sub(" ", subject).strip(" .")
    purpose = _SPACE.sub(" ", purpose).strip(" .")
    composition = _SPACE.sub(" ", composition).strip(" .")
    art_style = _SPACE.sub(" ", art_style).strip(" .")
    required = normalize_items(must_have)
    exclusions = normalize_items(avoid)
    preserved = normalize_items(preserve)
    changes = normalize_items(change)

    questions: list[dict[str, object]] = []
    required_questions = (
        ("subject", subject, "What exactly should be depicted?"),
        ("purpose", purpose, "Where will the asset be used, and at what readable scale?"),
        ("composition", composition, _COMPOSITION_QUESTIONS[kind]),
        ("mustHave", required, "Which visible structures or details must be unmistakable?"),
        ("artStyle", art_style, "Which shared art direction, palette, and pixel density apply?"),
    )
    for field, value, question in required_questions:
        if not value:
            questions.append({"field": field, "question": question, "required": True})
    if is_revision:
        if not preserved:
            questions.append(
                {
                    "field": "preserve",
                    "question": "Which successful details from the reviewed prototype must remain?",
                    "required": True,
                }
            )
        if not changes:
            questions.append(
                {
                    "field": "change",
                    "question": "What should replace each unsuccessful detail, stated positively?",
                    "required": True,
                }
            )
    if not exclusions:
        questions.append(
            {
                "field": "avoid",
                "question": "Which misleading interpretations or production defects should be excluded?",
                "required": False,
            }
        )

    ready = not any(question["required"] for question in questions)
    prompt: str | None = None
    if ready:
        sections = [
            subject,
            f"Composition: {composition}",
            f"Required visual structure: {'; '.join(required)}",
        ]
        if preserved:
            sections.append(f"Preserve from the reviewed prototype: {'; '.join(preserved)}")
        if changes:
            sections.append(f"Revision target: {'; '.join(changes)}")
        sections.append(f"Readability target: {purpose}")
        if exclusions:
            sections.append(f"Exclude: {'; '.join(exclusions)}")
        prompt = ". ".join(sections) + "."

    return {
        "assetKind": kind,
        "readyForPrototype": ready,
        "questions": questions,
        "prompt": prompt,
        "artStyle": art_style or None,
        "feedbackApplied": bool(preserved or changes),
        "promptCharacters": len(prompt) if prompt else 0,
    }


def compose(prompt: str, kind: AssetKind) -> PromptPlan:
    """Preserve intent, remove structured duplicates, and add kind framing."""

    normalized = _SPACE.sub(" ", prompt).strip()
    kept: list[str] = []
    removed: list[str] = []
    for clause in _SEPARATOR.split(normalized):
        clause = clause.strip(" .")
        if not clause:
            continue
        lowered = clause.casefold()
        if lowered in _STRUCTURED_CLAUSES or _SIZE_CLAUSE.fullmatch(lowered):
            removed.append(clause)
        elif lowered not in {item.casefold() for item in kept}:
            kept.append(clause)

    subject = ", ".join(kept) or normalized
    framing = _FRAMING[kind]
    structured_brief = all(
        marker in subject.casefold()
        for marker in ("composition:", "required visual structure:")
    )
    if not structured_brief and framing.casefold() not in subject.casefold():
        subject = f"{subject}; {framing}"

    return PromptPlan(
        prompt=subject,
        original_characters=len(normalized),
        composed_characters=len(subject),
        removed_structured_clauses=tuple(removed),
    )


__all__ = ["PromptPlan", "compose", "normalize_items", "prepare"]
