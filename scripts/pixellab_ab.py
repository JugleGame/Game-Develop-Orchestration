"""Generate the same prompt through both PixelLab image paths, side by side.

This exists because the only honest way to decide between the two paths is to
look at what they draw. Both calls use the **same seed and the same composed
prompt**, so the images differ by the path and its controls, not by luck:

* ``pixflux`` — ``/create-image-pixflux``. No style reference, no
  ``negative_description`` (the field is ``(Deprecated)`` there), no
  ``coverage_percentage``. Up to 400px per side.
* ``bitforge`` — ``/create-image-bitforge``. Takes one ``style_image`` with a
  ``style_strength``, a live ``negative_description``, and
  ``coverage_percentage``. Stops at 200px per side.

Each run writes one directory under ``ASSET_ROOT/assets/experiments/<name>/``
holding every generated PNG and a ``runs.json`` recording, per image, the exact
request that produced it. The payloads are the point: an image with no record
of what was asked for cannot settle an argument later.

**This spends PixelLab credits** — one generation per image. It prints the
plan and the total first and asks for confirmation unless ``--yes`` is passed.

Usage::

    python scripts/pixellab_ab.py --name round-1 \\
        --prompt "a lone knight on a hill, no city" \\
        --kind character \\
        --style-image var/assets/<game>/<feature>/<asset>.png \\
        --style-strength 30 --style-strength 50 --style-strength 80
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Src" / "McpServers"))

from PIL import Image  # noqa: E402

from asset import pixellab_client, prompting, render  # noqa: E402
from asset.server import (  # noqa: E402
    _KIND_SIZE_RATIO,
    _PIXELLAB_UPSCALE,
    _pixellab_palette,
    _pixellab_style_params,
)
from asset.style import derive  # noqa: E402


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="experiment directory name")
    parser.add_argument("--prompt", required=True, action="append", help="repeatable")
    parser.add_argument("--kind", default="character", choices=sorted(_KIND_SIZE_RATIO))
    parser.add_argument("--game-id", default="ab-experiment")
    parser.add_argument("--art-style", default="pixel art")
    parser.add_argument("--grid", type=int, default=32)
    parser.add_argument(
        "--style-image",
        type=Path,
        help="approved sprite to use as the bitforge style reference",
    )
    parser.add_argument(
        "--style-strength",
        type=int,
        action="append",
        help="repeatable; one bitforge image per value (default: 50)",
    )
    parser.add_argument("--coverage", type=float, default=None)
    parser.add_argument(
        "--skip-pixflux", action="store_true", help="only run the bitforge arm"
    )
    parser.add_argument("--yes", action="store_true", help="skip the cost confirmation")
    parser.add_argument("--out", type=Path, default=None, help="override ASSET_ROOT")
    return parser.parse_args(argv)


def _plan(args: argparse.Namespace) -> list[dict[str, object]]:
    """Every image this run would generate, before anything is spent."""

    strengths = args.style_strength or [pixellab_client.BITFORGE_BALANCED_STYLE_STRENGTH]
    width = int(args.grid * _KIND_SIZE_RATIO[args.kind][0])
    height = int(args.grid * _KIND_SIZE_RATIO[args.kind][1])
    runs: list[dict[str, object]] = []
    for index, prompt in enumerate(args.prompt):
        if not args.skip_pixflux:
            runs.append(
                {
                    "arm": "pixflux",
                    "promptIndex": index,
                    "prompt": prompt,
                    "width": width,
                    "height": height,
                }
            )
        if args.style_image:
            for strength in strengths:
                runs.append(
                    {
                        "arm": "bitforge",
                        "promptIndex": index,
                        "prompt": prompt,
                        "width": width,
                        "height": height,
                        "styleStrength": strength,
                    }
                )
    return runs


def _confirm(runs: list[dict[str, object]], *, assume_yes: bool) -> bool:
    print(f"{len(runs)} generations, one PixelLab credit each:")
    for run in runs:
        extra = f" style_strength={run['styleStrength']}" if "styleStrength" in run else ""
        print(f"  [{run['arm']}] {run['width']}x{run['height']} {run['prompt']!r}{extra}")
    if assume_yes:
        return True
    answer = input("Spend these credits? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if not pixellab_client.is_configured():
        print("PIXELLAB_API_KEY is not set", file=sys.stderr)
        return 2

    style_image: Image.Image | None = None
    if args.style_image:
        if not args.style_image.is_file():
            print(f"style image not found: {args.style_image}", file=sys.stderr)
            return 2
        with Image.open(args.style_image) as opened:
            style_image = opened.convert("RGBA").copy()
    elif not args.skip_pixflux:
        print(
            "no --style-image: only the pixflux arm would run, which is not a comparison",
            file=sys.stderr,
        )
        return 2

    runs = _plan(args)
    if not runs:
        print("nothing to generate", file=sys.stderr)
        return 2
    if not _confirm(runs, assume_yes=args.yes):
        print("cancelled; nothing was spent")
        return 1

    root = args.out or Path(os.getenv("ASSET_ROOT", "var/assets"))
    out_dir = root / "experiments" / args.name
    out_dir.mkdir(parents=True, exist_ok=True)

    style = derive(args.game_id, args.art_style)
    style_params = _pixellab_style_params(style, args.kind)
    records: list[dict[str, object]] = []

    for index, run in enumerate(runs):
        prompt = str(run["prompt"])
        plan = prompting.compose(prompt, args.kind)
        # One seed per prompt, shared by both arms: the arms must differ by the
        # path, not by the starting noise.
        seed = render.rng_for(style, f"ab:{run['promptIndex']}", prompt).getrandbits(32)
        palette = _pixellab_palette(style, args.kind, prompt)
        common = {
            "prompt": plan.prompt,
            "width": int(run["width"]),
            "height": int(run["height"]),
            "seed": seed,
            "forced_palette": palette,
            **style_params,
        }

        try:
            if run["arm"] == "bitforge":
                image, usage = pixellab_client.create_image_bitforge(
                    style_image=style_image,
                    style_strength=int(run["styleStrength"]),
                    negative_description=plan.negative_description,
                    coverage_percentage=args.coverage,
                    **common,
                )
            else:
                image, usage = pixellab_client.generate_image(**common)
        except pixellab_client.PixelLabUnavailable as exc:
            # Recorded, not raised: a failed arm is a result, and the images
            # already paid for must still be written out.
            records.append({**run, "seed": seed, "error": str(exc)})
            print(f"[{index}] {run['arm']} FAILED: {exc}", file=sys.stderr)
            continue

        image = image.resize(
            (image.width * _PIXELLAB_UPSCALE, image.height * _PIXELLAB_UPSCALE),
            Image.NEAREST,
        )
        suffix = f"_s{run['styleStrength']}" if "styleStrength" in run else ""
        name = f"{run['promptIndex']:02d}_{run['arm']}{suffix}.png"
        image.save(out_dir / name)
        records.append(
            {
                **run,
                "seed": seed,
                "file": name,
                "providerPrompt": plan.prompt,
                "negativeDescription": plan.negative_description,
                "forcedPalette": palette,
                "styleParams": style_params,
                "coveragePercentage": args.coverage,
                "styleImage": str(args.style_image) if args.style_image else None,
                "usage": usage,
            }
        )
        print(f"[{index}] {run['arm']} -> {name}")

    # Merge rather than overwrite: a second run of the same experiment (the
    # other arm, or a retry after a provider error) must not erase the record
    # of images the first run already paid for.
    index_path = out_dir / "runs.json"
    previous: list[dict[str, object]] = []
    if index_path.is_file():
        previous = json.loads(index_path.read_text(encoding="utf-8")).get("runs") or []
    fresh = {record.get("file") for record in records}
    records = [
        record
        for record in previous
        if record.get("file") and record["file"] not in fresh
    ] + records

    index_path.write_text(
        json.dumps(
            {
                "name": args.name,
                "generatedAt": datetime.now(timezone.utc).isoformat(),
                "gameId": args.game_id,
                "artStyle": args.art_style,
                "kind": args.kind,
                "grid": args.grid,
                "runs": records,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(f"\n{index_path}")
    return 0 if all("error" not in record for record in records) else 1


def _self_check() -> None:
    """The planning and confirmation logic, without spending anything."""

    args = _parse_args(
        [
            "--name",
            "t",
            "--prompt",
            "a knight",
            "--prompt",
            "a mage",
            "--kind",
            "character",
            "--style-image",
            "anywhere.png",
            "--style-strength",
            "30",
            "--style-strength",
            "80",
        ]
    )
    runs = _plan(args)
    # 2 prompts x (1 pixflux + 2 bitforge strengths)
    assert len(runs) == 6, runs
    assert [run["arm"] for run in runs[:3]] == ["pixflux", "bitforge", "bitforge"]
    assert all((run["width"], run["height"]) == (32, 64) for run in runs)

    skipped = _plan(_parse_args(["--name", "t", "--prompt", "a knight"]))
    assert [run["arm"] for run in skipped] == ["pixflux"], skipped
    print("self-check ok")


if __name__ == "__main__":
    if "--self-check" in sys.argv:
        _self_check()
    else:
        raise SystemExit(main())
