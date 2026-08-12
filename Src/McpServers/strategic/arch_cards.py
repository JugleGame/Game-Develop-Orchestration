"""Carry cited architecture-card guidance into implementation specs unchanged.

The research database owns Markdown parsing and stores sections under stable,
language-neutral keys. This module only converts those section bodies into the
shape consumed by specs and agent prompts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Agent-facing titles defined by the English research-card schema.
SECTION_BUILD_STEPS = "Unity Implementation Steps"
SECTION_ANTI_PATTERNS = "Anti-patterns"
SECTION_VERIFICATION = "Verification"

# Stable lookup keys from the research repository's card_schema.SECTIONS.
KEY_BUILD_STEPS = "unity_procedure"
KEY_ANTI_PATTERNS = "antipatterns"
KEY_VERIFICATION = "verification"

# Output order is intentional.
GUIDANCE_SECTIONS: list[tuple[str, str]] = [
    (KEY_BUILD_STEPS, SECTION_BUILD_STEPS),
    (KEY_ANTI_PATTERNS, SECTION_ANTI_PATTERNS),
    (KEY_VERIFICATION, SECTION_VERIFICATION),
]

ARCH_ID_PATTERN = re.compile(r"ARCH-\d{3}")

_ITEM_START = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def is_arch_card(card_id: str) -> bool:
    """Return whether this is a canonical architecture-card ID."""

    return bool(ARCH_ID_PATTERN.fullmatch(card_id))


def arch_ids(card_ids: list[str]) -> list[str]:
    """Keep architecture-card IDs in citation order and remove duplicates."""

    seen: set[str] = set()
    picked: list[str] = []
    for card_id in card_ids:
        if is_arch_card(card_id) and card_id not in seen:
            seen.add(card_id)
            picked.append(card_id)
    return picked


def section_items(text: str) -> list[str]:
    """Convert a Markdown list to items, joining continuation lines."""

    items: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        if _ITEM_START.match(line):
            items.append(_ITEM_START.sub("", line).strip())
        elif items:
            items[-1] = f"{items[-1]} {line.strip()}"
    return items


@dataclass(frozen=True)
class ArchGuidance:
    """Verbatim implementation guidance from one architecture card."""

    card_id: str
    title: str
    build_steps: list[str]
    anti_patterns: list[str]
    verification: list[str]

    @property
    def empty_sections(self) -> list[str]:
        """Return the agent-facing titles of missing source sections."""

        return [
            title
            for (_key, title), items in zip(
                GUIDANCE_SECTIONS,
                (self.build_steps, self.anti_patterns, self.verification),
            )
            if not items
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "cardId": self.card_id,
            "title": self.title,
            "buildSteps": self.build_steps,
            "antiPatterns": self.anti_patterns,
            "verification": self.verification,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArchGuidance:
        return cls(
            card_id=data["cardId"],
            title=data.get("title", ""),
            build_steps=list(data.get("buildSteps") or []),
            anti_patterns=list(data.get("antiPatterns") or []),
            verification=list(data.get("verification") or []),
        )

    def to_markdown(self) -> str:
        """Render the same guidance for specs and implementation prompts."""

        lines = [f"### {self.card_id} {self.title}".rstrip()]
        for heading, items, ordered in (
            (SECTION_BUILD_STEPS, self.build_steps, True),
            (SECTION_ANTI_PATTERNS, self.anti_patterns, False),
            (SECTION_VERIFICATION, self.verification, False),
        ):
            lines.append(f"#### {heading}")
            if not items:
                lines.append("- (empty source section)")
            elif ordered:
                lines += [f"{i}. {item}" for i, item in enumerate(items, 1)]
            else:
                lines += [f"- {item}" for item in items]
        return "\n".join(lines)


def guidance_from_sections(card_id: str, title: str, sections: dict[str, str]) -> ArchGuidance:
    """Build guidance from ``card_sections`` keyed by stable schema keys."""

    return ArchGuidance(
        card_id=card_id,
        title=title,
        build_steps=section_items(sections.get(KEY_BUILD_STEPS, "")),
        anti_patterns=section_items(sections.get(KEY_ANTI_PATTERNS, "")),
        verification=section_items(sections.get(KEY_VERIFICATION, "")),
    )
