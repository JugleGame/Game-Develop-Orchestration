# Game-Developer-AI

2D 게임을 자동 생성하는 파이프라인. 사람의 아이디어 한 줄에서
기획 → C# 코드 → 에셋 → Unity 빌드 → QA → Git 배포까지 이어진다.

## 저장소 구조

| 경로 | 무엇 |
|---|---|
| `Doc/설계/` | 설계 명세. **이 문서들이 진실의 원천이다.** |
| `Src/DeveloperAI/` | FastAPI + LangGraph 오케스트레이터 (파이프라인 두뇌) |
| `Src/McpServers/` | MCP 도구 서버 5종 — strategic / unity / asset / qa / git |
| `Src/Web/` | React + Vite 대시보드 |
| `.claude/skills/` | Claude Code 가 이 프로젝트에서 일하는 방법 |
| `SETUP.md` | 새 컴퓨터에서 시작하는 법. `python scripts/bootstrap.py` 한 줄 |

## 생성된 게임 프로젝트

이 파이프라인이 만든 Unity 프로젝트는 **이 저장소 밖, 별도 git 저장소**에
있다 — 절대경로는 사람·기기마다 다르므로 여기 박아두지 않는다. 예: Slime.
위치를 모르면 사용자에게 물어본다(같은 세션에서 이미 언급됐으면 그걸 쓴다) —
흔한 함정으로, 이름이 비슷한 사본 디렉터리(`HackathonProto` 등)가 같이
있을 수 있으니 실제 git 저장소인지(`.git` 존재) 확인한다. 이 저장소의
CLAUDE.md 는 자동으로 읽히지만, 작업 대상이 다른 경로면 그 저장소의
CLAUDE.md 는 **자동으로 안 읽힌다** — 그쪽 프로젝트를 만지는 작업이면 시작
전에 그 저장소 루트의 CLAUDE.md 를 먼저 읽는다. spec→파일 매핑, 씬 구조,
에셋 파이프라인 함정처럼 그 프로젝트 전용 캐시가 거기 있다 — 여기 다시
적지 않는다.

## 두 가지 실행 경로

이 저장소는 **같은 파이프라인을 두 가지 방법으로** 돌릴 수 있다.

| | 경로 A — 오케스트레이터 | 경로 B — Claude Code |
|---|---|---|
| 두뇌 | LangGraph (`Src/DeveloperAI/app/graph/`) | 이 세션의 Claude |
| LLM 호출 | `planner.py` / `codegen.py` / `judge.py` 안에서 `AsyncAnthropic()` | Claude Code 자신 |
| 필요한 것 | `ANTHROPIC_API_KEY` | 없음 |
| 무인 실행 | 가능 | 불가 (사람 세션 필요) |
| MCP 서버 | 동일 | 동일 |

**중요:** 두 경로는 **같은 MCP 서버, 같은 명세, 같은 검사 규칙**을 쓴다.
경로 B는 경로 A를 대체하는 게 아니라, 키 없이 파이프라인을 검증하는 방법이다.
`Src/DeveloperAI/app/graph/` 는 어느 경로에서도 수정 대상이 아니다.

### LLM 을 쓰는 도구를 경로 B 에서 부르는 법

키가 필요한 도구는 **완성된 산출물을 직접 받는 인자**를 하나씩 갖고 있다. 그 인자를
채우면 서버는 모델을 호출하지 않고 검증만 한 뒤 같은 형태로 돌려준다 — 두 경로가
같은 산출물을 내도록 하는 장치다.

| 서버 | 도구 | 키 없이 쓰려면 |
|---|---|---|
| `unity` | `design_architecture` | `design` 에 완성된 설계안 |
| `unity` | `create_script` | `contents` 에 완성된 C# |
| `qa` | `establish_qa_policy` | `qaPolicy` 에 완성된 정책 |
| `qa` | `verify_prototype_structure` / `compare_structure` | `result` 에 `{match, missing, structuralDefects}` |
| `qa` | `run_functional_verification` | `verdict` 에 `{result, errorReport?}` |
| `qa` | `generate_error_report` | `errorReport` 에 완성된 보고 |
| `strategic` | `add_spec` | `spec` 에 완성된 spec 한 장 |

넘긴 값도 **생성된 값과 똑같이 검증된다.** 형태가 틀리면 §03 에러코드로 거부되므로,
이 경로가 스키마 보장에 구멍을 내지 않는다. 판정 규칙은
[qa-review](.claude/skills/qa-review/SKILL.md) 스킬에 있다.

`strategic` 에서 이런 인자를 가진 것은 `add_spec` 하나뿐이다 — `spec` 을 채우면
모델을 안 부르고 lint 만 한다(`server.py::add_spec`). 나머지 중
`generate_game_design` 은 경로 B 에서
[game-planning](.claude/skills/game-planning/SKILL.md) 스킬이 대신한다.
`git` 은 LLM 을 쓰지 않아 처음부터 키가 필요 없다. `strategic` 의
`propose_concept`/`decide_concept`(ConceptGate)도 LLM 을 쓰지 않으므로 두 경로가
키 없이 똑같이 호출한다.

### 서버를 띄우는 법

경로 A 는 서버를 **Streamable HTTP** 로 부르고, 경로 B(`.mcp.json`)는 stdio 로
띄운다. 포트는 `Src/McpServers/common/registry.py` 한 곳에서만 정한다.

```bash
cd Src/McpServers && python serve_all.py        # 다섯 대 모두 (Ctrl-C 로 종료)
cd Src/McpServers && python serve_all.py qa git # 일부만
```

`docker-compose.yml` 은 Postgres·Redis 만 띄운다 — MCP 서버는 위 스크립트가
띄운다. `verify_contract.py` 도 같은 HTTP 주소를 걸므로 이 스크립트가 먼저다.

### LLM 비용은 도구가 보고한다

모델을 부른 도구는 결과에 `usage` 를 실어 보낸다. `app/utils/usage.py` 가 이를 모아
`game_jobs.cost_usd` 에 적재하므로, **LLM 을 쓰면서 `usage` 를 안 싣는 서버는 그
지출을 통째로 숨기는 셈이다.** `common/usage.py::usage_of()` 를 쓴다.
경로 B 로 넘긴 산출물은 지출이 0 이므로 `usage` 를 싣지 않는다.

## 이 저장소에서 일할 때

- **명세를 따른다.** 추측해서 구현하지 않는다. `Doc/설계/CLAUDE.md` 의 코딩 규칙
  (Python 3.12 / PEP8 / Black 100칼럼 / Type Hint / Docstring / Pydantic V2 /
  async / `print()` 금지)이 모든 Python 코드에 적용된다.
- **TODO 를 남기지 않는다.** 동작하는 코드만 커밋한다. (코드 내 `TODO` 주석
  얘기다 — 아래 "미완료 작업 목록"은 별개의, 문서 레벨 백로그다.)
- **비즈니스 로직은 Service 에만.** API 라우터와 Repository 에 쓰지 않는다.
- **LangGraph 노드는 `GraphState` 외의 상태를 갖지 않는다.**
- **기획과 개발의 경계를 넘지 않는다.** 기획은 **기능 경계**("무엇을 해야 하는가"),
  개발은 **코드 경계**("어떤 클래스·파일로 나누는가")를 정한다. 기획 산출물에
  `PlayerController` 같은 C# 타입명이 등장하면 경계가 무너진 것이다. 경계마다 누가
  기계로 확인하는지는 [11_역할경계_계약서](Doc/설계/11_역할경계_계약서.md) —
  **확인자가 없는 규칙은 규칙이 아니라 희망이다.**
- 커밋 메시지: `feat:` `fix:` `refactor:` `test:` `docs:` `chore:` — 한 커밋 한 기능.

## 미완료 작업 목록 관리 (`Doc/설계/00_미완료_작업_목록.md`)

이 파일은 **살아있는 백로그**다 — 완료 이력을 쌓아두는 문서가 아니라 지금
남은 일만 담는다. 팀원이 저장소를 새로 받아 Claude Code 에게 작업을 맡길 때
쓰는 진입점이기도 하다.

- **세션을 시작하면 이 파일부터 읽는다.** 사용자가 특정 작업을 콕 집지
  않았다면 여기서 다음 작업을 고른다.
- **작업 도중에는 이 파일을 고치지 않는다.** 작업 하나를 끝낼 때마다 08 을
  고치면 한 세션에서 같은 문단을 여러 번 다시 쓰게 되고, 중간 상태가 파일에
  남는다. 진행 상황 추적은 세션의 작업 목록(TaskCreate/TaskUpdate)이 담당한다.
- **작업 전체가 끝나면 08 갱신안을 사용자에게 제안한다.** 무엇을 지우고 무엇을
  남길지, 남기는 항목의 상태를 어떻게 고칠지 요약해 보여주고 — 승인받은 뒤
  파일을 고친다. 사용자가 08 갱신을 미리 위임했으면 그때는 바로 반영하고 무엇을
  바꿨는지 알린다.
- **끝난 항목은 통째로 지운다.** `✅ 완료` 표시를 남기고 쌓아두지 않는다 —
  완료 이력은 git 커밋 로그가 담당하고, 이 파일은 현재 시점의 "할 일"만 반영해야
  다음 사람이 뭘 볼지 헷갈리지 않는다.
- **아직 목록에 없는 일은 갱신안에 새 항목으로 넣는다** — 사용자가 새로 시킨
  작업이든, 일하다가 스스로 발견한 후속 작업이든 동일하다. 항목 형식은 파일에
  있는 기존 항목들을 그대로 따른다 — 상태(왜 아직 안 끝났는지)와 "다음에 할 일"을
  반드시 적는다.
- **세션이 중간에 끊길 것 같으면 그 시점에 제안한다.** 다음 사람(또는 다음
  세션)이 이어받을 수 있어야 한다는 것이 이 파일의 존재 이유이므로, "작업 전체가
  끝날 때까지" 를 이유로 아무 기록도 남기지 않은 채 끊기지는 않게 한다.
- 항목이 하나도 안 남아도 파일 자체는 지우지 않는다 — 헤더만 남긴다.
- 다른 설계 문서(`06`, `07` 등)에 상태 표시(`✅ 해결됨`)를 남기는 것과는
  별개다. 그런 문서는 특정 논의의 히스토리를 남기는 게 목적이라 append-only가
  맞고, `00_미완료_작업_목록.md` 만 "지금 남은 일" 전용으로 비운 채 유지한다.

## MCP 서버 연결 (`.mcp.json`)

stdio 핸드셰이크로 검증된 상태. **도구 개수는 서버가 노출하는 전부**이고, 그중
§03 계약이 정한 것만 `verify_contract.py` 가 본다 — 계약 초과분은 의도적으로
무시하므로, 이 표가 틀려도 그 검사기는 잡아주지 않는다:

| 서버 | 도구 | 상태 |
|---|---|---|
| `asset` | 9개 | ⚠️ `PIXELLAB_API_KEY` 필수 — 없으면 생성 도구가 전부 에러를 반환한다 (폴백 경로 없음) |
| `unity` | 13개 | ✅ 뜬다. 실제 작업은 Unity Editor 가 떠 있어야 한다 — 단 `inspect_project_layout` 은 파일만 읽어 Editor 없이 돈다 (`define_assemblies` 도 계획은 파일만 읽고, 반영할 때만 Editor 가 필요하다) |
| `strategic` | 11개 | ⚠️ `RESEARCH_DSN` (또는 `NEON_DSN`) 필요 |
| `qa` | 5개 | ✅ 뜬다. 판정에는 키가 필요하고, 없으면 위의 인자로 넘긴다 |
| `git` | 7개 | ✅ 추가 설정 없이 뜬다. 저장소는 `GIT_ROOT` (기본 `./git_output`) |

Streamable HTTP 로도 5종 전부 §03 계약을 충족하는 것이 확인됐다 (2026-07-30,
`serve_all.py` → `verify_contract.py`). `strategic` 은 사내망에서 5432 가 막혀도
`connect_pool` 의 443 폴백으로 MCP 핸드셰이크 30초 안에 뜬다.

설정은 저장소 루트의 `.env` 한 장이다. `RESEARCH_DSN` 을 포함해 서버가 읽는 값을 모두
거기 적는다 — `common/env.py` 가 `import common` 시점에 적용하므로 stdio·HTTP 어느
경로로 띄우든 같은 값을 본다. 셸에 export 한 값이 항상 이긴다. 자격증명은 `.mcp.json`
에 적지 않는다 — 이 파일은 커밋된다. 전체 목록은 `.env.example`, 절차는 `SETUP.md`.

**`command`·`cwd`·`env` 값은 `${CLAUDE_PROJECT_DIR:-.}` 로 시작한다.** 손으로 쓴
임의 `${VAR}` 나 상대경로는 안 된다 — Claude Code 는 `command` 를 확장하지 않고
문자열 그대로 실행 파일 경로로 쓰며, 상대경로를 `cwd` 필드 기준으로 풀지도 않는다.
`CLAUDE_PROJECT_DIR` 만 예외다: Claude Code 가 stdio MCP 서버를 띄울 때 프로젝트
루트로 직접 주입하는 내장 변수라 (v2.1.203+), `.mcp.json` 안에서 유일하게 확장된다.
둘 중 어느 쪽으로 어긋나든 증상은 똑같이 **다섯 서버가 전부 "지정된 경로를 찾을
수 없습니다" 로 즉사**하는 것이다 (Windows 는 실행 파일과 작업 디렉터리를 못 찾을
때 같은 메시지를 낸다).

실제로 두 번 겪었다. `${GDAI_PYTHON:-...}` 리터럴이 경로로 넘어가 죽었고, 그걸
`../DeveloperAI/...` 상대경로로 되돌렸더니 이번엔 그게 안 풀려 또 죽었다. 그
사이 **경로 B 가 통째로 멈췄는데 아무 검사기도 잡지 못했다** —
`verify_contract.py` 는 서버를 직접 띄우므로 이 파일을 읽지도 않는다. 두 시도
모두 손으로 짠 변수였다는 게 공통점이다 — `CLAUDE_PROJECT_DIR` 는 Claude Code
자신이 채우므로 같은 증상이 나지 않는다.

그래서 이 파일의 경로는 **머신·클론 위치와 무관하게 동일하다.** 저장소를 새로
받아도 손댈 필요 없다. 확인은 두 가지다:

- 설정값이 옳은가 — `.mcp.json` 그대로 다섯 서버를 띄워 `tools/list` 를 받아본다.
  기대값은 `asset 9 / unity 13 / strategic 11 / qa 5 / git 7`.

**무거운 import 를 서버 lifespan 에 두지 않는다.** Claude Code 의 stdio 핸드셰이크
제한은 30초이고, 그 안에 `tools/list` 까지 끝나야 그 서버가 통째로 올라온다.
실제로 `SentenceTransformerEmbedder.available()` 이 존재 확인을 하려고
`import sentence_transformers` 를 하는 바람에 torch 가 딸려 와 **혼자 55.2초**를
먹었고, `strategic` 만 도구 목록에서 사라졌다 (2026-08-06 실측: DB 2.0초 +
이 호출 55.2초 = 58.4초). `importlib.util.find_spec` 으로 바꿔 3.2초가 됐다.

이 결함의 고약한 점은 **sentence-transformers 를 설치해야만 나타난다**는 것이다 —
없으면 ImportError 가 즉시 나서 빠르다. 즉 "임베딩을 켜면 경로 B 가 죽는" 모양이라
임베딩을 안 쓰는 사람에게는 영원히 안 보인다. `tests/test_embedder_availability.py`
가 "available() 이 import 하지 않는가"를 고정한다.
- Claude Code 가 실제로 물었는가 — 재시작 뒤 도구가 올라오는지 본다. 실패하면
  `%LOCALAPPDATA%\claude-cli-nodejs\Cache\<프로젝트>\mcp-logs-<서버>\` 의 최신
  `.jsonl` 에 이유가 있다. **앞의 검사만 통과해도 뒤가 깨질 수 있다.**

맨 `python` 은 안 된다 — 시스템 인터프리터에는 `mcp` 패키지가 없다.

## 자주 쓰는 명령

```bash
# 새 컴퓨터 환경 구성 (venv + 양쪽 의존성 + .env). 상세는 SETUP.md
python scripts/bootstrap.py
python scripts/bootstrap.py --check   # 설치 없이 현재 상태만 검사

# 오케스트레이터 테스트
cd Src/DeveloperAI && python -m pytest

# MCP 서버 테스트
cd Src/McpServers && python -m pytest

# MCP 계약 검증 (§03 명세와 실제 도구 목록 대조)
cd Src/McpServers && python verify_contract.py

# 품질 회귀 감지 (프롬프트를 바꿨으면 이걸 돌린다)
cd Src/McpServers && python run_evals.py

# 웹 대시보드
cd Src/Web && npm run dev
```

## 두 가지 검사 — 무엇을 재는지 다르다

`verify_contract.py` 는 **"계약을 지키나"**, `run_evals.py` 는 **"결과가 쓸 만한가"**
를 본다. 앞쪽만 통과해도 게임이 안 돌 수 있으므로 프롬프트나 생성 로직을 만졌으면
뒤쪽도 돌린다. eval 은 키·DB 가 없으면 **건너뛰고 종료 코드 0** 을 낸다 —
"자원 미설정"과 "품질 회귀"를 CI 가 구분할 수 있어야 하기 때문이다.

`evals/*.jsonl` 이 그 기준이고, 케이스를 늘리는 것이 곧 검사를 강화하는 것이다.

## 파이프라인 실행 기록 (`pipeline_traces`)

한 번 돌 때마다 `spec → 생성된 C# → 컴파일 결과 → QA 판정` 이 `pipeline_traces`
테이블에 쌓인다. 실행 중에만 존재하는 데이터라 나중에 복원할 수 없어서 미리
적어 둔다. RAG 예시 검색과 파인튜닝 데이터의 재료다.

`compile_ok=true AND qa_verdict='PASS'` 인 행이 검증된 (프롬프트, 코드) 쌍이고,
같은 `feature_id` 의 `attempt=1` 실패와 `attempt=2` 성공이 짝지어진 전후 예시다.
`Src/DeveloperAI/scripts/export_traces.py`가 이 둘을 JSONL 로 뽑아낸다
(`status`/`verified`/`pairs` 세 명령). 의미 검색 기반 RAG 예시 검색은 아직
없다 — 3군의 임베딩 인프라가 있어야 한다.

## 토큰 비용에 대한 주의

`claude -p` 를 프로세스로 띄울 때마다 Claude Code 자체 시스템 프롬프트
약 25,000 토큰이 매번 실린다. 스크립트 8개를 8번 호출로 나누면 오버헤드만
8배가 된다. **스킬은 항상 배치로 설계한다** — 한 번의 호출에서 여러 산출물을
만들고, 호출 횟수를 최소화한다.
