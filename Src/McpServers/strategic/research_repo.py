"""리서치 카드 DB(Neon) 읽기 전용 접근 계층.

``Game-Design-and-Planning_resarch`` 의 카드 거울을 조회한다. 그 저장소의
철칙은 **"md 파일이 원본, DB는 거울, 손으로 INSERT 금지"** 이므로 이 모듈은
``cards`` / ``card_refs`` / ``digests`` 에 대해 **SELECT 만** 수행한다.

시너지와 반례를 데이터로 정의한다 (``prompts/6_planner.md`` §3 규칙):

* **지지 근거(시너지)** — 질의와 유사하면서 성공 사례이거나 설계 요소인 카드
  (``GAME/success``, ``ELEM/*``, ``GENRE/*``).
* **반례** — 질의와 유사한 **실패·혼재 사례** (``GAME`` 이면서
  ``type IN ('failure','mixed')``). 기획 규칙상 **최소 1장이 필수**이고,
  없으면 비워두지 말고 "반례 조사 부족"이라고 명시해야 한다.
* **아키텍처** — ``ARCH`` 카드. "무엇을 만들까"가 아니라 "어떻게 만들까"를
  정하므로 위 둘과 몫을 나누지 않고 따로 뽑는다. 이 종류만 본문까지 읽는다
  (``arch_guidance``) — 그 이유는 ``arch_cards.py`` 에 적어 두었다.

검색은 두 신호를 **하나의 순위로 융합한다** (Reciprocal Rank Fusion).

1. **의미 유사도** — ``card_sections.embedding`` (pgvector, 1024차원, 정규화됨).
   질의 벡터는 카드를 만들 때 쓴 것과 **같은 모델**로 뽑아야 좌표계가 맞는다
   (``tools/embed_cards.py`` 기본값 ``BAAI/bge-m3``).
2. **표층 유사도** — ``pg_trgm`` 트라이그램. 임베딩 모델을 설치할 수 없는
   환경에서도 검색이 동작하게 만드는 대비책이기도 하다.

둘은 잘하는 질의가 다르다. 임베딩은 의역("죽을 때마다 무작위 업그레이드를 뽑는")
을 잡고 고유명사를 뭉갠다 — 게임 제목은 의미가 아니라 글자이기 때문이다.
트라이그램은 정확히 그 반대다. 실측으로 고유명사 질의 17개의 recall@6 이
벡터 단독 12/17 에서 융합 후 17/17 이 됐다 (``_HYBRID_SQL`` 위의 표).
연구 저장소의 ``tools/search_cards.py`` 가 같은 방식으로 검색한다 — 한쪽만
고치면 같은 질의에 다른 근거가 나온다.

## 스키마 v2 — 회수 단위가 카드에서 **절**로 바뀌었다 (2026-08-12)

연구 저장소의 진단에서 드러난 것: 이전 임베딩 모델 ``ko-sroberta`` 는 입력 창이
128 토큰인데 카드 임베딩 텍스트는 중앙값 811 토큰이었다. **카드 168 장 전부가
잘려 실제로는 카드의 15.8% 만 벡터에 들어가 있었다.** ``## 실패 사례`` ·
``## 리스크`` · ``## 안티패턴`` 같은 절은 벡터 공간에 존재한 적이 없다 —
반례 검색이 구조적으로 불가능했다는 뜻이다.

바뀐 것:

* 벡터 대상 ``cards`` → ``card_sections`` (절 하나가 한 벡터). 검색은 절에서
  하고, 카드 단위로 접어서(``DISTINCT ON``) 돌려준다. 계약(``Card``)은 그대로다.
* 모델 ``ko-sroberta``(128 창 / 768 차원) → ``bge-m3``(8192 창 / 1024 차원).
* 반례 정의가 **카드 속성에서 절 속성으로** 바뀌었다. 아래 ``COUNTER_SECTION_KEYS``.
* ``Card.matched_section`` 이 붙었다 — 어느 절이 걸렸는지. 기존 필드는 그대로다.
"""

from __future__ import annotations

import logging
import os
import ssl
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

import asyncpg
import certifi

from .arch_cards import GUIDANCE_SECTIONS, ArchGuidance, arch_ids, guidance_from_sections

logger = logging.getLogger(__name__)

# tools/embed_cards.py 와 반드시 같아야 한다. 다르면 벡터 공간이 어긋난다.
# 차원이 다르면 pgvector 가 즉시 에러를 내므로 조용히 틀리지는 않는다.
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"
EMBEDDING_MODEL = os.getenv("RESEARCH_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL)
EMBEDDING_DIM = 1024

COUNTEREXAMPLE_MISSING = "Insufficient counterexample evidence"

# 반례가 사는 절. **내용만으로 반례임이 분명한 절**만 넣는다.
#
# 예전 정의는 ``kind='GAME' AND type IN ('failure','mixed')`` 였다. 그 정의로는
# 실패 사례 카드가 아닌 곳에 적힌 위험 신호(ELEM 의 ``## 리스크``, ARCH 의
# ``## 안티패턴``)에 도달하지 못한다 — 반례는 카드가 아니라 절의 속성이다.
#
# 다만 절이면 무엇이든 반례인 것은 아니다. 실측으로 두 절을 빼야 했다
# (2026-08-12, evals/retrieval.jsonl):
#
# * ``gaps`` (GENRE 빈칸) — 빈칸은 **기회**지 반례가 아니다. ret-005 에서
#   GENRE-011/GENRE-010 의 빈칸 절이 진짜 반례인 GAME-025(메이플스토리 큐브
#   확률 조작 제재)를 밀어냈다.
# * ``cause_analysis`` (GAME 성공/실패 원인) — 무조건은 아니고 아래 참조.
COUNTER_SECTION_KEYS = [
    "failure_cases",     # ELEM: Failure Cases
    "risk",              # ELEM: Risks
    "antipatterns",      # ARCH: Anti-patterns
    "market_saturation", # GENRE: Market Saturation
]

# ``## 성공/실패 원인`` 은 실패·혼재 사례 카드일 때만 반례다.
#
# type='success' 카드의 이 절은 대개 **성공한 이유**를 적는다. 그것을 반례로
# 내밀면 기획자는 자기 아이디어의 성공 사례를 반증으로 읽는다 — ret-002 에서
# GAME-001(Inscryption)이 정확히 그렇게 잘못 분류됐다.
#
# 대가는 있다. 성공 카드 안에 섞여 있는 실패 문단(GAME-031 Balatro 의 PEGI 18+
# 재분류로 유럽 콘솔 판매가 멈춘 사건)은 이 조건에서 반례로 잡히지 않는다.
# 그래도 이쪽을 고른 이유: **오도하는 것이 놓치는 것보다 나쁘다.** 놓친 카드도
# 지지 근거로는 올라오므로 기획자가 본문에서 읽을 수 있지만, 잘못 분류된 반례는
# 기획자가 그것을 반증으로 믿게 만든다.
#
# 문단 단위로 사실/실패를 가르려면 카드 쪽에 그 표시가 있어야 한다 (지금은
# "실패 지점:" 이 관례적으로만 쓰인다) — 절 단위 분해로는 여기까지가 한계다.
_COUNTER_PREDICATE = (
    "(s.section_key = ANY($6::text[])"
    " OR (s.section_key = 'cause_analysis'"
    "     AND c.kind = 'GAME' AND c.type IN ('failure','mixed')))"
)

# 반례 후보를 몇 배로 넉넉히 뽑은 뒤 실제 사례를 앞으로 당길지.
#
# 순위만으로 자르면 **일반적인 위험 서술이 실제 사건을 이긴다.** 실측
# (2026-08-12, ret-005 "가챠 확률과 천장 시스템이 과금 신뢰에 미치는 영향"):
# 진짜 반례인 GAME-025(메이플스토리 큐브 확률 조작, 공정위 제재)가 7위였고,
# 그 위를 ELEM 들의 ``## 리스크`` 절이 채웠다 — "가챠는 위험할 수 있다"는
# 서술이 "가챠 확률을 조작해 제재받았다"는 사건을 밀어낸 것이다.
#
# 반례의 뜻은 '이 아이디어가 실제로 어긋난 사례'다. 그래서 GAME 실패·혼재
# 카드를 먼저 채우고 남는 자리를 나머지 위험 절로 메운다.
_COUNTER_OVERFETCH = 4


def _cases_first(cards: list[Card]) -> list[Card]:
    """실제 사례(GAME 실패·혼재)를 앞으로. 그룹 안의 검색 순위는 유지한다."""

    cases = [c for c in cards if c.kind == "GAME"]
    rest = [c for c in cards if c.kind != "GAME"]
    return cases + rest

# 반례로 인정할 최소 코사인 유사도. **벡터 검색일 때만** 적용한다.
#
# 반례 질의는 ``kind='GAME' AND type IN ('failure','mixed')`` 라는 하드 필터라,
# 하한선이 없으면 그 풀이 작을 때 **질의와 무관한 카드가 무조건 올라온다.**
# 기획자는 그걸 "이 아이디어의 반례"로 읽으므로, 없는 것보다 나쁘다.
#
# 값의 근거 (2026-07-29, 카드 55장·반례 풀 10장, ``inspect_retrieval_scores.py``):
#
#   * 사람이 봐서 맞는 반례 — ret-004 → GAME-005(Twelve Minutes) 0.4921,
#     ret-005 → GAME-025(메이플스토리 큐브 확률조작) 0.4805
#   * 나머지 6개 질의의 최상위(= 전부 노이즈) 천장 — 0.4150
#
# 0.4150 과 0.4805 사이면 같은 분할이 나오고, 0.45 는 양쪽에 여유를 둔 가운데다.
# 트라이그램 모드에서는 점수 척도가 완전히 달라(0.03대) 이 값을 쓰면 반례가 전멸
# 한다 — §2.3 이 "임베딩 없이는 재료가 없다"고 적어둔 이유다.
#
# ⚠ 이 값은 **지금 사실상 아무것도 거르지 않는다** (2026-08-12 재측정).
#
# 위 근거는 ko-sroberta 좌표계에서 나왔다. bge-m3 로 바꾸면서 코사인 분포가
# 통째로 올라갔고, 노이즈가 0.45 아래로 내려오지 않는다:
#
#   진짜 반례   GAME-005 0.5056 · GAME-025 0.5028
#   노이즈      GAME-042(CS2, 가챠 질의에) 0.4643 · ELEM-035(접객 루프,
#               루프 내러티브 질의에) 0.5364
#
# 즉 **하한선 하나로 가르는 분할선이 더는 존재하지 않는다.** 0.50 으로 올리면
# GAME-025 를 여유 0.003 으로 자르면서 그보다 높은 노이즈는 그대로 남는다.
# 그래서 새 숫자를 지어내지 않고 값을 둔 채로 이 사실을 적어 둔다.
#
# 지금 반례 품질을 실제로 지키는 것은 이 값이 아니라 아래 둘이다.
#   1. 지지 근거로 쓴 카드를 반례에서 제외 (gather_evidence)
#   2. 실제 사례(GAME 실패·혼재)를 앞으로 당기기 (_cases_first)
# 두 장치로 evals/retrieval.jsonl 의 counterexample_precision 이 1.00 이다.
#
# 다시 재려면: RESEARCH_DSN 을 걸고 inspect_retrieval_scores.py.
COUNTEREXAMPLE_MIN_SCORE = float(os.getenv("RESEARCH_COUNTEREXAMPLE_MIN_SCORE", "0.45"))

# 아키텍처 후보 기본 개수. 지지 근거(6)보다 크게 잡는다 — 한 기획은 spec 을
# 3 개 이상으로 쪼개고 spec 마다 필요한 구조가 다른데, 검색은 아이디어 한 줄로 **한 번**
# 만 돈다. 후보가 spec 수보다 적으면 어떤 spec 은 남의 카드를 받거나 빈손이 된다.
#
# 실제 사고 (2026-08-02, warrior-loot 기획): 이 값이 3 이던 시절, 카드 27 장 중
# 3 장만 후보로 올라갔다. 기획은 그 3 장을 100% 썼는데도 몬스터 spec 이
# 플레이어 이동 카드(ARCH-009)를 인용했다 — 고를 것이 그것뿐이었기 때문이다.
#
# 올려도 안전한 이유는 아래 architecture 검색부 주석과 같다: 무관한 카드가
# 올라와도 기획이 refs 에 넣지 않으면 아무 일도 일어나지 않는다. 비용은
# 프롬프트 토큰뿐이고 _truncate 가 카드당 100자로 자른다.
ARCH_LIMIT_DEFAULT = int(os.getenv("RESEARCH_ARCH_LIMIT", "12"))

# 검색 순위와 무관하게 **항상** 후보에 넣는 카드.
#
# 규약·기반 카드는 의미 유사도로 뽑히지 않는다. "전사가 장비를 파밍한다"는
# 문장은 폴더 규약이나 로그 형식과 의미가 가까울 이유가 없는데, 그 규약을
# 어기면 QA 1층에서 전체 FAIL 이 난다. 즉 **중요도와 유사도가 반대로 노는**
# 카드들이라 검색으로 풀 문제가 아니라 상수로 풀 문제다.
PINNED_ARCH_IDS: list[str] = [
    "ARCH-001",  # 이벤트 버스 — 방송 규칙의 출처
    "ARCH-008",  # 폴더·네이밍 규약 — QA B4 의 판정 근거
    "ARCH-010",  # 로그 규약 — QA 가 판정에 쓰는 증거의 형식
    "ARCH-011",  # 매니저 수명 — 씬 재로드 때 상태가 증발하는 사고의 예방
]


class EmbedderProtocol(Protocol):
    async def embed(self, text: str) -> list[float]: ...


@dataclass(frozen=True)
class Card:
    """리서치 카드 한 장. 인용은 반드시 ``card_id`` 로 한다."""

    card_id: str
    kind: str
    type: str
    title: str
    summary: str
    tags: list[str]
    elements: list[str]
    genres: list[str]
    confidence: str
    updated: str
    score: float = 0.0
    matched_by: str = ""
    # 이 카드에서 실제로 걸린 절의 ``section_key`` (예: ``failure_cases``).
    # 검색이 절 단위로 바뀌면서 생겼다 - 같은 카드라도 어느 절 때문에 올라왔는지가
    # 기획 판단에 필요하다. 명시 조회(get_cards)나 고정 카드는 빈 문자열이다.
    matched_section: str = ""

    def cite(self) -> str:
        return f"{self.card_id} ({self.title})"

    def to_dict(self) -> dict[str, Any]:
        return {
            "cardId": self.card_id,
            "kind": self.kind,
            "type": self.type,
            "title": self.title,
            "summary": self.summary,
            "tags": self.tags,
            "elements": self.elements,
            "genres": self.genres,
            "confidence": self.confidence,
            "updated": self.updated,
            "score": round(self.score, 4),
            "matchedBy": self.matched_by,
            "matchedSection": self.matched_section,
        }


@dataclass
class ResearchEvidence:
    """한 아이디어에 대한 근거 묶음."""

    query: str
    supporting: list[Card] = field(default_factory=list)
    counterexamples: list[Card] = field(default_factory=list)
    search_mode: str = "trigram"
    # 아키텍처 카드는 "무엇을 만들까"의 근거가 아니라 "어떻게 만들까"의 구조다.
    # 지지 근거와 같은 자리를 놓고 다투면 상위 6장에서 밀려 조용히 사라지므로
    # 몫을 따로 준다.
    architecture: list[Card] = field(default_factory=list)

    @property
    def has_counterexample(self) -> bool:
        return bool(self.counterexamples)

    def counterexample_note(self) -> str:
        """반례가 없을 때 규칙이 요구하는 문구를 돌려준다."""

        return "" if self.has_counterexample else COUNTEREXAMPLE_MISSING

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "searchMode": self.search_mode,
            "supporting": [c.to_dict() for c in self.supporting],
            "counterexamples": [c.to_dict() for c in self.counterexamples],
            "architecture": [c.to_dict() for c in self.architecture],
            "counterexampleNote": self.counterexample_note(),
            "citableCardIds": sorted(
                {c.card_id for c in self.supporting}
                | {c.card_id for c in self.counterexamples}
                | {c.card_id for c in self.architecture}
            ),
        }


class SentenceTransformerEmbedder:
    """카드와 동일한 모델로 질의를 임베딩한다.

    ``sentence-transformers`` 는 torch 를 끌고 오는 무거운 의존성이라
    **선택 사항**으로 둔다. 없으면 ``ResearchRepository`` 가 트라이그램 검색으로
    자동 강등되며, 그 사실을 결과의 ``searchMode`` 에 밝힌다.
    """

    def __init__(self, model_name: str = EMBEDDING_MODEL) -> None:
        self._model_name = model_name
        self._model: Any | None = None

    @staticmethod
    def available() -> bool:
        """설치 여부만 본다 — **import 하지 않는다.**

        예전에는 ``import sentence_transformers`` 로 확인했는데, 그 한 줄이
        torch 를 통째로 끌고 와 이 머신에서 55초가 걸렸다. 서버 lifespan 이
        시작하자마자 이걸 부르므로 **MCP stdio 핸드셰이크(30초 제한)가 통째로
        터졌고, strategic 서버만 도구 목록에 안 올라왔다** (2026-08-06 실측:
        DB 연결 2.0초 + 이 호출 55.2초 = 58.4초). sentence-transformers 를
        설치하기 전에는 ImportError 가 즉시 나서 드러나지 않던 결함이다.

        무거운 import 는 ``_load()`` 가 첫 ``embed()`` 때 한다 — 핸드셰이크
        경로 밖이다.
        """

        import importlib.util

        try:
            return importlib.util.find_spec("sentence_transformers") is not None
        except (ImportError, ValueError):
            # 부모 패키지가 깨졌거나 __spec__ 이 없는 설치. 트라이그램으로 강등한다.
            return False

    def _load(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            logger.info("Loading embedding model %s …", self._model_name)
            self._model = SentenceTransformer(self._model_name)
        return self._model

    async def embed(self, text: str) -> list[float]:
        import asyncio

        def _run() -> list[float]:
            model = self._load()
            # embed_cards.py 와 동일하게 정규화해야 코사인 거리가 맞는다.
            vector = model.encode([text], normalize_embeddings=True)[0]
            return [float(x) for x in vector]

        return await asyncio.to_thread(_run)


def build_ssl_context(dsn: str) -> ssl.SSLContext | None:
    """Neon 은 TLS 를 요구한다. 인증서는 ``certifi`` 번들로 검증한다.

    예전에는 검증을 끄고(``ssl.CERT_NONE``) 암호화만 켰다 — Windows 한글 사용자
    경로에서 OS 인증서 저장소 탐색이 실패하는 사례를 우회하려던 타협이다. 그러나
    Neon 은 **외부망**이고, 검증 없는 TLS 는 중간자 공격 앞에서 평문과 같다.

    OS 저장소 대신 ``certifi`` 번들을 명시하면 경로 문제와 무관해진다 — 번들은
    파이썬 패키지 안에 있고 ``cafile`` 로 직접 열기 때문이다. 같은 저장소의 HTTP
    폴백(``neon_http.py`` 의 httpx)이 처음부터 이 방식으로 검증하며 잘 돌고 있다.
    """

    if "neon.tech" not in dsn:
        return None
    return ssl.create_default_context(cafile=certifi.where())


_SELECT = """
    SELECT card_id, kind, type, title, summary, tags, elements, genres,
           confidence, updated::text AS updated
"""

# --- 하이브리드 검색 ---------------------------------------------------------
#
# 두 검색기는 서로 다른 종류의 질의에 강하다. 실측(2026-07-29, 카드 55장,
# 어려운 질의 18개):
#
# | 질의 | 벡터 순위 | 트라이그램 순위 | RRF |
# |---|---|---|---|
# | "High on Life 동반자 총" | 11 | 1 | 1 |
# | "inZOI 크래프톤 인생 시뮬레이션" | 6 | 1 | 2 |
# | "Suck Up! LLM NPC 설득" | 4 | 1 | 1 |
#
# 고유명사는 임베딩이 뭉갠다 — 게임 제목은 의미가 아니라 **글자**이기 때문이다.
# 반대로 "죽을 때마다 무작위 업그레이드를 뽑는" 같은 의역 질의는 벡터가 잡는다.
# 18개 질의에서 RRF 는 6건 개선, 12건 동률, **0건 악화**였고 recall@6 이
# 17/18 → 18/18 이 됐다.
#
# 융합은 Reciprocal Rank Fusion — 점수가 아니라 **순위**를 더한다. 코사인
# 유사도(0~1)와 트라이그램 유사도(여기서는 0.03 대)는 척도가 달라서 점수를 직접
# 섞으면 한쪽이 항상 이긴다. 순위는 척도가 없으므로 그 문제가 생기지 않는다.
RRF_K = 60

# 각 검색기에서 몇 위까지를 후보로 볼지. 이 창 밖은 융합 점수 0 이라 올라오지
# 않는다 — 창이 없으면 꼴찌 카드도 1/(60+55) 만큼 표를 얻어, 두 검색기가 모두
# 무관하다고 본 카드가 결과를 채운다.
#
# 창을 20 으로 잡았다가 40 으로 넓혔다. 실측(recall@6, 고유명사 질의 17개):
#
# | K  | 창  | supporting 풀 | 전체 풀 |
# |----|-----|---------------|---------|
# | 60 |  20 | 17/17         | **15/17** |
# | 60 |  40 | 17/17         | 17/17   |
# | 10 |  20 | 17/17         | 17/17   |
#
# 전체 풀에서만 갈리는 이유: 후보가 55장으로 늘면 "Undertale 불살 루트" 같은
# 질의의 정답이 벡터 쪽에서 20위 밖으로 밀려 **어휘 표 하나만** 남는다. 그러면
# 양쪽에서 어중간한 카드가 표 두 장으로 이긴다. 창을 넓히면 정답이 벡터 표도
# 받아 제자리로 돌아온다. K 를 줄여도(10) 같은 효과지만, 60 은 RRF 원 논문
# (Cormack et al. 2009)의 값이라 근거 없이 바꾸지 않는다.
_CANDIDATE_WINDOW_FACTOR = 4
_MIN_CANDIDATE_WINDOW = 40

# 보고하는 ``score`` 는 **코사인 유사도**다 (융합 점수가 아니라). 반례 하한선
# (``COUNTEREXAMPLE_MIN_SCORE``)이 그 척도 위에서 실측으로 정해졌고, RRF 점수는
# 질의마다 크기가 달라 하한선을 걸 수 있는 값이 아니기 때문이다. 임베딩이 없는
# 카드는 코사인이 NULL 이라 0 으로 떨어지고, 그래서 반례 자리에는 오르지 못한다
# — 의미적 근거 없이 글자만 겹친 실패 사례는 반례가 아니다.
# 순위는 **절** 단위로 매기고, 마지막에 카드 단위로 접는다(``DISTINCT ON``).
#
# 접지 않으면 한 카드의 절 여러 개가 상위 K 를 다 차지해 근거 다양성이 죽는다
# (ARCH 카드 하나가 절 7 개를 갖고 있으므로 실제로 일어난다). 카드를 대표하는
# 절은 융합 점수가 가장 높은 절이고, 그 절의 코사인이 그 카드의 ``score`` 가 된다.
#
# 트라이그램 대상에 절 본문(``s.body``)을 넣었다. 예전에는 카드 표면(제목·요약·
# 태그)만 봐서 본문 안의 고유명사(개발사명·인용 매체)가 어휘 검색에 안 걸렸다.
_HYBRID_SQL = """
    WITH scored AS (
        SELECT c.card_id, c.kind, c.type, c.title, c.summary, c.tags, c.elements,
               c.genres, c.confidence, c.updated::text AS updated,
               s.section_key,
               1 - (s.embedding <=> $1::vector) AS vec_score,
               similarity(c.title || ' ' || c.summary || ' '
                          || array_to_string(c.tags,' ') || ' ' || s.body, $2) AS trg_score
        FROM card_sections s
        JOIN cards c ON c.card_id = s.card_id
        WHERE TRUE {extra_where}
    ), ranked AS (
        SELECT *,
               ROW_NUMBER() OVER (ORDER BY vec_score DESC NULLS LAST) AS vec_rank,
               ROW_NUMBER() OVER (ORDER BY trg_score DESC NULLS LAST) AS trg_rank
        FROM scored
    ), fused AS (
        SELECT *,
               (CASE WHEN vec_rank <= $3 THEN 1.0 / ($4 + vec_rank) ELSE 0 END)
             + (CASE WHEN trg_rank <= $3 THEN 1.0 / ($4 + trg_rank) ELSE 0 END) AS rrf,
               CASE WHEN vec_rank <= $3 AND trg_rank <= $3 THEN 'vector+trigram'
                    WHEN vec_rank <= $3 THEN 'vector'
                    ELSE 'trigram' END AS matched_by
        FROM ranked
        WHERE vec_rank <= $3 OR trg_rank <= $3
    ), best AS (
        SELECT DISTINCT ON (card_id) *
        FROM fused
        ORDER BY card_id, rrf DESC, vec_score DESC NULLS LAST
    )
    SELECT card_id, kind, type, title, summary, tags, elements, genres, confidence, updated,
           vec_score AS score, matched_by, section_key AS matched_section
    FROM best
    ORDER BY rrf DESC, score DESC NULLS LAST
    LIMIT $5
"""


class ResearchRepository:
    """카드 DB 조회. 풀은 서버 lifespan 이 소유한다."""

    def __init__(self, pool: asyncpg.Pool, embedder: EmbedderProtocol | None = None) -> None:
        self._pool = pool
        self._embedder = embedder

    @property
    def search_mode(self) -> str:
        return "vector+trigram" if self._embedder is not None else "trigram"

    # ------------------------------------------------------------------
    async def card_index(self) -> dict[str, str]:
        """존재하는 카드 ID → 제목. 인용 검증에 쓴다.

        ``6_planner.md`` §2: 존재하지 않는 카드 ID 를 인용하면 안 된다.
        """

        rows = await self._pool.fetch("SELECT card_id, title FROM cards")
        return {row["card_id"]: row["title"] for row in rows}

    async def get_cards(self, card_ids: list[str]) -> list[Card]:
        if not card_ids:
            return []
        rows = await self._pool.fetch(
            f"{_SELECT} FROM cards WHERE card_id = ANY($1::text[]) ORDER BY card_id", card_ids
        )
        return [self._to_card(row, matched_by="explicit") for row in rows]

    async def card_body(self, card_id: str) -> str | None:
        return await self._pool.fetchval("SELECT body FROM cards WHERE card_id = $1", card_id)

    async def related_cards(self, card_id: str) -> list[str]:
        """``card_refs`` 를 따라 이어진 카드 ID (양방향)."""

        rows = await self._pool.fetch(
            "SELECT to_id AS id FROM card_refs WHERE from_id=$1 "
            "UNION SELECT from_id FROM card_refs WHERE to_id=$1",
            card_id,
        )
        return sorted(row["id"] for row in rows)

    # ------------------------------------------------------------------
    async def arch_guidance(self, card_ids: list[str]) -> dict[str, ArchGuidance]:
        """Return implementation guidance from normalized ARCH-card sections."""

        wanted = arch_ids(card_ids)
        if not wanted:
            return {}
        # sync_db.py already splits English Markdown into language-neutral section keys.
        rows = await self._pool.fetch(
            "SELECT s.card_id, c.title, s.section_key, s.body "
            "FROM card_sections s JOIN cards c ON c.card_id = s.card_id "
            "WHERE s.card_id = ANY($1::text[]) AND s.section_key = ANY($2::text[])",
            wanted,
            [key for key, _title in GUIDANCE_SECTIONS],
        )
        by_card: dict[str, tuple[str, dict[str, str]]] = {}
        for row in rows:
            title, sections = by_card.setdefault(row["card_id"], (row["title"], {}))
            sections[row["section_key"]] = row["body"] or ""
        return {
            card_id: guidance_from_sections(card_id, title, sections)
            for card_id, (title, sections) in by_card.items()
        }

    # ------------------------------------------------------------------
    async def gather_evidence(
        self, idea: str, support_k: int = 6, counter_k: int = 3, arch_k: int | None = None
    ) -> ResearchEvidence:
        """아이디어에 대한 지지 근거와 반례, 그리고 아키텍처 카드를 모은다.

        ``search_cards.py`` 의 2단 쿼리(전체 유사도 + 실패/혼재 사례)를 그대로
        따르되, 지지 근거 쪽은 실패 사례를 빼서 "시너지" 의미를 분명히 한다.
        아키텍처 카드는 성격이 달라(구현 구조) 세 번째 몫으로 따로 뽑는다.
        """

        vector = None
        if self._embedder is not None:
            try:
                vector = await self._embedder.embed(idea)
            except Exception:  # noqa: BLE001 — 임베딩 실패는 검색 실패가 아니다
                logger.warning("질의 임베딩 실패 — 트라이그램으로 강등", exc_info=True)
        else:
            # 서버 기동 시 한 번만 경고하고 넘어가면 그 뒤로는 매 요청이 강등된
            # 채로 조용히 돈다 (§2.4). 실측 결과 트라이그램 점수는 노이즈와
            # 신호가 겹칠 만큼 신뢰도가 낮으므로(06_3-4군_인수인계.md §2.3의
            # 진단 참고) 요청 단위로도 남겨 둔다.
            logger.warning("임베딩 모델 없음 — 이번 검색은 트라이그램으로만 동작합니다: %.60s", idea)

        supporting = await self._search(
            idea,
            vector,
            support_k,
            # 지지 근거에서는 반례 성격의 절도 뺀다. 예전에는 '실패/혼재 GAME 카드'만
            # 뺐는데, 절 단위로 검색하면 성공 카드의 '실패 사례' 절이 시너지 자리에
            # 올라올 수 있다 - 그건 지지 근거가 아니다.
            "AND c.kind <> 'ARCH' "
            "AND NOT (c.kind = 'GAME' AND c.type IN ('failure','mixed')) "
            f"AND NOT {_COUNTER_PREDICATE}",
            trigram_where="AND kind <> 'ARCH' "
                          "AND NOT (kind = 'GAME' AND type IN ('failure','mixed'))",
            extra_args=[COUNTER_SECTION_KEYS],
        )
        # 지지 근거로 이미 쓴 카드는 반례가 될 수 없다. 절 단위 검색으로 바꾸면서
        # 반례 풀이 '실패/혼재 GAME 카드 ~10장'에서 '반례 성격 절 ~250개'로 넓어졌는데,
        # 그 안에는 **방금 시너지로 뽑힌 바로 그 카드의 리스크 절**이 들어 있다.
        # 그게 상위를 채우면 기획자는 "내 아이디어를 지지하는 카드"를 반례로 읽는다.
        #
        # 실측 (2026-08-12, evals/retrieval.jsonl): 이 제외가 없으면 ret-004 는
        # GAME-005(Twelve Minutes) 대신 ELEM-004·GENRE-002 의 절을, ret-005 는
        # GAME-025 대신 ELEM-017 의 절을 반례로 내놓는다.
        supporting_ids = [c.card_id for c in supporting]
        counter_pool = await self._search(
            idea, vector, counter_k * _COUNTER_OVERFETCH,
            f"AND {_COUNTER_PREDICATE} AND c.card_id <> ALL($7::text[])",
            trigram_where="AND kind = 'GAME' AND type IN ('failure','mixed')",
            extra_args=[COUNTER_SECTION_KEYS, supporting_ids],
        )
        counterexamples = self._above_floor(_cases_first(counter_pool)[:counter_k], vector)
        # 하한선을 걸지 않는다. 반례와 달리 아키텍처 카드는 종류가 고정돼 있어,
        # 무관한 카드가 올라와도 기획이 refs 에 넣지 않으면 아무 일도 일어나지
        # 않는다 — 반례처럼 "있는데 무관한" 위험이 없다.
        architecture = await self._search(
            idea, vector, arch_k if arch_k is not None else ARCH_LIMIT_DEFAULT,
            "AND c.kind = 'ARCH'",
            trigram_where="AND kind = 'ARCH'",
        )
        architecture = await self._with_pinned(architecture)
        return ResearchEvidence(
            query=idea,
            supporting=supporting,
            counterexamples=counterexamples,
            architecture=architecture,
            search_mode=("vector+trigram" if vector is not None else "trigram"),
        )

    async def _with_pinned(self, found: list[Card]) -> list[Card]:
        """검색 결과에 ``PINNED_ARCH_IDS`` 중 빠진 카드를 덧붙인다.

        순서는 검색 결과가 앞이다 — 유사도로 뽑힌 것이 이 기획에 더 가깝고,
        고정 카드는 "빠지면 안 되는 것"이지 "가장 관련 있는 것"이 아니다.
        DB 에 없는 ID 는 ``get_cards`` 가 조용히 빼므로 여기서 검사하지 않는다.
        """

        missing = [cid for cid in PINNED_ARCH_IDS if cid not in {c.card_id for c in found}]
        if not missing:
            return found
        pinned = await self.get_cards(missing)
        return found + [replace(card, matched_by="pinned") for card in pinned]

    @staticmethod
    def _above_floor(cards: list[Card], vector: list[float] | None) -> list[Card]:
        """유사도 하한선 미달인 반례를 버린다 (벡터 검색일 때만).

        전부 버려서 빈 리스트가 되는 것이 **정상 결과**다. 그때 호출부는
        ``counterexample_note()`` 로 ``COUNTEREXAMPLE_MISSING`` 을 받게 되고,
        그게 무관한 카드를 반례라고 내미는 것보다 정직하다.
        """

        if vector is None:
            # 트라이그램 점수는 척도가 달라 이 하한선을 적용할 수 없다.
            return cards

        kept = [card for card in cards if card.score >= COUNTEREXAMPLE_MIN_SCORE]
        dropped = len(cards) - len(kept)
        if dropped:
            logger.info(
                "반례 %d장을 유사도 하한선(%.2f) 미달로 제외했습니다 (최고 점수 %.4f)",
                dropped,
                COUNTEREXAMPLE_MIN_SCORE,
                max(card.score for card in cards),
            )
        return kept

    async def _search(
        self,
        query: str,
        vector: list[float] | None,
        limit: int,
        extra_where: str,
        trigram_where: str = "",
        extra_args: list[Any] | None = None,
    ) -> list[Card]:
        """``extra_where`` 는 절 단위 하이브리드용, ``trigram_where`` 는 폴백용이다.

        둘을 나눠 받는 이유: 폴백은 ``cards`` 만 보므로 ``s.section_key`` 같은
        조건을 쓸 수 없다. 즉 **폴백에서는 반례를 절 단위로 정의할 수 없고**
        옛 카드 단위 근사(``kind='GAME' AND type IN ('failure','mixed')``)로
        돌아간다. 임베딩 없이 도는 것은 원래 강등 모드이고, 그 사실은
        결과의 ``searchMode`` 에 이미 드러난다.
        """

        if vector is not None:
            literal = "[" + ",".join(str(x) for x in vector) + "]"
            window = max(limit * _CANDIDATE_WINDOW_FACTOR, _MIN_CANDIDATE_WINDOW)
            rows = await self._pool.fetch(
                _HYBRID_SQL.format(extra_where=extra_where),
                literal,
                query,
                window,
                RRF_K,
                limit,
                *(extra_args or []),
            )
        else:
            # 트라이그램 폴백. 제목·요약·태그를 한 덩어리로 보고 유사도를 잰다.
            sql = f"""
                {_SELECT},
                       similarity(title || ' ' || summary || ' ' || array_to_string(tags,' '), $1)
                         AS score,
                       'trigram' AS matched_by,
                       '' AS matched_section
                FROM cards
                WHERE TRUE {trigram_where}
                ORDER BY score DESC
                LIMIT $2
            """
            rows = await self._pool.fetch(sql, query, limit)

        return [
            self._to_card(
                row,
                score=row["score"],
                matched_by=row["matched_by"],
                # 트라이그램 폴백 경로에는 절 개념이 없다 (카드 표면만 본다).
                matched_section=(row["matched_section"] if "matched_section" in row else ""),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    @staticmethod
    def _to_card(
        row: asyncpg.Record, score: float = 0.0, matched_by: str = "", matched_section: str = ""
    ) -> Card:
        return Card(
            card_id=row["card_id"],
            kind=row["kind"],
            type=row["type"],
            title=row["title"],
            summary=row["summary"],
            tags=list(row["tags"] or []),
            elements=list(row["elements"] or []),
            genres=list(row["genres"] or []),
            confidence=row["confidence"],
            updated=row["updated"],
            score=float(score or 0.0),
            matched_by=matched_by,
            matched_section=matched_section or "",
        )
