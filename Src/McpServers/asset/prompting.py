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

from . import pixellab_client
from .render import AssetKind


_SPACE = re.compile(r"\s+")
# A sentence period separates clauses too. It did not used to, which mattered
# because ``prepare`` joins its own sections with ". " — so every clause it
# wrote straddled a sentence boundary ("a young female knight. Composition:
# standing idle") and the duplicate check below never saw a whole clause to
# compare. Following the intake procedure was the one way to defeat it.
# A bare "." with no space after it is left alone so "1.5" stays one number.
_SEPARATOR = re.compile(r"\s*[,;|]\s*|\.(?=\s|$)\s*")

#: Characters a composed provider description may occupy before its lowest
#: priority clauses are dropped. Not a tuned value — there is no measurement
#: that says 400 beats 380. It is a guard rail against a call site pasting
#: paragraphs: a well-formed brief composes to roughly 200 characters, while
#: PixelLab's own tutorial prompt is two words ("Human mage") and the quality
#: lever it documents is the init image, not a longer description. Clauses are
#: dropped from the tail because ``prepare`` writes them in priority order,
#: and what gets dropped is reported in ``droppedClauses``.
PROMPT_BUDGET = 400

#: ``init_image_strength`` at or above which a starting image leads and the
#: description follows. PixelLab documents the bands by purpose: 300-400 is
#: rough shape and colour guidance, 400-600 "variations on an existing image",
#: 600-900 detail on a nearly finished piece. From 400 up the reference is
#: stating the composition in pixels, so restating it in prose is words
#: arguing with an image that already won.
REFERENCE_LEAD_STRENGTH = 400

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

# Per clause, not per kind, so a framing requirement the brief already made can
# be dropped on its own instead of all-or-nothing. A character brief that says
# "centered" used to get "full body centered" appended anyway.
#
# Every clause here has to name something the generator can *draw*. "connected
# readable silhouette" failed that test: legibility is what a reviewer judges,
# not a shape a model can put on a canvas, so the word spent characters telling
# the provider about our review process. Dropping the intake labels and then
# appending our own production vocabulary would have been the same mistake in a
# different place.
# Measured 2026-08-23 (`var/assets/experiments/round-7-framing/`,
# `round-8-framing/`, 40 paid generations, decision rule fixed before the run —
# see `Src/McpServers/experiments/framing_ab.py`).
#
# ``character``, ``prop``, and ``icon`` are empty because six pairs each, same
# seed per pair, produced **no** technical failure or warning in either arm and
# no centring advantage: 3/1, 0/1, and 1/0 pairs better-vs-worse against a
# two-thirds bar. The clauses asked for centring, full visibility, and a
# connected silhouette; the generator delivered all three without being told,
# and the wording spent 22-70 characters on the one axis that has a strength
# control (`text_guidance_scale`) to say it.
#
# ``tile`` keeps its clauses because the same method found the opposite: at both
# seeds the framing roughly doubled edge coverage (0.498 vs 0.435, 0.309 vs
# 0.150), and without it the result is scattered debris on a transparent canvas
# rather than a tile. That was measured through pixflux, not `/tilesets`.
#
# ``monster`` and the UI kinds keep theirs because they were **not** measured,
# not because they were measured and passed. Extrapolating from ``character`` to
# ``monster`` is the inference this experiment exists to avoid making.
_FRAMING: dict[AssetKind, tuple[str, ...]] = {
    "character": (),
    "monster": ("single centered creature", "fully visible", "connected silhouette"),
    "tile": ("edge-to-edge tile", "repeatable boundaries", "consistent projection"),
    "prop": (),
    "icon": (),
    "ui_button": ("text-free button with a clean border",),
    "ui_panel": ("text-free panel with a clean border",),
}

#: Words that carry no requirement of their own, so they do not make two
#: clauses different when deciding whether one already covers the other.
_FILLER_WORDS = frozenset({"a", "an", "the", "with", "and", "of", "in", "on", "is"})


def _significant_words(clause: str) -> frozenset[str]:
    return frozenset(clause.casefold().split()) - _FILLER_WORDS


def _already_covered(framing: str, kept: list[str]) -> bool:
    """Whether a brief already asked for what this framing clause adds.

    Subset either way, because a brief states the requirement at its own length:
    "centered" is covered by "full body centered" and covers it in turn. Two
    clauses that merely share a word ("single figure" against "single centered
    creature") are different requirements and both survive.
    """

    words = _significant_words(framing)
    return any(
        (other := _significant_words(clause)) <= words or words <= other for clause in kept
    )


def _control_vocabulary() -> dict[str, tuple[str, str]]:
    """Style wording mapped to the structured field and value it names.

    Built from ``STYLE_ENUMS`` rather than restated here: the accepted values
    differ per endpoint and drift is what this table exists to prevent. Only
    the pixflux row is used — it is the widest, so a clause it does not know is
    not a style value on any path.
    """

    vocabulary: dict[str, tuple[str, str]] = {}
    for field, values in pixellab_client.STYLE_ENUMS["create-image-pixflux"].items():
        for value in values:
            # "side" is the field's value but "side view" is how a description
            # says it; "direction" needs no such suffix.
            clause = f"{value} view" if field == "view" else value
            vocabulary[clause.casefold()] = (field, value)
    return vocabulary


_COMPOSITION_QUESTIONS: dict[AssetKind, str] = {
    "character": "What pose, facing direction, and equipment define the silhouette?",
    "monster": "What anatomy, posture, and carried elements define the silhouette?",
    "tile": "What projection, repeat axis, walkable edge, and transition behavior are required?",
    "prop": "What dominant mass, support/contact structure, and viewing angle are required?",
    "icon": "What complete item silhouette, fill ratio, and border treatment are required?",
    "ui_button": "What dimensions, states, border, and content slots are required?",
    "ui_panel": "What dimensions, hierarchy, border, and content regions are required?",
}


def _translation_questions(answers: dict[str, str]) -> list[dict[str, object]]:
    """One required question per answer that arrived in Korean.

    A refusal was the first shape of this, and it was the wrong one for the
    intake. The generation gates still refuse — they are what protects the bill
    — but here a Korean answer means the host has not written the English yet,
    which is a question, and this tool's whole job is asking questions.

    The server cannot answer it: it never calls a model, so it has nothing to
    translate with. The host does have one, so the question names both ways
    through — translate it, or settle the wording with the user first. Until one
    of them happens the brief stays unready and nothing generates.
    """

    return [
        {
            "field": field,
            "question": (
                f"{field} is written in Korean: {value!r}. PixelLab prompt fields are "
                "English only, and this server does not translate — it never calls a "
                "model. Supply the English wording: translate it yourself when the "
                "meaning is unambiguous, or confirm the intended English with the user "
                "when a choice of wording would change the picture."
            ),
            "required": True,
            "koreanText": value,
        }
        for field, value in answers.items()
        if pixellab_client.contains_hangul(value)
    ]


@dataclass(frozen=True)
class PromptPlan:
    """A composed prompt plus observable character-count metadata."""

    prompt: str
    original_characters: int
    composed_characters: int
    #: Clauses that a structured field also carries. They stay in the prompt;
    #: this only reports which parts of it are duplicated by a field.
    structured_clauses: tuple[str, ...]
    removed_negations: tuple[str, ...] = ()
    #: Clauses dropped to fit ``PROMPT_BUDGET``, lowest priority first.
    dropped_clauses: tuple[str, ...] = ()
    #: Description wording that names a *different* value of a structured
    #: field than the one being sent, as ``(clause, field, sent value)``.
    #: Reported, never resolved: the description is the strong signal and the
    #: field is ``(weakly guiding)``, so silently deleting either one would
    #: pick a winner the caller did not ask for.
    control_conflicts: tuple[tuple[str, str, str], ...] = ()
    #: Whether the kind framing was left off because a starting image is
    #: carrying the composition instead.
    framing_suppressed: bool = False

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
            "structuredClauses": list(self.structured_clauses),
            "removedNegations": list(self.removed_negations),
            "negativeDescription": self.negative_description,
            "promptBudget": PROMPT_BUDGET,
            "droppedClauses": list(self.dropped_clauses),
            "framingSuppressed": self.framing_suppressed,
            "controlConflicts": [
                {"clause": clause, "field": field, "sent": sent}
                for clause, field, sent in self.control_conflicts
            ],
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
    grid_size: int | None = None,
    palette_lock: bool | None = None,
    init_asset_id: str | None = None,
    init_image_strength: int | None = None,
    direction: str | None = None,
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

    # Generation parameters, asked the same way the content is asked. They used
    # to be optional arguments with defaults, so an agent that never asked the
    # user still produced a sprite — and the cost of the guess landed on
    # generation credits and human review. ``None`` is "nobody has answered
    # this yet"; that is why ``palette_lock`` is a tri-state here while the
    # provider call still sees a plain bool.
    parameter_questions = (
        (
            "gridSize",
            grid_size,
            "Which pixel grid should this asset generate on? Answer 0 for the game's locked "
            "grid. PixelLab's posed path grows 16, 32, and 64 to a square canvas, so a 1:2 "
            "figure asked for at those grids comes back squashed.",
        ),
        (
            "paletteLock",
            palette_lock,
            "Should the game's locked palette be forced onto this asset, or does it keep "
            "its own colours?",
        ),
        (
            "initAssetId",
            init_asset_id,
            "Which approved sprite or concept:<filename> reference should this start from? "
            'Answer "none" to generate without one.',
        ),
        (
            "initImageStrength",
            init_image_strength,
            "How much of that reference should survive? Answer 0 when there is no "
            "reference. PixelLab documents the range by purpose: 0-300 extremely rough "
            "colour guidance, 300-400 rough shapes and colours, 400-600 a variation on "
            "an existing image, 600-900 detail added to a nearly finished piece.",
        ),
        (
            "direction",
            direction,
            "Which way does the subject face? Answer \"none\" to use the game's locked "
            "direction.",
        ),
    )
    for field, value, question in parameter_questions:
        if value is None:
            questions.append({"field": field, "question": question, "required": True})

    # Korean answers become questions rather than refusals: the host is the one
    # with a model, so it can translate or ask the user. Required, so the brief
    # stays unready and nothing generates until English arrives.
    questions.extend(
        _translation_questions(
            {
                "subject": subject,
                "composition": composition,
                "artStyle": art_style,
                **{f"mustHave[{index}]": item for index, item in enumerate(required)},
                **{f"avoid[{index}]": item for index, item in enumerate(exclusions)},
                **{f"preserve[{index}]": item for index, item in enumerate(preserved)},
                **{f"change[{index}]": item for index, item in enumerate(changes)},
            }
        )
    )

    ready = not any(question["required"] for question in questions)
    prompt: str | None = None
    if ready:
        # Clauses, not labelled sentences. "Composition:", "Required visual
        # structure:" and "Readability target:" describe the intake form, not
        # anything that should appear on screen, and they reached the provider
        # verbatim. ``purpose`` goes the same way: it says where the asset will
        # be used, which is production context rather than a visual feature. It
        # is returned below instead, so the answer is kept without being drawn.
        #
        # The order is the priority order, because that is what the budget in
        # ``compose`` drops from the tail of: subject first, then the structures
        # that must be unmistakable, then feedback, then the arrangement.
        clauses = [subject, *required, *preserved, *changes, composition]
        prompt = ", ".join(clause for clause in clauses if clause)

    # "none" and 0 are answers, not omissions: they say "no reference", "no
    # direction override", "the game's locked grid". Normalising them here is
    # what lets the caller store one answered brief and lets the generator tell
    # an answered-as-nothing apart from an unasked question.
    def _optional(value: object) -> object:
        if isinstance(value, str) and value.strip().lower() in ("none", ""):
            return None
        if isinstance(value, int) and not isinstance(value, bool) and value == 0:
            return None
        return value

    return {
        "assetKind": kind,
        "readyForPrototype": ready,
        "questions": questions,
        "prompt": prompt,
        "parameters": {
            "gridSize": _optional(grid_size),
            "paletteLock": palette_lock,
            "initAssetId": _optional(init_asset_id),
            "initImageStrength": _optional(init_image_strength),
            "direction": _optional(direction),
        },
        "artStyle": art_style or None,
        # Kept, not drawn. It is the answer to "where will this be used, and at
        # what readable scale" — a production fact the host and the reviewer
        # need, and one the generator would have rendered as scenery.
        "purpose": purpose or None,
        "exclusions": list(exclusions),
        "feedbackApplied": bool(preserved or changes),
        "promptCharacters": len(prompt) if prompt else 0,
    }


def compose(
    prompt: str,
    kind: AssetKind,
    controls: dict[str, object] | None = None,
    *,
    reference_leads: bool = False,
) -> PromptPlan:
    """Preserve intent, drop repeats, fit the budget, and add kind framing.

    Style wording is kept rather than stripped: the fields that would carry it
    are all ``(weakly guiding)`` in PixelLab's schema, so removing it from the
    description traded a strong signal for a weak one. The clauses a field also
    covers are reported in ``structuredClauses`` instead.

    ``controls`` is the structured style payload this same request will send.
    Passing it turns on conflict reporting: wording that names a *different*
    value of a field than the one being sent is recorded in
    ``controlConflicts``. Duplication between the two is expected and stays;
    contradiction is what nothing was watching for.

    ``reference_leads`` says a starting image is carrying the composition at a
    strength where it outranks the description (see
    ``REFERENCE_LEAD_STRENGTH``). The kind framing is then left off: it exists
    to say how the subject sits on the canvas, and a reference already says
    that in pixels. This is not true of a *pose* reference — keypoints are
    coordinates and carry no composition — so only a starting image sets it.
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

    # Budget before framing, so the framing a kind needs is never what gets
    # dropped to make room for a caller's prose.
    dropped: list[str] = []
    while kept and len(", ".join(kept)) > PROMPT_BUDGET:
        dropped.insert(0, kept.pop())

    # No exemption for a structured brief. The check used to skip the framing
    # whenever the prompt contained "Composition:" and "Required visual
    # structure:" — which is exactly what ``prepare`` writes, so following the
    # intake procedure was the one way to always lose the framing. It is now
    # per clause, and it is skipped only where the brief already asked for it.
    if not reference_leads:
        kept.extend(
            clause for clause in _FRAMING[kind] if not _already_covered(clause, kept)
        )

    conflicts: list[tuple[str, str, str]] = []
    if controls:
        vocabulary = _control_vocabulary()
        for clause in kept:
            named = vocabulary.get(clause.casefold())
            if named is None:
                continue
            field, value = named
            sent = controls.get(field)
            if isinstance(sent, str) and sent != value:
                conflicts.append((clause, field, sent))

    subject = ", ".join(kept) or normalized
    return PromptPlan(
        prompt=subject,
        original_characters=len(normalized),
        composed_characters=len(subject),
        structured_clauses=tuple(structured),
        removed_negations=tuple(negated),
        dropped_clauses=tuple(dropped),
        control_conflicts=tuple(conflicts),
        framing_suppressed=reference_leads,
    )


__all__ = ["PromptPlan", "compose", "normalize_items", "prepare"]
