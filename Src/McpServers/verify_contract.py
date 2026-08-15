"""Verify the static contract of all four Agent-first MCP servers."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVERS = {
    "research": ROOT / "strategic" / "server.py",
    "unity": ROOT / "unity" / "server.py",
    "asset": ROOT / "asset" / "server.py",
    "asset3d": ROOT / "asset3d" / "server.py",
}
REQUIRED_TOOLS = {
    "research": {
        "publish_game_design": {"gameId", "blueprint"},
        "research_idea": {"idea"},
        "revise_spec": {"specId", "spec"},
        "add_spec": {"gameId", "spec"},
        "research_status": set(),
    },
    "unity": {
        "design_architecture": {"gameId", "featurePrompts", "design"},
        "create_script": {"featureId", "contents"},
        "create_scene": {"featureId", "sceneName"},
        "build_project": {"gameId"},
        "run_playmode_test": {"gameId"},
        "inspect_project_layout": set(),
        "create_animation_clip": {"gameId", "clipName", "framePaths"},
        "create_animator_controller": {"gameId", "controllerName", "states"},
        "inspect_animator": {"gameId", "target"},
    },
    "asset": {
        "prepare_asset_prompt": {"assetKind"},
        "generate_2d_sprite": {"featureId", "prompt"},
        "generate_2d_variations": {"featureId", "prototypeAssetId", "prompts"},
        "generate_2d_animation": {"featureId", "firstFrameAssetId", "action"},
        "generate_ui_asset": {"featureId", "prompt"},
        "generate_tileset": {"featureId", "lowerDescription", "upperDescription"},
        "establish_art_style": {"gameId"},
        "inspect_asset": {"assetId"},
        "list_assets": {"gameId"},
        "review_asset": {"assetId", "approved"},
    },
    "asset3d": {
        "compose_3d_asset_prompts": {"assetSpec"},
        "validate_3d_asset_prompts": {
            "assetSpec",
            "generationPrompt",
            "referenceSearchPrompt",
        },
        "prepare_3d_asset_request": {"featureId", "assetSpec"},
        "submit_3d_asset_generation": {"featureId", "assetSpec"},
        "refine_3d_asset_generation": {"taskId"},
        "get_3d_asset_generation": {"taskId"},
        "cancel_3d_asset_generation": {"taskId"},
    },
}
FORBIDDEN_IMPORTS = {"anthropic", "openai", "fastapi", "langgraph", "git"}


def is_tool(node: ast.AsyncFunctionDef | ast.FunctionDef) -> bool:
    for decorator in node.decorator_list:
        call = decorator if isinstance(decorator, ast.Call) else None
        target = call.func if call else decorator
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "mcp"
            and target.attr == "tool"
        ):
            return True
    return False


def tool_arguments(tree: ast.Module) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) or not is_tool(node):
            continue
        positional = [*node.args.posonlyargs, *node.args.args]
        required_count = len(positional) - len(node.args.defaults)
        required = {item.arg for item in positional[:required_count]} | {
            item.arg for item, default in zip(node.args.kwonlyargs, node.args.kw_defaults) if default is None
        }
        result[node.name] = required - {"ctx"}
    return result


def imported_roots(tree: ast.Module) -> set[str]:
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def main() -> int:
    failures: list[str] = []
    for server_name, path in SERVERS.items():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        tools = tool_arguments(tree)
        for name, required in REQUIRED_TOOLS[server_name].items():
            if name not in tools:
                failures.append(f"{server_name}: 도구 없음: {name}")
            elif not required <= tools[name]:
                failures.append(
                    f"{server_name}.{name}: 필수 인자 불일치 "
                    f"(기대={sorted(required)}, 실제={sorted(tools[name])})"
                )
        forbidden = imported_roots(tree) & FORBIDDEN_IMPORTS
        if forbidden:
            failures.append(f"{server_name}: 금지 의존성 import: {sorted(forbidden)}")
        print(f"OK {server_name}: {len(tools)} tools")

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
    for dependency in FORBIDDEN_IMPORTS:
        if f'"{dependency}' in pyproject:
            failures.append(f"pyproject: 금지 의존성: {dependency}")

    if failures:
        print("\n계약 검증 실패:", file=sys.stderr)
        for failure in failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print("\nOK Agent-first 4서버 계약")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
