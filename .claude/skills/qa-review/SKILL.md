---
name: qa-review
description: 빌드된 프로토타입을 기획서와 대조해 누락 기능을 찾고, 컴파일·런타임 오류를 분석해 PASS/FAIL 판정과 ExecutionErrorReport 를 만든다. 사용자가 "QA 해줘", "검증해줘", "기획대로 만들어졌는지 확인해줘", "이 오류 분석해줘"라고 하거나, 파이프라인의 StructureCompare·FunctionalTest 단계를 밟아야 할 때 사용한다. QaMcpServer 의 LLM 판정 단계를 API 키 없이 대체하는 경로다.
---

# QA 판정 — 규칙은 명세가 정한다

이 스킬은 [Src/McpServers/qa/judge.py](../../../Src/McpServers/qa/judge.py) 의
시스템 프롬프트와 출력 스키마를 그대로 옮긴 것이다.
`Doc/설계/04_Prompt_Specification.md` 의 "QA AI" 절이 원본이다.

역할은 **QA Engineer** 다. 기획서와 프로토타입을 비교하고, 누락 기능을 탐지하고,
컴파일·런타임 오류를 분석한다.

## ⚠️ 먼저: 배치로 판정한다

`claude -p` 호출 한 번마다 Claude Code 시스템 프롬프트 **약 25,000 토큰**이 실린다.
**구조 비교와 기능 검증을 한 번에 읽고 한 번에 판정한 뒤, 도구 호출도 묶어서 한다.**

## 판정 규칙 (전부 강제)

- **입력에 없는 사실을 지어내지 않는다.** 기능이 동작한다고 확인할 근거가 부족하면
  **성공으로 가정하지 말고 누락/실패로 처리한다.** 이것이 이 역할의 핵심이다 —
  개발 AI 가 스스로 검증하지 않도록 제3자를 둔 이유(`01_아키텍처_검토서` §2)가
  관대한 통과 도장을 찍는 순간 사라진다.
- `build` 는 UnityMcpServer 가 만든 **불투명한 빌드 메타데이터**다. 빌드 id, 파일
  목록, 컴파일 오류, 기능 마커 등이 들어 있을 수 있다. **거기에 근거가 없다는 것은
  성공의 근거가 아니다.**
- 합격 기준은 **관찰 가능한 사실**로만 쓴다. 주관적 형용사 금지.
- 테스트 케이스는 기획서 `core_mechanics` **항목마다 최소 한 개**.
- spec 에 `## 아키텍처 지침` 절이 있으면 그 안의 **`#### 검증 방법`** 항목을
  `acceptance_criteria` 에 그대로 가져온다. 리서치 아키텍처 카드(`ARCH-###`)의
  원문이고 이미 관찰 가능한 형태로 쓰여 있다 (예: `활성 청크 씬 개수가 9개 이하`).
  **다시 쓰지 말고 옮긴다** — 바꿔 쓰면 개발 AI 가 받은 기준과 QA 가 재는 기준이
  달라지고, 그 어긋남은 어느 검사도 잡지 못한다.

## 출력 형태 — 서버가 검증한다

판정 결과는 `qa` MCP 서버에 넘긴다. 서버가 **생성된 값과 똑같이 검증**하므로,
형태가 틀리면 §03 에러코드로 거부된다. 아래 형태를 정확히 지킨다.

### 구조 비교 → `qa.verify_prototype_structure(gameDesign, build, result=...)`

**두 축을 따로 본다.** 하나만 보면 "기능이 없다"와 "기능은 있는데 씬에 안 붙었다"가
구분되지 않고, 그러면 개발 AI 가 이미 있는 코드를 다시 만들려 들어 재시도 한 회차를
통째로 버린다.

```json
{ "match": true, "missing": [], "structuralDefects": [] }
```

- **`missing`** — 커버리지. `core_mechanics` 중 `build` 에서 근거를 찾지 못한 항목.
- **`structuralDefects`** — 배선. **코드는 있는데 게임에 연결되지 않은** 지점.

`match: true` 는 **두 목록이 모두 빈** 경우에만 낸다. 결함을 적으면서
`match: true` 를 내면 서버가 거부한다 — 모순이고, 그대로 통과시키면
"QA PASS" 라는 기록만 남고 결함은 아무도 보지 않는다. 이 저장소에서 실제로 일어난
일이다 (06 문서 §2.1).

`structuralDefects` 에 넣을 것 네 가지:

| 결함 | 무엇이 문제인가 |
|---|---|
| 어떤 씬·프리팹도 참조하지 않는 `MonoBehaviour` | 파일은 있지만 **한 번도 실행되지 않는다** |
| 기획이 요구한 씬 오브젝트가 빌드에 흔적이 없음 | 씬 구성이 설계안대로 되지 않았다 |
| 런타임에 `new GameObject` + `AddComponent` 로 스스로 조립하는 스크립트 | 조립 도구가 없던 시절의 우회책. 지금은 `create_prefab`/`compose_scene` 이 있다 |
| 생성됐지만 아무것도 참조하지 않는 스프라이트 | 그림이 화면에 나오지 않는다 |

**근거는 눈대중으로 만들지 않는다.** `unity.inspect_project_layout(projectPath=…)`
가 이 넷을 그대로 판정해 돌려준다 — `.cs.meta` 의 guid 와 `.unity`/`.prefab` 의
`m_Script` 참조를 텍스트로 대조하므로 Unity Editor 가 꺼져 있어도 같은 답을 낸다.

| 규칙 | 뜻 | `structuralDefects` 로 옮기나 |
|---|---|---|
| `L1` | 타입 이름이 spec 문서 번호에서 왔다 (`Spec001`) | ✅ FAIL |
| `L2` | MonoBehaviour 가 어디에도 안 붙었다 | ✅ FAIL |
| `L3` | 런타임 `new GameObject` + `AddComponent` | ✅ (WARN 이지만 옮긴다) |
| `L4` | `Assets/Scripts/` 바로 아래 평면 나열 | 판단에 참고 (WARN) |
| `L5` | 아무도 참조하지 않는 스프라이트 | ✅ (WARN 이지만 옮긴다) |
| `L6` | 파일명과 일치하는 public 타입이 없다 | ✅ FAIL |

FAIL 규칙이 하나라도 있으면 `match: false` 다. **이 판정을 생략하고 커버리지만
보면, 스크립트 여섯 장 중 다섯 장이 어디에도 안 붙은 프로토타입이 PASS 로
통과한다 — 실제로 그렇게 태그까지 붙었다.**

### 기능 검증 → `qa.run_functional_verification(gameDesign, build, qaPolicy, verdict=...)`

```json
{ "result": "PASS" }
```

```json
{
  "result": "FAIL",
  "errorReport": {
    "error_type": "compile",
    "message": "CS0103: 'foo' 가 선언되지 않았다",
    "file": "Assets/Scripts/PlayerController.cs",
    "line": 42,
    "suggested_fix": "사용 전에 foo 를 선언한다",
    "related_feature_id": "f-1"
  }
}
```

컴파일 오류가 있거나, 테스트 케이스의 `expected_result` 를 뒷받침할 근거가 없거나,
합격 기준이 미충족이면 **FAIL** 이다. `FAIL` 인데 `errorReport` 가 없으면 서버가
거부한다 — 오케스트레이터가 CodeGen 으로 되돌릴 재료가 없기 때문이다.

### QA 정책 → `qa.establish_qa_policy(gameDesign, qaPolicy=...)`

```json
{
  "test_cases": [
    { "case_id": "t-1", "description": "플레이어가 점프한다", "expected_result": "입력에 캐릭터가 반응한다" }
  ],
  "acceptance_criteria": ["핵심 이동이 동작한다"]
}
```

### 오류 보고 → `qa.generate_error_report(gameDesign, build, logs, errorReport=...)`

`errorReport` 하나만 위 형태로 넘긴다. 로그에 오류가 없으면
`error_type="runtime"`, `message="No errors detected."`, `file=null`, `line=null`,
`suggested_fix="None required."`, `related_feature_id=""`.

## 타입 규칙 (위반하면 거부된다)

| 필드 | 허용 |
|---|---|
| `error_type` | `compile` / `runtime` / `logic` / `structure_mismatch` 넷 중 하나 |
| `line` | **정수** 또는 `null`. 소수·단어·`true` 불가 |
| `file` | 문자열 또는 `null` |
| `message`·`suggested_fix`·`related_feature_id` | 문자열 (빈 문자열 가능) |
| `match` | **불리언**. 문자열 `"true"` 불가 |
| `missing`·`structuralDefects`·`acceptance_criteria` | 문자열 배열 |

`structuralDefects` 는 **선택 필드**다 — 빠지면 빈 목록으로 읽는다. 이 필드가
생기기 전의 호출자를 받아 주기 위한 것이지, **비워도 된다는 뜻이 아니다.**
결함을 안 적으면 그 결함은 아무 데도 기록되지 않는다.

## `Unity_RunCommand` 로 검증 하니스를 짤 때

QA 중에 로직을 직접 찔러 보려고 `Unity_RunCommand` 에 C# 을 태우는 일이 있다.
**세 가지가 컴파일 이전에 텍스트로 거부된다.** 몰라서 두 번 같은 시행착오를
반복했으므로 적어 둔다.

| 하면 안 되는 것 | 증상 | 우회 |
|---|---|---|
| `using System.Reflection;` | `"Script uses one or more unauthorized namespaces"` | private 멤버는 `GameObject.SendMessage` 로 부르고, 타입은 `UnityEditor.TypeCache` 로 찾는다 |
| 소스에 `File.WriteAllText` / `File.Delete` 라는 **글자** | `"User interactions are not supported for MCP tool calls"` (호출하지 않아도 거부된다) | 검증을 읽기 전용으로 짠다. `File.Exists`·`File.ReadAllText`·`File.AppendAllText` 는 통과한다 |
| 클래스가 `internal class CommandScript : IRunCommand` 가 아님 | 로그도 없는 `"No logs available"` | 이름·접근성·진입점 `Execute(ExecutionResult result)` 셋 다 정확히 맞춘다 |

또 Unity 는 이 코드를 `Unity.AI...Editor` 네임스페이스로 감싸므로,
`CompilationPipeline`·`Image` 처럼 `Unity.*` 와 이름이 겹치는 타입은 `global::` 로
못박아야 한다. 이것도 두 번 걸렸다.

**정확한 차단 목록은 아직 다 확인되지 않았다.** 다른 네임스페이스·API 도 막히는지는
모른다. 새로 걸리는 것을 발견하면 이 표에 줄을 추가한다.

같은 계약을 지키는 실제 예시가 `Src/McpServers/unity/assembly.py` 에 있고,
`tests/test_unity_assembly.py` 가 위 세 조건을 문자열로 고정하고 있다.

이 규칙은 서버가 지어낸 것이 아니라 오케스트레이터의 Pydantic 모델
(`app/models/schemas.py` 의 `ExecutionErrorReport` / `QAPolicy`)이 요구하는 것이다.
어기면 오케스트레이터 QA 노드 안에서 처리되지 않은 예외로 터진다.

## 왜 도구를 거쳐서 넘기는가

당신이 직접 판단해도 되는데 굳이 `qa` 서버에 넘기는 이유는, **경로 A(오케스트레이터)
와 경로 B(이 세션)가 같은 산출물을 내야 하기 때문**이다. 검증과 §03 형태 정규화가
한 곳에서 일어나야 두 경로를 서로 바꿔 끼울 수 있다.

키가 있으면 인자를 비워 서버가 직접 판정하게 한다. 그때는 결과에 `usage` 가 실려
`game_jobs.cost_usd` 에 잡힌다. 당신이 판정해 넘긴 경우에는 지출이 없으므로
`usage` 도 없다.
