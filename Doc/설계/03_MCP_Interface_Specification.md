# MCP Interface Specification

Version 2.0

> **v2.0 변경 요약**: v1.0의 자체 HTTP 규약(`POST /{tool}` + `success` 봉투)을 폐기하고
> **Model Context Protocol(MCP) 표준**을 채택한다. 오케스트레이터는 공식 `mcp` Python SDK
> (`ClientSession`)를 사용하며, 5개 서버는 모두 MCP 서버로 구현되어야 한다.

---

# 1. 프로토콜

| 항목 | 규격 |
|---|---|
| 와이어 포맷 | JSON-RPC 2.0 |
| 전송 계층 | Streamable HTTP |
| 프로토콜 버전 | `2025-11-25` (SDK가 `initialize`에서 자동 협상) |
| 엔드포인트 | 서버당 **단일 경로** (예: `http://host:9105/mcp`) |
| 세션 | `Mcp-Session-Id` 헤더로 유지, 종료 시 `DELETE` |

도구는 URL 경로가 아니라 **요청 본문의 `name` 필드**로 지정한다.

```jsonc
// tools/call 요청
{"jsonrpc": "2.0", "id": 1, "method": "tools/call",
 "params": {"name": "git_commit",
            "arguments": {"branch": "main", "message": "[AutoGen] ..."},
            "_meta": {"requestId": "<uuid>"}}}
```

---

# 2. 연결 수명주기

```
클라이언트                          서버
    │  initialize (버전 + capabilities)  │
    │ ─────────────────────────────────► │
    │  InitializeResult (버전 + 도구 지원) │
    │ ◄───────────────────────────────── │
    │  notifications/initialized          │
    │ ─────────────────────────────────► │
    │            ── 정상 통신 ──           │
```

오케스트레이터는 **호출 1건당 세션 1개**를 열고 닫는다. SDK의 전송 계층이 anyio
task group 기반이라 세션을 연 태스크와 닫는 태스크가 같아야 하는데, 세션을 클라이언트에
캐시하면 게임별 워크플로 태스크에서 열고 FastAPI lifespan에서 닫게 되어 종료 시 예외가
난다. 핸드셰이크 비용은 수 밀리초이고 도구 호출은 수 초~수 분이므로 이 교환은 타당하다.

---

# 3. 서버가 제공해야 하는 것

## 3.1 Tools (필수)

각 서버는 아래 도구를 `tools/list`에 노출하고 `tools/call`로 실행한다.
**반환값은 dict(매핑)이어야 한다** — FastMCP는 이를 `structuredContent`에 자동으로 채우며,
오케스트레이터는 `structuredContent`를 우선 읽고 없으면 텍스트 블록을 JSON으로 파싱한다.

### StrategicMcpServer

| 도구 | 인자 | 반환 |
|---|---|---|
| `propose_concept` | `gameId: str, idea: str` | `{gameId, version, idea, evidence, status: "pending", ...}` |
| `decide_concept` | `gameId: str, decision: "approve"\|"revise"\|"reject", note?: str, editedIdea?: str` | `{gameId, version, idea, status, decisions: [...]}` |
| `generate_game_design` | `prompt: str` | `{gameDesign: {...}, featurePrompts: [...]}` |
| `analyze_genre` | `prompt: str` | `{genre: str}` |
| `generate_feature_prompt` | `gameDesign: dict` | `{featurePrompts: [...]}` |
| `add_spec` | `gameId: str, idea?: str, spec?: dict` | `{specId, title, ..., featurePrompt: {...}}` (`SpecDocument.to_dict()` + `featurePrompt`) |

#### ConceptGate — 청사진 작성 이전의 사람 검토 (`propose_concept` / `decide_concept`)

청사진 작성은 이 파이프라인에서 가장 비싼 LLM 단계다. 방향이 틀린 아이디어로
청사진을 쓰면 그 비용은 전부 버려진다. 그래서 **청사진을 쓰기 전에** 근거만
모아 사람에게 보여주고 승인을 받는다. 두 도구 모두 LLM 을 호출하지 않으므로
`ANTHROPIC_API_KEY` 없이 동작한다.

`propose_concept` 은 리서치 DB 에서 지지 근거와 반례를 모아 `status="pending"`
으로 저장한다. `decide_concept` 이 그 제안을 처리한다.

| `decision` | 의미 | 다음 |
|---|---|---|
| `approve` | 이대로 진행 | 승인된 `idea` 를 `generate_game_design(prompt=...)` 로 넘긴다 |
| `revise` | `editedIdea` 로 질의를 바꿔 다시 제안 | 버전을 올려 `pending` 으로 되돌아간다 |
| `reject` | 이 제안을 종료 (`note` 필수) | 사람 개입 |

`revise` 루프는 **반드시 상한을 둔다** (오케스트레이터 기본값
`CONCEPT_MAX_ITERATIONS=3`). 상한을 넘으면 `HumanEscalation` 으로 보낸다 — §3.3
의 다른 재시도 루프와 같은 규칙이며, 출구 없는 루프를 두지 않기 위함이다.

LangGraph 에서는 `ConceptGate` 노드가 `Intake` 와 `Planning` 사이에 위치하고,
`ApprovalGate` 와 같은 방식으로 `interrupt()` 로 멈춘다. 경로 B(Claude Code)
에서는 [pipeline-run](../../.claude/skills/pipeline-run/SKILL.md) 스킬의 2a 단계가
같은 두 도구를 호출한다 — **두 경로가 같은 게이트를 통과한다.**

#### `add_spec` — 이미 있는 게임에 spec 하나를 더한다

`revise_spec`(기존 spec 개정)과 `generate_game_design`(청사진·spec 전체 재작성)
사이에는 자리가 하나 비어 있었다 — **기존 게임에 독립 spec 하나를 더하는 것.**
`add_spec`이 그 자리를 채운다. 청사진과 기존 spec 은 건드리지 않는다.

`specId`는 이 서버가 게임의 기존 spec 다음 번호로 정한다(`spec-00N` → `spec-00(N+1)`).
`spec` 인자에 담아 넘긴 값에 `specId`가 있어도 무시한다 — 호출자가 번호를 잘못
짚어 기존 spec 을 덮어쓰는 사고를 막기 위해서다.

`spec`을 채우면 모델을 부르지 않고 검증(lint)만 한다 (경로 B, `design_architecture`의
`design` 인자와 같은 규칙). 비우면 `idea`로 모델을 불러 spec 하나를 만든다 — 이때
`ANTHROPIC_API_KEY`가 필요하다.

### UnityMcpServer

| 도구 | 인자 | 반환 |
|---|---|---|
| `design_architecture` | `gameId: str, gameDesign?: dict, featurePrompts?: [dict], design?: dict` | `{files: [...], prefabs: [...], scene: {...}, notes: [...], typeMap: str}` |
| `create_script` | `featureId: str, prompt: str` | `{file: "Assets/..."}` |
| `create_scene` | `featureId: str, sceneName: str` | `{scene: str}` |
| `create_prefab` | `gameId: str, prefabName: str, components?: [str], sprite?: str, prefabPath?: str` | `{prefab: str, attached: [str], missing: [str]}` |
| `compose_scene` | `gameId: str, sceneName: str, objects?: [{name, parent, components, prefab}]` | `{scene: str, objects: int, attached: [str], missing: [str]}` |
| `bind_reference` | `gameId: str, target: str, field: str, value: str, scene?: str` | `{bound: bool, target: str, field: str, component: str, value: str}` |
| `define_assemblies` | `gameId: str, projectPath?: str, apply?: bool` | `{assemblies: [{name, folder, path, references, packages, scripts}], skipped: [{category, reason}], written: [str]}` |
| `import_asset` | `featureId: str, assetPath: str` | `{imported: str}` |
| `build_project` | `gameId: str` | `{buildId: str, ...}` |
| `run_playmode_test` | `gameId: str` | `{passed: bool}` |
| `get_compile_errors` | `gameId: str` | `{errors: [{file, line, message}]}` |
| `inspect_project_layout` | `gameId?: str, projectPath?: str` | `{ok: bool, counts: {...}, findings: [{rule, severity, target, message}]}` |

**설계 → 조립의 순서가 이 표의 핵심이다.** `design_architecture` 는 **게임당 한 번**
불러 파일·프리팹·씬 구성을 한꺼번에 정하고, 그 결과가 뒤 도구들의 인자가 된다.

| 설계안의 자리 | 그 값을 받는 도구 |
|---|---|
| `files[].path` · `files[].className` | `create_script(plannedPath=…, plannedClass=…)` |
| `prefabs[]` 한 항목 | `create_prefab` |
| `scene` | `compose_scene` |

즉 `create_prefab` · `compose_scene` · `bind_reference` 는 **새로 판단하지 않고
설계안을 실행만 한다.** 같은 설계안이면 조립 결과가 실행마다 같은 이유가 이것이다.

`create_script` 는 파일을 **하나** 만든다. spec 하나가 파일 여럿이 되는 것은
설계안이 파일을 여럿으로 나누고 호출자가 그만큼 부르기 때문이지, 이 도구가
여러 개를 돌려주기 때문이 아니다 — 그래서 `{file}` 반환이 그대로 유지된다.

셋 중 `inspect_project_layout` 만은 Unity Editor 없이 돈다. `.cs.meta` 의 guid 와
`.unity`/`.prefab` 의 `m_Script` 참조를 텍스트로 대조하므로 CI 에서도 같은 답을 낸다.
나머지 조립 3종은 `create_script` 와 마찬가지로 Editor 가 떠 있어야 한다.

`define_assemblies` 는 조립이 끝난 뒤 마지막에 부른다 (04 명세의 **Assembly Rule**).
**설계안이 아니라 실제 소스를 읽어** 카테고리 사이의 참조를 구하고, 그중 떼어내도
순환이 생기지 않는 것에만 `.asmdef` 를 놓는다. 나머지는 이유(`skipped`)와 함께
`Assembly-CSharp` 에 남는다 — 덜 나뉜 상태는 느릴 뿐 깨지지 않지만, 잘못 나누면
컴파일이 통째로 막히기 때문이다. `apply=false` 로 부르면 계획만 돌려준다.

배경과 근거는 `Doc/설계/06_코드생성_아키텍처_진단_260730.md`, 계약 변경 이력은
`Doc/설계/05_계약_변경_제안서.md` §8 에 있다.

### QaMcpServer

| 도구 | 인자 | 반환 |
|---|---|---|
| `establish_qa_policy` | `gameDesign: dict` | `{qaPolicy: {...}}` |
| `verify_prototype_structure` | `gameDesign: dict, build: str` | `{match: bool, missing: [...]}` |
| `compare_structure` | `gameDesign: dict, build: str` | 〃 |
| `run_functional_verification` | `gameDesign: dict, build: str, qaPolicy: dict` | `{result: "PASS"\|"FAIL", errorReport?: {...}}` |
| `generate_error_report` | `gameDesign: dict, build: str, logs: str` | `{errorReport: {...}}` |

### AssetGenMcpServer

| 도구 | 인자 | 반환 |
|---|---|---|
| `generate_2d_sprite` | `featureId: str, prompt: str, gameId?: str, artStyle?: str` | `{assetPath, assetId, kind, gameId, status, styleSeed, generatedBy}` |
| `generate_ui_asset` | `featureId: str, prompt: str, gameId?: str, artStyle?: str` | 〃 |
| `generate_3d_placeholder` | `featureId: str, prompt: str, gameId?: str, artStyle?: str` | 〃 |

오케스트레이터는 `assetPath` 만 읽지만(`app/graph/nodes/assetgen.py`), 나머지
필드도 계약이다 — 개발 하네스의 목 서버가 이 형태를 같이 내야 목으로 돌린
파이프라인과 실물로 돌린 파이프라인의 반환이 갈리지 않는다.

`gameId` 를 생략하면 모든 게임이 `"default"` 하나를 공유해 팔레트와 매니페스트가
섞이므로, 호출자가 아는 값이 있으면 반드시 넘긴다.

`generatedBy` 는 그 에셋을 무엇이 만들었는지다 — 지금은 `"pixellab"` 하나뿐이다.
생성 경로가 하나이고 폴백이 없어서, `PIXELLAB_API_KEY` 가 없거나 호출이 실패하면
값이 달라지는 대신 `errorCode 3000` 으로 실패한다. 목 서버도 같은 값을 낸다.

에셋 생성량은 `imagesGenerated`(정수, 이 호출이 계정 월 할당량에서 쓴 이미지 장
수)로 보고한다. 0 이면 필드를 싣지 않는다.

### GitMcpServer

저장소 전략: 게임 1개 = 저장소 1개. 이름 규칙 `{genre}-{prompt-slug}-{gameId}`
(예: `platformer-double-jump-hero-3f9a1c2e`).

| 도구 | 인자 | 반환 |
|---|---|---|
| `git_init` | `repoName: str` | `{repoName: str, created: bool}` |
| `git_branch` | `branch: str, repoName?: str` | `{branch: str}` |
| `git_pull` | `branch: str, repoName?: str` | `{branch: str, updated: bool}` |
| `git_commit` | `branch: str, message: str, repoName?: str, sourcePath?: str` | `{commit: str}` |
| `git_push` | `branch: str, repoName?: str` | `{branch: str, pushed: bool}` |
| `git_tag` | `tag: str, repoName?: str` | `{tag: str}` |
| `git_status` | `repoName?: str` | `{clean: bool}` |

`git_init`은 **멱등**이다: `repoName`이 없으면 새로 만들고, 이미 있으면 그대로 재사용한다.
배포 순서: `git_init → git_branch → git_pull → git_commit → git_push → git_tag`.
저장소가 게임별로 분리되므로 브랜치는 항상 `main` 하나만 사용한다.
`git_pull`은 이전 배포 이력을 먼저 반영해 non-fast-forward push 실패를 막는다.

`repoName`은 `git_init` 외 모든 도구에서 **옵션**이다 — 생략하면 "가장 최근에
`git_init`된 저장소"로 폴백한다. 오케스트레이터는 Planning 단계부터 유지하는
`state["repo_name"]`을 매 호출에 실어 이 폴백에 의존하지 않는다
(`app/graph/nodes/deployment.py`). `MAX_CONCURRENT_JOBS=1`인 동안은 폴백도
안전하지만, 동시 실행을 켤 때는 반드시 명시적으로 보내야 한다.
`sourcePath`는 `git_commit`에서만 쓰는 옵션 인자로, 커밋에 포함할 Unity
프로젝트 실물 경로를 가리킨다 — 이 인자가 왜 필요한지는
`05_계약_변경_제안서.md` §1에 있다.

## 3.2 Prompts (선택, 권장)

[04_Prompt_Specification](04_Prompt_Specification.md)의 역할 프롬프트를 `prompts/list` /
`prompts/get`으로 노출하면 오케스트레이터가 런타임에 조회할 수 있다.

## 3.3 Resources (선택)

빌드 로그, 생성된 기획서 등 읽기 전용 산출물은 Resources로 노출할 수 있다.

---

# 4. 에러 처리 — 2계층

| 계층 | 형태 | 언제 | 오케스트레이터 매핑 |
|---|---|---|---|
| **프로토콜** | JSON-RPC error | 없는 도구, 잘못된 인자, 세션 실패 | `-32602` → `1000`, 그 외 → `3000` |
| **도구 실행** | `CallToolResult.isError = true` | 도구는 찾았으나 실행이 실패 | 본문의 `errorCode` 우선, 없으면 `3000` |

§03 v1.0의 숫자 에러 코드는 유지된다. 서버가 이를 보존하려면 에러 본문에
`errorCode` 필드를 포함시킨다.

| 코드 | 의미 |
|---|---|
| 1000 | Validation Error |
| 2000 | Timeout |
| 3000 | MCP Error |
| 4000 | Unity Build Error |
| 5000 | Unknown |

---

# 5. 타임아웃

| 서버 | 타임아웃 |
|---|---|
| Strategic | 120초 |
| Unity | 300초 |
| QA | 180초 |
| Asset | 180초 |
| Git | 60초 |

`ClientSession(read_timeout_seconds=...)`와 `call_tool(read_timeout_seconds=...)`
양쪽에 적용된다.

---

# 6. 재시도

Exponential backoff, 최대 3회 **시도**(재시도 2회).

**전송 계층 실패만 재시도한다.** JSON-RPC 에러와 `isError` 결과는 서버가 이미 요청을
받았다는 뜻이므로 재시도하지 않는다.

**아래 도구는 재시도하지 않는다** (응답이 유실돼도 서버 상태는 이미 바뀌었을 수 있음):

- `GitMcpServer`: `git_commit`, `git_push`, `git_tag`

서버는 `_meta.requestId`를 활용해 멱등 처리를 구현할 수 있다.

---

# 7. 인증

Streamable HTTP 전송은 httpx `auth`를 지원한다. 오케스트레이터 API의 `API_KEY`와
동일한 방식으로 서버 측 인증을 붙일 수 있다 (현재 미구현).

---

# 8. 로컬 검증

[`scripts/mock_mcp_servers.py`](../../Src/DeveloperAI/scripts/mock_mcp_servers.py)가
5개 서버를 FastMCP 기반 실제 MCP 서버로 띄운다. 임의의 MCP 클라이언트로 점검 가능:

```bash
npx @modelcontextprotocol/inspector
```
