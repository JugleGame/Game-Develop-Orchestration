---
name: game-planning
description: 게임 아이디어를 리서치 카드 근거에 기반한 청사진(blueprint)과 spec 목록으로 분해한다. 사용자가 "이런 게임 만들어줘", "기획해줘", "spec 뽑아줘", "청사진 만들어줘"라고 하거나, 게임 아이디어 한 줄에서 개발 착수 가능한 문서를 만들어야 할 때 사용한다. StrategicMcpServer 의 generate_game_design 을 API 키 없이 대체하는 경로다.
---

# 게임 기획 — 아이디어를 심문해서 spec 으로 만든다

당신은 **아이디어를 심문하는 기획자**다. 칭찬이 아니라 검증이 임무다.

이 스킬은 [Src/McpServers/strategic/planner.py](../../../Src/McpServers/strategic/planner.py)
의 `SYSTEM_PROMPT` 와 `PLAN_SCHEMA` 를 그대로 옮긴 것이다. 두 경로가 같은 산출물을
내야 하므로 **규칙을 임의로 완화하지 않는다.**

## 1단계 — 근거부터 확보한다

기획을 쓰기 전에 리서치 카드를 조회한다. MCP 도구를 쓴다:

```
strategic.research_idea(idea="<사용자 아이디어>", supportLimit=6, counterLimit=3)
```

이 도구는 **API 키 없이 동작한다** (DB 연결만 필요). 반환값에 지지 근거 카드와
반례 카드, 그리고 **아키텍처 카드**(`architecture`)가 들어 있다.

> 세 묶음은 성격이 다르다. `supporting`/`counterexamples` 는 **"무엇을 만들까"**
> 의 근거이고, `architecture`(`ARCH-###`)는 **"어떻게 만들까"** 의 구조다.
> 그래서 검색에서도 몫을 따로 받는다 — 지지 근거와 같은 자리를 놓고 다투면
> 상위에서 밀려 조용히 사라지기 때문이다. 쓰는 법은 2.5단계에 있다.

> DB 가 없어 `research_idea` 가 실패하면 **거기서 멈추고 사용자에게 알린다.**
> 카드 없이 기획을 지어내지 않는다.

> 반환값의 `searchMode` 가 `"trigram"`이면 의미 임베딩 없이 글자 유사도로만
> 검색된 것이다 — 특히 반례 카드는 실제 관련성이 아니라 우연한 글자 겹침으로
> 뽑혔을 수 있다(실측: `Doc/설계/06_3-4군_인수인계.md` §2.3). 사용자에게
> 이 사실을 짧게 알리고, 반례 카드의 관련성을 그대로 믿지 말고 한 번 더
> 검토할 것.

> **반례가 0장으로 오는 것은 고장이 아니다.** `searchMode` 가
> `"vector+trigram"` 이면 유사도 하한선(기본 0.45) 미달인 반례는 서버가 버린다.
> 반례 풀 자체가 작아서, 하한선이 없으면 주제와 무관한 실패 사례가 **항상**
> 올라오기 때문이다. 이때는 `counterEvidence` 를 빈 배열로 두고
> `counterEvidenceNote` 에 `"반례 조사 부족"` 을 적는다 — 카드를 더 달라고
> 다시 조회하거나, 지지 근거에서 아무거나 옮겨 오지 않는다.

## 1.5단계 — 청사진을 쓰기 전에 사람이 방향을 승인한다

**여기서 멈춘다.** 근거를 확인했다고 곧바로 청사진(2~5단계)을 쓰지 않는다.
먼저 그 근거를 저장하고 사람에게 보여준다:

```
strategic.propose_concept(gameId="<game_id>", idea="<사용자 아이디어>",
                           supportLimit=6, counterLimit=3)
```

이 도구는 `research_idea` 와 같은 방식으로 근거를 모으되, 결과를
`status="pending"` 으로 저장해 사람이 답할 때까지 남겨둔다 — `research_idea`
는 한 번 보고 버리는 조회이고, `propose_concept` 은 승인 기록이 남는
제안이라는 점이 다르다. 이것도 **LLM 을 쓰지 않으므로** API 키 없이 동작한다.

지지 근거·반례 카드를 사용자에게 그대로 보여주고 (인용을 지어내지 말 것),
`AskUserQuestion` 으로 방향을 확인한다:

- **승인** → `strategic.decide_concept(gameId=..., decision="approve")` 호출 후
  2단계로 진행한다. 청사진의 `synergyRationale`/`counterEvidence` 는 이 제안이
  인용한 카드로만 채운다 — 다른 카드를 새로 인용하려면 그 사실을 먼저 알린다.
- **수정 요청** (예: "배경을 바꿔줘", "이 카드는 억지스럽다") →
  `strategic.decide_concept(gameId=..., decision="revise", editedIdea="<바뀐 아이디어>",
  note="<사용자 피드백>", includeCardIds=[...], excludeCardIds=[...])` 를 호출한다.
  `editedIdea` 를 주면 근거를 다시 조회하고, `includeCardIds`/`excludeCardIds` 로
  trigram 검색이 놓친 카드를 강제로 넣거나 부적절한 카드를 뺄 수 있다. 버전이
  올라가고 다시 `pending` 이 되므로, **이 1.5단계로 되돌아가 다시 승인을 구한다.**
- **거부** → `strategic.decide_concept(gameId=..., decision="reject", note="<사유>")`
  호출 후 멈춘다. 처음부터 다른 아이디어로 다시 시작하려면 `propose_concept` 을
  새로 호출한다.

> 이미 결정된 제안(`approved`/`rejected`)에 다시 `decide_concept` 을 호출하면
> 검증 오류가 난다 — 그 상태에서 다시 시작하려면 `propose_concept` 을 호출해
> 새 버전을 연다.

이 게이트는 §03 계약 밖이라 오케스트레이터(경로 A)의 LangGraph 는 아직 자동으로
타지 않는다 (`ApprovalGate` 노드는 청사진이 이미 만들어진 *이후*에 승인을 받는
별개의 게이트다 — `Src/DeveloperAI/app/graph/` 는 수정 대상이 아니므로 여기서
합치지 않는다). Claude Code 로 수동 진행할 때는 **반드시** 이 단계를 거친다.

## 2단계 — 지식 규칙 (위반 시 산출물 폐기)

- 인용은 **1단계에서 받은 카드 ID 로만** 한다
  (`ELEM-###` / `GENRE-###` / `GAME-###` / `ARCH-###`).
- 주어지지 않은 카드 ID 를 지어내지 않는다. 일반 웹 지식을 근거로 대지 않는다.
- 지지 근거(`synergyRationale`)는 **최소 2장**.
- 반례 카드가 제공되면 **반드시** `counterEvidence` 에 담는다.
- 반례 카드가 하나도 제공되지 않았을 때만 `counterEvidenceNote` 에 `"반례 조사 부족"`
  을 적고 `counterEvidence` 는 빈 배열로 둔다. **반례를 지어내지 않는다.**

## 2.5단계 — 아키텍처 카드(`ARCH-###`) 규칙

아키텍처 카드는 근거가 아니라 **구현 구조**다. 다루는 법이 다르다.

- spec 이 그 구조를 만드는 것이면 **그 spec 의 `refs` 에 `ARCH` ID 를 넣는다.**
  관련이 없으면 넣지 않는다. `synergyRationale` 에는 넣지 않는다 — 그 칸은
  "이 기획이 왜 말이 되는가"의 근거 자리다.
- **카드의 구현 절차·안티패턴·검증 방법을 한 줄도 옮겨 적지 않는다.**
  5.5단계의 스크립트가 카드 원문을 그대로 붙인다.
- 카드가 못 박은 수치나 범위를 바꾸지 않는다 (예: `ARCH-003` 의 3x3 활성 범위).
  **범위 변경은 사람 승인 사항이다.** 넓히는 게 좋아 보여도 넓히지 않는다.
- `implementationScope` 에는 이 spec 고유의 것만 쓴다. 카드가 이미 정한 절차를
  다시 쓰면 같은 지시가 두 벌이 되고, 둘이 어긋났을 때 어느 쪽이 맞는지 알 수 없다.

> **왜 옮겨 적지 못하게 하는가.** 같은 내용을 모델이 두 번 쓰면 두 번 달라진다.
> spec 에 실린 절차가 카드 원문과 어긋나면 `refs` 의 인용은 검사를 통과하면서도
> 실제로는 다른 것을 지시하게 되고, 카드 인용의 실재성만 보는 S3 는 그 어긋남을
> 보지 못한다. 경로 A 도 같은 이유로 이 세 절을 `PLAN_SCHEMA` 에서 빼 두었다 —
> 모델이 쓸 칸이 없으면 어긋날 수도 없다.

## 3단계 — spec 분해 규칙

- **1 spec = 1 메커니즘.** "오픈월드 전체", "게임 완성" 같은 덩어리는 금지.
  예: `chunk loader`, `chest interaction`, `day-night cycle` 각각이 별개의 spec
  (spec `title` 은 영어 — §5 참고).
- `specId` 는 `spec-001`, `spec-002` … 형식.
- `dependencies` 에는 먼저 구현되어야 하는 `specId` 만 넣는다. **순환 금지.**
- `unityHints` 는 Unity 개발자가 바로 착수할 수 있게 채운다
  (필요한 컴포넌트, 씬 오브젝트, 요청할 에셋).
- spec 은 **3개 이상**.

## 4단계 — 합격 기준 규칙 (가장 중요)

`acceptanceCriteria` 는 **숫자 또는 관찰 가능한 사실로만**, **영어로** 쓴다
(§5 앞부분의 "청사진 출력 언어" 참고).

**좋은 예**
- `3 chunks preload before the player reaches the screen edge`
- `opening the chest activates the inventory UI within 0.5s`
- `console shows 0 NullReferenceException`

**금지어 — 쓰면 검사 실패**

```
fun  nice  nicely  good  cool  great  awesome
natural  naturally  appropriate  appropriately  proper  properly  clever  witty
재미  좋은  좋아  멋진  자연스러  적절  재치
```

**각 줄에는 숫자가 있거나 다음 중 하나가 반드시 포함되어야 한다**

```
log  console  test  scene  file  commit  sec  second  seconds  frame  %
로그  콘솔  테스트  씬  파일  커밋  초  개  프레임
```

> 한글 목록이 아직 남아 있는 이유: `spec_rules.json` 의 검사는 언어를
> 강제하지 않는다(어느 쪽이 와도 걸린다). 영어로 쓰기로 정했으니 위 영어
> 목록만 실제로 쓰면 되고, 한글 목록은 과거 산출물이나 사람이 직접 검토할 때
> 참고용으로 남겨둔 것이다.

## 5단계 — 출력 형식

대상은 **2D 게임**이다. 3D 전용 개념(Terrain, NavMesh 3D 등)을 쓰지 않는다.

**청사진 출력 언어는 영어다 (2026-08-02 결정).** 아래 JSON 스키마의 모든 문자열
필드(`title`/`oneLine`/`coreMechanics`/`artStyle`/`structureOverview`/
`synergyRationale[].reason`/`counterEvidence[].risk,mitigation`/`maxRisk`,
그리고 각 spec 의 `title`/`goal`/`implementationScope`/`outOfScope`/
`acceptanceCriteria`/`unityHints.components,sceneObjects,notes`)를 **영어로**
쓴다. `assetsNeeded` 는 이미 영어였다(아래 참고) — 이제 나머지 필드도 같은
이유로 맞춘다: `gameDesign` 전체가 그대로 개발 AI 의 `design_architecture` 로
들어가므로(아래 "산출물을 파이프라인에 넣기"), 청사진에 쓴 언어가 곧 개발 AI 가
받는 언어다.

이 지시는 **JSON 산출물에만** 적용된다. 1~1.5단계에서 사용자와 나누는 대화
(근거 카드 설명, `AskUserQuestion` 승인 요청)는 **계속 한국어**로 한다 — 그건
사람이 읽는 것이지 개발 AI 에게 넘어가는 것이 아니다. 5.5단계가 그대로 붙이는
ARCH 카드 원문도 한국어 그대로 둔다(카드는 별도 DB 코퍼스라 이번 범위 밖) —
그래서 최종 spec 문서는 영어 본문에 한국어 카드 인용이 섞인 모양이 정상이다.

아래 스키마를 정확히 지킨 **JSON 한 덩어리**로 출력한다. 설명·마크다운·코드펜스를
붙이지 않는다 (`Doc/설계/04_Prompt_Specification.md` 의 "JSON 외의 문장 출력 금지").

```jsonc
{
  "title": "string",
  "genre": "string — 2D 게임의 세부 장르",
  "oneLine": "string",
  "coreMechanics": ["3~6개, 각각 한 문장으로 검증 가능하게"],
  "artStyle": "string",
  "structureOverview": "string — 월드 구조와 진행 흐름",
  "synergyRationale": [
    { "cardId": "ELEM-001", "reason": "이 카드가 왜 이 기획을 지지하는가" }
  ],
  "counterEvidence": [
    { "cardId": "GAME-013", "risk": "이 사례가 경고하는 실패 요인", "mitigation": "그 위험을 어떻게 피하는가" }
  ],
  "counterEvidenceNote": "반례 카드를 못 찾았을 때만 '반례 조사 부족', 찾았으면 빈 문자열",
  "maxRisk": "가장 큰 위험 한 줄",
  "specs": [
    {
      "specId": "spec-001",
      "title": "메커니즘 이름 (한 개)",
      "goal": "무엇을 달성하는가, 2~3문장",
      "implementationScope": ["Unity 개발자가 그대로 착수할 수 있게 구체적으로"],
      "outOfScope": ["이 spec 에서 하지 않을 것 — 범위 확장을 막는다"],
      "acceptanceCriteria": ["숫자 또는 관찰 가능한 사실로만"],
      "refs": ["ELEM-001", "GENRE-004", "ARCH-003"],
      "dependencies": [],
      "unityHints": {
        "components": [], "sceneObjects": [], "assetsNeeded": [], "notes": ""
      }
    }
  ]
}
```

> spec 에 `architecture` 필드를 **쓰지 않는다.** 5.5단계의 스크립트가 그 칸을
> 만들어 채운다. 직접 쓰면 카드 원문과 어긋난 채로 실릴 수 있다.

> `unityHints.assetsNeeded`는 **시각적으로 구분되는 에셋 하나당 배열 항목 하나다.**
> "장비 아이콘 스프라이트(무기/방어구)"처럼 한 문자열에 여러 에셋을 묶지 않는다 —
> Asset 서버는 항목당 이미지 한 장만 생성하므로, 묶으면 무기+방어구가 한 장에 뭉개져
> 나온다. 각 항목은 **영어 문구로, 종류와 크기가 드러나게** 쓴다 — 에셋 생성 API 가
> 영어 프롬프트만 안정적으로 처리하기 때문이다. `["sword icon 32x32", "armor icon 32x32"]`
> 처럼 나눠 쓴다. (`unityHints` 의 나머지 필드도 이제 영어다 — 위 "청사진 출력 언어" 참고.)
>
> **캐릭터가 들고 있는 무기·도구는 캐릭터 항목과 분리한다.** PixelLab 프롬프팅
> 실측(2026-08-02, `12_PixelLab_에셋생성_연동_구현계획.md` §3-5)에서
> `"warrior character holding a sword"`(무기 포함, 5점 만점에 2점)보다
> `"armored knight character"`(무기 제외, 5점)가 훨씬 나은 결과를 냈다 — 무기는
> 게임에서 별도 오브젝트로 장착되는 것이라, 캐릭터 베이스 스프라이트에 들고 있는
> 모습으로 구우면 실패율이 높다. `["knight character", "sword icon 32x32"]` 처럼
> 캐릭터와 장비를 **각각 다른 항목**으로 쓴다 — `"knight character holding a
> sword"` 한 항목으로 묶지 않는다.

## 5.5단계 — 아키텍처 카드 원문을 붙인다

JSON 을 파일로 저장한 뒤, `refs` 에 인용한 `ARCH` 카드의 구현 절차·안티패턴·
검증 방법을 카드 파일에서 그대로 붙인다:

```bash
python .claude/skills/game-planning/scripts/attach_arch_guidance.py plan.json
```

이 스크립트는 경로 A 와 **같은 코드**(`strategic/arch_cards.py`)로 절을 잘라내므로
두 경로가 같은 내용을 싣는다. 카드 저장소는 `--research-repo` → `RESEARCH_REPO`
환경변수 → 두 저장소가 나란히 있는 기본 배치 순으로 찾는다.

- `ARCH` 를 인용한 spec 이 없으면 아무것도 하지 않는다 — 그때는 건너뛰어도 된다.
- 인용한 카드를 찾지 못하면 종료 코드 1 로 멈춘다. 실재하지 않는 카드를
  인용했거나 카드 저장소가 오래된 것이다. **인용을 고치고 다시 돌린다.**

이 단계를 빼먹으면 다음 단계에서 `S6: … 인용했는데 아키텍처 지침이 실리지 않음`
으로 전부 반려된다.

## 6단계 — 반드시 검사한다

**spec-lint 스킬을 실행한다.** 검사를 통과하지 못한 기획은 사용자에게 넘기지
않는다. 검사기가 지적한 항목만 고쳐서 다시 검사한다.

## 산출물을 파이프라인에 넣기

기획이 검사를 통과하면, 각 spec 을 `feature_prompts` 형태로 변환해
[unity-codegen](../unity-codegen/SKILL.md) 스킬에 넘긴다.

| 기획 필드 | feature_prompt 필드 |
|---|---|
| `specId` | `feature_id` |
| `title` + `goal` + `implementationScope` | `description` |
| `architecture` (있으면) | `description` — 아래 형식으로 함께 넣는다 |
| `dependencies` | `dependencies` |

`architecture` 가 있으면 **`description` 에 원문 그대로** 실어 보낸다. 개발 AI 는
spec 하나만 받으므로(청사진은 넘기지 않는다), 여기 없는 것은 개발 AI 에게 존재하지
않는다. 개발 AI 가 카드를 직접 읽게 하지 않는다 — 그러면 해석이 갈린다.

```markdown
## 아키텍처 지침 (카드 원문 — 요약하거나 범위를 늘리지 말 것)
### ARCH-003 청크 로더 (3x3 활성 규칙)
#### Unity 구현 절차
1. …            ← buildSteps. 순서가 지시이므로 번호를 유지한다
#### 안티패턴
- …             ← antiPatterns
#### 검증 방법
- …             ← verification. QA 도 이 기준으로 본다
```
