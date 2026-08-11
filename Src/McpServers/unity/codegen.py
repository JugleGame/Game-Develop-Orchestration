"""기능 설명(prompt) → Unity 6 C# 소스 생성.

Unity 공식 MCP 는 코드를 **생성하지 않는다**. ``Unity_CreateScript`` 는 완성된
``Contents`` 를 요구하는 에디터 자동화 도구다. 반면 오케스트레이터의 §03
``create_script(featureId, prompt)`` 는 자연어 프롬프트를 넘긴다.

그 간극을 여기서 메운다. 규칙은 ``04_Prompt_Specification`` 의 "Unity AI" 절
(Unity 6 / C# / MonoBehaviour / 네임스페이스·폴더 규칙 준수, 설명 금지)을
그대로 따른다.

두 가지 입력이 생성 품질을 좌우한다.

* ``project_context`` — 이 게임의 청사진 요약과 코드 규약. **한 게임 안에서
  모든 feature 가 공유하며 변하지 않는다.** 이게 없으면 각 호출이 완전히
  독립된 대화라서, ``PlayerController`` 를 쓸 때 ``InventorySystem`` 이
  존재하는지조차 모른 채 작성한다 — 컴파일 에러의 구조적 원인이다.
* ``existing_types`` — 지금까지 만들어진 타입 목록. 같은 이유로 필요하지만
  호출마다 **늘어나므로** 캐시 접두사에 넣을 수 없다 (아래 참고).

``previous_source`` 가 오면 생성이 아니라 **수정**으로 전환한다. 재시도가
백지에서 파일을 다시 쓰면 고쳐진 곳 옆이 새로 깨질 수 있고, 매 라운드가
사실상 무작위 재추첨이라 수렴한다는 보장이 없다.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from typing import Any

from common.usage import usage_of

# §04 Prompt Specification — Unity AI 역할 정의를 시스템 프롬프트로 옮긴 것.
SYSTEM_PROMPT = """You are a Unity Senior Developer writing production C# for Unity 6.

Rules:
- Target Unity 6 (6000.x). Use the new Input System when handling input.
- Derive from MonoBehaviour unless the feature clearly calls for ScriptableObject
  or a plain class.
- Put the type in the namespace you are given. Do not invent a different one.
- Serialize inspector-facing fields with [SerializeField] private, not public fields.
- Null-guard any reference obtained via GetComponent/Find before use.
- Write every comment in Korean (한글). Add a single line comment naming the
  feature id above the class declaration (e.g. "// 기능: spec-003"), so QA can
  trace a runtime error back to the feature that caused it.
- Every Debug.Log / Debug.LogWarning / Debug.LogError call must carry a Korean
  message stating what happened and any value that matters for debugging —
  this is the primary debugging aid for a Korean-speaking developer reading
  the console.
- This is a 2D game: prefer Rigidbody2D / Collider2D / Vector2 over their 3D
  counterparts.

Output rules:
- Output ONLY the C# source. No prose, no explanation, no markdown fences.
- Comments and log strings are the one exception to "no prose" — they must be
  Korean. Everything else (identifiers, keywords, API names) stays as normal
  C#.
- The output must compile as-is."""

_REPAIR_RULES = """
Repair rules (these override the generation framing above):
- You are fixing an existing file, not rewriting it. Preserve every line that
  the reported error does not require changing — including formatting, member
  order, comments, and names other code may already reference.
- Make the smallest change that removes the reported error.
- Do not rename the type, change its namespace, or drop existing members.
- Any comment or Debug.Log message you add or change must still be Korean.
- Output the COMPLETE corrected file, not a diff or a fragment."""

# claude-opus-5 의 최소 캐시 접두사는 512토큰이다. SYSTEM_PROMPT 만으로는
# 849자(약 210토큰)라 cache_control 을 붙여도 **오류 없이 무시**되고
# cache_creation_input_tokens 가 0 으로 남는다 — 절감했다는 착시만 생긴다.
# project_context 가 함께 실려 이 선을 넘을 때만 붙인다.
_MIN_CACHEABLE_PREFIX_TOKENS = 512


def _estimate_tokens(text: str) -> int:
    """접두사가 캐시 최소치를 넘는지 가늠한다. 정확한 값이 목적이 아니다.

    정확히 세려면 ``count_tokens`` 를 불러야 하는데, 스크립트 하나 만들 때마다
    왕복을 한 번 더 하는 비용이 캐시로 아끼는 것보다 크다. 그래서 추정한다.

    문자 수만으로는 안 된다 — 언어에 따라 토큰 밀도가 3배 넘게 차이 난다.
    영어/코드는 약 4자당 1토큰인 반면 한글 음절은 대체로 1자당 1토큰에
    가깝다(자모로 쪼개지면 그보다 더 든다). 청사진 요약은 한국어로 오는 일이
    많아 이 구분이 실제로 판정을 뒤집는다: 같은 621자 문맥이 영어면 155토큰,
    한국어면 400토큰대다.

    한글 나눗값은 1.2 로 잡았다 — 1자=1토큰과 넉넉한 쪽 사이의 중간이라,
    빗나가더라도 실제 토큰 수를 **과대**평가하지 않는다.

    추정이므로 틀릴 수 있고, 양쪽 다 조용히 실패한다: 낮게 잡으면 캐시 기회를
    놓치고, 높게 잡으면 breakpoint 가 무시된다(손해는 없고 착시만 남는다).
    실제 성립 여부는 응답의 ``cache_read_input_tokens`` 가 0보다 큰지로만
    확인되며, 그 값은 ``usage`` 에 실려 오케스트레이터까지 간다.
    """

    ascii_chars = sum(1 for ch in text if ch.isascii())
    wide_chars = len(text) - ascii_chars
    return int(ascii_chars / 4 + wide_chars / 1.2)


@dataclass(frozen=True)
class GeneratedScript:
    class_name: str
    path: str
    contents: str
    namespace: str
    # Every type this file declares, not just the first one. The caller feeds
    # these to the next script as ``existing_types``; carrying only the first
    # made every sibling type in the same file invisible to later features,
    # which is how one file came to redefine what another already had
    # (Doc/설계/06 §2.1).
    types: tuple[str, ...] = ()
    # Token cost of producing this script. ``None`` when the source was handed
    # in rather than generated — that path spends nothing.
    usage: dict[str, Any] | None = None


class CodeGenerationError(RuntimeError):
    """C# 생성 실패."""


def _sanitize_class_name(feature_id: str) -> str:
    """feature_id 를 유효한 C# 식별자(PascalCase)로 바꾼다.

    'f-1', 'FEAT-PLAYER-001' 같은 값이 그대로는 클래스명이 될 수 없다.
    """

    parts = re.split(r"[^0-9A-Za-z]+", feature_id)
    name = "".join(part[:1].upper() + part[1:] for part in parts if part)
    if not name or name[0].isdigit():
        name = f"Feature{name}"
    return name


def looks_like_csharp(text: str) -> bool:
    """이미 C# 소스가 넘어온 경우를 알아본다 (LLM 호출을 건너뛰기 위함)."""

    lowered = text.lower()
    return ("class " in lowered or "struct " in lowered) and "{" in text and "}" in text


def strip_fences(text: str) -> str:
    """모델이 규칙을 어기고 ```csharp 펜스를 붙였을 때를 대비한 방어."""

    cleaned = text.strip()
    cleaned = re.sub(r"^```[a-zA-Z#]*\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _extract_class_name(source: str, fallback: str) -> str:
    match = re.search(r"\b(?:class|struct)\s+([A-Za-z_]\w*)", source)
    return match.group(1) if match else fallback


def extract_declared_types(source: str) -> tuple[str, ...]:
    """이 소스가 선언한 **모든** 타입 이름.

    판정은 ``project_layout.parse_types`` 를 빌려 쓴다 — 주석·문자열 안의
    ``class`` 를 세지 않는 스캐너가 이미 거기 있고, 같은 것을 두 벌 두면 생성기와
    검사기가 서로 다른 답을 내게 된다.
    """

    from project_layout import parse_types

    return tuple(item.name for item in parse_types(source))


class ScriptGenerator:
    """Claude 로 C# 을 생성한다. 키가 없으면 명확히 실패한다."""

    def __init__(
        self,
        model: str | None = None,
        namespace: str | None = None,
        script_root: str | None = None,
    ) -> None:
        self._model = model or os.getenv("UNITY_CODEGEN_MODEL", "claude-opus-5")
        self._namespace = namespace or os.getenv("UNITY_SCRIPT_NAMESPACE", "Game.Gameplay")
        self._script_root = (script_root or os.getenv("UNITY_SCRIPT_ROOT", "Assets/Scripts")).rstrip("/")
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            if not os.getenv("ANTHROPIC_API_KEY"):
                raise CodeGenerationError(
                    "ANTHROPIC_API_KEY 가 없어 C# 을 생성할 수 없습니다. "
                    "키를 설정하거나, create_script 호출 시 contents 인자로 "
                    "완성된 C# 소스를 직접 넘기세요."
                )
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic()
        return self._client

    def plan(
        self,
        feature_id: str,
        source: str,
        *,
        planned_path: str = "",
        planned_class: str = "",
    ) -> GeneratedScript:
        """소스에서 클래스명을 뽑아 저장 경로까지 확정한다.

        ``planned_path``/``planned_class`` 는 설계 패스(``design_architecture``)가
        정한 값이다. 주어지면 그것을 쓴다 — 설계가 이미 게임 개념으로 이름을
        지었으므로 여기서 다시 지을 이유가 없다.

        둘 다 없을 때만 ``_sanitize_class_name`` 이 쓰인다. 그 함수는 ``spec-001``
        을 ``Spec001`` 로 바꾸는데, **클래스 이름이 게임 개념이 아니라 문서 번호에서
        오는 원인이 정확히 이것이었다** (06 문서 §1.3). 그래서 이제는 설계안이
        없을 때만 도는 최후 수단이다.
        """

        fallback = planned_class or _sanitize_class_name(feature_id)
        class_name = _extract_class_name(source, fallback)
        path = planned_path or f"{self._script_root}/{class_name}.cs"
        return GeneratedScript(
            class_name=class_name,
            path=path,
            contents=source,
            namespace=self._namespace,
            types=extract_declared_types(source),
        )

    def _system_blocks(self, project_context: str, repairing: bool) -> list[dict[str, Any]]:
        """캐시가 실제로 성립하도록 안정/변동 구간을 나눈다.

        캐싱은 바이트 접두사 일치라서, 순서가 곧 설계다. 한 게임 안에서
        변하지 않는 것(규칙 + 청사진 요약)을 앞에 두고 그 끝에 breakpoint 를
        찍는다. 호출마다 달라지는 것(수정 지시)은 그 뒤에 붙어 캐시를 깨지
        않는다.
        """

        blocks: list[dict[str, Any]] = [{"type": "text", "text": SYSTEM_PROMPT}]
        if project_context.strip():
            blocks.append(
                {
                    "type": "text",
                    "text": "[Project context — shared by every script in this game]\n"
                    + project_context.strip(),
                }
            )
            # 접두사가 512토큰을 넘길 가망이 있을 때만 breakpoint 를 찍는다.
            # 미달이면 조용히 무시되므로 붙여봐야 착시만 남는다.
            prefix = "".join(block["text"] for block in blocks)
            if _estimate_tokens(prefix) >= _MIN_CACHEABLE_PREFIX_TOKENS:
                blocks[-1]["cache_control"] = {"type": "ephemeral"}

        if repairing:
            # breakpoint 뒤 — 이 지시문은 재시도에서만 붙으므로 캐시 접두사에
            # 들어가면 첫 호출과 재시도가 서로의 캐시를 깬다.
            blocks.append({"type": "text", "text": _REPAIR_RULES})
        return blocks

    @staticmethod
    def _user_prompt(
        feature_id: str,
        namespace: str,
        suggested: str,
        prompt: str,
        existing_types: list[str],
        previous_source: str,
    ) -> str:
        lines = [
            f"Feature id: {feature_id}",
            f"Namespace: {namespace}",
            f"Suggested class name: {suggested}",
        ]
        if existing_types:
            # 캐시 breakpoint 뒤에 오는 변동 구간. 호출마다 늘어난다.
            lines.append(
                "\nTypes already generated for this game — reference them by these exact "
                "names instead of inventing new ones, and do not redefine them:\n"
                + "\n".join(f"- {item}" for item in existing_types)
            )
        if previous_source.strip():
            lines.append(f"\nCurrent contents of this file:\n{previous_source.strip()}")
            lines.append(f"\nFix this file so the following is resolved:\n{prompt}")
        else:
            lines.append(f"\nImplement this feature:\n{prompt}")
        return "\n".join(lines)

    async def generate(
        self,
        feature_id: str,
        prompt: str,
        *,
        project_context: str = "",
        existing_types: list[str] | None = None,
        previous_source: str = "",
        planned_path: str = "",
        planned_class: str = "",
    ) -> GeneratedScript:
        """기능 설명에서 C# 을 만든다 (또는 ``previous_source`` 를 고친다)."""

        client = self._ensure_client()
        suggested = planned_class or _sanitize_class_name(feature_id)
        repairing = bool(previous_source.strip())

        response = await client.messages.create(
            model=self._model,
            max_tokens=8000,
            system=self._system_blocks(project_context, repairing),
            messages=[
                {
                    "role": "user",
                    "content": self._user_prompt(
                        feature_id,
                        self._namespace,
                        suggested,
                        prompt,
                        existing_types or [],
                        previous_source,
                    ),
                }
            ],
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
        )

        if response.stop_reason == "refusal":
            raise CodeGenerationError(f"코드 생성이 거부되었습니다: {response.stop_details}")

        text = "".join(block.text for block in response.content if block.type == "text")
        source = strip_fences(text)
        if not source:
            raise CodeGenerationError("모델이 빈 응답을 반환했습니다.")
        if not looks_like_csharp(source):
            raise CodeGenerationError(
                f"생성 결과가 C# 소스로 보이지 않습니다: {source[:160]!r}"
            )

        planned = self.plan(
            feature_id, source, planned_path=planned_path, planned_class=planned_class
        )
        return replace(planned, usage=usage_of(response, self._model))
