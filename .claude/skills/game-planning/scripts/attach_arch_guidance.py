#!/usr/bin/env python3
"""기획 JSON 의 각 spec 에 아키텍처 카드(``ARCH-###``) 원문 지침을 붙인다.

경로 A(`strategic` 서버)가 하는 일을 경로 B 에서 **같은 코드로** 한다. 붙이는
내용은 ``Src/McpServers/strategic/arch_cards.py`` 가 카드 본문에서 잘라내고,
이 스크립트는 카드 파일을 찾아 그 함수에 넘기는 일만 한다.

**왜 모델이 직접 옮겨 적지 않는가.** 같은 내용을 모델이 두 번 쓰면 두 번
달라진다. spec 에 실린 절차가 카드 원문과 어긋나면 `refs` 의 인용은 검사를
통과하면서도 실제로는 다른 것을 지시하게 되고, 카드 인용의 실재성만 보는
S3 는 그 어긋남을 보지 못한다. 그래서 모델은 `refs` 에 **카드 ID 만** 고르고,
본문은 코드가 옮긴다 — 옮겨 적을 기회가 없으면 어긋날 수도 없다.

토큰도 이유의 하나다. 카드 세 절은 길다. 모델이 매 spec 마다 그걸 다시 쓰면
출력 토큰이 그만큼 늘고, 그렇게 쓴 결과는 원문보다 나을 수 없다.

사용법::

    python attach_arch_guidance.py plan.json
    python attach_arch_guidance.py plan.json --research-repo ../Game-Design-and-Planning_resarch
    python attach_arch_guidance.py plan.json --dry-run   # 파일을 고치지 않고 결과만 본다

카드 저장소는 ``--research-repo`` → ``RESEARCH_REPO`` 환경변수 → 두 저장소가
나란히 있는 기본 배치 순으로 찾는다.

종료 코드: 0 = 붙였다(또는 붙일 것이 없다), 1 = 인용한 카드를 찾지 못했다,
2 = 입력 오류.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

# 한국어 로케일 Windows 콘솔은 cp949 라서 아래 기호를 인코딩하지 못하고 죽는다.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

_REPO_ROOT = Path(__file__).resolve().parents[4]
_MCP_ROOT = _REPO_ROOT / "Src" / "McpServers"

# tools/sync_db.py 의 FM_PAT 와 같다 — 카드는 `+++ ` 뒤에 공백이 붙고 줄끝이
# CRLF 인 경우가 있어서 `\s*` 가 필요하다.
_FRONTMATTER = re.compile(r"^\+\+\+\s*\n(.*?)\n\+\+\+\s*\n(.*)$", re.S)


def _import_arch_cards() -> Any:
    """``strategic.arch_cards`` 를 import 한다 (외부 의존성 없는 모듈이다)."""

    if not _MCP_ROOT.is_dir():
        raise SystemExit(f"[입력 오류] MCP 서버 경로를 찾을 수 없다: {_MCP_ROOT}")

    sys.path.insert(0, str(_MCP_ROOT))

    from strategic import arch_cards  # noqa: PLC0415 — sys.path 조작 후에만 가능

    return arch_cards


def _find_research_repo(explicit: Path | None) -> Path:
    candidates = [explicit] if explicit else []
    if os.getenv("RESEARCH_REPO"):
        candidates.append(Path(os.environ["RESEARCH_REPO"]))
    candidates.append(_REPO_ROOT.parent / "Game-Design-and-Planning_resarch")

    for root in candidates:
        if root and (root / "research" / "architecture").is_dir():
            return root
    raise SystemExit(
        "[입력 오류] 리서치 저장소를 찾지 못했다. --research-repo 로 지정하거나 "
        "RESEARCH_REPO 환경변수를 설정한다 (research/architecture/ 가 있어야 한다)."
    )


def load_arch_cards(research_repo: Path, arch_cards: Any) -> dict[str, Any]:
    """``research/architecture/*.md`` 를 훑어 카드 ID → 지침으로 만든다.

    파일명(``003_chunk_loader.md``)이 아니라 frontmatter 의 ``card_id`` 로
    맞춘다 — 파일명은 규약이고 ``card_id`` 가 진실이다.
    """

    guidance: dict[str, Any] = {}
    for path in sorted((research_repo / "research" / "architecture").glob("*.md")):
        match = _FRONTMATTER.match(path.read_text(encoding="utf-8"))
        if match is None:
            print(f"  주의  frontmatter 를 읽지 못해 건너뜀: {path.name}")
            continue
        meta = tomllib.loads(match.group(1))
        card_id = str(meta.get("card_id", ""))
        if not arch_cards.is_arch_card(card_id):
            continue
        guidance[card_id] = arch_cards.guidance_from_body(
            card_id, str(meta.get("title", "")), match.group(2).strip()
        )
    return guidance


def attach(plan: dict[str, Any], guidance: dict[str, Any], arch_cards: Any) -> list[str]:
    """각 spec 에 지침을 붙이고, 찾지 못한 인용 목록을 돌려준다."""

    missing: list[str] = []
    for spec in plan.get("specs", []):
        cited = arch_cards.arch_ids(spec.get("refs", []))
        if not cited:
            # 붙일 것이 없으면 키를 만들지 않는다 — 빈 배열은 "확인했고 없음"과
            # "아직 안 붙였음"을 구분하지 못한다.
            spec.pop("architecture", None)
            continue

        attached = []
        for card_id in cited:
            if card_id in guidance:
                attached.append(guidance[card_id].to_dict())
            else:
                missing.append(f"{spec.get('specId', '?')} → {card_id}")
        spec["architecture"] = attached

        empty = [
            f"{card_id}({', '.join(guidance[card_id].empty_sections)})"
            for card_id in cited
            if card_id in guidance and guidance[card_id].empty_sections
        ]
        if empty:
            print(f"  주의  카드 쪽 절이 비었다 — {', '.join(empty)}")

    return missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("plan", type=Path, help="game-planning 이 만든 기획 JSON 경로")
    parser.add_argument("--research-repo", type=Path, default=None, help="리서치 카드 저장소 경로")
    parser.add_argument("--dry-run", action="store_true", help="파일을 고치지 않고 결과만 출력")
    args = parser.parse_args()

    try:
        # utf-8-sig — Windows 도구(메모장, PowerShell 5.1 의 Set-Content)가 쓴
        # JSON 에는 BOM 이 붙고, 순수 utf-8 로 읽으면 "Unexpected UTF-8 BOM" 으로
        # 죽는다. BOM 이 없는 파일도 같은 코덱으로 읽힌다.
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

    arch_cards = _import_arch_cards()
    research_repo = _find_research_repo(args.research_repo)
    guidance = load_arch_cards(research_repo, arch_cards)
    print(f"아키텍처 카드 {len(guidance)}장을 읽었다 — {research_repo}")

    missing = attach(plan, guidance, arch_cards)

    attached_specs = [s for s in plan.get("specs", []) if s.get("architecture")]
    for spec in attached_specs:
        ids = ", ".join(g["cardId"] for g in spec["architecture"])
        steps = sum(len(g["buildSteps"]) for g in spec["architecture"])
        print(f"  {spec.get('specId', '?')}: {ids} (구현 절차 {steps}단계)")

    if missing:
        print(f"\n인용한 카드를 찾지 못했다 — {len(missing)}건")
        for item in missing:
            print(f"  ✗ {item}")
        print("\n실재하지 않는 카드를 인용했거나 카드 저장소가 최신이 아니다. "
              "인용을 고치거나 저장소를 갱신한 뒤 다시 돌린다.")
        return 1

    if args.dry_run:
        print("\n--dry-run — 파일을 고치지 않았다.")
        return 0

    args.plan.write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if attached_specs:
        print(f"\n{args.plan} 에 spec {len(attached_specs)}개의 지침을 붙였다. "
              "이제 spec-lint 를 돌린다.")
    else:
        print(f"\n붙일 아키텍처 카드가 없다 (어느 spec 도 ARCH 를 인용하지 않았다).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
