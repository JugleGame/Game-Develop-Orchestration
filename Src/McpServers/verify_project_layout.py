"""생성된 Unity 프로젝트가 아키텍처 규칙을 지키는지 검사한다 (Unity 불필요).

``verify_contract.py`` 는 **"계약을 지키나"**, ``run_evals.py`` 는 **"결과가 쓸
만한가"**, 이 검사기는 **"구조가 Unity 답나"** 를 본다. 셋은 서로 다른 것을 재므로
어느 하나가 다른 하나를 대신하지 못한다.

사용법::

    python verify_project_layout.py                       # git_output/work 전체
    python verify_project_layout.py ../../git_output/work/my-game
    python verify_project_layout.py --warnings-as-errors  # WARN 도 실패로

FAIL 이 하나라도 있으면 종료 코드 1 이라 CI 게이트로 쓸 수 있다.
판정 규칙은 ``project_layout.py`` 에 있고, 이 파일은 출력만 맡는다.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from common.console import use_utf8_output

use_utf8_output()

from project_layout import (  # noqa: E402  (위 인코딩 설정이 먼저여야 한다)
    LayoutReport,
    ProjectLayoutError,
    Severity,
    analyze_project,
    find_projects,
)

OK = "✅"
BAD = "❌"
WARN = "⚠️"

_DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "git_output" / "work"


def _render(report: LayoutReport) -> None:
    counts = (
        f"스크립트 {report.scripts} / MonoBehaviour {report.behaviours} / "
        f"프리팹 {report.prefabs} / 씬 {report.scenes}"
    )
    print(f"\n[{report.project}] {counts}")

    if not report.findings:
        print(f"  {OK} 구조 규칙 위반 없음")
        return

    for finding in report.findings:
        mark = BAD if finding.severity is Severity.FAIL else WARN
        print(f"  {mark} {finding.rule} {finding.target}")
        print(f"       {finding.message}")


def _resolve_targets(arguments: list[str]) -> list[Path]:
    if arguments:
        return [Path(item).resolve() for item in arguments]

    root = Path(os.getenv("UNITY_PROJECT_PATH", str(_DEFAULT_ROOT))).resolve()
    if root.name != "work" and (root / "work").is_dir():
        root = root / "work"
    return find_projects(root)


def main(argv: list[str]) -> int:
    strict = "--warnings-as-errors" in argv
    targets = _resolve_targets([item for item in argv if not item.startswith("--")])

    if not targets:
        print("검사할 Unity 프로젝트를 찾지 못했습니다 (Assets/ 를 가진 폴더가 없습니다).")
        print("경로를 인자로 주거나 UNITY_PROJECT_PATH를 설정하세요.")
        return 0

    reports: list[LayoutReport] = []
    for target in targets:
        try:
            reports.append(analyze_project(target))
        except ProjectLayoutError as exc:
            print(f"\n[{target.name}] {BAD} {exc}")
            return 1

    for report in reports:
        _render(report)

    failures = sum(len(report.failures) for report in reports)
    warnings = sum(len(report.warnings) for report in reports)

    print("\n" + "=" * 60)
    if failures:
        print(f"{BAD} 구조 규칙 위반 {failures}건 (경고 {warnings}건)")
        return 1
    if warnings and strict:
        print(f"{BAD} 경고 {warnings}건 — --warnings-as-errors 로 실행됨")
        return 1
    print(f"{OK} 프로젝트 {len(reports)}개 구조 규칙 충족 (경고 {warnings}건)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
