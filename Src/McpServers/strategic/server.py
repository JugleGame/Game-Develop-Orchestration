"""StrategicMcpServer — 리서치 카드에 근거한 2D 기획 서버.

§03 계약의 ``generate_game_design`` 을 제공하면서, 그 안에서
``prompts/6_planner.md`` 의 3단계를 수행한다.

    아이디어 → ① 근거 조회(시너지/반례) → ② 청사진 → ③ spec 분해 → 검사 → 발행

§03 이 다루지 않는 요구사항 넷을 위해 도구를 추가로 노출한다.

* **청사진 통째가 아니라 기능 단위 전달** — ``featurePrompts`` 는 spec 한 장씩이며,
  각 프롬프트에 목표·구현 범위·제외 범위·합격 기준·Unity 힌트가 모두 들어간다.
  청사진 자체는 개발 AI 에게 넘기지 않는다 (§4 규칙).
* **진행 상황 확인과 재발행** — spec 은 Neon 에 저장된다. ``list_specs`` /
  ``get_spec`` 으로 언제든 다시 꺼낼 수 있다.
* **개발/QA 피드백 반영** — ``revise_spec`` 이 버전을 올리고 change_log 를 남긴다.
* **Unity 가 어려움 없이 구현** — 합격 기준을 숫자·관찰 사실로 강제하고
  ``unityHints`` 로 컴포넌트·씬 오브젝트·필요 에셋을 명시한다. 아키텍처
  카드(``ARCH-###``)를 인용한 spec 에는 그 카드의 구현 절차·안티패턴·검증
  방법이 **원문 그대로** 실린다. 카드를 읽고 해석하는 지점을 기획 한 곳으로
  모으는 장치다 — 개발 AI 와 QA 가 각자 카드를 다시 읽으면 해석이 갈리고,
  그 갈림은 인용만 보는 검사(S3)로는 드러나지 않는다 (``arch_cards.py``).
* **청사진 작성 전 사람 검토** — ``propose_concept`` 이 DB 근거만으로 제안을
  만들고, ``decide_concept`` 으로 사람이 승인/수정/거부한다. LLM 을 쓰지 않는
  ``generate_game_design`` 이전 단계이므로 API 키 유무와 무관하게 동작한다
  (``concepts.py``).
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from common import registry
from common.errors import MCP_ERROR, VALIDATION_ERROR, tool_error
from common.server import expects_dict_return, parse_transport

from .arch_cards import ArchGuidance, arch_ids
from .concepts import ConceptProposal, ConceptStore, adjust_evidence
from .neon_http import connect_pool
from .planner import Planner, PlanningError
from .research_repo import COUNTEREXAMPLE_MISSING, ResearchRepository, SentenceTransformerEmbedder
from .specs import SpecDocument, SpecStore, lint_spec

logger = logging.getLogger(__name__)

RESEARCH_DSN = os.getenv("RESEARCH_DSN") or os.getenv("NEON_DSN", "")


@dataclass
class StrategicContext:
    # asyncpg.Pool 또는 NeonHttpPool(사내망이 5432 를 막는 환경의 HTTP 폴백) —
    # connect_pool() 이 이 서버가 쓰는 부분에 한해 계약을 맞춰 준다.
    pool: Any
    research: ResearchRepository
    store: SpecStore
    planner: Planner
    concepts: ConceptStore


@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncIterator[StrategicContext]:
    """Neon 풀을 한 번만 열고 반드시 닫는다."""

    if not RESEARCH_DSN:
        raise RuntimeError("RESEARCH_DSN(또는 NEON_DSN) 환경변수가 필요합니다.")

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
                "sentence-transformers 미설치 — 트라이그램 검색으로 동작합니다. "
                "의미 검색을 켜려면 `pip install sentence-transformers` 후 재시작하세요."
            )
        store = SpecStore(pool)
        await store.init_schema()
        concepts = ConceptStore(pool)
        await concepts.init_schema()
        yield StrategicContext(
            pool=pool,
            research=ResearchRepository(pool, embedder),
            store=store,
            planner=Planner(),
            concepts=concepts,
        )
    finally:
        logger.info("Closing research DB pool …")
        await pool.close()


mcp = FastMCP(
    "StrategicMcpServer",
    instructions=(
        "리서치 카드 DB에 근거해 2D 게임을 기획한다. 모든 주장은 카드 ID로 "
        "뒷받침되며, 반례 없이 기획을 통과시키지 않는다. 청사진은 기능(spec) 단위로 "
        "쪼개어 전달하고, 개발/QA 피드백을 받아 버전을 올려 재발행한다."
    ),
    lifespan=lifespan,
    host=os.getenv("MCP_HOST", "127.0.0.1"),
    # 포트는 common.registry 한 곳에서만 정한다. 이 서버는 lifespan 이 필요해
    # common.server.build() 를 쓰지 못하므로 표를 직접 참조한다.
    port=registry.get("strategic").resolved_port(),
)


def _ctx(ctx: Context) -> StrategicContext:
    return ctx.request_context.lifespan_context  # type: ignore[return-value]


def _require(value: str, field: str) -> str:
    if not value or not value.strip():
        raise tool_error(VALIDATION_ERROR, f"{field} must not be empty")
    return value.strip()


def _slug(text: str, limit: int = 32) -> str:
    slug = re.sub(r"[^0-9a-z가-힣]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "game"


# ---------------------------------------------------------------------------
# §03 계약 도구
# ---------------------------------------------------------------------------
@mcp.tool(
    description=(
        "아이디어를 리서치 카드로 검증해 2D 기획서를 만들고, "
        "기능(spec) 단위 구현 프롬프트로 분해해 반환한다."
    )
)
@expects_dict_return
async def generate_game_design(ctx: Context, prompt: str, gameId: str = "") -> dict[str, Any]:
    """§5.1 GameDesignDocument + §5.2 FeatureImplementationPrompt[] 를 돌려준다.

    ``featurePrompts[].description`` 에는 spec 전문(목표/구현 범위/제외 범위/
    합격 기준/Unity 힌트/아키텍처 지침)이 들어간다. 개발 AI 는 이것만 보고
    구현할 수 있어야 하며, 청사진 원문은 넘기지 않는다.
    """

    prompt = _require(prompt, "prompt")
    context = _ctx(ctx)
    game_id = gameId.strip() or f"game-{_slug(prompt, 24)}"

    # ① 근거 조회 — 시너지와 반례
    try:
        evidence = await context.research.gather_evidence(prompt)
    except Exception as exc:  # noqa: BLE001
        raise tool_error(MCP_ERROR, f"리서치 DB 조회 실패: {type(exc).__name__}: {exc}") from exc

    # ②③ 청사진 + spec 분해
    try:
        plan, plan_usage = await context.planner.plan(prompt, evidence)
    except PlanningError as exc:
        raise tool_error(VALIDATION_ERROR, str(exc), gameId=game_id) from exc
    except Exception as exc:  # noqa: BLE001
        raise tool_error(MCP_ERROR, f"기획 생성 실패: {type(exc).__name__}: {exc}") from exc

    known_ids = set(await context.research.card_index())
    blueprint_version = await context.store.save_blueprint(
        game_id=game_id,
        seed_prompt=prompt,
        title=plan["title"],
        genre=plan["genre"],
        document=plan,
        evidence=evidence.to_dict(),
    )

    # 인용된 아키텍처 카드의 원문 지침을 한 번에 읽어 온다. 모델 출력에서
    # 가져오지 않는 이유는 arch_cards.py 에 적혀 있다 — 모델은 어느 카드를 쓸지만
    # 고르고(refs), 절차·안티패턴·검증 방법은 코드가 카드에서 옮긴다.
    #
    # 청사진 저장 뒤에 읽는다. 이건 부수적인 조회인데 앞에 두면, 여기서 DB 가
    # 한 번 흔들릴 때 방금 모델이 만든(그리고 돈을 쓴) 청사진까지 함께 날아간다.
    try:
        guidance = await context.research.arch_guidance(
            [ref for raw in plan["specs"] for ref in raw.get("refs", [])]
        )
    except Exception as exc:  # noqa: BLE001
        raise tool_error(
            MCP_ERROR,
            f"아키텍처 카드 조회 실패: {type(exc).__name__}: {exc}",
            gameId=game_id,
            blueprintVersion=blueprint_version,
        ) from exc

    # 검사 후 저장. 통과하지 못한 spec 은 개발 AI 에게 넘기지 않는다.
    published: list[SpecDocument] = []
    rejected: list[dict[str, Any]] = []
    for raw in plan["specs"]:
        spec = _to_spec(raw, game_id, blueprint_version, guidance)
        errors = lint_spec(spec, known_ids)
        if errors:
            rejected.append({"specId": spec.spec_id, "title": spec.title, "errors": errors})
            continue
        spec.status = "published"
        await context.store.save_spec(spec)
        published.append(spec)

    if not published:
        raise tool_error(
            VALIDATION_ERROR,
            "lint 를 통과한 spec 이 하나도 없어 개발 AI 에게 전달할 수 없습니다.",
            gameId=game_id,
            rejected=rejected,
        )

    design = {
        "game_id": game_id,
        "genre": plan["genre"],
        "core_mechanics": plan["coreMechanics"],
        "art_style": plan["artStyle"],
        "target_platform": "PC",
        "structure_overview": plan["structureOverview"],
        "created_at": _utcnow(),
    }

    return {
        "gameDesign": design,
        "featurePrompts": [_to_feature_prompt(spec) for spec in published],
        # 이 호출이 쓴 토큰. 오케스트레이터가 잡별 비용으로 합산한다.
        "usage": plan_usage,
        # 아래는 §03 밖 진단 정보 — 오케스트레이터는 무시한다.
        "blueprintVersion": blueprint_version,
        "evidence": evidence.to_dict(),
        "counterEvidence": plan["counterEvidence"],
        "counterEvidenceNote": plan["counterEvidenceNote"] or evidence.counterexample_note(),
        "maxRisk": plan["maxRisk"],
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
        unity_hints=raw.get("unityHints", {}),
        dependencies=[f"{game_id}__{d}" for d in raw.get("dependencies", [])],
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
    """spec → §5.2 FeatureImplementationPrompt.

    ``description`` 이 곧 개발 AI 가 받는 전부다. 청사진은 넘기지 않으므로
    여기에 구현에 필요한 것이 모두 담겨야 한다.
    """

    hints = spec.unity_hints or {}
    lines = [
        f"## 목표\n{spec.goal}",
        "\n## 구현 범위\n" + "\n".join(f"- {x}" for x in spec.implementation_scope),
        "\n## 제외 범위\n" + "\n".join(f"- {x}" for x in spec.out_of_scope),
        "\n## 합격 기준\n" + "\n".join(f"- {x}" for x in spec.acceptance_criteria),
    ]
    if hints:
        lines.append(
            "\n## Unity 구현 힌트"
            + f"\n- 컴포넌트: {', '.join(hints.get('components') or []) or '-'}"
            + f"\n- 씬 오브젝트: {', '.join(hints.get('sceneObjects') or []) or '-'}"
            + f"\n- 필요 에셋: {', '.join(hints.get('assetsNeeded') or []) or '-'}"
            + (f"\n- 비고: {hints['notes']}" if hints.get("notes") else "")
        )
    # 아키텍처 카드 원문. 개발 AI 가 카드를 직접 읽지 않아도(읽으면 해석이 갈린다)
    # 절차·안티패턴·검증 방법을 그대로 받는다.
    if spec.architecture:
        lines.append(
            "\n## 아키텍처 지침 (카드 원문 — 요약하거나 범위를 늘리지 말 것)\n"
            + "\n\n".join(g.to_markdown() for g in spec.architecture)
        )
    lines.append(f"\n## 근거 카드\n{', '.join(spec.refs)}")

    return {
        "feature_id": spec.spec_id,
        "title": spec.title,
        "description": "\n".join(lines),
        "priority": "P0" if not spec.dependencies else "P1",
        "dependencies": spec.dependencies,
        # 위 "필요 에셋" 줄과 같은 값을 구조체로도 낸다. 문장으로만 주면 AssetGen 이
        # 긴 description 전체를 프롬프트로 삼게 되고, 어떤 자산을 만들지가
        # 키워드 등장 순서로 정해진다 (05_계약_변경_제안서 §4.4).
        "assets_needed": list(hints.get("assetsNeeded") or []),
    }


# ---------------------------------------------------------------------------
# 요구사항 전용 도구
# ---------------------------------------------------------------------------
@mcp.tool(description="아이디어에 대한 시너지 근거와 반례 카드를 조회한다 (기획 없이 근거만).")
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
            f"{COUNTEREXAMPLE_MISSING} — 기획 규칙상 반례는 최소 1장이 필요합니다. "
            "질의를 바꾸거나 카드 생성 요청을 남기세요."
        )
    return payload


# ---------------------------------------------------------------------------
# 아이디어 제안 — 청사진(generate_game_design) 이전, 사람이 검토하는 단계
# ---------------------------------------------------------------------------
@mcp.tool(
    description=(
        "DB 근거를 모아 아이디어 제안을 만들고 사람 검토를 기다리는 상태로 저장한다 "
        "(LLM 미사용, generate_game_design 이전 단계)."
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
        else f"{COUNTEREXAMPLE_MISSING} — 승인 전에 반례를 보강할지 검토하세요."
    )
    return payload


@mcp.tool(description="사람 검토를 기다리는 아이디어 제안 전체 목록을 반환한다 (게임 무관, 리뷰어 받은편지함).")
@expects_dict_return
async def list_pending_concepts(ctx: Context) -> dict[str, Any]:
    pending = await _ctx(ctx).concepts.list_pending()
    return {"pending": pending, "count": len(pending)}


@mcp.tool(description="한 게임의 아이디어 제안 전문(근거 포함)을 반환한다.")
@expects_dict_return
async def get_concept(ctx: Context, gameId: str) -> dict[str, Any]:
    game_id = _require(gameId, "gameId")
    concept = await _ctx(ctx).concepts.get(game_id)
    if concept is None:
        raise tool_error(VALIDATION_ERROR, f"존재하지 않는 gameId: {game_id}")
    return concept.to_dict()


@mcp.tool(
    description=(
        "아이디어 제안을 승인(approve)/수정(revise)/거부(reject) 한다. "
        "승인되면 그 idea 를 generate_game_design 의 prompt 로 넘겨 청사진 작성을 시작한다."
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
    """§03 밖 — 청사진 작성 이전에 사람이 개입하는 게이트.

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
            f"generate_game_design(prompt=idea, gameId=\"{game_id}\") 를 호출해 청사진을 작성하세요."
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


@mcp.tool(description="한 게임의 spec 목록과 진행 상태를 반환한다.")
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


@mcp.tool(description="spec 한 장을 전문(마크다운 포함)으로 다시 발행한다.")
@expects_dict_return
async def get_spec(ctx: Context, specId: str) -> dict[str, Any]:
    """언제든 재발행 — 개발 AI 가 잃어버려도 여기서 다시 받는다."""

    spec_id = _require(specId, "specId")
    context = _ctx(ctx)
    spec = await context.store.get_spec(spec_id)
    if spec is None:
        raise tool_error(VALIDATION_ERROR, f"존재하지 않는 specId: {spec_id}")

    payload = spec.to_dict()
    payload["featurePrompt"] = _to_feature_prompt(spec)
    payload["feedbackHistory"] = await context.store.feedback_history(spec_id)
    return payload


@mcp.tool(description="개발 AI 또는 QA AI 의 피드백을 반영해 spec 을 수정하고 버전을 올린다.")
@expects_dict_return
async def revise_spec(
    ctx: Context, specId: str, feedback: str, source: str = "developer"
) -> dict[str, Any]:
    """``6_planner.md`` §6 Inbox 규칙 구현.

    피드백은 채택 여부와 이유를 함께 기록한다. 수정본은 lint 를 통과해야만
    저장되며, 실패하면 기존 버전이 그대로 남는다 (나쁜 spec 이 개발 AI 에게
    전달되는 것을 막는다).
    """

    spec_id = _require(specId, "specId")
    feedback = _require(feedback, "feedback")
    if source not in ("developer", "qa", "human"):
        raise tool_error(
            VALIDATION_ERROR, f"source 는 developer|qa|human 이어야 합니다: {source}"
        )

    context = _ctx(ctx)
    current = await context.store.get_spec(spec_id)
    if current is None:
        raise tool_error(VALIDATION_ERROR, f"존재하지 않는 specId: {spec_id}")

    known_ids = set(await context.research.card_index())
    cards = await context.research.get_cards(current.refs)
    card_text = "\n".join(f"- {c.card_id} {c.title}: {c.summary}" for c in cards) or "(없음)"

    try:
        revised_raw, revise_usage = await context.planner.revise_spec(
            current.to_markdown(), feedback, source, card_text
        )
    except PlanningError as exc:
        raise tool_error(VALIDATION_ERROR, str(exc), specId=spec_id) from exc
    except Exception as exc:  # noqa: BLE001
        raise tool_error(MCP_ERROR, f"spec 수정 실패: {type(exc).__name__}: {exc}") from exc

    # 수정본의 refs 기준으로 아키텍처 지침을 **DB 에서 다시** 붙인다. 모델은
    # 이 세 절을 담을 칸이 애초에 없지만(_SPEC_SCHEMA), 그래서 수정 때마다
    # 다시 붙이지 않으면 인용은 남고 내용은 사라진다.
    try:
        guidance = await context.research.arch_guidance(revised_raw["refs"])
    except Exception as exc:  # noqa: BLE001
        raise tool_error(
            MCP_ERROR, f"아키텍처 카드 조회 실패: {type(exc).__name__}: {exc}"
        ) from exc

    revised = SpecDocument(
        spec_id=current.spec_id,
        game_id=current.game_id,
        title=revised_raw["title"],
        version=current.version + 1,
        blueprint_version=current.blueprint_version,
        refs=revised_raw["refs"],
        goal=revised_raw["goal"],
        implementation_scope=revised_raw["implementationScope"],
        out_of_scope=revised_raw["outOfScope"],
        acceptance_criteria=revised_raw["acceptanceCriteria"],
        unity_hints=revised_raw.get("unityHints", {}),
        dependencies=current.dependencies,
        status="published",
        architecture=_architecture_for(revised_raw["refs"], guidance),
        change_log=[
            *current.change_log,
            {
                "version": current.version + 1,
                "source": source,
                "change": feedback[:300],
            },
        ],
    )

    errors = lint_spec(revised, known_ids)
    if errors:
        await context.store.record_feedback(
            spec_id, source, feedback, "reject", f"lint 실패: {errors}", current.version, None
        )
        raise tool_error(
            VALIDATION_ERROR,
            "수정본이 lint 를 통과하지 못해 반영하지 않았습니다. 기존 버전이 유지됩니다.",
            specId=spec_id,
            lintErrors=errors,
        )

    await context.store.save_spec(revised)
    feedback_id = await context.store.record_feedback(
        spec_id, source, feedback, "adopt", "lint 통과 후 반영", current.version, revised.version
    )

    payload = revised.to_dict()
    payload["featurePrompt"] = _to_feature_prompt(revised)
    payload["feedbackId"] = feedback_id
    payload["previousVersion"] = current.version
    payload["usage"] = revise_usage
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
    description=(
        "이미 있는 게임에 spec 한 장을 새로 추가한다 (청사진·기존 spec 은 건드리지 "
        "않는다). spec 인자로 완성된 spec 을 넘기면 모델을 부르지 않는다."
    )
)
@expects_dict_return
async def add_spec(
    ctx: Context, gameId: str, idea: str = "", spec: dict[str, Any] | None = None
) -> dict[str, Any]:
    """``revise_spec``(기존 spec 개정)과 ``generate_game_design``(전체 재기획)
    사이에 비어 있던 자리 — **기존 게임에 독립 spec 하나를 더하는 것.**

    specId 는 이 서버가 게임의 기존 spec 다음 번호로 정한다. ``spec`` 인자로
    넘긴 값에 specId 가 있어도 무시한다 — 호출자가 번호를 잘못 짚어 기존 spec 을
    덮어쓰는 사고를 원천적으로 없앤다.

    ``spec`` 을 채우면 서버는 모델을 호출하지 않고 **검증만** 한다 (경로 B).
    넘긴 값도 생성한 값과 똑같이 lint 되므로 이 우회가 규칙에 구멍을 내지 않는다.
    비우면 ``idea`` 로 모델을 불러 spec 하나를 만든다.
    """

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
    usage: dict[str, Any] | None = None

    if spec is not None:
        raw = dict(spec)
    else:
        idea = _require(idea, "idea (spec 를 직접 넘기지 않을 때는 필수)")
        try:
            evidence = await context.research.gather_evidence(idea)
        except Exception as exc:  # noqa: BLE001
            raise tool_error(
                MCP_ERROR, f"리서치 DB 조회 실패: {type(exc).__name__}: {exc}", gameId=game_id
            ) from exc
        try:
            raw, usage = await context.planner.add_spec(
                idea, evidence, blueprint["genre"], [s["title"] for s in existing], next_id
            )
        except PlanningError as exc:
            raise tool_error(VALIDATION_ERROR, str(exc), gameId=game_id) from exc
        except Exception as exc:  # noqa: BLE001
            raise tool_error(
                MCP_ERROR, f"spec 생성 실패: {type(exc).__name__}: {exc}", gameId=game_id
            ) from exc

    # 호출자·모델이 무엇을 적었든 서버가 정한 번호로 못박는다.
    raw["specId"] = next_id

    try:
        guidance = await context.research.arch_guidance(raw.get("refs") or [])
    except Exception as exc:  # noqa: BLE001
        raise tool_error(
            MCP_ERROR, f"아키텍처 카드 조회 실패: {type(exc).__name__}: {exc}", gameId=game_id
        ) from exc

    try:
        new_spec = _to_spec(raw, game_id, blueprint["version"], guidance)
    except KeyError as exc:
        # generate_game_design 의 raw 는 모델의 구조화 출력이라 항상 완전하지만,
        # 여기서는 spec 인자를 호출자가 직접 채울 수 있어 누락이 생길 수 있다.
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

    new_spec.status = "published"
    await context.store.save_spec(new_spec)

    payload = new_spec.to_dict()
    payload["featurePrompt"] = _to_feature_prompt(new_spec)
    # 넘겨받은 spec 은 지출이 0 이므로 usage 를 싣지 않는다.
    if usage is not None:
        payload["usage"] = usage
    return payload


@mcp.tool(description="리서치 DB 연결 상태와 검색 모드를 보고한다.")
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
            else "sentence-transformers 를 설치하면 의미 검색이 켜집니다."
        ),
    }


if __name__ == "__main__":
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    mcp.run(transport=parse_transport())
