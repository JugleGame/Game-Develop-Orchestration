"""Standalone happy-path stand-ins for Strategic/Unity/QA/Asset/GitMcpServer.

These 5 real MCP servers are separate components out of this repository's scope
(§01_DeveloperAI_Design §2). This script exists purely so a developer can see the
whole DeveloperAI pipeline — and the Web front-end that visualizes it — run
end-to-end locally without those components: every tool call succeeds on the
first try, QA always passes, and there are never any compile errors.

They are real MCP servers (JSON-RPC 2.0 over the Streamable HTTP transport),
built with the ``mcp`` SDK's ``FastMCP``, so the orchestrator's clients talk to
them through the same protocol they use in production — including the
``initialize`` handshake and ``tools/list`` discovery.

Usage::

    python scripts/mock_mcp_servers.py

Then point ``.env`` (or the shell env) at them — this matches the defaults in
``.env.example``, so no changes are needed if you're using those defaults:

    STRATEGIC_MCP_URL=http://localhost:9101/mcp
    UNITY_MCP_URL=http://localhost:9102/mcp
    QA_MCP_URL=http://localhost:9103/mcp
    ASSET_MCP_URL=http://localhost:9104/mcp
    GIT_MCP_URL=http://localhost:9105/mcp

Inspect them with any MCP client, e.g. ``npx @modelcontextprotocol/inspector``.
"""

from __future__ import annotations

import argparse
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from mcp.server.fastmcp import FastMCP

JsonDict = dict[str, Any]

# The real servers (``Src/McpServers/serve_all.py``) claim 9101-9105 too. An
# offset lets both run at once, which is what you want when the real strategic
# server is up but has no ANTHROPIC_API_KEY and you still need a full pipeline
# run to look at. Set it with ``--port-offset`` or ``MOCK_MCP_PORT_OFFSET``, then
# point the orchestrator at the shifted ports:
#
#     python scripts/mock_mcp_servers.py --port-offset 100
#     STRATEGIC_MCP_URL=http://127.0.0.1:9201/mcp ... python scripts/dev_server.py
PORT_OFFSET = int(os.getenv("MOCK_MCP_PORT_OFFSET", "0"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fake_usage(*, input_tokens: int, output_tokens: int) -> JsonDict:
    """Stand-in for the ``usage`` object a real LLM-backed tool server returns.

    Present so the orchestrator's cost accounting (``app/utils/usage.py``) is
    exercised on the mock path too — otherwise every mocked run reports a $0
    bill and a regression there would go unnoticed until production.
    """

    return {
        "model": "claude-opus-5",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


def build_strategic_server() -> FastMCP:
    mcp = FastMCP("StrategicMcpServer", host="127.0.0.1", port=9101 + PORT_OFFSET)

    @mcp.tool(description="Gather evidence for an idea and park it for human review.")
    def propose_concept(
        gameId: str, idea: str, supportLimit: int = 6, counterLimit: int = 3
    ) -> JsonDict:
        """ConceptPropose. No LLM in the real server either — this one is cheap
        for the same reason, so the mock stays close to the real shape."""

        return {
            "gameId": gameId,
            "version": 1,
            "status": "pending",
            "idea": idea,
            "searchMode": "vector+trigram",
            "supporting": [
                {"cardId": "GENRE-006", "title": "생존 샌드박스", "summary": "요약", "score": 0.61},
                {"cardId": "ELEM-018", "title": "로그라이크 무작위 업그레이드", "summary": "요약", "score": 0.55},
            ],
            # 하한선(0.45) 미달이면 실제 서버는 여기를 비우고 아래 note 를 채운다.
            "counterexamples": [],
            "counterexampleNote": "반례 조사 부족",
            "citableCardIds": ["ELEM-018", "GENRE-006"],
        }

    @mcp.tool(description="Record the human decision on a pending concept proposal.")
    def decide_concept(
        gameId: str, decision: str, note: str = "", editedIdea: str = ""
    ) -> JsonDict:
        return {
            "gameId": gameId,
            "version": 1,
            "status": {"approve": "approved", "revise": "revising"}.get(decision, "rejected"),
            "decision": decision,
            "idea": editedIdea or "",
            "note": note,
        }

    @mcp.tool(description="Produce a GameDesignDocument and feature list from a user prompt.")
    def generate_game_design(prompt: str) -> JsonDict:
        return {
            "gameDesign": {
                "game_id": str(uuid.uuid4()),
                "genre": "platformer",
                "core_mechanics": ["jump", "run", "collect"],
                "art_style": "pixel art",
                "target_platform": "PC",
                "structure_overview": f"Prototype generated from prompt: {prompt}",
                "created_at": _now(),
            },
            "featurePrompts": [
                {
                    "feature_id": "f-1",
                    "title": "Core movement",
                    "description": "Add left/right movement and a jump.",
                    "priority": "P0",
                    "dependencies": [],
                    # 자산을 이름 단위로 준다 — AssetGen 이 이걸 보고 2D/UI/3D 도구를
                    # 갈라 부른다 (05_계약_변경_제안서 §4.4). 세 종류를 일부러 섞어
                    # 두어 mock 한 판에 세 도구가 모두 호출되게 한다.
                    "assets_needed": ["플레이어 스프라이트", "체력 HUD 패널"],
                },
                {
                    "feature_id": "f-2",
                    "title": "Collectibles",
                    "description": "Add coins the player can pick up.",
                    "priority": "P1",
                    "dependencies": ["f-1"],
                    "assets_needed": ["코인 스프라이트", "3D 보물상자 메시"],
                },
            ],
            "usage": _fake_usage(input_tokens=6_000, output_tokens=15_000),
        }

    return mcp


def build_unity_server() -> FastMCP:
    mcp = FastMCP("UnityMcpServer", host="127.0.0.1", port=9102 + PORT_OFFSET)

    @mcp.tool(description="Generate a C# script implementing one feature.")
    def create_script(
        featureId: str,
        prompt: str,
        contents: str = "",
        projectContext: str = "",
        existingTypes: str = "",
        previousSource: str = "",
    ) -> JsonDict:
        body: JsonDict = {"file": f"Assets/Scripts/{featureId}.cs"}
        # Finished source handed in means no model ran, so no tokens are billed
        # — the same rule the real UnityMcpServer follows.
        if not contents:
            body["usage"] = _fake_usage(input_tokens=1_500, output_tokens=4_000)
        return body

    @mcp.tool(description="Create a scene in the Unity project.")
    def create_scene(featureId: str, sceneName: str) -> JsonDict:
        return {"scene": f"Assets/Scenes/{sceneName}.unity"}

    @mcp.tool(description="Import a generated asset into the Unity project.")
    def import_asset(featureId: str, assetPath: str) -> JsonDict:
        return {"imported": assetPath}

    @mcp.tool(description="Build the Unity project for a game.")
    def build_project(gameId: str) -> JsonDict:
        return {"buildId": f"build-{gameId[:8]}", "artifactPath": "Builds/prototype.zip"}

    @mcp.tool(description="Run the project's play-mode tests.")
    def run_playmode_test(gameId: str) -> JsonDict:
        return {"passed": True, "durationSeconds": 10, "errorCount": 0, "errors": []}

    @mcp.tool(description="Return compiler diagnostics for the last build.")
    def get_compile_errors(gameId: str) -> JsonDict:
        return {"errors": []}

    return mcp


def build_qa_server() -> FastMCP:
    mcp = FastMCP("QaMcpServer", host="127.0.0.1", port=9103 + PORT_OFFSET)

    @mcp.tool(description="Derive a QA policy from the game design document.")
    def establish_qa_policy(gameDesign: JsonDict) -> JsonDict:
        return {
            "qaPolicy": {
                "test_cases": [
                    {
                        "case_id": "t-1",
                        "description": "Player can move and jump",
                        "expected_result": "Character responds to input",
                    }
                ],
                "acceptance_criteria": ["Core movement works"],
            }
        }

    @mcp.tool(description="Compare the built prototype's structure against the design.")
    def verify_prototype_structure(gameDesign: JsonDict, build: str) -> JsonDict:
        return {"match": True, "missing": []}

    @mcp.tool(description="Diff the prototype structure against the design document.")
    def compare_structure(gameDesign: JsonDict, build: str) -> JsonDict:
        return {"match": True, "missing": []}

    @mcp.tool(description="Run the QA policy against the build and return a verdict.")
    def run_functional_verification(gameDesign: JsonDict, build: str, qaPolicy: JsonDict) -> JsonDict:
        return {"result": "PASS"}

    @mcp.tool(description="Turn execution logs into an ExecutionErrorReport.")
    def generate_error_report(gameDesign: JsonDict, build: str, logs: str) -> JsonDict:
        return {
            "errorReport": {
                "error_type": "runtime",
                "message": "No errors detected.",
                "file": None,
                "line": None,
                "suggested_fix": "None required.",
                "related_feature_id": "",
            }
        }

    return mcp


def build_asset_server() -> FastMCP:
    mcp = FastMCP("AssetGenMcpServer", host="127.0.0.1", port=9104 + PORT_OFFSET)

    # 게임별로 몇 장 만들었는지 세어 asset_review_summary 가 0 이 아닌 값을 낸다 —
    # 항상 0/0/0 을 돌려주면 AssetReview 게이트가 "반려 없음"을 아무것도 안 세고
    # 통과시키는지, 실제로 세고 통과시키는지 구분할 수 없다.
    made: dict[str, int] = {}

    def _asset(kind: str, feature_id: str, game_id: str | None, suffix: str) -> JsonDict:
        """§03 의 asset 반환 **형태**를 그대로 낸다 (값은 스텁이어도 된다).

        키가 하나라도 빠지면 목으로 돌린 파이프라인과 실물로 돌린 파이프라인이
        다른 모양을 보게 되고, 그 차이는 목에서는 절대 드러나지 않는다.
        `generatedBy` 는 실물과 같은 `"pixellab"` 이다 — 생성 경로가 하나뿐이라
        실물이 낼 수 있는 다른 값이 없다. 다만 `imagesGenerated` 는 싣지
        않는다: 목은 실제로 할당량을 쓰지 않고, 쓴 척하면 잡의 소진량 집계가
        목 실행에서 부풀려진다.
        """

        resolved = game_id or "mock-game"
        made[resolved] = made.get(resolved, 0) + 1
        return {
            "assetPath": f"Assets/{kind}/{resolved}/{feature_id}{suffix}",
            "assetId": f"{resolved}__{feature_id}__{kind.lower()}",
            "kind": kind.lower(),
            "gameId": resolved,
            "status": "pending",
            "styleSeed": 0,
            "generatedBy": "pixellab",
        }

    @mcp.tool(description="Generate a placeholder 2D sprite for a feature.")
    def generate_2d_sprite(
        featureId: str, prompt: str, gameId: str | None = None, artStyle: str | None = None
    ) -> JsonDict:
        return _asset("Sprites", featureId, gameId, ".png")

    @mcp.tool(description="Generate a placeholder UI asset for a feature.")
    def generate_ui_asset(
        featureId: str, prompt: str, gameId: str | None = None, artStyle: str | None = None
    ) -> JsonDict:
        return _asset("UI", featureId, gameId, ".png")

    @mcp.tool(description="Generate a placeholder 3D model for a feature.")
    def generate_3d_placeholder(
        featureId: str, prompt: str, gameId: str | None = None, artStyle: str | None = None
    ) -> JsonDict:
        return _asset("Models", featureId, gameId, ".fbx")

    @mcp.tool(description="Lock a game's art style and return its palette. Idempotent.")
    def establish_art_style(gameId: str, artStyle: str = "") -> JsonDict:
        """AssetGen 이 첫 generate_* 앞에서 부른다. 이게 없으면 그 노드가 통째로
        실패해 파이프라인이 여기서 멈춘다."""

        return {"gameId": gameId, "artStyle": artStyle or "pixel art", "palette": ["#1b1b1b"]}

    @mcp.tool(description="Report the review state of every asset in a game.")
    def asset_review_summary(gameId: str) -> JsonDict:
        total = made.get(gameId, 0)
        return {
            "gameId": gameId,
            "total": total,
            "pending": total,
            "approved": 0,
            "rejected": 0,
            # 반려만 게이트로 삼는다 (05 §4.2, 사용자 결정) — pending 은 막지 않는다.
            "readyForBuild": True,
        }

    @mcp.tool(description="List assets still awaiting human review.")
    def list_pending_assets(gameId: str) -> JsonDict:
        return {"gameId": gameId, "assets": []}

    @mcp.tool(description="Record a human approve/reject decision on one asset.")
    def review_asset(assetId: str, approved: bool, note: str = "") -> JsonDict:
        return {"assetId": assetId, "status": "approved" if approved else "rejected", "note": note}

    return mcp


def build_git_server() -> FastMCP:
    mcp = FastMCP("GitMcpServer", host="127.0.0.1", port=9105 + PORT_OFFSET)

    @mcp.tool(description="Create the repository if absent, otherwise reuse it (idempotent).")
    def git_init(repoName: str) -> JsonDict:
        return {"repoName": repoName, "created": False}

    @mcp.tool(description="Check out or create a branch.")
    def git_branch(branch: str, repoName: str = "") -> JsonDict:
        return {"branch": branch}

    @mcp.tool(description="Pull remote changes for a branch.")
    def git_pull(branch: str, repoName: str = "") -> JsonDict:
        return {"branch": branch, "updated": False}

    @mcp.tool(description="Commit the working tree.")
    def git_commit(
        branch: str, message: str, repoName: str = "", sourcePath: str = ""
    ) -> JsonDict:
        # sourcePath 가 없으면 진짜 서버는 빈 커밋을 만든다 (05 §1). 그 사실을
        # 결과에 남겨, 오케스트레이터가 인자를 흘리면 mock 에서도 보이게 한다.
        return {
            "commit": uuid.uuid4().hex[:12],
            "message": message,
            "sourcePath": sourcePath,
            "empty": not sourcePath,
        }

    @mcp.tool(description="Push a branch to the remote.")
    def git_push(branch: str, repoName: str = "") -> JsonDict:
        return {"branch": branch, "pushed": True}

    @mcp.tool(description="Tag the current commit.")
    def git_tag(tag: str, repoName: str = "") -> JsonDict:
        return {"tag": tag}

    @mcp.tool(description="Report the repository's working-tree status.")
    def git_status(repoName: str = "") -> JsonDict:
        return {"clean": True}

    return mcp


_BUILDERS = (
    ("StrategicMcpServer", 9101, build_strategic_server),
    ("UnityMcpServer", 9102, build_unity_server),
    ("QaMcpServer", 9103, build_qa_server),
    ("AssetGenMcpServer", 9104, build_asset_server),
    ("GitMcpServer", 9105, build_git_server),
)


def main() -> None:
    # build_*_server() reads PORT_OFFSET at call time, so the CLI value has to
    # land on the module global before any of them run.
    global PORT_OFFSET

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port-offset",
        type=int,
        default=PORT_OFFSET,
        help="Added to every port so these can run beside the real servers (default 0).",
    )
    args = parser.parse_args()
    PORT_OFFSET = args.port_offset

    threads: list[threading.Thread] = []
    for name, port, build in _BUILDERS:
        port += PORT_OFFSET
        server = build()
        thread = threading.Thread(
            target=server.run,
            kwargs={"transport": "streamable-http"},
            name=name,
            daemon=True,
        )
        thread.start()
        threads.append(thread)
        print(f"{name} listening on http://127.0.0.1:{port}/mcp")

    print("\nPress Ctrl+C to stop.")
    try:
        for thread in threads:
            thread.join()
    except KeyboardInterrupt:
        print("\nShutting down.")


if __name__ == "__main__":
    main()
