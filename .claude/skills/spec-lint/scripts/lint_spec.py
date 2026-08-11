#!/usr/bin/env python3
"""기획 산출물(JSON)을 발행 전에 검사한다.

검사 규칙의 원본은 ``Src/McpServers/strategic/specs.py::lint_spec`` 이다. 이 스크립트는
그 함수를 **직접 import** 해서 쓴다 — 규칙을 복사하면 두 검사기가 갈라지기 때문이다.

spec 단위 검사(S2~S5)는 원본 함수가 하고, 청사진 단위 검사(B1~B5)만 여기서 더한다.

사용법::

    python lint_spec.py plan.json
    python lint_spec.py plan.json --cards ELEM-001 GENRE-004 GAME-013

``--cards`` 를 주면 그 목록만 인용 가능한 카드로 본다. 생략하면 청사진 자신이
인용한 카드(synergyRationale + counterEvidence)를 인용 가능 집합으로 삼는다.

종료 코드: 0 = 통과, 1 = 위반 있음, 2 = 입력 오류.
"""

from __future__ import annotations

import argparse
import json
import sys
import types
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[4]
_MCP_ROOT = _REPO_ROOT / "Src" / "McpServers"

# Windows 콘솔은 시스템 코드페이지(한국어면 cp949)를 쓴다. 위반 메시지에는 한글과
# ``✗``/``—`` 가 들어가므로 이 줄이 없으면 UnicodeEncodeError 로 죽는다 — 통과
# 출력만 인코딩되는 탓에 **위반이 있을 때만** 아무것도 못 보게 된다.
# (같은 처리를 MCP 쪽은 ``common/console.py`` 가 한다. 이 스크립트는 그 패키지를
#  import 하기 전에도 오류를 찍으므로 여기서 직접 한다.)
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def _import_specs_module() -> Any:
    """``strategic.specs`` 를 DB 드라이버 없이 import 한다.

    ``specs.py`` 는 영속화 계층 때문에 모듈 최상단에서 ``asyncpg`` 를 import 하지만,
    검사 규칙 자체는 asyncpg 를 전혀 쓰지 않는다. 린터를 돌리자고 DB 드라이버를
    설치하게 만들 이유가 없으므로 빈 모듈을 끼워 넣는다.
    """

    if not _MCP_ROOT.is_dir():
        raise SystemExit(f"[입력 오류] MCP 서버 경로를 찾을 수 없다: {_MCP_ROOT}")

    sys.path.insert(0, str(_MCP_ROOT))
    sys.modules.setdefault("asyncpg", types.ModuleType("asyncpg"))

    from strategic import specs  # noqa: PLC0415 — sys.path 조작 후에만 가능

    return specs


def _to_spec_document(specs_mod: Any, raw: dict[str, Any], game_id: str) -> Any:
    """기획 JSON 의 spec 한 장을 ``SpecDocument`` 로 옮긴다.

    ``architecture`` 를 함께 옮겨야 S6(아키텍처 카드 인용과 실린 지침이 맞는지)가
    실제로 동작한다. 이 필드는 모델이 쓰는 것이 아니라
    ``game-planning/scripts/attach_arch_guidance.py`` 가 카드 원문에서 붙인다 —
    빠진 채로 검사하면 "인용했는데 지침이 없다"로 전부 반려된다.
    """

    from strategic.arch_cards import ArchGuidance  # noqa: PLC0415 — sys.path 조작 후

    return specs_mod.SpecDocument(
        spec_id=raw.get("specId", ""),
        game_id=game_id,
        title=raw.get("title", ""),
        version=1,
        blueprint_version=1,
        refs=raw.get("refs", []),
        goal=raw.get("goal", ""),
        implementation_scope=raw.get("implementationScope", []),
        out_of_scope=raw.get("outOfScope", []),
        acceptance_criteria=raw.get("acceptanceCriteria", []),
        unity_hints=raw.get("unityHints", {}),
        dependencies=raw.get("dependencies", []),
        architecture=[ArchGuidance.from_dict(g) for g in raw.get("architecture") or []],
    )


def _cited_card_ids(plan: dict[str, Any]) -> set[str]:
    """청사진이 인용한 카드 ID 를 모은다."""

    cited = {entry.get("cardId", "") for entry in plan.get("synergyRationale", [])}
    cited |= {entry.get("cardId", "") for entry in plan.get("counterEvidence", [])}
    return {card_id for card_id in cited if card_id}


def _find_dependency_cycle(specs_raw: list[dict[str, Any]]) -> list[str] | None:
    """의존 그래프에서 순환을 하나 찾아 돌려준다. 없으면 ``None``."""

    graph = {s.get("specId", ""): list(s.get("dependencies", [])) for s in specs_raw}
    visiting: set[str] = set()
    done: set[str] = set()
    trail: list[str] = []

    def walk(node: str) -> list[str] | None:
        if node in done:
            return None
        if node in visiting:
            return trail[trail.index(node) :] + [node]

        visiting.add(node)
        trail.append(node)
        for nxt in graph.get(node, []):
            if nxt not in graph:  # 존재하지 않는 spec 참조는 B4 가 따로 잡는다
                continue
            if (cycle := walk(nxt)) is not None:
                return cycle
        trail.pop()
        visiting.discard(node)
        done.add(node)
        return None

    for spec_id in graph:
        if (cycle := walk(spec_id)) is not None:
            return cycle
    return None


def check_blueprint(plan: dict[str, Any], known_cards: set[str]) -> list[str]:
    """청사진 단위 규칙. spec 단위 규칙은 원본 ``lint_spec`` 이 본다."""

    errors: list[str] = []
    specs_raw = plan.get("specs", [])

    # B1 — spec 개수. 상한은 없다: 게임이 자라면 spec 도 늘고, 8번째 기능을
    # 7개 안에 우겨넣는 것은 기획이 아니라 규칙 회피다.
    if len(specs_raw) < 3:
        errors.append(f"B1: spec 은 최소 3개 이상이어야 함 — 현재 {len(specs_raw)}개")

    # B2 — 지지 근거 최소 2장 (SYSTEM_PROMPT 지식 규칙)
    if len(plan.get("synergyRationale", [])) < 2:
        errors.append("B2: synergyRationale 은 최소 2장 — 근거 없이 발행할 수 없음")

    # B3 — 반례는 지어내지도, 빠뜨리지도 않는다
    counter = plan.get("counterEvidence", [])
    note = (plan.get("counterEvidenceNote") or "").strip()
    if counter and note:
        errors.append("B3: 반례 카드가 있는데 counterEvidenceNote 가 채워져 있음 — 빈 문자열이어야 함")
    if not counter and note != "반례 조사 부족":
        errors.append('B3: 반례 카드가 없으면 counterEvidenceNote 는 정확히 "반례 조사 부족" 이어야 함')

    # B4 — dependencies 는 실재하는 spec 만 가리킨다
    spec_ids = {s.get("specId", "") for s in specs_raw}
    for spec in specs_raw:
        for dep in spec.get("dependencies", []):
            if dep not in spec_ids:
                errors.append(f"B4: 존재하지 않는 spec 의존 — {spec.get('specId')} → {dep}")

    # B5 — 순환 의존 금지
    if (cycle := _find_dependency_cycle(specs_raw)) is not None:
        errors.append(f"B5: 순환 의존 — {' → '.join(cycle)}")

    # B6 — 청사진이 인용한 카드도 실재해야 한다
    for card_id in sorted(_cited_card_ids(plan) - known_cards):
        errors.append(f"B6: 존재하지 않는 카드 인용 (청사진) — {card_id}")

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("plan", type=Path, help="game-planning 이 만든 기획 JSON 경로")
    parser.add_argument(
        "--cards",
        nargs="*",
        default=None,
        help="인용 가능한 카드 ID 목록. 생략하면 청사진이 인용한 카드를 그대로 인정한다.",
    )
    parser.add_argument("--game-id", default="lint", help="검사용 gameId (기본: lint)")
    args = parser.parse_args()

    try:
        # utf-8-sig — Windows 도구가 쓴 JSON 에는 BOM 이 붙고, 순수 utf-8 로 읽으면
        # "Unexpected UTF-8 BOM" 으로 죽는다. BOM 없는 파일도 같은 코덱으로 읽힌다.
        plan = json.loads(args.plan.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        print(f"[입력 오류] 파일이 없다: {args.plan}")
        return 2
    except json.JSONDecodeError as exc:
        print(f"[입력 오류] JSON 파싱 실패: {exc}")
        return 2

    if not isinstance(plan, dict):
        print("[입력 오류] 최상위가 객체(JSON object)가 아니다")
        return 2

    specs_mod = _import_specs_module()
    known_cards = set(args.cards) if args.cards is not None else _cited_card_ids(plan)

    failures: list[str] = check_blueprint(plan, known_cards)
    for raw in plan.get("specs", []):
        spec = _to_spec_document(specs_mod, raw, args.game_id)
        failures += [f"[{spec.spec_id or '?'}] {msg}" for msg in specs_mod.lint_spec(spec, known_cards)]

    if failures:
        print(f"검사 실패 — {len(failures)}건\n")
        for msg in failures:
            print(f"  ✗ {msg}")
        print("\n지적된 항목만 고치고 다시 검사한다. 규칙을 완화하지 않는다.")
        return 1

    spec_count = len(plan.get("specs", []))
    print(f"검사 통과 — spec {spec_count}개, 인용 카드 {len(known_cards)}종")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
