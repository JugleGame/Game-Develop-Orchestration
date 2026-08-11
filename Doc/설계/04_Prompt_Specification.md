# Prompt Specification

Version 1.0

---

# Strategic AI

역할

게임 기획자

출력

반드시 JSON

추측 금지

Schema 준수

---

Output

Genre

Mechanics

Art Style

Scene

Feature List

---

# Unity AI

역할

Unity Senior Developer

Unity 6

C#

MonoBehaviour

Namespace

Folder Rule

Prefab Rule

Assembly Rule

주석·로그는 한글

반드시 준수

---

## 다섯 번째 규칙 — 주석·로그는 한글 (2026-08-02)

디버깅은 QA 판정을 넘어 사람이 콘솔·소스를 직접 읽는 작업이다. 팀이 한글 화자라
Unity AI 가 내는 클래스 주석과 `Debug.Log`/`Debug.LogWarning`/`Debug.LogError`
메시지를 한글로 고정한다 — 식별자·키워드·API 이름은 그대로 C# 이고, "산문 금지"
규칙의 유일한 예외가 주석·로그 문자열이다.

구현은 `unity/codegen.py::SYSTEM_PROMPT`(생성) 와 `_REPAIR_RULES`(재시도 수정) 다.
아래 네 규칙과 달리 이 규칙은 정적 검사기가 없다 — 언어 판정은 프롬프트 수준에
서만 강제되고, 위반해도 컴파일이나 `project_layout.py` 는 통과한다. 필요해지면
그때 검사를 추가한다.

## 위 네 규칙이 지금 어디에 구현돼 있나 (2026-07-31)

이 절이 오랫동안 **문장으로만** 존재했다. 2026-07-30 진단 시점에 `Src/` 전체에서
`prefab` 문자열이 0건이었다 — 즉 세 규칙 중 둘은 이행할 코드가 한 줄도 없었고,
그래서 지키는지 어기는지를 논할 수조차 없었다
(`06_코드생성_아키텍처_진단_260730.md` §2.2). 같은 일이 반복되지 않도록,
규칙마다 **구현 위치와 그것을 지키는지 보는 검사**를 함께 적는다.

| 규칙 | 무엇을 요구하나 | 구현 | 위반을 잡는 검사 |
|---|---|---|---|
| **Namespace** | 주어진 네임스페이스에 타입을 넣는다 (기본 `Game.Gameplay`) | `unity/codegen.py::SYSTEM_PROMPT`, `ScriptGenerator._namespace` | — |
| **Folder Rule** | `Assets/Scripts/{Category}/{ClassName}.cs` | `unity/architecture.py::_PATH_PATTERN` (설계 시점 거부) → `create_script(plannedPath=…)` | `project_layout.py` L4 (평면 나열 = WARN), L6 (파일명 불일치 = FAIL) |
| **Prefab Rule** | 반복 등장물은 프리팹으로 만들어 재사용한다 | `unity/architecture.py` 의 `prefabs[]` 검증 → `unity.create_prefab` / `compose_scene` / `bind_reference` (`unity/assembly.py`) | `project_layout.py` L2 (미부착 MonoBehaviour = FAIL), L3 (런타임 조립 = WARN), L5 (미참조 스프라이트 = WARN) |
| **Assembly Rule** | `.asmdef` 로 빌드를 분리한다 | `unity/assemblies.py::plan_assemblies` → `unity.define_assemblies` | `project_layout.py` L7 (어셈블리 참조 순환 = FAIL) |

**네 규칙이 모두 이행됐다** (2026-07-31). Assembly Rule 이 마지막이었던 것은 의도한
순서다 — 어셈블리를 잘못 나누면 순환 참조로 컴파일이 **아예 안 되므로**, 증상이
"느려진다"가 아니라 "게임이 안 만들어진다"라서 조립이 실물로 안정된 뒤에 했다.

그 위험을 다루는 방식이 이 규칙의 핵심이다. `plan_assemblies` 는 **순환을 검출해
거부하지 않고 애초에 만들지 않는다.** `.asmdef` 의 제약 두 가지가 그렇게 만든다:
하나의 `.asmdef` 는 자기 폴더와 하위 전체를 덮으므로 **서로 참조하는 두 형제
폴더는 합칠 수 없고**(합칠 수 없으면 나눌 수도 없다), `.asmdef` 어셈블리는
**`Assembly-CSharp` 를 참조할 수 없다**(그러니 떼어내려면 의존 대상이 전부 함께
떨어져 나와야 한다). 두 조건을 못 채우는 카테고리는 후보에서 빠져
`Assembly-CSharp` 에 남는다 — **덜 나뉜 상태는 느릴 뿐 깨지지 않는다.**

L7 은 그래서 우리 도구가 만든 것을 잡으려는 검사가 아니다(거기엔 순환이 생길 수
없다). 사람이 손으로 고쳤거나 다른 도구가 놓은 `.asmdef` 가 섞였을 때, 빌드 한
바퀴를 쓰기 전에 텍스트로 잡는 자리다.

**함정 하나가 더 있고, 실물에서 실제로 걸렸다.** `Assembly-CSharp` 는 프로젝트의
모든 패키지 어셈블리를 자동으로 참조한다. `.asmdef` 를 놓는 순간 그 자동 참조가
사라지므로 `using UnityEngine.InputSystem;` 한 줄이 들어 있던 파일은 참조를
명시하지 않으면 컴파일되지 않는다 — 실측하면
`error CS0234: ... (are you missing an assembly reference?)` 다. 그래서
`assemblies.py` 는 소스의 `using` 을 읽어 `PACKAGE_ASSEMBLIES` 로 어셈블리
이름을 되짚어 참조에 싣는다. 표에 없는 패키지를 쓰는 코드가 나오면 컴파일
오류로 즉시 드러나므로, 그때 한 줄 더하면 된다.

여기에 더해, **클래스 이름은 게임 개념이어야 한다** — `ChunkLoader`,
`PlayerController` 는 맞고 `Spec001`, `Feature003` 은 틀리다. 이것은 원래 이 문서에
없던 규칙인데, 이름을 `feature_id` 에서 기계적으로 만들던 구현 때문에
`Assets/Scripts/Spec001.cs` ~ `Spec006.cs` 가 나왔다. 지금은
`architecture.py` 가 설계 시점에 거부하고 `project_layout.py` L1 이 결과물에서
잡는다. 두 곳이 **같은 정규식 하나**(`project_layout.DOCUMENT_NAME_PATTERN`)를
쓰는 것이 중요하다 — 두 벌을 두면 설계에서 통과한 이름이 검사에서 막히는 상태가
생기고, 그 어긋남은 실행해 보기 전까지 드러나지 않는다.

---

출력

C#

JSON

둘 중 하나

설명 금지

---

# QA AI

역할

QA Engineer

기획서와

Prototype 비교

누락 기능 탐지

Compile Error 분석

Runtime Error 분석

---

출력

PASS

FAIL

Error Report

JSON

---

# JSON Schema

GameDesign

{

gameId

genre

mechanics

platform

artStyle

featureList

}

---

ErrorReport

{

errorType

message

file

line

suggestedFix

}

---

규칙

JSON 외의 문장 출력 금지.

Markdown 출력 금지.

설명 출력 금지.

반드시 Schema 준수.