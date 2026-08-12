"""ResearchMcpServer: retrieve evidence; validate and store host-authored plans."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver import Context, MCPServer as FastMCP

from common.errors import MCP_ERROR, VALIDATION_ERROR, tool_error
from common.server import expects_dict_return, serve

from .arch_cards import ArchGuidance, arch_ids
from .concepts import ConceptProposal, ConceptStore, adjust_evidence
from .neon_http import connect_pool
from .research_repo import COUNTEREXAMPLE_MISSING, ResearchRepository, SentenceTransformerEmbedder
from .handoff import HandoffError, export_handoff
from .specs import SpecDocument, SpecStore, declared_spec_ids, dependency_errors, lint_spec

logger = logging.getLogger(__name__)

RESEARCH_DSN = os.getenv("RESEARCH_DSN") or os.getenv("NEON_DSN", "")


@dataclass
class ResearchContext:
    # asyncpg.Pool 또는 NeonHttpPool(사내망이 5432 를 막는 환경의 HTTP 폴백) —
    # connect_pool() 이 이 서버가 쓰는 부분에 한해 계약을 맞춰 준다.
    pool: Any
    research: ResearchRepository
    store: SpecStore
    concepts: ConceptStore


@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncIterator[ResearchContext]:
    """Neon 풀을 한 번만 열고 반드시 닫는다."""

    if not RESEARCH_DSN:
        raise RuntimeError("RESEARCH_DSN (or NEON_DSN) is required.")

    logger.info("Connecting to research DB …")
    pool = await connect_pool(
        RESEARCH_DSN,
        max_size=int(os.getenv("STRATEGIC_POOL_MAX", "5")),
        tcp_timeout=float(os.getenv("STRATEGIC_DB_TCP_TIMEOUT_SECONDS", "8")),
    )

    try:
        embedder = SentenceTransformerEmbedder() if SentenceTransformerEmbedder.available() else None
        if embedder is None:
            logger.warning(
                "sentence-transformers is unavailable; using trigram search. "
                "Install sentence-transformers and restart to enable semantic search."
            )
        store = SpecStore(pool)
        await store.init_schema()
        concepts = ConceptStore(pool)
        await concepts.init_schema()
        yield ResearchContext(
            pool=pool,
            research=ResearchRepository(pool, embedder),
            store=store,
            concepts=concepts,
        )
    finally:
        logger.info("Closing research DB pool …")
        await pool.close()


mcp = FastMCP(
    "ResearchMcpServer",
    instructions=(
        "Retrieve source evidence and counterexamples. Validate and store host-authored "
        "blueprints and specs. Never call a model."
    ),
    lifespan=lifespan,
)

def _ctx(ctx: Context) -> ResearchContext:
    return ctx.request_context.lifespan_context  # type: ignore[return-value]


def _require(value: str, field: str) -> str:
    if not value or not value.strip():
        raise tool_error(VALIDATION_ERROR, f"{field} must not be empty")
    return value.strip()


def _publication_state(blueprint: dict[str, Any]) -> str:
    """Return the explicit planning state; designs are editable drafts by default."""

    state = str(blueprint.get("status", "draft")).strip().lower()
    if state not in ("draft", "published"):
        raise tool_error(VALIDATION_ERROR, "blueprint.status must be draft or published")
    return state


def _blueprint_document(blueprint: dict[str, Any], specs: list[SpecDocument]) -> dict[str, Any]:
    """Store game-level data plus a spec ID list, never duplicate spec bodies."""

    document = dict(blueprint)
    document.pop("specs", None)
    document["specIds"] = declared_spec_ids(specs)
    return document


def _blueprint_spec_ids(game_id: str, blueprint: dict[str, Any]) -> set[str]:
    """Read new ID-only blueprints and legacy blueprints during migration."""

    ids = blueprint.get("specIds")
    if isinstance(ids, list) and all(isinstance(spec_id, str) for spec_id in ids):
        return set(ids)
    return {
        f"{game_id}__{raw.get('specId', '')}"
        for raw in blueprint.get("specs") or []
        if isinstance(raw, dict)
    }


def _dependencies_for(game_id: str, dependencies: list[str]) -> list[str]:
    prefix = f"{game_id}__"
    return [item if item.startswith(prefix) else f"{prefix}{item}" for item in dependencies]


@mcp.tool(
    description="Validate and store a host-authored blueprint and specs; return implementation inputs."
)
@expects_dict_return
async def publish_game_design(
    ctx: Context, gameId: str, blueprint: dict[str, Any]
) -> dict[str, Any]:
    """완성된 청사진과 spec을 lint한 뒤 저장한다."""

    game_id = _require(gameId, "gameId")
    if not isinstance(blueprint, dict):
        raise tool_error(VALIDATION_ERROR, "blueprint는 객체여야 합니다", gameId=game_id)
    required = ("title", "genre", "coreMechanics", "artStyle", "structureOverview", "specs")
    missing_fields = [name for name in required if not blueprint.get(name)]
    if missing_fields:
        raise tool_error(
            VALIDATION_ERROR,
            f"blueprint 필수 필드가 없습니다: {missing_fields}",
            gameId=game_id,
        )
    if not isinstance(blueprint["specs"], list) or not blueprint["specs"]:
        raise tool_error(VALIDATION_ERROR, "blueprint.specs는 비어 있지 않은 배열이어야 합니다")

    publication_state = _publication_state(blueprint)
    context = _ctx(ctx)
    known_ids = set(await context.research.card_index())
    try:
        guidance = await context.research.arch_guidance(
            [ref for raw in blueprint["specs"] for ref in raw.get("refs", [])]
        )
    except Exception as exc:  # noqa: BLE001
        raise tool_error(
            MCP_ERROR,
            f"아키텍처 카드 조회 실패: {type(exc).__name__}: {exc}",
            gameId=game_id,
        ) from exc

    published: list[SpecDocument] = []
    rejected: list[dict[str, Any]] = []
    for raw in blueprint["specs"]:
        try:
            spec = _to_spec(raw, game_id, 1, guidance)
        except KeyError as exc:
            rejected.append({"specId": raw.get("specId", ""), "errors": [f"필수 필드 누락: {exc}"]})
            continue
        errors = lint_spec(spec, known_ids)
        if errors:
            rejected.append({"specId": spec.spec_id, "title": spec.title, "errors": errors})
            continue
        spec.status = publication_state
        published.append(spec)

    if not published or rejected:
        raise tool_error(
            VALIDATION_ERROR,
            "lint를 통과한 spec이 하나도 없습니다.",
            gameId=game_id,
            rejected=rejected,
        )

    graph_errors = dependency_errors(published)
    if graph_errors:
        raise tool_error(
            VALIDATION_ERROR,
            "specification dependency graph cannot be executed",
            gameId=game_id,
            dependencyErrors=graph_errors,
        )

    existing_specs = {
        str(summary["specId"]): await context.store.get_spec(str(summary["specId"]))
        for summary in await context.store.list_specs(game_id)
    }
    for spec in published:
        previous = existing_specs.get(spec.spec_id)
        if previous is not None:
            spec.version = previous.version + 1
            spec.change_log = [
                *previous.change_log,
                {"version": spec.version, "source": "planner", "change": "design revision"},
            ]

    # Neon HTTP does not support transactions. Validate all feature documents
    # first, then write specs before the visible blueprint. A failed write can
    # therefore leave only unreferenced rows, never a blueprint with missing specs.
    for spec in published:
        await context.store.save_spec(spec)
    blueprint_document = _blueprint_document(blueprint, published)
    blueprint_version = await context.store.save_blueprint(
        game_id=game_id,
        seed_prompt=str(blueprint.get("seedPrompt") or ""),
        title=str(blueprint["title"]),
        genre=str(blueprint["genre"]),
        document=blueprint_document,
        evidence=dict(blueprint.get("evidence") or {}),
    )
    for spec in published:
        spec.blueprint_version = blueprint_version
        await context.store.save_spec(spec)

    design = {
        "game_id": game_id,
        "genre": blueprint["genre"],
        "core_mechanics": blueprint["coreMechanics"],
        "art_style": blueprint["artStyle"],
        "target_platform": "PC",
        "structure_overview": blueprint["structureOverview"],
        "created_at": _utcnow(),
    }

    ready_for_execution = publication_state == "published" and not rejected
    return {
        "gameDesign": design,
        "designState": publication_state,
        "readyForExecution": ready_for_execution,
        "featurePrompts": [_to_feature_prompt(spec) for spec in published]
        if ready_for_execution
        else [],
        "blueprintVersion": blueprint_version,
        "rejectedSpecs": rejected,
    }


def _utcnow() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _to_spec(
    raw: dict[str, Any],
    game_id: str,
    blueprint_version: int,
    guidance: dict[str, ArchGuidance] | None = None,
) -> SpecDocument:
    return SpecDocument(
        spec_id=f"{game_id}__{raw['specId']}",
        game_id=game_id,
        title=raw["title"],
        version=1,
        blueprint_version=blueprint_version,
        refs=raw["refs"],
        goal=raw["goal"],
        implementation_scope=raw["implementationScope"],
        out_of_scope=raw["outOfScope"],
        acceptance_criteria=raw["acceptanceCriteria"],
        context=str(raw.get("context") or ""),
        relevant_systems=list(raw.get("relevantSystems") or []),
        constraints=list(raw.get("constraints") or []),
        verification_method=list(raw.get("verificationMethod") or []),
        unity_hints=raw.get("unityHints", {}),
        dependencies=_dependencies_for(game_id, list(raw.get("dependencies") or [])),
        architecture=_architecture_for(raw["refs"], guidance),
    )


def _architecture_for(
    refs: list[str], guidance: dict[str, ArchGuidance] | None
) -> list[ArchGuidance]:
    """refs 에 인용된 ARCH 카드의 지침을 인용 순서대로 싣는다.

    지침을 못 찾은 카드는 빠지고, 그 spec 은 ``lint_spec`` 의 S6 에서 반려된다.
    인용만 남고 내용이 빈 spec 이 개발 AI 에게 가지 않게 하는 쪽을 택했다.
    """

    found = guidance or {}
    return [found[card_id] for card_id in arch_ids(refs) if card_id in found]


def _to_feature_prompt(spec: SpecDocument) -> dict[str, Any]:
    """spec을 호스트가 구현에 사용할 feature prompt로 변환한다.

    ``description`` 이 곧 개발 AI 가 받는 전부다. 청사진은 넘기지 않으므로
    여기에 구현에 필요한 것이 모두 담겨야 한다.
    """

    hints = spec.unity_hints or {}
    lines = [
        f"## Goal\n{spec.goal}",
        (f"\n## Context\n{spec.context}" if spec.context else ""),
        "\n## Implementation scope\n" + "\n".join(f"- {x}" for x in spec.implementation_scope),
        "\n## Out of scope\n" + "\n".join(f"- {x}" for x in spec.out_of_scope),
        "\n## Acceptance criteria\n" + "\n".join(f"- {x}" for x in spec.acceptance_criteria),
    ]
    if spec.relevant_systems:
        lines.append("\n## Relevant systems\n" + "\n".join(f"- {x}" for x in spec.relevant_systems))
    if spec.constraints:
        lines.append("\n## Constraints\n" + "\n".join(f"- {x}" for x in spec.constraints))
    if spec.verification_method:
        lines.append(
            "\n## Verification method\n" + "\n".join(f"- {x}" for x in spec.verification_method)
        )
    if hints:
        lines.append(
            "\n## Unity hints"
            + f"\n- Components: {', '.join(hints.get('components') or []) or '-'}"
            + f"\n- Scene objects: {', '.join(hints.get('sceneObjects') or []) or '-'}"
            + f"\n- Required assets: {', '.join(hints.get('assetsNeeded') or []) or '-'}"
            + (f"\n- Notes: {hints['notes']}" if hints.get("notes") else "")
        )
    # 아키텍처 카드 원문. 개발 AI 가 카드를 직접 읽지 않아도(읽으면 해석이 갈린다)
    # 절차·안티패턴·검증 방법을 그대로 받는다.
    if spec.architecture:
        lines.append(
            "\n## Architecture guidance (verbatim; do not summarize or expand scope)\n"
            + "\n\n".join(g.to_markdown() for g in spec.architecture)
        )
    lines.append(f"\n## Source cards\n{', '.join(spec.refs)}")

    return {
        "feature_id": spec.spec_id,
        "title": spec.title,
        "description": "\n".join(lines),
        "priority": "P0" if not spec.dependencies else "P1",
        "dependencies": spec.dependencies,
        "objective": spec.goal,
        "context": spec.context,
        "relevantSystems": spec.relevant_systems,
        "implementationRequirements": spec.implementation_scope,
        "constraints": spec.constraints,
        "acceptanceCriteria": spec.acceptance_criteria,
        "verificationMethod": spec.verification_method,
        # 위 "필요 에셋" 줄과 같은 값을 구조체로도 낸다. 문장으로만 주면 AssetGen 이
        # 긴 description 전체를 프롬프트로 삼게 되고, 어떤 자산을 만들지가
        # 키워드 등장 순서로 정해지므로 구조화된 값도 함께 보낸다.
        "assets_needed": list(hints.get("assetsNeeded") or []),
    }


def _attach_execution_prompt(payload: dict[str, Any], spec: SpecDocument) -> None:
    """Expose implementation input only for an explicitly approved specification."""

    payload["readyForExecution"] = spec.status == "published"
    payload["featurePrompt"] = _to_feature_prompt(spec) if spec.status == "published" else None


# ---------------------------------------------------------------------------
# 요구사항 전용 도구
# ---------------------------------------------------------------------------
@mcp.tool(description="Retrieve supporting evidence and counterexamples for an idea; do not plan.")
@expects_dict_return
async def research_idea(
    ctx: Context, idea: str, supportLimit: int = 6, counterLimit: int = 3,
    archLimit: int | None = None,
) -> dict[str, Any]:
    """LLM 없이 동작한다 — 근거만 먼저 보고 싶을 때 쓴다.

    ``archLimit`` 을 열어 둔 이유: 이 값이 코드 안에만 있던 동안 아키텍처 후보가
    3장으로 고정돼 있었고, 밖에서는 그 사실조차 보이지 않았다. 조절할 수 없는
    상한은 조용히 틀린다.
    """

    idea = _require(idea, "idea")
    try:
        evidence = await _ctx(ctx).research.gather_evidence(
            idea, supportLimit, counterLimit, archLimit
        )
    except Exception as exc:  # noqa: BLE001
        raise tool_error(MCP_ERROR, f"리서치 DB 조회 실패: {type(exc).__name__}: {exc}") from exc

    payload = evidence.to_dict()
    payload["counterexampleRequired"] = True
    if not evidence.has_counterexample:
        payload["warning"] = (
            f"{COUNTEREXAMPLE_MISSING}: at least one counterexample is required. "
            "Revise the query or request a new research card."
        )
    return payload


# ---------------------------------------------------------------------------
# 아이디어 제안 — 청사진 발행 이전, 사람이 검토하는 단계
# ---------------------------------------------------------------------------
@mcp.tool(
    description=(
        "Collect evidence and store an idea proposal for human review before blueprint publication."
    )
)
@expects_dict_return
async def propose_concept(
    ctx: Context, gameId: str, idea: str, supportLimit: int = 6, counterLimit: int = 3,
    archLimit: int | None = None,
) -> dict[str, Any]:
    """청사진을 쓰기 전, 근거만으로 사람이 방향을 확인하게 한다.

    ``research_idea`` 와 같은 방식으로 근거를 모으지만, 그 결과를
    ``strategic_concepts`` 에 ``status="pending"`` 으로 저장해 사람이
    ``decide_concept`` 으로 승인/수정/거부할 때까지 남겨둔다. 같은 ``gameId``
    로 다시 호출하면 버전을 올려 새 제안으로 갱신한다(이전 제안의 상태와 무관).
    """

    game_id = _require(gameId, "gameId")
    idea = _require(idea, "idea")
    context = _ctx(ctx)

    try:
        evidence = await context.research.gather_evidence(
            idea, supportLimit, counterLimit, archLimit
        )
    except Exception as exc:  # noqa: BLE001
        raise tool_error(MCP_ERROR, f"리서치 DB 조회 실패: {type(exc).__name__}: {exc}") from exc

    previous = await context.concepts.get(game_id)
    concept = ConceptProposal(
        game_id=game_id,
        version=(previous.version + 1) if previous else 1,
        idea=idea,
        evidence=evidence.to_dict(),
        status="pending",
        decisions=previous.decisions if previous else [],
    )
    await context.concepts.save(concept)

    payload = concept.to_dict()
    payload["counterexampleWarning"] = (
        "" if evidence.has_counterexample
        else f"{COUNTEREXAMPLE_MISSING}: decide whether to add evidence before approval."
    )
    return payload


@mcp.tool(description="List all idea proposals awaiting human review.")
@expects_dict_return
async def list_pending_concepts(ctx: Context) -> dict[str, Any]:
    pending = await _ctx(ctx).concepts.list_pending()
    return {"pending": pending, "count": len(pending)}


@mcp.tool(description="Return one game's full idea proposal and evidence.")
@expects_dict_return
async def get_concept(ctx: Context, gameId: str) -> dict[str, Any]:
    game_id = _require(gameId, "gameId")
    concept = await _ctx(ctx).concepts.get(game_id)
    if concept is None:
        raise tool_error(VALIDATION_ERROR, f"존재하지 않는 gameId: {game_id}")
    return concept.to_dict()


@mcp.tool(
    description=(
        "Approve, revise, or reject an idea proposal. Approval lets the host draft a blueprint."
    )
)
@expects_dict_return
async def decide_concept(
    ctx: Context,
    gameId: str,
    decision: str,
    note: str = "",
    editedIdea: str = "",
    includeCardIds: list[str] | None = None,
    excludeCardIds: list[str] | None = None,
) -> dict[str, Any]:
    """청사진 작성 이전에 사람이 개입하는 게이트.

    - ``approve``: 지금 상태 그대로 승인한다. 응답의 ``nextStep`` 이 다음 호출을
      안내한다.
    - ``revise``: ``editedIdea`` 로 질의를 바꾸거나, ``includeCardIds``/
      ``excludeCardIds`` 로 근거 카드를 사람이 직접 조정한다. 버전을 올려 다시
      ``pending`` 으로 되돌린다 — 재검토가 필요하다는 뜻이다.
    - ``reject``: 이 제안을 종료한다. ``note`` (거부 사유) 는 필수다. 다시
      시작하려면 ``propose_concept`` 을 새로 호출한다.
    """

    game_id = _require(gameId, "gameId")
    if decision not in ("approve", "revise", "reject"):
        raise tool_error(VALIDATION_ERROR, f"decision 은 approve|revise|reject 여야 합니다: {decision}")

    context = _ctx(ctx)
    current = await context.concepts.get(game_id)
    if current is None:
        raise tool_error(VALIDATION_ERROR, f"존재하지 않는 gameId: {game_id}")
    if current.status != "pending":
        raise tool_error(
            VALIDATION_ERROR,
            f"이미 처리된 제안입니다 (status={current.status}). "
            "다시 시작하려면 propose_concept 을 호출하세요.",
            gameId=game_id,
        )

    decided_at = _utcnow()

    if decision == "approve":
        current.status = "approved"
        current.decisions.append(
            {"version": current.version, "decision": "approve", "note": note, "decidedAt": decided_at}
        )
        await context.concepts.save(current)
        payload = current.to_dict()
        payload["nextStep"] = (
            f"Draft the blueprint, then call publish_game_design(gameId=\"{game_id}\", blueprint=...)."
        )
        return payload

    if decision == "reject":
        note = _require(note, "note")
        current.status = "rejected"
        current.decisions.append(
            {"version": current.version, "decision": "reject", "note": note, "decidedAt": decided_at}
        )
        await context.concepts.save(current)
        return current.to_dict()

    # decision == "revise"
    new_idea = editedIdea.strip() or current.idea
    if editedIdea.strip():
        try:
            evidence = await context.research.gather_evidence(new_idea, 6, 3)
            evidence_dict = evidence.to_dict()
        except Exception as exc:  # noqa: BLE001
            raise tool_error(MCP_ERROR, f"리서치 DB 조회 실패: {type(exc).__name__}: {exc}") from exc
    else:
        evidence_dict = current.evidence

    include_cards: list[dict[str, Any]] = []
    if includeCardIds:
        cards = await context.research.get_cards(includeCardIds)
        found_ids = {c.card_id for c in cards}
        missing = set(includeCardIds) - found_ids
        if missing:
            raise tool_error(VALIDATION_ERROR, f"존재하지 않는 카드 ID: {sorted(missing)}")
        include_cards = [c.to_dict() for c in cards]

    evidence_dict = adjust_evidence(evidence_dict, include_cards, set(excludeCardIds or []))

    decisions = [*current.decisions, {
        "version": current.version, "decision": "revise", "note": note, "decidedAt": decided_at,
    }]
    revised = ConceptProposal(
        game_id=game_id,
        version=current.version + 1,
        idea=new_idea,
        evidence=evidence_dict,
        status="pending",
        decisions=decisions,
    )
    await context.concepts.save(revised)
    return revised.to_dict()


@mcp.tool(description="List specs and publication state for one game.")
@expects_dict_return
async def list_specs(ctx: Context, gameId: str) -> dict[str, Any]:
    game_id = _require(gameId, "gameId")
    context = _ctx(ctx)
    blueprint = await context.store.get_blueprint(game_id)
    specs = await context.store.list_specs(game_id)
    return {
        "gameId": game_id,
        "blueprintVersion": (blueprint or {}).get("version"),
        "specCount": len(specs),
        "specs": specs,
    }


@mcp.tool(description="Return one complete spec, including Markdown and implementation prompt.")
@expects_dict_return
async def get_spec(ctx: Context, specId: str) -> dict[str, Any]:
    """언제든 재발행 — 개발 AI 가 잃어버려도 여기서 다시 받는다."""

    spec_id = _require(specId, "specId")
    context = _ctx(ctx)
    spec = await context.store.get_spec(spec_id)
    if spec is None:
        raise tool_error(VALIDATION_ERROR, f"존재하지 않는 specId: {spec_id}")

    payload = spec.to_dict()
    _attach_execution_prompt(payload, spec)
    payload["feedbackHistory"] = await context.store.feedback_history(spec_id)
    return payload


@mcp.tool(description="Validate a host-authored spec revision and store a new version.")
@expects_dict_return
async def revise_spec(
    ctx: Context,
    specId: str,
    spec: dict[str, Any],
    changeNote: str = "",
    source: str = "human",
) -> dict[str, Any]:
    """완성된 수정본만 받아 lint 통과 시 새 버전으로 저장한다."""

    spec_id = _require(specId, "specId")
    if not isinstance(spec, dict):
        raise tool_error(VALIDATION_ERROR, "spec은 객체여야 합니다", specId=spec_id)
    if source not in ("codex", "claude", "human"):
        raise tool_error(
            VALIDATION_ERROR, f"source는 codex|claude|human이어야 합니다: {source}"
        )

    context = _ctx(ctx)
    current = await context.store.get_spec(spec_id)
    if current is None:
        raise tool_error(VALIDATION_ERROR, f"존재하지 않는 specId: {spec_id}")

    known_ids = set(await context.research.card_index())
    revised_raw = dict(spec)
    blueprint = await context.store.get_blueprint(current.game_id)
    if blueprint is None:
        raise tool_error(VALIDATION_ERROR, f"missing blueprint for spec: {spec_id}")

    refs = list(revised_raw.get("refs") or current.refs)
    try:
        guidance = await context.research.arch_guidance(refs)
    except Exception as exc:  # noqa: BLE001
        raise tool_error(
            MCP_ERROR, f"아키텍처 카드 조회 실패: {type(exc).__name__}: {exc}"
        ) from exc

    revised = SpecDocument(
        spec_id=current.spec_id,
        game_id=current.game_id,
        title=str(revised_raw.get("title") or current.title),
        version=current.version + 1,
        blueprint_version=current.blueprint_version,
        refs=refs,
        goal=str(revised_raw.get("goal") or current.goal),
        implementation_scope=list(
            revised_raw.get("implementationScope") or current.implementation_scope
        ),
        out_of_scope=list(revised_raw.get("outOfScope") or current.out_of_scope),
        acceptance_criteria=list(
            revised_raw.get("acceptanceCriteria") or current.acceptance_criteria
        ),
        context=str(revised_raw.get("context") or current.context),
        relevant_systems=list(revised_raw.get("relevantSystems") or current.relevant_systems),
        constraints=list(revised_raw.get("constraints") or current.constraints),
        verification_method=list(
            revised_raw.get("verificationMethod") or current.verification_method
        ),
        unity_hints=revised_raw.get("unityHints", current.unity_hints),
        dependencies=_dependencies_for(
            current.game_id,
            list(revised_raw["dependencies"])
            if "dependencies" in revised_raw
            else current.dependencies,
        ),
        status=current.status,
        architecture=_architecture_for(refs, guidance),
        change_log=[
            *current.change_log,
            {
                "version": current.version + 1,
                "source": source,
                "change": changeNote[:300],
            },
        ],
    )

    errors = lint_spec(revised, known_ids)
    if errors:
        await context.store.record_feedback(
            spec_id, source, changeNote, "reject", f"lint 실패: {errors}", current.version, None
        )
        raise tool_error(
            VALIDATION_ERROR,
            "수정본이 lint 를 통과하지 못해 반영하지 않았습니다. 기존 버전이 유지됩니다.",
            specId=spec_id,
            lintErrors=errors,
        )

    all_specs: list[SpecDocument] = []
    declared_ids = _blueprint_spec_ids(current.game_id, dict(blueprint.get("document") or {}))
    for summary in await context.store.list_specs(current.game_id):
        if str(summary["specId"]) not in declared_ids:
            continue
        candidate = await context.store.get_spec(str(summary["specId"]))
        if candidate is not None:
            all_specs.append(revised if candidate.spec_id == revised.spec_id else candidate)
    graph_errors = dependency_errors(all_specs)
    if graph_errors:
        raise tool_error(
            VALIDATION_ERROR,
            "specification dependency graph cannot be updated",
            specId=spec_id,
            dependencyErrors=graph_errors,
        )

    await context.store.save_spec(revised)
    blueprint_document = _blueprint_document(dict(blueprint.get("document") or {}), all_specs)
    blueprint_version = await context.store.save_blueprint(
        game_id=current.game_id,
        seed_prompt=str(blueprint.get("seedPrompt") or ""),
        title=str(blueprint_document.get("title") or blueprint["title"]),
        genre=str(blueprint_document.get("genre") or blueprint["genre"]),
        document=blueprint_document,
        evidence=dict(blueprint.get("evidence") or {}),
    )
    revised.blueprint_version = blueprint_version
    await context.store.save_spec(revised)
    feedback_id = await context.store.record_feedback(
        spec_id,
        source,
        changeNote,
        "adopt",
        "lint 통과 후 반영",
        current.version,
        revised.version,
    )

    payload = revised.to_dict()
    _attach_execution_prompt(payload, revised)
    payload["feedbackId"] = feedback_id
    payload["previousVersion"] = current.version
    return payload


#: spec_id 는 항상 "…spec-NNN" 으로 끝난다 (``_to_spec`` 이 그렇게 만든다).
_SPEC_NUMBER = re.compile(r"spec-(\d+)$")


def _next_spec_id(existing_specs: list[dict[str, Any]]) -> str:
    """게임의 기존 spec 다음 번호. 호출자가 번호를 고르면 겹칠 수 있어 서버가 정한다."""

    max_n = 0
    for item in existing_specs:
        match = _SPEC_NUMBER.search(str(item.get("specId") or ""))
        if match:
            max_n = max(max_n, int(match.group(1)))
    return f"spec-{max_n + 1:03d}"


@mcp.tool(
    description="Validate and add one host-authored spec to an existing game."
)
@expects_dict_return
async def add_spec(ctx: Context, gameId: str, spec: dict[str, Any]) -> dict[str, Any]:
    """기존 게임에 독립 spec 하나를 더한다."""

    game_id = _require(gameId, "gameId")
    context = _ctx(ctx)

    blueprint = await context.store.get_blueprint(game_id)
    if blueprint is None:
        raise tool_error(
            VALIDATION_ERROR, f"존재하지 않는 게임입니다: {game_id}", gameId=game_id
        )

    existing = await context.store.list_specs(game_id)
    next_id = _next_spec_id(existing)
    known_ids = set(await context.research.card_index())
    if not isinstance(spec, dict):
        raise tool_error(VALIDATION_ERROR, "spec은 객체여야 합니다", gameId=game_id)
    raw = dict(spec)

    # 호출자·모델이 무엇을 적었든 서버가 정한 번호로 못박는다.
    raw["specId"] = next_id

    try:
        guidance = await context.research.arch_guidance(raw.get("refs") or [])
    except Exception as exc:  # noqa: BLE001
        raise tool_error(
            MCP_ERROR, f"아키텍처 카드 조회 실패: {type(exc).__name__}: {exc}", gameId=game_id
        ) from exc

    try:
        new_spec = _to_spec(raw, game_id, int(blueprint["version"]) + 1, guidance)
    except KeyError as exc:
        # 호스트가 직접 채운 spec은 필드 누락이 생길 수 있다.
        raise tool_error(
            VALIDATION_ERROR, f"spec 에 필수 필드가 없습니다: {exc}", gameId=game_id
        ) from exc
    errors = lint_spec(new_spec, known_ids)
    if errors:
        raise tool_error(
            VALIDATION_ERROR,
            "spec 이 lint 를 통과하지 못해 추가하지 않았습니다.",
            gameId=game_id,
            specId=new_spec.spec_id,
            lintErrors=errors,
        )

    blueprint_document = dict(blueprint.get("document") or {})
    new_spec.status = _publication_state(blueprint_document)
    existing_documents: list[SpecDocument] = []
    declared_ids = _blueprint_spec_ids(game_id, blueprint_document)
    for summary in existing:
        if str(summary["specId"]) not in declared_ids:
            continue
        existing_spec = await context.store.get_spec(str(summary["specId"]))
        if existing_spec is not None:
            existing_documents.append(existing_spec)
    graph_errors = dependency_errors([*existing_documents, new_spec])
    if graph_errors:
        raise tool_error(
            VALIDATION_ERROR,
            "specification dependency graph cannot be updated",
            gameId=game_id,
            dependencyErrors=graph_errors,
        )

    blueprint_document = _blueprint_document(blueprint_document, [*existing_documents, new_spec])
    blueprint_version = await context.store.save_blueprint(
        game_id=game_id,
        seed_prompt=str(blueprint.get("seedPrompt") or ""),
        title=str(blueprint_document.get("title") or blueprint["title"]),
        genre=str(blueprint_document.get("genre") or blueprint["genre"]),
        document=blueprint_document,
        evidence=dict(blueprint.get("evidence") or {}),
    )
    new_spec.blueprint_version = blueprint_version
    await context.store.save_spec(new_spec)

    payload = new_spec.to_dict()
    _attach_execution_prompt(payload, new_spec)
    return payload


@mcp.tool(
    description=(
        "Export a published, dependency-valid game design as immutable files for an execution AI."
    )
)
@expects_dict_return
async def export_execution_handoff(ctx: Context, gameId: str) -> dict[str, Any]:
    """Create a versioned package under ``var/handoffs`` without leaking environment data."""

    game_id = _require(gameId, "gameId")
    context = _ctx(ctx)
    blueprint_record = await context.store.get_blueprint(game_id)
    if blueprint_record is None:
        raise tool_error(VALIDATION_ERROR, f"unknown gameId: {game_id}", gameId=game_id)

    blueprint = dict(blueprint_record.get("document") or {})
    if _publication_state(blueprint) != "published":
        raise tool_error(
            VALIDATION_ERROR,
            "draft game designs cannot be exported; revise the draft and publish it first",
            gameId=game_id,
        )

    expected_ids = _blueprint_spec_ids(game_id, blueprint)
    if not expected_ids:
        raise tool_error(VALIDATION_ERROR, "published design declares no specifications", gameId=game_id)
    summaries = await context.store.list_specs(game_id)
    specs: list[SpecDocument] = []
    for summary in summaries:
        if str(summary["specId"]) not in expected_ids:
            continue
        spec = await context.store.get_spec(str(summary["specId"]))
        if spec is not None:
            specs.append(spec)
    found_ids = {spec.spec_id for spec in specs}
    missing_specs = sorted(expected_ids - found_ids)
    if missing_specs:
        raise tool_error(
            VALIDATION_ERROR,
            "published design has missing specifications",
            gameId=game_id,
            missingSpecs=missing_specs,
        )
    not_published = [spec.spec_id for spec in specs if spec.status != "published"]
    if not_published:
        raise tool_error(
            VALIDATION_ERROR,
            "all specifications must be published before hand-off",
            gameId=game_id,
            draftSpecs=not_published,
        )
    graph_errors = dependency_errors(specs)
    if graph_errors:
        raise tool_error(
            VALIDATION_ERROR,
            "specification dependency graph cannot be exported",
            gameId=game_id,
            dependencyErrors=graph_errors,
        )

    try:
        exported = export_handoff(
            game_id=game_id,
            blueprint=blueprint,
            blueprint_version=int(blueprint_record["version"]),
            specs=specs,
            feature_prompts=[_to_feature_prompt(spec) for spec in specs],
        )
    except HandoffError as exc:
        raise tool_error(VALIDATION_ERROR, str(exc), gameId=game_id) from exc
    return {"gameId": game_id, **exported}


@mcp.tool(description="Report Research DB connectivity and search mode.")
@expects_dict_return
async def research_status(ctx: Context) -> dict[str, Any]:
    context = _ctx(ctx)
    index = await context.research.card_index()
    kinds: dict[str, int] = {}
    for card_id in index:
        kinds[card_id.split("-")[0]] = kinds.get(card_id.split("-")[0], 0) + 1
    return {
        "connected": True,
        "searchMode": context.research.search_mode,
        "cardCount": len(index),
        "cardsByKind": kinds,
        "semanticSearchAvailable": context.research.search_mode.startswith("vector"),
        "note": (
            ""
            if context.research.search_mode.startswith("vector")
            else "Install sentence-transformers to enable semantic search."
        ),
    }


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    serve(mcp)
