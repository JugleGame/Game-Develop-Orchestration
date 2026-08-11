"""Check running MCP servers against the §03 contract.

Four people building four servers against one spec will drift — a `feature_id`
where the client sends `featureId`, a tool named `commit` instead of
`git_commit`. Those failures surface deep inside a pipeline run, minutes later,
as an unhelpful error. This catches them in two seconds.

Usage::

    python verify_contract.py                    # check all five servers
    python verify_contract.py git asset          # check only some

Exits non-zero on any mismatch, so it can gate CI.
"""

from __future__ import annotations

import asyncio
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from common import registry
from common.console import use_utf8_output

use_utf8_output()

# tool name -> required argument keys, straight from §03 and the orchestrator's
# client code. Argument *casing* is load-bearing: the clients send camelCase.
CONTRACT: dict[str, dict[str, set[str]]] = {
    "strategic": {
        "generate_game_design": {"prompt"},
        # 청사진 작성 이전의 사람 검토 게이트. ConceptGate 노드가 이 둘을 호출한다.
        "propose_concept": {"gameId", "idea"},
        "decide_concept": {"gameId", "decision"},
        # revise_spec(기존 개정)과 generate_game_design(전체 재기획) 사이 —
        # 기존 게임에 독립 spec 하나를 더한다. idea/spec 은 옵션(경로 B 는 spec).
        "add_spec": {"gameId"},
    },
    # 어댑터는 진단용 unity_bridge_status 를 추가로 노출한다 (계약 초과분은 무시)
    #
    # 필수 인자만 적는다. design_architecture 의 design, create_script 의
    # plannedPath 처럼 옵션인 것은 여기 없어도 되고, 없어야 계약을 깨지 않고
    # 늘릴 수 있다. 반대로 여기 적힌 이름은 casing 까지 그대로여야 한다 —
    # 오케스트레이터가 camelCase 로 보내므로 snake_case 면 -32602 로 튕긴다.
    "unity": {
        "design_architecture": {"gameId"},
        "create_script": {"featureId", "prompt"},
        "create_scene": {"featureId", "sceneName"},
        "create_prefab": {"gameId", "prefabName"},
        "compose_scene": {"gameId", "sceneName"},
        "bind_reference": {"gameId", "target", "field", "value"},
        "define_assemblies": {"gameId"},
        "import_asset": {"featureId", "assetPath"},
        "build_project": {"gameId"},
        "run_playmode_test": {"gameId"},
        "get_compile_errors": {"gameId"},
        "inspect_project_layout": set(),
    },
    "qa": {
        "establish_qa_policy": {"gameDesign"},
        "verify_prototype_structure": {"gameDesign", "build"},
        "compare_structure": {"gameDesign", "build"},
        "run_functional_verification": {"gameDesign", "build", "qaPolicy"},
        "generate_error_report": {"gameDesign", "build", "logs"},
    },
    "asset": {
        "generate_2d_sprite": {"featureId", "prompt"},
        "generate_ui_asset": {"featureId", "prompt"},
        "generate_3d_placeholder": {"featureId", "prompt"},
    },
    "git": {
        "git_init": {"repoName"},
        "git_branch": {"branch"},
        "git_pull": {"branch"},
        "git_commit": {"branch", "message"},
        "git_push": {"branch"},
        "git_tag": {"tag"},
        "git_status": set(),
    },
}

OK = "  OK  "
BAD = " FAIL "


async def check(name: str, url: str, expected: dict[str, set[str]]) -> list[str]:
    problems: list[str] = []
    async with streamable_http_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            listed = {tool.name: tool for tool in (await session.list_tools()).tools}

            print(f"\n[{name}] {info.server_info.name} @ {url} — {len(listed)} tools")

            for tool_name, required_args in sorted(expected.items()):
                tool = listed.get(tool_name)
                if tool is None:
                    problems.append(f"{name}.{tool_name}: MISSING")
                    print(f"  {BAD} {tool_name}  (없음)")
                    continue

                properties = set((tool.input_schema or {}).get("properties", {}))
                missing = required_args - properties
                if missing:
                    problems.append(f"{name}.{tool_name}: missing args {sorted(missing)}")
                    print(f"  {BAD} {tool_name}  인자 누락: {sorted(missing)}")
                    # A snake_case near-miss is the most common cause — say so.
                    for arg in sorted(missing):
                        snake = "".join(f"_{c.lower()}" if c.isupper() else c for c in arg)
                        if snake in properties:
                            print(f"         ↳ '{snake}' 가 있습니다. §03은 '{arg}' (camelCase) 입니다.")
                else:
                    print(f"  {OK} {tool_name}")

            if not tool_has_structured_output(listed, expected):
                print(
                    "  NOTE  일부 도구가 outputSchema를 선언하지 않았습니다 — 반환 타입을 "
                    "'dict[str, Any]'로 표기하면 structuredContent가 채워집니다."
                )
    return problems


def tool_has_structured_output(listed: dict, expected: dict[str, set[str]]) -> bool:
    return all(
        listed[name].output_schema is not None for name in expected if name in listed
    )


async def main(selected: list[str]) -> int:
    targets = selected or list(CONTRACT)
    unknown = [name for name in targets if name not in CONTRACT]
    if unknown:
        print(f"알 수 없는 서버: {unknown}. 가능한 값: {list(CONTRACT)}")
        return 2

    all_problems: list[str] = []
    for name in targets:
        expected = CONTRACT[name]
        url = registry.get(name).url()
        try:
            all_problems.extend(await check(name, url, expected))
        except Exception as exc:  # noqa: BLE001 — report, don't crash the run
            print(f"\n[{name}] {url}\n  {BAD} 연결 실패: {type(exc).__name__}: {exc}")
            all_problems.append(f"{name}: unreachable")

    print("\n" + "=" * 60)
    if all_problems:
        print(f"❌ 계약 불일치 {len(all_problems)}건")
        for problem in all_problems:
            print(f"   - {problem}")
        return 1
    print(f"✅ {len(targets)}개 서버 모두 §03 계약 충족")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
