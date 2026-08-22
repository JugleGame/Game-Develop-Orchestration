"""Deterministic PixelLab prompt composition.

The host owns the asset intent. This module normalises it, drops exact
duplicates, and appends the minimum framing needed for each asset kind. It
never calls a model or invents subject details.

It used to also *delete* wording that a structured field could carry —
"flat shading", "side view", "pixel art" — on the belief that the fields
outrank the description. PixelLab's own schema says the opposite about every
one of them:

    outline  "Outline style reference (weakly guiding)"
    shading  "Shading style reference (weakly guiding)"
    detail   "Detail style reference (weakly guiding)"
    view     "Camera view angle (weakly guiding)"

Weakly guiding fields bias a result; they do not override a description. So
deleting the description's own wording removed the strong signal and left only
the weak one. Both are sent now.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .render import AssetKind


_SPACE = re.compile(r"\s+")
_SEPARATOR = re.compile(r"\s*[,;|]\s*")

# Clauses that a structured field also carries. They are **kept** in the
# description — see the module docstring — and only reported, so a caller can
# still see which parts of a prompt are duplicated by a field. Exact repeats
# within one prompt are still collapsed, as they are for any other clause.
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

# A clause that opens by naming what must not appear. PixelLab reads the noun,
# not the negation: "no city, no buildings, no street" came back with a city in
# it five times out of five, while the same subject without those clauses came
# back clean, and the positive "empty background" worked (measured 2026-08-22).
# Only clause-leading forms are matched — "a knight with no helmet" would lose
# the knight along with the helmet, so it is left alone.
_NEGATION_CLAUSE = re.compile(
    r"^(?:no|not|never|without|avoid|avoiding|exclude|excluding)(?![A-Za-z])",
    re.IGNORECASE,
)

# What a clause forbids, with the negation word taken off: "no city" -> "city".
# ``negative_description`` wants the thing to avoid, not the instruction to
# avoid it, so a clause is only useful there once the leading word is gone.
_NEGATION_LEAD = re.compile(
    r"^(?:no|not|never|without|avoid|avoiding|exclude|excluding)\b[\s:,-]*",
    re.IGNORECASE,
)

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
    #: Clauses a structured field also carries. Kept in the prompt, not removed
    #: — the name is retained so existing readers of ``promptMetrics`` keep
    #: working, and ``structuredClauses`` in the metadata says what it means.
    removed_structured_clauses: tuple[str, ...]
    removed_negations: tuple[str, ...] = ()

    @property
    def negative_description(self) -> str:
        """The removed negations as PixelLab's ``negative_description``.

        Removing a negation from the description is right for pixflux, which
        marks ``negative_description`` ``(Deprecated)`` and draws the noun
        anyway. It was never right to *discard* the information: bitforge's
        ``negative_description`` is live, so the same clauses become a usable
        field there rather than something the caller has to remember.
        """

        subjects = [
            stripped
            for clause in self.removed_negations
            if (stripped := _NEGATION_LEAD.sub("", clause).strip(" .,;"))
        ]
        return ", ".join(dict.fromkeys(subjects))

    def metadata(self) -> dict[str, object]:
        return {
            "originalCharacters": self.original_characters,
            "composedCharacters": self.composed_characters,
            "characterDelta": self.composed_characters - self.original_characters,
            # Reported, not removed. The old key keeps its name for readers
            # that already look for it; both now list what was *kept*.
            "removedStructuredClauses": [],
            "structuredClauses": list(self.removed_structured_clauses),
            "removedNegations": list(self.removed_negations),
            "negativeDescription": self.negative_description,
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
    required structures lead and feedback is explicit.

    ``avoid`` answers are returned as ``exclusions`` and are deliberately kept
    out of the provider *description*. PixelLab draws the noun and ignores the
    negation, so an exclusion written into the description makes the excluded
    thing more likely, not less (measured 2026-08-22). They are not thrown
    away: on the bitforge path they are sent as ``negative_description``, a
    live field there and ``(Deprecated)`` on pixflux. Restating an exclusion
    positively in ``mustHave`` is still a judgement call and belongs to the
    host: this server never calls a model.
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
                "question": (
                    "Which misleading interpretations or production defects should be excluded? "
                    "State the replacement positively in mustHave as well: exclusions travel as "
                    "negative_description on the style-reference path and are dropped entirely "
                    "on the plain one."
                ),
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
        prompt = ". ".join(sections) + "."

    return {
        "assetKind": kind,
        "readyForPrototype": ready,
        "questions": questions,
        "prompt": prompt,
        "artStyle": art_style or None,
        "exclusions": list(exclusions),
        "feedbackApplied": bool(preserved or changes),
        "promptCharacters": len(prompt) if prompt else 0,
    }


def compose(prompt: str, kind: AssetKind) -> PromptPlan:
    """Preserve intent, drop exact repeats, and add kind framing.

    Style wording is kept rather than stripped: the fields that would carry it
    are all ``(weakly guiding)`` in PixelLab's schema, so removing it from the
    description traded a strong signal for a weak one. The clauses a field also
    covers are reported in ``structuredClauses`` instead.
    """

    normalized = _SPACE.sub(" ", prompt).strip()
    kept: list[str] = []
    structured: list[str] = []
    negated: list[str] = []
    for clause in _SEPARATOR.split(normalized):
        clause = clause.strip(" .")
        if not clause:
            continue
        lowered = clause.casefold()
        if _NEGATION_CLAUSE.match(clause):
            negated.append(clause)
            continue
        if lowered in {item.casefold() for item in kept}:
            continue
        if lowered in _STRUCTURED_CLAUSES or _SIZE_CLAUSE.fullmatch(lowered):
            structured.append(clause)
        kept.append(clause)

    subject = ", ".join(kept) or normalized
    framing = _FRAMING[kind]
    # No exemption for a structured brief. The check used to skip the framing
    # whenever the prompt contained "Composition:" and "Required visual
    # structure:" — which is exactly what ``prepare`` writes, so following the
    # intake procedure was the one way to always lose the framing.
    if framing.casefold() not in subject.casefold():
        subject = f"{subject}; {framing}"

    return PromptPlan(
        prompt=subject,
        original_characters=len(normalized),
        composed_characters=len(subject),
        removed_structured_clauses=tuple(structured),
        removed_negations=tuple(negated),
    )


__all__ = ["PromptPlan", "compose", "normalize_items", "prepare"]
