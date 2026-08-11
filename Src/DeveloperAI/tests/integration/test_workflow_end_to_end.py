"""End-to-end test: runs the real LangGraph workflow against in-process MCP
servers and an in-memory Job Store, proving Intake -> Planning -> ApprovalGate
(pause) -> Development -> QA -> Deployment actually wires together (§3, §4).

The five servers are real ``FastMCP`` instances reached over the SDK's
in-memory transport, so the orchestrator speaks the same protocol it uses in
production — initialize handshake included — without binding a port.
"""

from langgraph.checkpoint.memory import InMemorySaver
from mcp.server.mcpserver import MCPServer as FastMCP

from app.graph.graph import build_graph
from app.graph.state import Stage
from app.mcp.asset_client import AssetClient
from app.mcp.clients import ToolClients
from app.mcp.git_client import GitClient
from app.mcp.qa_client import QaClient
from app.mcp.strategic_client import StrategicClient
from app.mcp.unity_client import UnityClient
from app.models.schemas import JobStatus
from app.repository.job_repository import JobRepository
from app.repository.trace_repository import TraceRepository
from app.services.workflow_runner import WorkflowRunner
from tests.conftest import mcp_session_factory


class _FakeRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, data: str) -> None:
        self.published.append((channel, data))


def _strategic_server() -> FastMCP:
    server = FastMCP("StrategicMcpServer")

    @server.tool()
    def propose_concept(gameId: str, idea: str) -> dict:
        """ConceptGate's evidence-gathering step (§3.1). No LLM involved."""

        return {
            "gameId": gameId,
            "version": 1,
            "idea": idea,
            "status": "pending",
            "evidence": {"supporting": [], "counterexamples": []},
            "decisions": [],
        }

    @server.tool()
    def decide_concept(
        gameId: str, decision: str, note: str = "", editedIdea: str = ""
    ) -> dict:
        return {
            "gameId": gameId,
            "version": 2 if decision == "revise" else 1,
            "idea": editedIdea or "a platformer with a double jump",
            "status": "pending" if decision == "revise" else f"{decision}d",
            "evidence": {"supporting": [], "counterexamples": []},
            "decisions": [{"decision": decision, "note": note}],
        }

    @server.tool()
    def generate_game_design(prompt: str) -> dict:
        return {
            "gameDesign": {
                "game_id": "g1",
                "genre": "platformer",
                "core_mechanics": ["jump"],
                "art_style": "pixel",
                "target_platform": "PC",
                "structure_overview": "one level",
                "created_at": "2026-07-15T00:00:00Z",
            },
            "featurePrompts": [
                {
                    "feature_id": "f-1",
                    "title": "Jump",
                    "description": "Add jump ability",
                    "priority": "P0",
                    "dependencies": [],
                }
            ],
        }

    return server


def _unity_server(calls: list[str]) -> FastMCP:
    server = FastMCP("UnityMcpServer")

    @server.tool()
    def create_script(
        featureId: str,
        prompt: str,
        contents: str = "",
        projectContext: str = "",
        existingTypes: list[str] | None = None,
        previousSource: str = "",
        plannedPath: str = "",
        plannedClass: str = "",
    ) -> dict:
        calls.append("create_script")
        # The real server returns the source and every declared type alongside
        # the path; the retry loop and the trace store both read them. The path
        # and class come from the architecture pass, not from the feature id.
        path = plannedPath or "Assets/Scripts/Jump.cs"
        class_name = plannedClass or "Jump"
        return {
            "file": path,
            "className": class_name,
            "types": [class_name],
            "contents": (
                f"namespace Game.Gameplay {{ public class {class_name} : MonoBehaviour {{ }} }}"
            ),
        }

    @server.tool()
    def design_architecture(
        gameId: str,
        gameDesign: dict | None = None,
        featurePrompts: list[dict] | None = None,
        design: dict | None = None,
    ) -> dict:
        # CodeGen plans the whole game before writing anything, so the file
        # layout — and every class name — comes from here rather than from the
        # spec id. One file per planned entry; the loop follows this list.
        calls.append("design_architecture")
        feature_ids = [item["feature_id"] for item in featurePrompts or []]
        return {
            "gameId": gameId,
            "files": [
                {
                    "path": "Assets/Scripts/Player/JumpAbility.cs",
                    "className": "JumpAbility",
                    "kind": "MonoBehaviour",
                    "featureIds": feature_ids,
                    "responsibility": "Apply upward impulse on the jump input.",
                    "dependsOn": [],
                }
            ],
            "prefabs": [],
            "scene": {
                "name": "Main",
                "objects": [
                    {
                        "name": "Player",
                        "parent": "",
                        "components": ["JumpAbility"],
                        "prefab": "",
                    }
                ],
            },
            "notes": [],
            "typeMap": "Types in this game (all of them, decided up front):\n- JumpAbility",
        }

    @server.tool()
    def import_asset(featureId: str, assetPath: str) -> dict:
        calls.append("import_asset")
        return {"imported": assetPath}

    @server.tool()
    def build_project(gameId: str) -> dict:
        calls.append("build_project")
        # projectPath is what Deployment forwards as the commit's sourcePath.
        return {"buildId": "build-1", "projectPath": "/unity/AutoGenProject"}

    @server.tool()
    def inspect_project_layout(gameId: str = "", projectPath: str = "") -> dict:
        # BuildPrototype records this beside the build result; QA folds it into
        # the `build` reference so the structure judge can see whether scripts
        # are actually attached to anything.
        calls.append("inspect_project_layout")
        return {
            "gameId": gameId,
            "projectPath": "/unity/AutoGenProject",
            "counts": {"scripts": 1, "monoBehaviours": 1, "prefabs": 1, "scenes": 1},
            "ok": True,
            "findings": [],
        }

    @server.tool()
    def get_compile_errors(gameId: str) -> dict:
        return {"errors": []}

    @server.tool()
    def run_playmode_test(gameId: str) -> dict:
        calls.append("run_playmode_test")
        return {"passed": True, "durationSeconds": 10, "errorCount": 0, "errors": []}

    return server


def _asset_server(calls: list[str] | None = None) -> FastMCP:
    """Mirrors AssetGenMcpServer's real signatures: gameId/artStyle are optional
    arguments beyond §03, and establish_art_style locks the palette."""

    server = FastMCP("AssetGenMcpServer")
    seen = calls if calls is not None else []

    @server.tool()
    def establish_art_style(gameId: str, artStyle: str = "") -> dict:
        seen.append("establish_art_style")
        return {"gameId": gameId, "artStyle": artStyle, "palette": []}

    @server.tool()
    def generate_2d_sprite(
        featureId: str, prompt: str, gameId: str = "", artStyle: str = ""
    ) -> dict:
        seen.append("generate_2d_sprite")
        return {"assetPath": "Assets/Sprites/jump.png", "gameId": gameId}

    @server.tool()
    def asset_review_summary(gameId: str) -> dict:
        seen.append("asset_review_summary")
        # Nothing has been explicitly rejected, so AssetReview must let the
        # pending sprite above through rather than stalling every job.
        return {"gameId": gameId, "total": 1, "pending": 1, "approved": 0, "rejected": 0}

    return server


def _qa_server() -> FastMCP:
    server = FastMCP("QaMcpServer")

    @server.tool()
    def establish_qa_policy(gameDesign: dict) -> dict:
        return {
            "qaPolicy": {
                "test_cases": [
                    {
                        "case_id": "t-1",
                        "description": "jump works",
                        "expected_result": "player jumps",
                    }
                ],
                "acceptance_criteria": ["player can jump"],
            }
        }

    @server.tool()
    def verify_prototype_structure(gameDesign: dict, build: str) -> dict:
        return {"match": True}

    @server.tool()
    def run_functional_verification(gameDesign: dict, build: str, qaPolicy: dict) -> dict:
        return {"result": "PASS"}

    return server


def _git_server(calls: list[str]) -> FastMCP:
    server = FastMCP("GitMcpServer")

    # repoName/sourcePath are optional arguments the real server accepts beyond
    # §03; declaring them here is what lets the test see whether the
    # orchestrator actually sends them.
    committed: dict[str, str] = {}

    @server.tool()
    def git_init(repoName: str) -> dict:
        calls.append("git_init")
        return {"repoName": repoName}

    @server.tool()
    def git_branch(branch: str, repoName: str = "") -> dict:
        calls.append("git_branch")
        return {"branch": branch}

    @server.tool()
    def git_pull(branch: str, repoName: str = "") -> dict:
        calls.append("git_pull")
        return {"branch": branch}

    @server.tool()
    def git_commit(branch: str, message: str, repoName: str = "", sourcePath: str = "") -> dict:
        calls.append("git_commit")
        committed["repoName"] = repoName
        committed["sourcePath"] = sourcePath
        return {"commit": "abc123"}

    @server.tool()
    def git_push(branch: str, repoName: str = "") -> dict:
        calls.append("git_push")
        return {"branch": branch}

    @server.tool()
    def git_tag(tag: str, repoName: str = "") -> dict:
        calls.append("git_tag")
        return {"tag": tag}

    server.committed = committed  # type: ignore[attr-defined]
    return server


def _build_tool_clients(
    unity_calls: list[str], git_calls: list[str], asset_calls: list[str]
) -> tuple[ToolClients, FastMCP]:
    def _client(cls, name: str, server: FastMCP):
        return cls(
            server_name=name,
            url=f"memory://{name}",
            timeout_seconds=5,
            max_retries=1,
            session_factory=mcp_session_factory(server),
        )

    git_server = _git_server(git_calls)
    clients = ToolClients(
        strategic=_client(StrategicClient, "StrategicMcpServer", _strategic_server()),
        unity=_client(UnityClient, "UnityMcpServer", _unity_server(unity_calls)),
        qa=_client(QaClient, "QaMcpServer", _qa_server()),
        asset=_client(AssetClient, "AssetGenMcpServer", _asset_server(asset_calls)),
        git=_client(GitClient, "GitMcpServer", git_server),
    )
    return clients, git_server


async def test_full_pipeline_pauses_for_approval_then_deploys(session_factory):
    unity_calls: list[str] = []
    git_calls: list[str] = []
    asset_calls: list[str] = []
    mcp, git_server = _build_tool_clients(unity_calls, git_calls, asset_calls)
    graph = build_graph(mcp, max_iterations=5, checkpointer=InMemorySaver())
    redis_client = _FakeRedis()
    runner = WorkflowRunner(graph, session_factory, redis_client)

    async with session_factory() as session:
        await JobRepository(session).create(game_id="g1", prompt="a simple platformer")

    await runner.start("g1", "a simple platformer")

    # First gate: ConceptGate, before any blueprint has been written. The job
    # must pause here with the proposal readable and no design produced yet —
    # that ordering is the whole point of the gate.
    async with session_factory() as session:
        job = await JobRepository(session).get_or_raise("g1")
    assert job.status == JobStatus.AWAITING_APPROVAL.value
    assert job.current_stage == Stage.CONCEPT_GATE
    assert job.concept["status"] == "pending"
    assert job.game_design is None

    await runner.resume("g1", approved=True, feedback=None)

    # Second gate: ApprovalGate, now with a blueprint to review.
    async with session_factory() as session:
        job = await JobRepository(session).get_or_raise("g1")
    assert job.status == JobStatus.AWAITING_APPROVAL.value
    assert job.current_stage == Stage.APPROVAL_GATE
    assert job.game_design["genre"] == "platformer"
    assert job.repo_name == "platformer-a-simple-platformer-g1"

    await runner.resume("g1", approved=True, feedback=None)

    async with session_factory() as session:
        job = await JobRepository(session).get_or_raise("g1")
    assert job.status == JobStatus.DONE.value
    assert job.artifact["commit_hash"] == "abc123"
    assert job.artifact["repo_name"] == "platformer-a-simple-platformer-g1"
    assert job.artifact["branch"] == "main"
    assert job.dev_iteration_count == 0
    assert job.qa_iteration_count == 0

    # Generated assets were imported before the build, and the deployment ran
    # the full §03 sequence in order.
    assert unity_calls.index("import_asset") < unity_calls.index("build_project")
    # RuntimeCheck runs after a successful build/compile check, before QA sees
    # the build reference (§3 of Doc/설계/06_3-4군_인수인계.md).
    assert unity_calls.index("build_project") < unity_calls.index("run_playmode_test")
    assert git_calls == ["git_init", "git_branch", "git_pull", "git_commit", "git_push", "git_tag"]

    # The art style is locked before anything is drawn — locking it afterwards
    # is a no-op on the real server, so the ordering is the guarantee.
    assert asset_calls[0] == "establish_art_style"
    assert "generate_2d_sprite" in asset_calls[1:]
    # A pending (never-reviewed) sprite must not stall deployment (doc05 §4).
    assert "asset_review_summary" in asset_calls

    # The commit names its repository and carries the Unity project source, so
    # the published repository is not an empty tree behind a valid SHA.
    assert git_server.committed["repoName"] == "platformer-a-simple-platformer-g1"
    assert git_server.committed["sourcePath"] == "/unity/AutoGenProject"
    assert job.artifact["source_path"] == "/unity/AutoGenProject"

    # The run left a labelled generation record behind. This is written while
    # the run is in flight because it cannot be reconstructed afterwards — the
    # generated sources exist nowhere else.
    async with session_factory() as session:
        traces = await TraceRepository(session).list_for_game("g1")

    assert len(traces) == 1
    trace = traces[0]
    assert trace.attempt == 1
    # 설계 패스가 정한 경로와 이름이 기록돼야 한다 — feature_id 에서 기계적으로
    # 만든 평면 경로(`Assets/Scripts/Jump.cs`)가 아니다. 그 평면 이름을 없애는
    # 것이 06 문서 §3.2 2단계의 목적이므로, 여기가 되돌아가면 그 성과가 풀린다.
    assert trace.file == "Assets/Scripts/Player/JumpAbility.cs"
    assert "class JumpAbility" in trace.generated_source
    assert trace.compile_ok is True
    assert trace.qa_verdict == "PASS"
    # The blueprint summary the model was actually given, recorded as sent.
    assert "platformer" in trace.project_context

    await mcp.aclose()


async def test_rejected_asset_escalates_instead_of_deploying(session_factory):
    """A human's explicit review_asset(approved=False) must stop the pipeline
    before Deployment — pending (unreviewed) is fine, rejected is not."""

    unity_calls: list[str] = []
    git_calls: list[str] = []

    def _asset_server_with_a_rejection() -> FastMCP:
        server = FastMCP("AssetGenMcpServer")

        @server.tool()
        def establish_art_style(gameId: str, artStyle: str = "") -> dict:
            return {"gameId": gameId, "artStyle": artStyle, "palette": []}

        @server.tool()
        def generate_2d_sprite(
            featureId: str, prompt: str, gameId: str = "", artStyle: str = ""
        ) -> dict:
            return {"assetPath": "Assets/Sprites/jump.png", "gameId": gameId}

        @server.tool()
        def asset_review_summary(gameId: str) -> dict:
            return {"gameId": gameId, "total": 1, "pending": 0, "approved": 0, "rejected": 1}

        return server

    def _client(cls, name: str, server: FastMCP):
        return cls(
            server_name=name,
            url=f"memory://{name}",
            timeout_seconds=5,
            max_retries=1,
            session_factory=mcp_session_factory(server),
        )

    mcp = ToolClients(
        strategic=_client(StrategicClient, "StrategicMcpServer", _strategic_server()),
        unity=_client(UnityClient, "UnityMcpServer", _unity_server(unity_calls)),
        qa=_client(QaClient, "QaMcpServer", _qa_server()),
        asset=_client(AssetClient, "AssetGenMcpServer", _asset_server_with_a_rejection()),
        git=_client(GitClient, "GitMcpServer", _git_server(git_calls)),
    )
    graph = build_graph(mcp, max_iterations=5, checkpointer=InMemorySaver())
    runner = WorkflowRunner(graph, session_factory, _FakeRedis())

    async with session_factory() as session:
        await JobRepository(session).create(game_id="g2", prompt="a simple platformer")

    await runner.start("g2", "a simple platformer")
    await runner.resume("g2", approved=True, feedback=None)  # ConceptGate
    await runner.resume("g2", approved=True, feedback=None)  # ApprovalGate

    async with session_factory() as session:
        job = await JobRepository(session).get_or_raise("g2")

    assert job.status == JobStatus.ESCALATED.value
    assert job.current_stage == Stage.HUMAN_ESCALATION
    # Deployment must never have run - no commit, no push.
    assert "git_commit" not in git_calls

    await mcp.aclose()
