"""Per-project art style: one palette per game, derived deterministically.

Style consistency means two different things and this module delivers both:

* **Per-game identity.** The palette is a pure function of ``game_id`` + the
  design document's ``art_style`` string. The same game always yields the same
  palette, on any machine, with no shared state. The resolved style is written
  to disk on first use so a later change to this module cannot silently re-skin
  a half-built game.

* **Concept correctness.** A palette anchored only to one arbitrary base hue
  produces cyan grass and pink stone — internally consistent but wrong. So
  world materials (grass, water, wood, …) are colour-anchored to what they
  actually are, then pulled toward the game's look by that game's
  saturation/lightness profile and a small per-game hue offset. Grass is always
  green; *this* game's green is its own.

Material colours are computed on demand from ``seed`` + ``art_style``, both of
which are persisted, so adding materials never invalidates a stored style.json.
"""

from __future__ import annotations

import colorsys
import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

RGB = tuple[int, int, int]

# Keys every renderer may rely on being present.
PALETTE_ROLES = (
    "background",
    "terrain_base",
    "terrain_accent",
    "prop_primary",
    "prop_secondary",
    "character_primary",
    "character_secondary",
    "outline",
    "ui_surface",
    "ui_text",
    "accent",
)

# Recognised art-style keywords → (saturation, lightness, outline darkness).
# Anything unrecognised falls back to the balanced "pixel art" profile.
_STYLE_PROFILES: dict[str, tuple[float, float, float]] = {
    "pixel": (0.62, 0.55, 0.18),
    "pixel art": (0.62, 0.55, 0.18),
    "cartoon": (0.78, 0.62, 0.22),
    "pastel": (0.38, 0.74, 0.35),
    "noir": (0.10, 0.42, 0.10),
    "vibrant": (0.88, 0.58, 0.20),
    "muted": (0.30, 0.50, 0.25),
    "dark fantasy": (0.45, 0.34, 0.10),
}

# Colour words that take the colour *out*, whatever the rendering style is.
# Kept apart from ``_STYLE_PROFILES`` because they combine with it rather than
# replace it: "monochrome silhouette 2D pixel art" is pixel art's ramp with no
# hue left in it. Folding them into that one table matched "pixel" first and
# handed a monochrome game pixel art's 0.62 saturation — which is how
# ``establish_art_style`` answered a monochrome brief with #45d35d (measured
# 2026-07-30).
_ACHROMATIC_KEYWORDS = (
    "monochrome",
    "grayscale",
    "greyscale",
    "black and white",
    "black-and-white",
    "흑백",
    "모노크롬",
)

# Canonical hue (0-1) and per-material saturation/lightness multipliers.
# The hue is what makes grass green rather than "whatever this game's base hue
# happens to be"; the multipliers keep stone desaturated and snow bright
# regardless of the art style in force.
#            hue,   sat x, light x
_MATERIALS: dict[str, tuple[float, float, float]] = {
    "grass": (100 / 360, 1.00, 1.00),
    "foliage": (108 / 360, 1.05, 0.92),
    "water": (205 / 360, 1.05, 1.02),
    "sand": (42 / 360, 0.80, 1.28),
    "dirt": (28 / 360, 0.75, 0.80),
    "wood": (25 / 360, 0.80, 0.70),
    "stone": (215 / 360, 0.22, 0.98),
    "metal": (210 / 360, 0.18, 1.12),
    "snow": (200 / 360, 0.14, 1.55),
    "lava": (12 / 360, 1.25, 0.95),
    # Living-subject materials. Reachable only through ``character_palette``:
    # ``render._MATERIAL_KEYWORDS`` names none of them, so a tile or prop can
    # never be resolved to one.
    "skin": (25 / 360, 0.55, 1.35),
    "cloth": (330 / 360, 0.70, 0.95),
}

DEFAULT_MATERIAL = "grass"

# How far a game may shift a canonical material hue, in turns (±10°). Small
# enough that grass stays unmistakably grass, large enough that two games do
# not look identical.
_MATERIAL_HUE_JITTER = 10 / 360


@dataclass(frozen=True)
class ArtStyle:
    """The resolved, frozen look of one game."""

    game_id: str
    art_style: str
    seed: int
    palette: dict[str, str]  # role -> "#rrggbb"
    pixel_grid: int  # sprites are drawn on this grid then scaled up
    outline: bool
    # Defaulted, not required: a style.json written before this field existed
    # still loads (``ArtStyle(**stored)`` falls back to "side"), so this
    # doesn't invalidate a game's frozen style the way a required field would.
    camera_view: str = "side"
    # PixelLab's structured detail/shading controls. They belong to the game,
    # not to one call: a request-level knob would let two assets in the same
    # game disagree about how many tones a surface has, which is the same
    # failure ``camera_view`` is locked to avoid. Defaults reproduce what the
    # server sent before these fields existed.
    detail: str = "medium detail"
    shading: str = "medium shading"
    # Isometric is its own boolean in the schema, not a ``CameraView`` value.
    # It used to be folded into ``camera_view`` as "high top-down", which is a
    # different projection: top-down looks straight down a vertical axis,
    # isometric looks along a diagonal one. A game asking for isometric got
    # top-down and no field ever said otherwise.
    isometric: bool = False
    # Which way the subject faces, locked per game like the view. PixelLab has
    # a field for this (``Direction``); before this existed the only way to ask
    # was prose in the description, competing with the subject for attention.
    direction: str = "east"

    def rgb(self, role: str) -> RGB:
        return _unhex(self.palette[role])

    # -- material colours -------------------------------------------------
    #
    # Derived rather than stored: a style.json written before a material
    # existed still resolves it, so the persistence guarantee above holds
    # without a schema migration.

    def _profile(self) -> tuple[float, float, float]:
        return _profile(self.art_style)

    def _material_hue(self, material: str) -> float:
        hue, _, _ = _MATERIALS.get(material, _MATERIALS[DEFAULT_MATERIAL])
        # Deterministic per (game, material): stable across processes.
        digest = hashlib.sha256(f"{self.seed}|material|{material}".encode()).digest()
        offset = (int.from_bytes(digest[:4], "big") / 0xFFFFFFFF) * 2 - 1  # -1..1
        return (hue + offset * _MATERIAL_HUE_JITTER) % 1.0

    def material_ramp(self, material: str) -> dict[str, RGB]:
        """Four-step shading ramp for one material.

        Pixel art reads as deliberate when a surface uses a small ordered ramp
        (shadow → base → light → highlight) instead of one flat fill, so every
        renderer draws from this rather than picking colours ad hoc.
        """

        saturation, lightness, _ = self._profile()
        _, sat_mul, light_mul = _MATERIALS.get(material, _MATERIALS[DEFAULT_MATERIAL])
        hue = self._material_hue(material)
        sat = _clamp(saturation * sat_mul, 0.0, 1.0)
        light = _clamp(lightness * light_mul, 0.06, 0.94)

        return {
            "shadow": _hls(hue, _clamp(light - 0.16, 0.04, 0.94), _sat_bump(sat, 0.05)),
            "base": _hls(hue, light, sat),
            "light": _hls(hue, _clamp(light + 0.11, 0.04, 0.96), _sat_bump(sat, -0.04)),
            "highlight": _hls(hue, _clamp(light + 0.22, 0.04, 0.98), _sat_bump(sat, -0.10)),
        }

    def outline_for(self, material: str) -> RGB:
        """A dark outline that carries the material's hue.

        A single neutral outline colour across every sprite is what makes
        procedural art look assembled from unrelated parts; tinting the outline
        toward its own material keeps each sprite coherent while the shared
        darkness keeps the whole set coherent.
        """

        saturation, _, outline_l = self._profile()
        hue = self._material_hue(material)
        return _hls(hue, _clamp(outline_l, 0.04, 0.30), _clamp(saturation * 0.45, 0, 1))

    # How many swatches a character palette holds. Tiles and props take five,
    # which is what made the locked ramp read as "too green, no character" on a
    # living subject: skin, cloth, and metal cannot share four steps of one
    # hue. ``color_image`` is a PNG with one pixel per colour, so the count is
    # a design decision, not a provider limit.
    CHARACTER_SWATCHES = 12

    def character_palette(self) -> list[RGB]:
        """This game's colours, wide enough for a living subject.

        The identity ramp still leads — the first four entries are the same
        ``character_ramp`` a sprite was always drawn from — but skin, metal,
        and leather follow so the model has somewhere to put a face, a blade,
        and a strap without borrowing the cloth hue for all three.

        Every entry is derived from ``seed`` and ``art_style``, both of which
        are persisted, so this needs no new stored field and a style.json
        written before it existed still resolves.
        """

        cloth = self.character_ramp()
        skin = self.material_ramp("skin")
        metal = self.material_ramp("metal")
        leather = self.material_ramp("wood")
        ordered = [
            cloth["shadow"],
            cloth["base"],
            cloth["light"],
            cloth["highlight"],
            skin["shadow"],
            skin["base"],
            skin["light"],
            metal["base"],
            metal["highlight"],
            leather["shadow"],
            leather["base"],
            self.rgb("outline"),
        ]
        # A monochrome game collapses several of these onto the same grey.
        # Duplicated swatches say nothing, so they are dropped rather than
        # padding the count for its own sake.
        return list(dict.fromkeys(ordered))

    def character_ramp(self) -> dict[str, RGB]:
        """Character ramp built from the game's own identity colours.

        Characters are deliberately *not* material-anchored: they are the one
        element that should read as this game's, and the palette's
        character_primary is already the complement of the terrain hue, which
        is what keeps the character legible against the world.
        """

        saturation, lightness, _ = self._profile()
        base = _unhex(self.palette["character_primary"])
        hue, light, sat = colorsys.rgb_to_hls(*(channel / 255 for channel in base))
        sat = _clamp(max(sat, saturation * 0.9), 0.0, 1.0)
        light = _clamp(max(light, lightness * 0.8), 0.10, 0.86)
        return {
            "shadow": _hls(hue, _clamp(light - 0.17, 0.04, 0.94), _sat_bump(sat, 0.05)),
            "base": _hls(hue, light, sat),
            "light": _hls(hue, _clamp(light + 0.12, 0.04, 0.96), _sat_bump(sat, -0.05)),
            "highlight": _hls(hue, _clamp(light + 0.24, 0.04, 0.98), _sat_bump(sat, -0.12)),
        }


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _sat_bump(saturation: float, delta: float) -> float:
    """Shift a ramp step's saturation — except there is nothing to shift at zero.

    A shadow step is a little more saturated than its base, which is right up
    until the palette has no colour in it: a +0.05 bump is enough to put a tint
    back into a monochrome game's shading ramp.
    """

    if saturation <= 0.0:
        return 0.0
    return _clamp(saturation + delta, 0.0, 1.0)


def _unhex(value: str) -> RGB:
    value = value.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def _hls(hue: float, lightness: float, saturation: float) -> RGB:
    r, g, b = colorsys.hls_to_rgb(hue, lightness, saturation)
    return (
        max(0, min(255, round(r * 255))),
        max(0, min(255, round(g * 255))),
        max(0, min(255, round(b * 255))),
    )


def _seed_for(game_id: str, art_style: str) -> int:
    digest = hashlib.sha256(f"{game_id}|{art_style}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def _hex(rgb: tuple[float, float, float]) -> str:
    r, g, b = (max(0, min(255, round(channel * 255))) for channel in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


def _profile(art_style: str) -> tuple[float, float, float]:
    lowered = art_style.lower()
    profile = _STYLE_PROFILES["pixel art"]
    for keyword, candidate in _STYLE_PROFILES.items():
        if keyword in lowered:
            profile = candidate
            break

    if any(keyword in lowered for keyword in _ACHROMATIC_KEYWORDS):
        return (0.0, profile[1], profile[2])
    return profile


def _grid_for(art_style: str) -> int:
    """Logical drawing grid.

    32 rather than 16 for pixel art: a 16x16 grid leaves roughly 4 rows for a
    character's head, which cannot hold a recognisable face, and was the direct
    cause of the earlier "noise blob" output. 32 is still unmistakably pixel
    art and is a standard sprite size.
    """

    return 32 if "pixel" in art_style.lower() else 48


# Keyword → PixelLab CameraView enum ("side"/"low top-down"/"high top-down",
# confirmed v2/openapi.json). Locked per game alongside the palette — measured
# 2026-08-02: a game mixing views per asset call reads as broken, the same way
# mixing palettes per call did before ``color_image`` locked colour.
#
# "isometric" is deliberately *not* here. It is a separate boolean in the same
# request and naming it a camera view sent a diagonal-axis projection request
# as a straight-down one.
_VIEW_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("high top-down", ("top-down", "top down", "topdown", "탑뷰", "쿼터뷰")),
    (
        "side",
        ("side-scroll", "sidescroll", "side scroll", "platformer", "횡스크롤", "사이드스크롤"),
    ),
)

_ISOMETRIC_KEYWORDS = ("isometric", "아이소메트릭", "쿼터뷰", "quarter view")

# Keyword → PixelLab Direction enum. "east" is the default because a
# side-scroller's sprite faces the way it walks, and left-facing frames are
# produced by mirroring rather than by a second generation.
DIRECTIONS = (
    "north",
    "north-east",
    "east",
    "south-east",
    "south",
    "south-west",
    "west",
    "north-west",
)

_DIRECTION_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("south", ("facing camera", "front-facing", "front facing", "정면", "앞모습")),
    ("north", ("facing away", "back-facing", "back facing", "뒷모습")),
    ("west", ("facing left", "left-facing", "왼쪽", "좌향")),
    ("east", ("facing right", "right-facing", "오른쪽", "우향")),
)


def _isometric_for(art_style: str) -> bool:
    """Whether this game is drawn on a diagonal axis.

    "쿼터뷰" appears in both tables on purpose: Korean usage covers the
    isometric family, so such a game gets the boolean *and* the top-down view
    it previously got alone.
    """

    lowered = art_style.lower()
    return any(keyword in lowered for keyword in _ISOMETRIC_KEYWORDS)


def _direction_for(art_style: str) -> str:
    """The direction every asset in this game faces unless one asks otherwise."""

    lowered = art_style.lower()
    for direction, keywords in _DIRECTION_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return direction
    return "east"


def _view_for(art_style: str) -> str:
    """Camera view for every asset in this game. Defaults to "side".

    "side" is the safe default: it is what a side-scroller or a front-facing
    character sprite wants, and it is the only view this session validated
    (12문서 §10-7) — "low"/"high top-down" are wired for a caller that
    explicitly names a top-down genre in ``art_style``, but untested.
    """

    lowered = art_style.lower()
    for view, keywords in _VIEW_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return view
    # An isometric game looks down a diagonal axis, so "side" would contradict
    # the projection. This is the view such a game already got before
    # ``isometric`` became its own field; the boolean is added to it, not
    # instead of it.
    return "high top-down" if _isometric_for(art_style) else "side"


def derive(game_id: str, art_style: str) -> ArtStyle:
    """Compute the palette for a game. Pure — no I/O, no randomness."""

    seed = _seed_for(game_id, art_style)
    saturation, lightness, outline_l = _profile(art_style)

    base_hue = (seed % 360) / 360.0
    # Analogous neighbours ±30°, plus the complement for the accent.
    hues = {
        "background": (base_hue + 0.55) % 1.0,
        "terrain_base": base_hue,
        "terrain_accent": (base_hue + 0.083) % 1.0,
        "prop_primary": (base_hue - 0.083) % 1.0,
        "prop_secondary": (base_hue - 0.042) % 1.0,
        "character_primary": (base_hue + 0.5) % 1.0,
        "character_secondary": (base_hue + 0.55) % 1.0,
        "outline": base_hue,
        "ui_surface": (base_hue + 0.5) % 1.0,
        "ui_text": base_hue,
        "accent": (base_hue + 0.5) % 1.0,
    }
    lightness_by_role = {
        "background": min(0.92, lightness + 0.28),
        "terrain_base": lightness,
        "terrain_accent": max(0.08, lightness - 0.12),
        "prop_primary": max(0.08, lightness - 0.06),
        "prop_secondary": min(0.92, lightness + 0.10),
        "character_primary": lightness,
        "character_secondary": min(0.92, lightness + 0.18),
        "outline": outline_l,
        "ui_surface": min(0.94, lightness + 0.34),
        "ui_text": outline_l,
        "accent": min(0.80, lightness + 0.14),
    }
    saturation_by_role = {role: saturation for role in PALETTE_ROLES}
    saturation_by_role["outline"] = saturation * 0.4
    saturation_by_role["ui_text"] = saturation * 0.3
    # The accent is "the palette's colour, more so" — at zero saturation there
    # is no colour to push, and adding some would put a tinted accent in a
    # monochrome game's palette.
    saturation_by_role["accent"] = min(1.0, saturation + 0.15) if saturation else 0.0

    palette = {
        role: _hex(
            colorsys.hls_to_rgb(hues[role], lightness_by_role[role], saturation_by_role[role])
        )
        for role in PALETTE_ROLES
    }

    return ArtStyle(
        game_id=game_id,
        art_style=art_style,
        seed=seed,
        palette=palette,
        pixel_grid=_grid_for(art_style),
        outline=True,
        camera_view=_view_for(art_style),
        isometric=_isometric_for(art_style),
        direction=_direction_for(art_style),
    )


def load_or_create(
    root: Path,
    game_id: str,
    art_style: str,
    *,
    detail: str = "",
    shading: str = "",
) -> ArtStyle:
    """Return the game's frozen style, deriving and persisting it on first use.

    Once written, the stored palette wins: regenerating an asset months later
    must not produce a differently-coloured sprite because this module changed.
    """

    path = root / "styles" / f"{game_id}.json"
    if path.exists():
        stored = json.loads(path.read_text(encoding="utf-8"))
        return ArtStyle(**stored)

    style = derive(game_id, art_style)
    if detail or shading:
        # Only on first use: the branch above already returned for a game whose
        # look is frozen, so this cannot re-skin work in progress.
        style = replace(
            style, detail=detail or style.detail, shading=shading or style.shading
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(style), indent=2, ensure_ascii=False), encoding="utf-8")
    return style
