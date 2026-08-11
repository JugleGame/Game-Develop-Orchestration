---
name: pipeline-run
description: 게임 생성 파이프라인 전체를 Claude Code 에서 수동으로 실행한다. 사용자가 "게임 만들어줘", "파이프라인 돌려줘", "처음부터 끝까지 해줘"라고 하거나, 기획부터 Unity 빌드까지 이어서 진행해야 할 때 사용한다. LangGraph 오케스트레이터를 API 키 없이 대체하는 경로이며, 단계 순서와 재시도 상한은 graph.py 와 동일하다.
---

# 파이프라인 수동 실행 — LangGraph 가 하던 일을 그대로 따라간다

이 스킬은 [Src/DeveloperAI/app/graph/graph.py](../../../Src/DeveloperAI/app/graph/graph.py)
의 노드·엣지를 **손으로 밟는** 절차다. 순서를 바꾸거나 단계를 건너뛰지 않는다.

## 시작 전 확인

```
unity.unity_bridge_status()      # Unity Editor 가 붙어 있는가
strategic.research_status()      # 리서치 DB 가 붙어 있는가
```

둘 중 하나라도 실패하면 **거기서 멈추고 사용자에게 알린다.** 반쯤 진행된 상태로
두는 것이 아무것도 안 한 것보다 나쁘다.

`qa` 와 `git` 은 사전 점검 도구가 없다. `qa` 는 판정 시점에 키가 없으면 실패하는데,
경로 B 에서는 애초에 키를 쓰지 않으므로(10·11번 참고) 문제가 되지 않는다.

## 단계표

| # | LangGraph 노드 | 여기서 할 일 | 도구 / 스킬 |
|---|---|---|---|
| 1 | `Intake` | `game_id` 를 정하고 요청을 기록 | — |
| 2a | `ConceptPropose` / `ConceptGate` | 근거 조회 → **아이디어 제안**(사람이 승인/수정/거부) | `strategic.propose_concept` → `decide_concept` ([game-planning](../game-planning/SKILL.md) 1.5단계) |
| 2b | `Planning` | 승인된 아이디어로 청사진 + spec 작성 | [game-planning](../game-planning/SKILL.md) 2단계~ |
| 3 | — | 발행 전 검사 | [spec-lint](../spec-lint/SKILL.md) |
| 4 | `ApprovalGate` | **사용자에게 완성된 청사진을 보여주고 승인을 받는다** | 사람 |
| 4.5 | `CodeGen` (앞부분) | **설계 패스 — 파일·타입·프리팹·씬을 게임당 한 번 정한다** | `unity.design_architecture` ([unity-codegen](../unity-codegen/SKILL.md)) |
| 5 | `CodeGen` | 설계안의 **파일마다** C# → Unity 투입 | [unity-codegen](../unity-codegen/SKILL.md) |
| 6 | `AssetGen` | 스프라이트·UI 생성 후 임포트 | `asset.*` → `unity.import_asset` |
| 7a | `BuildPrototype` | **조립 — 프리팹 → 씬 계층 → 인스펙터 참조** | `unity.create_prefab` → `compose_scene` → `bind_reference` |
| 7b | `BuildPrototype` | 어셈블리 분리 (컴파일을 나눠 재시도를 빠르게) | `unity.define_assemblies` |
| 7c | `BuildPrototype` | 구조 자가 검사 후 빌드 | `unity.inspect_project_layout` → `unity.build_project` |
| 8 | `CompileCheck` | 컴파일 오류 확인 | `unity.get_compile_errors` |
| 9 | `ErrorCorrection` | 오류 있으면 5번으로 되돌아감 | [unity-codegen](../unity-codegen/SKILL.md) |
| 10 | `StructureCompare` | 기획의 기능이 다 구현됐는지 대조 | [qa-review](../qa-review/SKILL.md) → `qa.*` |
| 11 | `FunctionalTest` | 플레이 모드 실행, 런타임 오류 수집 | `unity.run_playmode_test` → [qa-review](../qa-review/SKILL.md) |
| 12 | `Deployment` | 커밋·태그·푸시 | `git.git_init` → `branch` → `pull` → `commit` → `push` → `tag` |

> **QA·Git 서버는 이제 있다.** `Src/McpServers/` 에 strategic / unity / asset /
> qa / git 다섯 서버가 모두 있고 `.mcp.json` 에 연결되어 있다. QA 판정에는 API 키가
> 필요하므로, 경로 B 에서는 당신이 [qa-review](../qa-review/SKILL.md) 규칙으로 판단한
> 뒤 그 결과를 도구 인자(`result` / `verdict` / `errorReport`)로 넘긴다. 넘긴 값도
> 생성된 값과 똑같이 검증되므로 두 경로의 산출물이 같아진다.

> **6번은 spec 의 `unityHints.assetsNeeded` 를 자산 하나씩으로 쪼갠다.** 한
> feature 의 긴 설명을 통째로 프롬프트로 주면 서버가 그 안에서 **처음 만난
> 키워드**로 종류를 정한다 — 플랫포머 spec 이 "재시작 버튼"을 한 번 언급했다는
> 이유로 버튼 스프라이트 하나만 나온다. 항목마다 도구를 골라 부른다.
>
> | 항목에 들어 있는 말 | 부를 도구 |
> |---|---|
> | `3d` `mesh` `메시` `메쉬` `입체` | `asset.generate_3d_placeholder` |
> | `ui` `hud` `버튼` `패널` `메뉴` `아이콘` `인벤토리` … | `asset.generate_ui_asset` |
> | 그 외 | `asset.generate_2d_sprite` |
>
> 3D 를 먼저 본다 — "3D 인벤토리 아이콘"은 UI 를 언급하는 3D 요청이다. 판단이
> 애매하면 어느 쪽으로 보내도 서버가 다시 분류하므로 조용히 망가지지는 않는다
> (경로 A 의 `app/graph/nodes/assetgen.py::select_asset_tool` 과 같은 규칙).
> 생성 후에는 항목마다 `unity.import_asset` 을 잊지 않는다.

> **4.5 · 7a 가 이 표에서 가장 새로운 부분이다.** 예전에는 5번이 spec 하나를
> 파일 하나로 만들고, 7번이 **빈 씬**을 만드는 `create_scene` 뿐이었다. 그래서
> 스크립트 여섯 장 중 다섯 장이 어떤 GameObject 에도 붙지 않은 채 빌드되고
> **QA PASS 까지 났다** (`Doc/설계/06_코드생성_아키텍처_진단_260730.md` §1.2).
>
> - **4.5 는 게임당 한 번이다.** 9번으로 되돌아와도 설계안을 다시 뽑지 않는다 —
>   회차마다 구조가 흔들리면 재현성이 무너진다.
> - **7a 는 순서가 있다.** 씬이 프리팹을 인스턴스화하므로 `create_prefab` 이 먼저다.
>   `bind_reference` 는 대상이 이미 존재해야 하므로 마지막이다.
> - **7a 는 6번 뒤에 온다.** 그래야 `bind_reference` 가 방금 임포트한 그림을 꽂을
>   수 있다. 순서를 뒤집으면 그림이 화면에 나오지 않는다.
> - **7c 를 건너뛰지 않는다.** `inspect_project_layout` 의 L1(문서 번호 이름)과
>   L2(미부착 MonoBehaviour)가 0건이어야 빌드로 넘어간다. 그 둘이 남은 채 빌드하면
>   위의 사고가 그대로 재현된다.
> - **7b 는 조립이 끝난 뒤다.** `define_assemblies` 는 실제 소스의 참조를 읽어
>   나누므로 코드가 다 들어온 뒤여야 한다. 나누지 못한 카테고리는 `skipped` 에
>   **이유와 함께** 나오니 그것을 읽는다 — 대개 `Assets/Scripts` 최상위에 평면으로
>   놓인 파일이 원인이고, 그건 Folder Rule 을 먼저 지키라는 신호다. 조립이 아직
>   불안하면 `apply=false` 로 계획만 먼저 봐도 된다.

> **12번 배포는 `sourcePath` 를 반드시 넘긴다.** `git_commit(branch, message, sourcePath=...)`
> 에 Unity 프로젝트 경로(`UNITY_PROJECT_PATH` 와 같은 값)를 주면 그 트리를 작업 클론에
> 미러링한 뒤 커밋한다. **넘기지 않으면 빈 커밋이 발행되고, 파이프라인은 성공을 보고한다.**
> `Library/`·`Temp/`·`obj/`·`Logs/`·`*.csproj` 는 서버가 제외하므로 직접 걸러낼 필요는 없다.
> 자세한 내용은 [05_계약_변경_제안서](../../../Doc/설계/05_계약_변경_제안서.md) §1.

## 승인 게이트가 두 곳이다 — 둘 다 건너뛰지 않는다

**2a 의 아이디어 제안 게이트**(`propose_concept`/`decide_concept`)는 이제 §03
계약에 포함되어 있고 LangGraph 에도 `ConceptPropose` → `ConceptGate` 두 노드로
존재한다. 두 경로가 같은 게이트를 통과한다. 거부되면 2a 로 되돌아가며,
**재제안은 최대 3회**(`CONCEPT_MAX_ITERATIONS`) — 상한을 넘으면 사람에게
넘긴다. 제안 자체는 LLM 을 쓰지 않아 저렴하지만, 출구 없는 루프를 두지 않는다는
§3.3 규칙은 이 루프에도 똑같이 적용된다.

**4번 `ApprovalGate`**는 LangGraph 에 실제로 있는 노드로, 여기서 `interrupt()`
로 **멈춘다**. 이 스킬도 멈춘다. 청사진을 사용자에게 제시하고 승인을 받기
전에는 5번으로 넘어가지 않는다. 거부되면 피드백을 받아 2b 로 되돌아간다 —
**재기획은 최대 3회** (`PLANNING_MAX_ITERATIONS`, 기본값 5이지만 비용 때문에
3 권장).

## 재시도 상한 (반드시 지킨다)

| 루프 | 상한 | 넘으면 |
|---|---|---|
| `ApprovalGate` → `Planning` 재기획 | 3회 | 사람에게 넘김 |
| `ErrorCorrection` → `CodeGen` 재생성 | 3회 | 사람에게 넘김 |
| `FunctionalTest` → `CodeGen` 재생성 | 3회 | 사람에게 넘김 |

> LangGraph 는 이 상한을 **그래프 구조로 강제**하지만, 여기서는 **당신이 세야 한다.**
> 상한을 넘겼는데 "한 번만 더" 하지 않는다. 같은 오류가 반복되면 그것은 재생성으로
> 풀리지 않는 문제다.

## 진행 상황 보고

LangGraph 는 노드마다 상태를 DB 와 Redis 에 흘려보낸다. 여기서는 **각 단계가 끝날 때
한 줄로 보고한다** — 무엇이 끝났고, 다음이 무엇인지. 도구 호출 사이에 침묵하지 않는다.

실패했을 때는 어느 단계에서, 어떤 도구가, 어떤 오류로 실패했는지 그대로 전한다.
"거의 다 됐다"고 말하지 않는다.

## 이미 만든 게임에 기능을 추가할 때 (FeatureExtend)

12단계까지 끝난 게임에 기능을 더할 때, **1번부터 다시 돌리지 않는다.** 기존
기능이 재기획 과정에서 바뀌거나 깨질 위험이 있고, 청사진·spec 전체를 다시
쓰는 비용도 크다.

1. `git.git_branch(branch="feature/<이름>")` — 원본을 건드리지 않는다. 실패해도
   되돌리기 쉽게 하는 안전망이다.
2. **기존 기능과 이어지는 독립적인 새 기능**은 `strategic.add_spec(gameId=<게임>,
   idea="<추가할 기능>")` 으로 spec 을 하나 더 발행한다. 청사진과 기존 spec 은
   건드리지 않는다 — specId 는 서버가 게임의 다음 번호로 정하므로 겹칠 걱정이
   없다. **기존 spec의 범위 자체를 넓히는 경우**(예: "적 공격 처리"가
   `outOfScope`로 빠져 있던 spec에 그 처리를 채워 넣는 것)라면 새 spec 대신
   `strategic.revise_spec(specId=<대상>, feedback="<추가할 내용>",
   source="human")` 으로 그 spec만 개정한다 — 둘 중 "새 메커니즘인가, 있는
   메커니즘의 확장인가"로 고른다.
3. **새 spec 이 새 파일을 필요로 하면** `unity.design_architecture` 를 다시
   부른다 — 단, `design` 인자에 **기존 architecture 의 `files`/`prefabs`/`scene`
   을 그대로 두고 새 spec 의 파일만 덧붙인** 값을 넘긴다. 게임당 한 번이라는
   규칙은 "처음부터 다시 설계하지 않는다"는 뜻이지 "다시 부르지 않는다"는
   뜻이 아니다 — 기존 파일의 경로·클래스명을 그대로 유지하면 재현성이
   깨지지 않는다.
4. 5번(`unity.create_script`)부터 재진입한다 — 새로 생긴 파일만 만든다.
5. 이하 6~12번을 그대로 밟는다 — 새로 생긴 파일만 조립·검증 대상이다.
6. QA(10~11번)를 통과한 뒤에만 `git.git_commit`으로 브랜치에 커밋하고,
   본 브랜치로 병합할지는 사람이 정한다.

## 오케스트레이터 경로로 넘길 때

`ANTHROPIC_API_KEY` 를 확보한 뒤에는 이 스킬 대신 오케스트레이터를 쓴다:

```bash
cd Src/DeveloperAI && uvicorn app.main:app --reload
curl -X POST localhost:8000/games -H 'Content-Type: application/json' \
     -d '{"prompt":"<아이디어>"}'
```

이 스킬로 만든 산출물(청사진 JSON, C# 파일)은 그대로 재사용된다. 두 경로는 같은
MCP 서버와 같은 Unity 프로젝트를 쓰기 때문이다.
