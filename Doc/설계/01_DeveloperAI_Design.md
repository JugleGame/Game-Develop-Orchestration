# Developer AI 상세 설계서

Version: v1.1 (2026-08-02 압축) · 경계와 책임만 정한다. 구성도·데이터 계약·API
전체는 [02](02_시스템_설계명세서.md), MCP 도구 계약은 [03](03_MCP_Interface_Specification.md) 이 권위다.

---

# 1. 목적

오케스트레이터. Web 요청 수신 · LangGraph 실행 · MCP 호출 · Job 상태 관리 ·
진행률 스트리밍 · QA 결과 처리 · Git 배포 요청.

**직접 게임 코드를 생성하지 않는다** — Unity/Asset MCP 에 위임한다.

---

# 2. 아키텍처 — 컴포넌트 경계

```
Web → FastAPI → LangGraph → Service → MCP Client
                                          ↓
        Strategic / Unity / QA / Asset / Git MCP  (별도 컴포넌트, 이 저장소 밖)
```

다섯 MCP 서버는 **동급의 별도 컴포넌트**이지 하위 모듈이 아니다 (02 §1).
서버 없이 돌려 보려면 `Src/DeveloperAI/scripts/mock_mcp_servers.py`.

---

# 3. 디렉터리 구조

`Src/DeveloperAI/app/` 아래 `api/` `graph/` `services/` `repository/` `models/`
`mcp/` `config/` `utils/` + `main.py`. 테스트는 `Src/DeveloperAI/tests/`.

---

# 4. Layer 책임

| Layer | 책임 |
|---|---|
| API | HTTP 요청 처리, Validation, Response 반환 |
| Service | Workflow 실행, Business Logic |
| Repository | PostgreSQL(Neon) 접근. **모든 SQL 은 `$1`·`$2` 자리표시자를 쓴다.** 접속 경로(5432 기본 / 443 HTTPS 폴백)는 Transport 계층이 정한다 — [05_NeonDB_Access_Specification.md](05_NeonDB_Access_Specification.md) |
| MCP Client | MCP Server 호출 |
| Graph | LangGraph State 관리 |

---

# 5. LangGraph Node

**목록을 여기 복사하지 않는다.** 노드와 엣지의 권위 있는 정의는
`Src/DeveloperAI/app/graph/graph.py` 이고, 이름은 `app/graph/state.py::Stage` 에
있다. 상태 흐름도는 02 §3.

각 Node 는 `GraphState` 만 읽고 쓰며, 그 밖의 상태를 갖지 않는다.

---

# 6. API

권위 목록은 02 §6. 핵심 넷은 `POST /games` · `GET /games/{id}` ·
`GET /games/{id}/stream` (SSE) · `POST /games/{id}/approve`.

---

# 7. MCP Client

`StrategicClient` `UnityClient` `QAClient` `AssetClient` `GitClient`.
모든 Client 는 **Timeout · Retry · Logging** 을 반드시 구현한다.

---

# 8. 개발 원칙

- API 는 Service 만 호출한다. Service 는 Repository 를 직접 호출하지 않는다.
- Graph Node 는 Business Logic 을 갖지 않는다.
- 모든 DTO 는 Pydantic BaseModel 을 쓴다.
