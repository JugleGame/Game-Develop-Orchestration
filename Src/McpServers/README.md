# McpServers

`DeveloperAI` 오케스트레이터가 호출하는 MCP 도구 서버 모음.
프로토콜 규격은 [`Doc/설계/03_MCP_Interface_Specification.md`](../../Doc/설계/03_MCP_Interface_Specification.md)(§03) 기준이다.

> **저장소 위치 주의**: [`Src/DeveloperAI/README.md`](../DeveloperAI/README.md)와
> `02_시스템_설계명세서` §8은 MCP 서버를 **별도 저장소**로 두는 것을 전제한다.
> 현재 이 디렉터리는 그 방침과 어긋나 있으므로, 서버를 여기에 둘지 분리할지 팀 합의가 필요하다.

## 1. 구현 현황

| 서버 | 포트 | 디렉터리 | 상태 |
|---|---|---|---|
| StrategicMcpServer | 9101 | `strategic/` | 구현됨 |
| UnityMcpServer | 9102 | `unity/` | 구현됨 (Unity Editor 어댑터) |
| QaMcpServer | 9103 | `qa/` | 구현됨 (LLM 판정) |
| AssetGenMcpServer | 9104 | `asset/` | 구현됨 (PixelLab, `PIXELLAB_API_KEY` 필수) |
| GitMcpServer | 9105 | `gitmcp/` | 구현됨 (로컬 bare 저장소) |

포트는 `910x` 체계를 쓴다. 오케스트레이터 기본값(`app/config/settings.py`)과
`verify_contract.py`가 이 번호를 전제하므로 서버를 추가·변경할 때 세 곳을 함께 맞춰야 한다.

해피패스만 흉내내는 목업이 필요하면
[`Src/DeveloperAI/scripts/mock_mcp_servers.py`](../DeveloperAI/scripts/mock_mcp_servers.py)를 쓴다.

`gitmcp/`가 `git/`이 아닌 이유: `tests/conftest.py`가 `Src/McpServers`를 `sys.path`에 넣기 때문에
`git`이라는 폴더는 설치된 GitPython(`import git`)을 프로젝트 전체에서 가려버린다.

## 2. 환경 구성

저장소 루트에서 한 번만 실행한다. 자세한 것은 [`SETUP.md`](../../SETUP.md).

```bash
python scripts/bootstrap.py
```

venv 는 **저장소 전체가 하나를 공유한다** — `Src/DeveloperAI/.venv`. `.mcp.json` 과
`.vscode/*.json` 이 그 경로를 이름으로 지목하기 때문이다. 이 디렉터리는 최상위 패키지가
여럿인 평면 구조라 `pip install -e .` 로 설치되지 않는다. `serve_all.py` 와 `.mcp.json` 이
하듯 `PYTHONPATH=.` 로 import 하고, `pyproject.toml` 의 의존성만 그 venv 에 설치한다.

## 3. 실행

각 서버는 §03 규격대로 Streamable HTTP 단일 엔드포인트(`/mcp`)를 노출한다.

```bash
python -m strategic.server
python -m unity.server
python -m qa.server
python -m asset.server
python -m gitmcp.server
```

`--transport stdio`를 주면 MCP Inspector나 에디터 통합용 stdio 모드로 뜬다.
`common/server.py`는 **loopback(127.0.0.1)에만 바인딩**한다 — §7.3에 따라 외부에 노출되는 것은
오케스트레이터 API뿐이다.

## 4. 환경변수

값은 저장소 루트의 **`.env` 한 장**에 모은다. `common/env.py` 가 `import common` 시점에
그 파일을 `os.environ` 에 적용하므로, 서버를 어느 경로로 띄우든(HTTP·stdio, 경로 A·B)
같은 값을 본다. 쉘에 export 된 값이 항상 이기고, 빈 값은 "설정 안 함"으로 취급되어
아래 기본값을 덮어쓰지 않는다. 전체 목록은 [`.env.example`](../../.env.example).

| 변수 | 서버 | 기본값 | 설명 |
|---|---|---|---|
| `MCP_HOST` / `MCP_PORT` | 공통 | `127.0.0.1` / 서버별 | 바인딩 주소 |
| `MCP_TRANSPORT` | 공통 | `streamable-http` | `stdio` / `sse` 선택 가능 |
| `LOG_LEVEL` | 공통 | `INFO` | |
| `ANTHROPIC_API_KEY` | QA / Strategic / Unity | (없음) | LLM 판정·생성에 필요. 없으면 완성된 산출물을 인자로 넘긴다 (§5) |
| `QA_MODEL` | QA | `claude-sonnet-5` | §02 §7.5의 "균형형". `app/utils/usage.py`에 단가가 있어 비용이 집계된다 |
| `QA_LLM_TIMEOUT_SECONDS` | QA | `150` | §03 §5의 QA 예산 180초보다 **작아야** 한다 |
| `QA_LLM_MAX_TOKENS` | QA | `4096` | 초과 시 잘림을 5000으로 보고 |
| `ASSET_ROOT` | Asset | `./asset_output` | 산출물 루트 |
| `ASSET_ART_STYLE` | Asset | `pixel art` | `artStyle` 인자가 없을 때의 대체값 |
| `ASSET_DEFAULT_GAME_ID` | Asset | `default` | `gameId` 인자가 없을 때의 대체값 (§6 주의) |
| `GIT_ROOT` | Git | `./git_output` | bare 저장소 + 작업 클론 루트 |
| `GIT_SOURCE_PATH` | Git | (없음) | 배포할 Unity 프로젝트 경로. `git_commit`의 `sourcePath` 인자가 없을 때의 대체값. 둘 다 없으면 빈 커밋이 된다 (§6-1) |

**자격증명은 환경변수로만 주입한다.** 코드·로그·커밋에 넣지 않는다. QA 서버는 API 키를 인자로
받지 않으며(SDK가 환경에서 직접 읽는다) 로그에는 모델명·경과시간·페이로드 길이만 남긴다.
`.env`는 커밋되지 않고(`.gitignore`), `.mcp.json`은 커밋되므로 값을 적지 않는다.

## 5. 두 가지 실행 경로와 키 없는 우회

[`CLAUDE.md`](../../CLAUDE.md)가 정의하는 두 경로 — 경로 A(LangGraph 오케스트레이터,
`ANTHROPIC_API_KEY` 필요)와 경로 B(Claude Code, 키 없음) — 는 **같은 MCP 서버**를 쓴다.

그래서 LLM을 쓰는 도구는 **완성된 산출물을 직접 받는 인자**를 갖는다. 그 인자를 채우면 서버는
모델을 호출하지 않고 검증만 한 뒤 같은 형태로 돌려준다.

| 도구 | 키 없이 쓰려면 |
|---|---|
| `establish_qa_policy` | `qaPolicy` |
| `verify_prototype_structure` / `compare_structure` | `result` |
| `run_functional_verification` | `verdict` |
| `generate_error_report` | `errorReport` |

넘긴 값도 생성된 값과 **똑같이 검증**되므로 이 경로가 스키마 보장에 구멍을 내지 않는다.
판정 규칙은 [`qa-review`](../../.claude/skills/qa-review/SKILL.md) 스킬에 있다.
UnityMcpServer의 `create_script(contents=...)`와 같은 장치다.

GitMcpServer는 LLM을 쓰지 않으므로 키가 필요 없다.

**LLM 비용 보고.** 모델을 부른 도구는 결과에 `usage`를 싣는다(`common/usage.py::usage_of`).
`app/utils/usage.py`가 이를 모아 `game_jobs.cost_usd`에 적재하므로, LLM을 쓰면서 `usage`를
싣지 않는 서버는 그 지출을 숨기는 셈이다. 키 없는 경로는 지출이 0이라 `usage`가 없다.

## 6. 알려진 제약 — 오케스트레이터 수정이 필요한 사항

전체 분석은 [`Doc/설계/05_계약_변경_제안서.md`](../../Doc/설계/05_계약_변경_제안서.md) 참고.

1. ~~**배포 저장소가 비어 있다**~~ — **서버 측에서 해결됨.** `git_commit`이 `sourcePath`
   (없으면 `GIT_SOURCE_PATH`)로 Unity 프로젝트를 작업 클론에 미러링한 뒤 커밋한다.
   Unity가 재생성하는 `Library/`·`Temp/`·`obj/`·`Logs/`·`*.csproj`는 제외하고, 발행 저장소에
   같은 규칙의 `.gitignore`를 넣는다. 오케스트레이터가 `sourcePath`를 보내주면 환경변수 없이도
   동작한다 — §03에 인자를 추가하는 제안은 제안서 §1 참고. 소스가 지정되지 않으면 종전처럼
   동작하고, 빈 커밋일 때 WARNING을 남긴다.
2. **`git_*` 5종에 `repoName`이 없다** — 세션이 호출마다 새로 열리므로 대상 저장소를 지목할 수
   없다. 옵션 인자로 받고, 없으면 "가장 최근 `git_init`된 저장소"로 폴백한다.
   `MAX_CONCURRENT_JOBS=1`에서만 안전하다.
3. **Asset에 `gameId`가 전달되지 않는다** — `asset_client.py`가 `featureId`/`prompt`만 보내므로
   모든 게임이 `"default"`로 합쳐진다. 팔레트·시드·매니페스트를 공유하고, Strategic AI가
   `f-1` 같은 feature id를 재사용하기 때문에 **뒤에 만든 게임이 앞의 게임 자산을 덮어쓴다.**
4. **`art_style`이 Asset까지 오지 않는다** — 기획서의 `art_style`을 받을 §03 경로가 없다.
   `artStyle` 옵션 인자를 추가해 두었으나 오케스트레이터가 아직 보내지 않는다.
5. **`establish_art_style`은 첫 생성보다 먼저 불러야 한다** — 스타일은 최초 `generate_*` 호출
   시점에 고정되며 이후 변경이 무시된다(의도된 잠금). 오케스트레이터가 이 도구를 호출하지 않으므로
   현재 모든 게임이 `ASSET_ART_STYLE` 기본값으로 잠긴다.
6. ~~**검수가 배포를 막지 않는다**~~ — **해결됨 (2026-07-29).** `AssetReview` 노드가
   `FunctionalTest` 통과 직후 `asset_review_summary`를 불러 Deployment 진입 조건으로 삼는다.
   단, `readyForBuild`(pending도 0이어야 함) 그대로는 아니다 — 자산은 기본이 `pending`이고
   자동으로 승인되지 않으므로 그 정의를 그대로 쓰면 모든 job이 항상 멈춘다. 실제로는
   `rejected == 0`만 본다: 사람이 `review_asset(approved=false)`로 명시적으로 반려한 게
   있을 때만 `HumanEscalation`으로 보내고, 리뷰를 아직 안 한 `pending` 자산은 그대로
   통과시킨다.

Asset 도구 7종 중 오케스트레이터가 호출하는 것은 `generate_2d_sprite`,
`establish_art_style`, `asset_review_summary` 세 개다.

## 7. 계약 검증

서버를 띄운 뒤 §03 계약(도구 이름, 인자 camelCase, `outputSchema`)을 대조한다.

```bash
python verify_contract.py            # 5개 서버 전부
python verify_contract.py qa git     # 일부만
```

불일치 시 non-zero로 종료하므로 CI 게이트로 쓸 수 있다.

## 8. 테스트

```bash
python -m pytest -q
```

포트나 외부 자격증명 없이 전부 통과한다. MCP 세션은 SDK 인메모리 전송으로 실제 프로토콜
(initialize → tools/call → isError)을 태우고, QA의 LLM 호출만 모킹한다.

주의: pytest는 root logger에 자체 핸들러를 미리 붙이므로 `common/server.py`의
`logging.basicConfig`가 무동작이 되고 `logger.info()`가 비활성된다. 그래서 로깅 경로는
`tests/test_logging_safety.py`가 서버 로거 레벨을 직접 올려 검증한다 —
`extra`에 `LogRecord` 예약 키(`created`, `message`, `filename`, `module`, `lineno`, `process`,
`args`, `name`, `levelname`, `asctime`)를 쓰면 `KeyError`로 도구 전체가 실패한다.

## 9. 서버 추가 시 규칙

`common/server.py`에 두 가지가 강제되어 있다.

- 도구 반환 타입은 `dict[str, Any]`로 표기한다. 맨 `dict`는 `outputSchema`를 만들지 않아
  `structuredContent`가 비고, 오케스트레이터의 텍스트 파싱 폴백에만 의존하게 된다.
  `@expects_dict_return`이 import 시점에 이를 잡는다.
- `0.0.0.0`이 아니라 loopback에 바인딩한다.

에러는 `common/errors.py`의 `tool_error(code, message)`로 발생시킨다. FastMCP가 예외를
`"Error executing tool X: ..."` 텍스트로 감싸기 때문에, 오케스트레이터가 §03 숫자 코드를 읽을 수
있는 형태를 이 함수가 만들어 준다. 코드는 **정수**여야 한다(문자열 `"1000"`은 3000으로 격하된다).

| 코드 | 의미 |
|---|---|
| 1000 | Validation Error |
| 2000 | Timeout |
| 3000 | MCP Error |
| 4000 | Unity Build Error |
| 5000 | Unknown |
