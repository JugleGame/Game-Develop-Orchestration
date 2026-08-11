"""로컬 C# 문법 게이트 — Unity 에 넣기 전에 30초 안에 거르는 층.

왜 필요한가. 지금 흐름은 ``생성 → Unity 투입 → 빌드 → 컴파일 에러 발견``
이고, 그 빌드의 기본 예산은 ``UNITY_BUILD_TIMEOUT=1800`` 초다. 세미콜론
하나가 빠진 것을 30분짜리 빌드로 확인하는 셈이라, 재시도 루프의 한 바퀴가
그만큼 비싸진다. 여기서 먼저 걸러 그 바퀴 자체를 줄인다.

이 모듈은 **거부만 한다**. 통과가 컴파일 성공을 보장하지는 않는다 — 타입
오류나 없는 API 참조는 여전히 Unity 가 잡는다. 목적은 명백한 것을 싸게
쳐내는 것이고, 그래서 **거짓 양성(멀쩡한 코드를 막는 것)을 피하는 쪽으로
보수적으로** 만들었다. 애매하면 통과시킨다.

두 단계로 검사한다.

1. **구조 검사** (항상). 의존성 없이 문자열/괄호를 상태 기계로 훑어
   짝이 맞지 않는 중괄호·닫히지 않은 문자열·클래스 부재를 잡는다.
2. **``dotnet build``** (있을 때만). 진짜 C# 파서를 태운다. ``dotnet`` 이
   PATH 에 없으면 조용히 1단계 결과만 쓴다 — 개발 머신에 SDK 를 강제하지
   않기 위해서다. ``UNITY_CSHARP_DOTNET_CHECK=0`` 으로 끌 수 있다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Unity 스크립트는 UnityEngine 어셈블리에 기대므로 `dotnet build` 는 타입을
# 해석하지 못한다. 그래서 **구문 오류만** 신뢰하고 나머지는 버린다.
# CS1002 ';' expected / CS1513 '}' expected 처럼 파서 단계에서 나오는 코드다.
_SYNTAX_ONLY_CODES = frozenset(
    {
        "CS1002",  # ; expected
        "CS1003",  # syntax error
        "CS1022",  # type or namespace definition, or end-of-file expected
        "CS1026",  # ) expected
        "CS1513",  # } expected
        "CS1514",  # { expected
        "CS1519",  # invalid token in class/struct/interface member declaration
        "CS1525",  # invalid expression term
        "CS1585",  # member modifier must precede the member type and name
        "CS8025",  # ) expected (interpolated)
    }
)

_DOTNET_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True)
class CheckResult:
    """검사 결과. ``ok`` 가 False 일 때만 ``errors`` 가 채워진다."""

    ok: bool
    errors: tuple[str, ...] = ()
    # 어느 단계까지 실제로 돌았는지. 로그와 eval 리포트가 이 값을 읽는다.
    checked_by: str = "structure"

    def summary(self) -> str:
        return "; ".join(self.errors) if self.errors else "ok"


def _strip_literals(source: str) -> tuple[str, str | None]:
    """문자열·주석을 공백으로 지운 사본과, 닫히지 않은 리터럴 오류를 돌려준다.

    괄호 균형을 세기 전에 반드시 거쳐야 한다. ``"}"`` 같은 문자열 안의
    중괄호를 코드로 세면 멀쩡한 파일을 불균형으로 오판한다.
    """

    out: list[str] = []
    i, n = 0, len(source)
    while i < n:
        ch = source[i]

        # 줄 주석
        if ch == "/" and i + 1 < n and source[i + 1] == "/":
            while i < n and source[i] != "\n":
                i += 1
            continue

        # 블록 주석
        if ch == "/" and i + 1 < n and source[i + 1] == "*":
            end = source.find("*/", i + 2)
            if end == -1:
                return "".join(out), "닫히지 않은 블록 주석 (/* ... */)"
            out.append(" " * (end + 2 - i))
            i = end + 2
            continue

        # verbatim 문자열 @"..."  (내부의 "" 는 escape 된 따옴표)
        if ch == "@" and i + 1 < n and source[i + 1] == '"':
            j = i + 2
            while j < n:
                if source[j] == '"':
                    if j + 1 < n and source[j + 1] == '"':
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                return "".join(out), '닫히지 않은 verbatim 문자열 (@"...)'
            out.append(" " * (j + 1 - i))
            i = j + 1
            continue

        # 일반 문자열 / 문자 리터럴
        if ch in {'"', "'"}:
            quote = ch
            j = i + 1
            while j < n and source[j] != quote:
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == "\n":
                    return "".join(out), f"닫히지 않은 리터럴 ({quote})"
                j += 1
            if j >= n:
                return "".join(out), f"닫히지 않은 리터럴 ({quote})"
            out.append(" " * (j + 1 - i))
            i = j + 1
            continue

        out.append(ch)
        i += 1

    return "".join(out), None


def strip_code(source: str) -> str:
    """주석·문자열을 공백으로 지운 사본. 닫히지 않은 리터럴은 무시하고 지운 만큼만 준다.

    ``_strip_literals`` 의 공개 창구다. 소스에서 **선언만** 정규식으로 긁고 싶은
    쪽(`verify_project_layout.py`)이 같은 스캐너를 쓰게 하려고 연다 — 주석 안의
    ``// class Foo`` 나 문자열 안의 ``"class Bar"`` 를 선언으로 세면 검사기가
    거짓 양성을 낸다. 두 번째 구현을 두면 두 판정이 갈라진다.
    """

    stripped, _ = _strip_literals(source)
    return stripped


def check_structure(source: str) -> CheckResult:
    """의존성 없는 1단계 검사. 명백한 것만 잡는다."""

    errors: list[str] = []
    text = source.strip()

    if not text:
        errors.append("소스가 비어 있습니다")
        return CheckResult(ok=False, errors=tuple(errors))

    if "```" in text:
        errors.append("마크다운 코드 펜스(```)가 남아 있습니다")

    code, literal_error = _strip_literals(text)
    if literal_error:
        errors.append(literal_error)

    for open_ch, close_ch, label in (
        ("{", "}", "중괄호"),
        ("(", ")", "괄호"),
        ("[", "]", "대괄호"),
    ):
        depth = 0
        broke_early = False
        for ch in code:
            if ch == open_ch:
                depth += 1
            elif ch == close_ch:
                depth -= 1
                if depth < 0:
                    errors.append(
                        f"{label} 짝이 맞지 않습니다: 여는 것보다 닫는 '{close_ch}' 가 많습니다"
                    )
                    broke_early = True
                    break
        if not broke_early and depth != 0:
            errors.append(f"{label} {depth}개가 닫히지 않았습니다 ('{open_ch}')")

    if "class " not in code and "struct " not in code and "interface " not in code:
        errors.append("class/struct/interface 선언이 없습니다")

    return CheckResult(ok=not errors, errors=tuple(errors))


def _dotnet_enabled() -> bool:
    if os.getenv("UNITY_CSHARP_DOTNET_CHECK", "1").strip() in {"0", "false", "no"}:
        return False
    return shutil.which("dotnet") is not None


def _syntax_errors_from_dotnet(output: str) -> list[str]:
    """빌드 출력에서 **구문 오류만** 골라낸다.

    UnityEngine 어셈블리가 없으니 CS0246(형식을 찾을 수 없음) 같은 것은
    당연히 쏟아진다. 그것까지 실패로 치면 모든 Unity 스크립트가 막힌다.
    """

    found: list[str] = []
    for line in output.splitlines():
        for code in _SYNTAX_ONLY_CODES:
            if f" {code}:" in line or f"{code}:" in line:
                cleaned = line.strip()
                if cleaned not in found:
                    found.append(cleaned)
                break
    return found


def check_with_dotnet(source: str) -> CheckResult | None:
    """``dotnet build`` 로 진짜 파서를 태운다. 못 쓰면 ``None``."""

    if not _dotnet_enabled():
        return None

    with tempfile.TemporaryDirectory(prefix="gdai-csharp-") as tmp:
        root = Path(tmp)
        (root / "Check.cs").write_text(source, encoding="utf-8")
        (root / "Check.csproj").write_text(
            '<Project Sdk="Microsoft.NET.Sdk">\n'
            "  <PropertyGroup>\n"
            "    <TargetFramework>netstandard2.1</TargetFramework>\n"
            "    <Nullable>disable</Nullable>\n"
            "    <EnableDefaultItems>true</EnableDefaultItems>\n"
            "  </PropertyGroup>\n"
            "</Project>\n",
            encoding="utf-8",
        )
        try:
            completed = subprocess.run(
                ["dotnet", "build", "--nologo", "-v", "quiet"],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_DOTNET_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            # SDK 가 깨졌거나 오래 걸린다 — 게이트가 파이프라인을 막아서는 안 된다.
            return None

    # dotnet 출력은 UTF-8 이다. 인코딩을 안 정해주면 subprocess 가 시스템
    # 로케일(cp949 등)로 디코드하려다 리더 스레드가 죽고 stdout/stderr 가
    # None 으로 남는다 — 실측: 한글 Windows 에서 매 create_script 호출이
    # 이걸로 막혔다.
    errors = _syntax_errors_from_dotnet((completed.stdout or "") + (completed.stderr or ""))
    return CheckResult(ok=not errors, errors=tuple(errors), checked_by="dotnet")


def check(source: str) -> CheckResult:
    """구조 검사 → (가능하면) dotnet 검사. 통과는 보장이 아니라 필터다."""

    structural = check_structure(source)
    if not structural.ok:
        return structural

    from_dotnet = check_with_dotnet(source)
    if from_dotnet is None:
        return structural
    return from_dotnet
