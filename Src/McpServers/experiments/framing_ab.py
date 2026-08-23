"""Round 6: does the kind framing earn its characters? (issue #92)

``prompting._FRAMING`` appends 35-70 characters to every provider description,
and nothing has ever measured whether that helps. ``docs/contracts.md`` records
why the framing is applied *consistently* (commit 78e4eeb), not that applying
it improves a result.

Same seed, same canvas, same structured style. The only difference between the
two arms is whether the framing is appended.

Spends 4 PixelLab generations (2 kinds x 2 arms), one per image.

    .venv\\Scripts\\python.exe Src/McpServers/experiments/framing_ab.py
"""

import json
import os
import pathlib
import sys

sys.path.insert(0, "Src/McpServers")
os.environ.pop("GDAI_SKIP_DOTENV", None)
import common  # noqa: F401,E402  (applies the repo .env)
from asset import pixellab_client, prompting  # noqa: E402

OUT = pathlib.Path("var/assets/experiments/round-6-framing")

STYLE = {
    "outline": "single color black outline",
    "shading": "flat shading",
    "detail": "medium detail",
    "view": "high top-down",
    "direction": "south",
}

# Fixed seeds: str.__hash__ is randomised per process, so hashing the kind would
# make a re-run a different experiment rather than a repeat of this one.
CASES = (
    (
        "character",
        "a young female knight, red cape, standing idle, sword in right hand",
        64,
        64,
        20260823,
    ),
    ("prop", "a brass lantern, warm glass panel, iron handle", 64, 64, 20260824),
)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    report = []
    for kind, subject, width, height, seed in CASES:
        for arm in ("with-framing", "without-framing"):
            plan = prompting.compose(
                subject, kind, STYLE, reference_leads=(arm == "without-framing")
            )
            image, usage = pixellab_client.generate_image(
                prompt=plan.prompt, width=width, height=height, seed=seed, **STYLE
            )
            path = OUT / f"{kind}__{arm}.png"
            image.save(path)
            alpha = image.convert("RGBA").split()[-1]
            coverage = sum(1 for value in alpha.getdata() if value > 0) / (width * height)
            report.append(
                {
                    "kind": kind,
                    "arm": arm,
                    "seed": seed,
                    "prompt": plan.prompt,
                    "chars": plan.composed_characters,
                    "alphaCoverage": round(coverage, 3),
                    "usage": usage,
                    "path": str(path),
                }
            )
            print(f"{kind:10} {arm:16} {plan.composed_characters:>3}c  alpha={coverage:.3f}")

    (OUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    spent = sum(float(row["usage"].get("generations") or 0) for row in report)
    print(f"\nwrote {OUT / 'report.json'}\ngenerations spent: {spent}")


if __name__ == "__main__":
    main()
