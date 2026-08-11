# DeveloperAI

AI Game Platform Backend — 오케스트레이터 (FastAPI + LangGraph + MCP Client).
직접 게임 코드를 생성하지 않으며, Strategic/Unity/QA/Asset/Git MCP 서버를 호출해 파이프라인을 지휘한다.

## 1. 사전 요구사항

- Python 3.12 (미설치 시 `pyproject.toml`의 `requires-python`은 3.11까지 허용)
- Docker Desktop (Postgres/Redis 로컬 실행용)

## 2. 인프라 기동 (Postgres + Redis)

```bash
cd Src/DeveloperAI
docker compose up -d
```

`docker-compose.yml`은 `.env`의 기본값과 맞춰져 있다: Postgres `localhost:5432` (`developer_ai`/`developer_ai`/`developer_ai`), Redis `localhost:6379`.

## 3. Python 환경 구성

저장소 루트에서 한 번만 실행한다. 오케스트레이터와 MCP 서버 다섯 대의 의존성이
`Src/DeveloperAI/.venv` 한 곳에 들어간다 — `.mcp.json`과 `.vscode/*.json`이 그 경로를
지목하므로 venv는 저장소 전체가 하나를 쓴다. 자세한 것은 [`SETUP.md`](../../SETUP.md).

```bash
python scripts/bootstrap.py
```

## 4. 환경변수

저장소 루트의 **`.env` 한 장**이 오케스트레이터와 MCP 서버를 함께 설정한다
(Git에는 커밋되지 않음, `.gitignore` 참고). 없으면 `bootstrap.py`가
[`.env.example`](../../.env.example)에서 만들어 준다. 아래 기본값은 그 파일과 같다.
쉘에 export된 값이 항상 이기고, 작업 디렉터리에 별도 `.env`를 두면 그쪽이 우선한다.

| 변수 | 설명 | 기본값 |
|---|---|---|
| `DATABASE_URL` | Job Store (Postgres, async) | `postgresql+asyncpg://developer_ai:developer_ai@localhost:5432/developer_ai` |
| `REDIS_URL` | 진행률 pub/sub | `redis://localhost:6379/0` |
| `STRATEGIC_MCP_URL` / `UNITY_MCP_URL` / `QA_MCP_URL` / `ASSET_MCP_URL` / `GIT_MCP_URL` | 각 MCP 서버의 **Streamable HTTP 엔드포인트**. 서버는 MCP(JSON-RPC 2.0) 서버여야 한다 (§03 MCP Interface Specification v2.0) | `http://localhost:9101~9105/mcp` |
| `*_MCP_TIMEOUT_SECONDS` | MCP 호출별 타임아웃 (Strategic 120 / Unity 300 / QA 180 / Asset 180 / Git 60) | 문서 §03 기준 |
| `MCP_MAX_RETRIES` | MCP 호출 실패 시 exponential backoff 재시도 횟수 | `3` |
| `DEVELOPMENT_QA_MAX_ITERATIONS` | Development↔QA 재시도 루프 최대 횟수. 초과 시 `HumanEscalation` | `5` |
| `PLANNING_MAX_ITERATIONS` | 기획 반려(ApprovalGate→Planning) 루프 최대 횟수. 초과 시 `HumanEscalation` | `5` |
| `MAX_CONCURRENT_JOBS` | 동시에 실행할 파이프라인 수. Unity Editor가 1개인 동안은 `1` 유지 (§7.1) | `1` |
| `API_KEY` | 모든 API 요청에 요구되는 `X-API-Key` 값 (SSE는 `?api_key=` 허용). 비우면 인증 비활성 — 로컬 데모 전용 | (없음) |
| `RATE_LIMIT_CREATE_PER_MINUTE` | `POST /games`·`/revise`의 IP당 분당 상한. `0`이면 비활성 | `10` |
| `CORS_ALLOW_ORIGINS` | 프론트엔드(`Src/Web`) Origin 허용 목록 (JSON 배열) | `["http://localhost:5173"]` |

StrategicMcpServer/UnityMcpServer/QaMcpServer/AssetGenMcpServer/GitMcpServer는 이 저장소의 범위 밖(별도 컴포넌트)이며,
**MCP(Model Context Protocol) 서버여야 한다** — 오케스트레이터는 공식 `mcp` SDK의 `ClientSession`으로
JSON-RPC 2.0 / Streamable HTTP 위에서 통신한다. 도구는 URL 경로가 아니라 `tools/call`의 `name`으로 지정된다.

로컬에서 아직 그 서버들이 없다면 [`scripts/mock_mcp_servers.py`](scripts/mock_mcp_servers.py)를 띄우면 된다
(`python scripts/mock_mcp_servers.py`). FastMCP 기반의 **실제 MCP 서버 5개**라서 프로덕션과 동일한 프로토콜로
동작하며, 항상 성공하는 해피 패스만 흉내내므로 `Src/Web` 프론트엔드로 전체 파이프라인이 도는 모습을 바로
확인할 수 있다. `npx @modelcontextprotocol/inspector`로 도구 목록을 직접 들여다볼 수도 있다.

`app/mcp/base_client.py`의 `BaseToolClient`는 `session_factory` 파라미터로 세션을 주입할 수 있어, 테스트는
SDK의 인메모리 전송으로 포트 없이 실제 프로토콜을 검증한다 — 예시는 [`tests/conftest.py`](tests/conftest.py)의
`mcp_session_factory`와 `tests/unit/test_tool_clients.py` 참고.

## 5. 애플리케이션 실행

```bash
uvicorn app.main:app --reload --port 8000
```

기동 시 `lifespan`에서 `init_models()`로 `game_jobs` 테이블을 자동 생성한다 (MVP 편의 기능; 운영 배포 시 Alembic 등 정식 마이그레이션으로 교체 권장).

Swagger UI: http://localhost:8000/docs

## 6. 테스트

```bash
python -m pytest -q
```

Postgres/Redis/포트 없이도 전부 통과한다: Repository/Service 단위 테스트는 SQLite in-memory, MCP 클라이언트 테스트와 End-to-end 테스트는 **인프로세스 FastMCP 서버 + SDK 인메모리 전송**으로 실제 MCP 프로토콜(initialize 핸드셰이크 → tools/call → isError)을 그대로 태워 Intake→Planning→ApprovalGate(승인 대기)→Development→QA→Deployment 전체 파이프라인을 검증한다.

## 7. API (§6)

| Method | Endpoint | 설명 |
|---|---|---|
| POST | `/games` | 게임 생성 요청 (`{"prompt": "..."}`) |
| GET | `/games` | 게임 히스토리 목록 (최근 수정 순, `?limit=`) |
| GET | `/games/{id}/status` | 상태 폴링 (기획서/기능 목록/마지막 에러 포함) |
| GET | `/games/{id}/stream` | SSE 진행률 스트림 |
| POST | `/games/{id}/approve` | 기획 승인/재요청 (`{"approved": true}`) |
| GET | `/games/{id}/artifact` | 배포 결과 조회 (Git 저장소 이름/커밋/태그) |
| POST | `/games/{id}/revise` | 완료된 게임을 새 프롬프트로 재실행 (`{"prompt": "..."}`), 기존 Git 저장소 재사용 |
| POST | `/games/{id}/cancel` | 진행 중 작업 취소 |

## 8. 터미널에서 게임 생성 프롬프트 실행 (curl)

앱이 8000번 포트에서 떠 있다고 가정한다 (`## 5. 애플리케이션 실행` 참고).

```bash
# 1) 게임 생성 프롬프트 제출 -> game_id 발급
GAME_ID=$(curl -s -X POST http://localhost:8000/games \
  -H "Content-Type: application/json" \
  -d '{"prompt": "더블 점프가 있는 플랫포머 게임을 만들어줘"}' | python -c "import sys,json;print(json.load(sys.stdin)['game_id'])")
echo "$GAME_ID"

# 2) 기획서(GameDesignDocument)가 나올 때까지 상태 폴링 (status == awaiting_approval)
curl -s http://localhost:8000/games/$GAME_ID/status

# 3) 기획 승인 -> UnityMcpServer(Unity Assistance)로 코드 생성이 시작됨
curl -s -X POST http://localhost:8000/games/$GAME_ID/approve \
  -H "Content-Type: application/json" \
  -d '{"approved": true}'

# 4) 진행 상황 실시간 확인 (SSE) 또는 status 재폴링, status == done 이 될 때까지 대기

# 5) QA 통과 후 자동 생성된 Git 저장소/커밋/태그 확인
curl -s http://localhost:8000/games/$GAME_ID/artifact
```

`artifact` 응답의 `repo_name`이 이번 게임을 위해 Deployment 노드가 만든(혹은 재사용한) Git 저장소 이름이다.
이름 규칙은 `{genre}-{prompt-slug}-{gameId}` (예: `platformer-double-jump-hero-3f9a1c2e`)이며,
`GitClient.git_init`이 멱등이라 같은 game_id로 다시 배포하면 새 저장소를 만들지 않고 기존 저장소를 그대로 재사용해 push한다 (§03 MCP Interface Specification 참고).
저장소 이름은 최초 생성 시(Planning 단계)에만 계산되고 이후 `POST /games/{id}/revise`로 다시 실행해도
그대로 유지된다 (`app/graph/nodes/planning.py`).

## 9. Web 프론트엔드

`Src/Web`에 이 API를 사용하는 React 대시보드가 있다. 게임 생성, 파이프라인/모듈별 진행 상태,
히스토리 조회, 기존 게임 수정을 한 화면에서 다룬다. 실행 방법과 구조는
[`Src/Web/README.md`](../Web/README.md), [`Src/Web/ARCHITECTURE.md`](../Web/ARCHITECTURE.md) 참고.
