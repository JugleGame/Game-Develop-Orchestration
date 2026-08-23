"""Does the kind framing earn its characters? (issue #92)

``prompting._FRAMING`` appends 22-70 characters to every provider description,
and nothing had ever measured whether that helps. ``docs/contracts.md`` records
why the framing is applied *consistently* (commit 78e4eeb), not that applying it
improves a result.

Same seed, same canvas, same structured style. The only difference between the
two arms is whether the framing is appended.

The framing's own claims are what gets measured, using the server's existing
``quality.inspect`` rather than a metric invented here:

    "centered"              -> horizontal offset of the subject box from centre
    "fully visible"         -> the subject_may_be_clipped warning
    "single centered ..."   -> subject_too_small / transparent_background_missing
    "edge-to-edge tile"     -> tile_has_transparent_gaps
    "repeatable boundaries" -> horizontalSeamMeanRgbDelta

## Rounds

* **Round 6** (4 generations, one seed per kind) read a fidelity loss into the
  framing arm — a lost face, lost glass panels. It did not replicate. One sample
  per arm cannot separate an effect from a draw, and that reading is withdrawn.
* **Round 7** (16 generations, 4 kinds x 2 seeds) settled ``tile``: the framing
  roughly doubles edge coverage at both seeds, and without it the result is
  scattered debris on transparency rather than a tile. ``character``, ``prop``,
  and ``icon`` showed nothing either way at n=2 — which is not evidence of
  harmlessness, just absence of signal at a thin sample.
* **Round 8** (this file, 24 generations) settles the three undecided kinds by
  taking them to six pairs each.

## Decision rule, fixed before the run

Stated up front so the result cannot be explained after the fact — the mistake
round 6 made.

For ``character``, ``prop``, and ``icon`` over all six pairs each:

1. **Flags.** Count technical failures and warnings per arm. Framing has to
   produce *strictly fewer* to have earned anything.
2. **Centring.** Count pairs where the framing arm's ``offsetFromCentre`` is
   lower against pairs where it is higher. A split near half is noise.

Framing is kept for a kind only if (1) favours it, or (2) favours it in at least
two thirds of that kind's pairs. Otherwise the framing has no measured effect
for that kind and its characters go back to the subject.

``tile`` is not re-run: round 7 settled it and re-litigating a settled arm is
just more spend.

Spends 24 PixelLab generations (3 kinds x 4 seeds x 2 arms), one per image.

    .venv\\Scripts\\python.exe Src/McpServers/experiments/framing_ab.py
"""

import json
import os
import pathlib
import sys
from collections import defaultdict

sys.path.insert(0, "Src/McpServers")
os.environ.pop("GDAI_SKIP_DOTENV", None)
import common  # noqa: F401,E402  (applies the repo .env)
from asset import pixellab_client, prompting, quality  # noqa: E402

OUT = pathlib.Path("var/assets/experiments/round-8-framing")
PRIOR = pathlib.Path("var/assets/experiments/round-7-framing/report.json")
CANVAS = 64

STYLE = {
    "outline": "single color black outline",
    "shading": "flat shading",
    "detail": "medium detail",
    "view": "high top-down",
    "direction": "south",
}

# Fixed seeds: str.__hash__ is randomised per process, so hashing the kind would
# make a re-run a different experiment rather than a repeat of this one. These
# four are new; round 7's two are merged in at analysis time for six per kind.
SEEDS = (20261012, 20261103, 20261207, 20260114)

SUBJECTS = {
    "character": "a young female knight, red cape, standing idle, sword in right hand",
    "prop": "a brass lantern, warm glass panel, iron handle",
    "icon": "a health potion bottle, red liquid, cork stopper",
}


def _offset_from_centre(metrics: dict) -> float | None:
    """How far the subject's own centre sits from the canvas centre, 0-1.

    This is what "centered" asks for, so it is what the framing has to move if
    the clause is doing anything.
    """

    box = metrics.get("boundingBox")
    if not box:
        return None
    left, _, right, _ = box
    return round(abs((left + right) / 2 - metrics["width"] / 2) / metrics["width"], 4)


def generate() -> list[dict]:
    OUT.mkdir(parents=True, exist_ok=True)
    report = []
    for kind, subject in SUBJECTS.items():
        for seed in SEEDS:
            for arm in ("with-framing", "without-framing"):
                plan = prompting.compose(
                    subject, kind, STYLE, reference_leads=(arm == "without-framing")
                )
                image, usage = pixellab_client.generate_image(
                    prompt=plan.prompt, width=CANVAS, height=CANVAS, seed=seed, **STYLE
                )
                path = OUT / f"{kind}__{seed}__{arm}.png"
                image.save(path)

                inspection = quality.inspect(image, kind, (CANVAS, CANVAS))
                metrics = inspection["metrics"]
                report.append(
                    {
                        "kind": kind,
                        "seed": seed,
                        "arm": arm,
                        "prompt": plan.prompt,
                        "chars": plan.composed_characters,
                        "alphaCoverage": metrics["alphaCoverage"],
                        "offsetFromCentre": _offset_from_centre(metrics),
                        "failures": inspection["failures"],
                        "warnings": inspection["warnings"],
                        "usage": usage,
                        "path": str(path),
                    }
                )
                print(
                    f"{kind:10} {seed:>9} {arm:16} {plan.composed_characters:>5}c "
                    f"cover={metrics['alphaCoverage']:.3f} "
                    f"offset={_offset_from_centre(metrics)}  "
                    f"{','.join(inspection['failures'] + inspection['warnings']) or '-'}"
                )
    (OUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def analyse(rows: list[dict]) -> None:
    """Apply the decision rule above to every pair, and say what it decides."""

    pairs: dict[tuple[str, int], dict[str, dict]] = defaultdict(dict)
    for row in rows:
        if row["kind"] in SUBJECTS:
            pairs[(row["kind"], row["seed"])][row["arm"]] = row

    print(f"\n{'kind':10} {'pairs':>5} {'flags with':>10} {'flags without':>13} "
          f"{'centre with':>12} {'centre without':>15}  verdict")
    for kind in SUBJECTS:
        complete = [p for (k, _), p in pairs.items() if k == kind and len(p) == 2]
        flags_with = sum(
            len(p["with-framing"]["failures"]) + len(p["with-framing"]["warnings"])
            for p in complete
        )
        flags_without = sum(
            len(p["without-framing"]["failures"]) + len(p["without-framing"]["warnings"])
            for p in complete
        )
        better = sum(
            1
            for p in complete
            if (p["with-framing"]["offsetFromCentre"] or 0)
            < (p["without-framing"]["offsetFromCentre"] or 0)
        )
        worse = sum(
            1
            for p in complete
            if (p["with-framing"]["offsetFromCentre"] or 0)
            > (p["without-framing"]["offsetFromCentre"] or 0)
        )
        keep = flags_with < flags_without or better >= (2 * len(complete) + 2) // 3
        print(
            f"{kind:10} {len(complete):>5} {flags_with:>10} {flags_without:>13} "
            f"{better:>12} {worse:>15}  {'KEEP' if keep else 'NO MEASURED EFFECT'}"
        )
    print("\n(ties on centring count as neither better nor worse)")


def main() -> None:
    rows = generate()
    if PRIOR.is_file():
        rows += json.loads(PRIOR.read_text(encoding="utf-8"))
        print(f"\nmerged prior round: {PRIOR}")
    analyse(rows)
    spent = sum(
        float(row["usage"].get("generations") or 0)
        for row in rows
        if row["path"].replace("\\", "/").startswith(str(OUT).replace("\\", "/"))
    )
    print(f"\ngenerations spent this round: {spent}")


if __name__ == "__main__":
    main()
