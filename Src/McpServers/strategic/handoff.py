"""Create immutable, reviewable planning hand-off packages for execution agents.

The database remains the source of truth while a design is being edited.  A
published design is exported as files only at the planning-to-execution
boundary so an execution agent can work without querying the planning store.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .specs import SpecDocument, dependency_order

_REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_ROOT = _REPOSITORY_ROOT / "var" / "handoffs"
_GAME_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


class HandoffError(ValueError):
    """The requested hand-off cannot safely be created."""


def handoff_root() -> Path:
    """Resolve the host-controlled output location.

    The default is disposable repository state.  An explicit ``HANDOFF_ROOT``
    supports a controlled shared hand-off volume; file names below it still use
    the strict game-ID validator, so a plan cannot escape the configured root.
    """

    return Path(os.getenv("HANDOFF_ROOT", str(_DEFAULT_ROOT))).resolve()


def _safe_game_id(game_id: str) -> str:
    if not _GAME_ID.fullmatch(game_id):
        raise HandoffError(
            "gameId must use 1-64 ASCII letters, digits, '.', '_' or '-' and cannot start with punctuation"
        )
    return game_id


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_checksum(path: Path, digest: str) -> None:
    path.write_text(f"{digest}  execution-manifest.json\n", encoding="ascii")


def verify_handoff(package: Path) -> dict[str, Any]:
    """Verify the manifest checksum and every execution input it describes."""

    root = package.resolve()
    manifest_path = root / "execution-manifest.json"
    checksum_path = root / "execution-manifest.sha256"
    if not manifest_path.is_file() or not checksum_path.is_file():
        raise HandoffError("handoff is missing execution-manifest.json or execution-manifest.sha256")
    expected = checksum_path.read_text(encoding="ascii").strip().split(maxsplit=1)
    if len(expected) != 2 or expected[1] != "execution-manifest.json":
        raise HandoffError("invalid execution-manifest.sha256 format")
    if _sha256(manifest_path) != expected[0]:
        raise HandoffError("execution manifest checksum does not match")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HandoffError(f"invalid execution manifest: {exc}") from exc

    checked: list[str] = []
    for item in manifest.get("files") or []:
        relative = Path(str(item.get("path") or ""))
        path = (root / relative).resolve()
        if not relative.parts or path.parent != root and root not in path.parents:
            raise HandoffError(f"unsafe manifest path: {relative}")
        if not path.is_file() or _sha256(path) != item.get("sha256"):
            raise HandoffError(f"handoff file checksum does not match: {relative}")
        checked.append(relative.as_posix())
    return {"packagePath": str(root), "verifiedFiles": checked, "manifestSha256": expected[0]}


def _readme(game_id: str, blueprint_version: int, specs: Iterable[SpecDocument]) -> str:
    items = "\n".join(f"- `{spec.spec_id}` — {spec.title}" for spec in specs)
    return f"""# Execution hand-off: {game_id}

This immutable package was exported from approved planning version {blueprint_version}.
Read `execution-manifest.json` first, verify every SHA-256 digest, then implement
features in dependency order. Do not treat this package as a permission to change game
rules; return rule changes to the planning team as feedback.

## Features

{items}

## Files

- `blueprint.json`: approved game-level intent and evidence metadata.
- `specs/*.md`: one implementation-ready feature specification per file.
- `execution-manifest.json`: ordered feature prompts, dependencies, and file digests.
"""


def export_handoff(
    *,
    game_id: str,
    blueprint: dict[str, Any],
    blueprint_version: int,
    specs: list[SpecDocument],
    feature_prompts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Write one versioned package without overwriting a prior execution input."""

    safe_id = _safe_game_id(game_id)
    if blueprint_version < 1:
        raise HandoffError("blueprintVersion must be at least 1")
    prompts_by_id = {str(prompt.get("feature_id", "")): prompt for prompt in feature_prompts}
    if len(specs) != len(feature_prompts) or set(spec.spec_id for spec in specs) != set(prompts_by_id):
        raise HandoffError("each specification must have exactly one matching feature prompt")
    try:
        ordered_specs = dependency_order(specs)
    except ValueError as exc:
        raise HandoffError(f"invalid dependency graph: {exc}") from exc

    root = handoff_root()
    target = root / safe_id / f"v{blueprint_version}"
    if target.exists():
        raise HandoffError(f"handoff already exists: {target}")

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{safe_id}-v{blueprint_version}-", dir=target.parent))
    try:
        specs_dir = temporary / "specs"
        specs_dir.mkdir()
        blueprint_path = temporary / "blueprint.json"
        _write_json(blueprint_path, blueprint)

        manifest_files: list[dict[str, str]] = [
            {"path": "blueprint.json", "sha256": _sha256(blueprint_path)}
        ]
        for spec in ordered_specs:
            filename = f"{spec.spec_id}.md"
            path = specs_dir / filename
            path.write_text(spec.to_markdown(), encoding="utf-8")
            manifest_files.append({"path": f"specs/{filename}", "sha256": _sha256(path)})

        readme_path = temporary / "README.md"
        readme_path.write_text(_readme(safe_id, blueprint_version, ordered_specs), encoding="utf-8")
        manifest_files.append({"path": "README.md", "sha256": _sha256(readme_path)})

        manifest = {
            "schemaVersion": 1,
            "gameId": safe_id,
            "blueprintVersion": blueprint_version,
            "featurePrompts": [prompts_by_id[spec.spec_id] for spec in ordered_specs],
            "files": manifest_files,
        }
        manifest_path = temporary / "execution-manifest.json"
        _write_json(manifest_path, manifest)
        manifest_digest = _sha256(manifest_path)
        _write_checksum(temporary / "execution-manifest.sha256", manifest_digest)

        # A package directory is published only after every file and digest is complete.
        temporary.replace(target)
    except Exception:
        if temporary.exists():
            for path in sorted(temporary.rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            temporary.rmdir()
        raise

    return {
        "handoffPath": str(target),
        "manifestPath": str(target / "execution-manifest.json"),
        "checksumPath": str(target / "execution-manifest.sha256"),
        "manifestSha256": manifest_digest,
        "blueprintVersion": blueprint_version,
        "specCount": len(ordered_specs),
    }
