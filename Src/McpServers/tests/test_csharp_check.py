"""로컬 C# 문법 게이트 검증.

이 게이트의 목적은 명백한 구문 오류를 Unity 빌드(기본 예산 1800초) **이전에**
쳐내는 것이다. 그래서 두 방향을 모두 고정한다.

* 깨진 소스를 정말 잡는가 (게이트가 하는 일)
* **멀쩡한 소스를 막지 않는가** (거짓 양성은 파이프라인을 세우므로 더 나쁘다)

``dotnet`` 유무에 결과가 흔들리면 안 되므로, 아래는 전부 구조 검사 단계만
직접 겨냥한다.
"""

from __future__ import annotations

import pytest

from unity import csharp_check

_VALID = """
using UnityEngine;

namespace Game.Gameplay
{
    // f-1
    public sealed class PlayerController : MonoBehaviour
    {
        [SerializeField] private float speed = 5f;

        private void Update()
        {
            var input = new Vector2(Input.GetAxis("Horizontal"), 0f);
            transform.Translate(input * speed * Time.deltaTime);
        }
    }
}
"""


def test_valid_source_passes():
    assert csharp_check.check_structure(_VALID).ok


def test_missing_closing_brace_is_caught():
    result = csharp_check.check_structure("public class A {\n  void B() {\n")

    assert not result.ok
    assert any("중괄호" in error for error in result.errors)


def test_extra_closing_brace_is_caught():
    result = csharp_check.check_structure("public class A { } }")

    assert not result.ok
    assert any("중괄호" in error for error in result.errors)


def test_unclosed_string_is_caught():
    result = csharp_check.check_structure('public class A { void B() { Debug.Log("oops); } }')

    assert not result.ok
    assert any("리터럴" in error for error in result.errors)


def test_empty_source_is_caught():
    assert not csharp_check.check_structure("   ").ok


def test_source_without_a_type_declaration_is_caught():
    result = csharp_check.check_structure("using UnityEngine;\nint x = 1;")

    assert not result.ok
    assert any("class/struct/interface" in error for error in result.errors)


def test_leftover_markdown_fence_is_caught():
    """모델이 출력 규칙을 어긴 흔적. 그대로 Unity 에 넣으면 반드시 깨진다."""

    result = csharp_check.check_structure("```csharp\npublic class A { }\n```")

    assert not result.ok
    assert any("```" in error for error in result.errors)


# ---------------------------------------------------------------------------
# 거짓 양성 방지 — 아래는 전부 유효한 C# 이며 통과해야 한다
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("label", "source"),
    [
        (
            "문자열 안의 중괄호",
            'public class A { void B() { Debug.Log("{ unbalanced }"); } }',
        ),
        (
            "문자 리터럴 안의 따옴표",
            "public class A { char q = '\\''; }",
        ),
        (
            "verbatim 문자열 안의 경로와 따옴표",
            'public class A { string p = @"C:\\Assets\\{x}"; string q = @"say ""hi"""; }',
        ),
        (
            "주석 안의 중괄호",
            "public class A { // } not real\n /* } neither */ }",
        ),
        (
            "제네릭과 인덱서",
            "using System.Collections.Generic;\npublic class A "
            "{ Dictionary<int, List<string>> m = new(); string F() => m[1][0]; }",
        ),
        (
            "인터페이스만 선언",
            "public interface IDamageable { void TakeDamage(int amount); }",
        ),
    ],
)
def test_valid_constructs_are_not_rejected(label: str, source: str):
    result = csharp_check.check_structure(source)

    assert result.ok, f"{label}: 멀쩡한 소스를 막았습니다 — {result.errors}"


# ---------------------------------------------------------------------------
# dotnet 단계
# ---------------------------------------------------------------------------
def test_dotnet_stage_is_skipped_when_disabled(monkeypatch):
    """SDK 를 개발 머신에 강제하지 않는다 — 없으면 구조 검사 결과만 쓴다."""

    monkeypatch.setenv("UNITY_CSHARP_DOTNET_CHECK", "0")

    assert csharp_check.check_with_dotnet(_VALID) is None
    assert csharp_check.check(_VALID).checked_by == "structure"


def test_only_syntax_diagnostics_count_as_failures():
    """UnityEngine 어셈블리가 없으니 CS0246 은 항상 나온다. 그걸 실패로 치면
    모든 Unity 스크립트가 게이트에 막힌다."""

    output = (
        "Check.cs(3,7): error CS0246: The type or namespace name 'UnityEngine' "
        "could not be found\n"
        "Check.cs(9,1): error CS1513: } expected\n"
    )

    found = csharp_check._syntax_errors_from_dotnet(output)

    assert len(found) == 1
    assert "CS1513" in found[0]


def test_structure_failure_short_circuits_before_dotnet(monkeypatch):
    """1단계에서 이미 깨졌으면 subprocess 를 띄울 이유가 없다."""

    def _boom(source: str):  # pragma: no cover - 호출되면 테스트가 실패한다
        raise AssertionError("dotnet 단계가 실행되면 안 됩니다")

    monkeypatch.setattr(csharp_check, "check_with_dotnet", _boom)

    result = csharp_check.check("public class A {")

    assert not result.ok
    assert result.checked_by == "structure"
