# DeveloperAI Console — 프론트엔드 구조 기술 문서

Version 1.0 — `Src/Web`

---

## 1. 목적과 위치

`Src/Web`은 `Src/DeveloperAI` 오케스트레이터(§01_DeveloperAI_Design)의 Web
Front-End다. `02_시스템_설계명세서` §1 구성도의 `Web Front-End (React/Next.js)`에
해당한다. SSR/라우팅이 필요 없는 단일 대시보드형 도구이므로 Next.js 대신
**Vite + React 18 + TypeScript**를 택했다 — 빌드/개발 서버가 가볍고, 백엔드가
이미 REST + SSE로 상태를 밀어주기 때문에 서버 컴포넌트나 파일 기반 라우팅의
이점이 없다.

프론트엔드는 **직접 게임 코드를 생성하지 않는다.** 오직 DeveloperAI 백엔드
API(§6)를 호출하고, 그 응답/이벤트를 사람이 보기 좋은 형태로 그릴 뿐이다 —
`01_DeveloperAI_Design.md`가 오케스트레이터에 대해 갖는 것과 같은 제약을
프론트엔드도 지킨다.

## 2. 요구사항 → 구현 매핑

| # | 요구사항 | 구현 |
|---|---|---|
| 1 | 게임 생성 프롬프트 입력 | [`PromptForm`](src/components/PromptForm.tsx) (`POST /games`) |
| 2 | 현재 동작 중인 AI 모듈/단계별 상태 확인 | [`PipelineStatus`](src/components/PipelineStatus.tsx) + [`useGameDetail`](src/hooks/useGameDetail.ts) (SSE) |
| 3 | 게임 히스토리 조회 | [`GameHistoryList`](src/components/GameHistoryList.tsx) + [`useGameList`](src/hooks/useGameList.ts) (`GET /games`) |
| 4 | 기존 게임 수정 | [`ArtifactPanel`](src/components/ArtifactPanel.tsx)의 "이 게임 수정하기" (`POST /games/{id}/revise`) |
| + | 승인/재기획 게이트 | [`ApprovalPanel`](src/components/ApprovalPanel.tsx) (`POST /games/{id}/approve`) |
| + | 진행 중 작업 취소 | [`GameDetail`](src/components/GameDetail.tsx)의 취소 버튼 (`POST /games/{id}/cancel`) |
| + | QA/컴파일 에러 표시 | [`ErrorBanner`](src/components/ErrorBanner.tsx) (`last_error`) |
| + | 재시도/에스컬레이션 표시 | `PipelineStatus`의 `ErrorCorrection`/`HumanEscalation` 배너 |
| + | PixelLab 이미지 소진량 표시 | `PipelineStatus` (`images_generated`) |

3, 4는 백엔드에 없던 기능이라 이번에 함께 추가했다: `GET /games`(히스토리
목록)와 `POST /games/{id}/revise`(기존 게임에 새 프롬프트로 재실행, 같은 Git
저장소 재사용). 자세한 내용은 `Src/DeveloperAI/README.md` §7, §8 참고.

## 3. 디렉터리 구조

```
Src/Web/
├── index.html
├── vite.config.ts
├── tsconfig.json / tsconfig.node.json
├── .env.example            # VITE_API_BASE_URL
└── src/
    ├── main.tsx             # ReactDOM 진입점
    ├── App.tsx               # 레이아웃 + 최상위 상태(selectedGameId)
    ├── index.css             # 전역 스타일 (디자인 시스템 없이 순수 CSS)
    ├── vite-env.d.ts         # import.meta.env 타입
    ├── api/
    │   ├── types.ts          # 백엔드 Pydantic DTO 1:1 미러링
    │   └── client.ts         # fetch 래퍼 (ApiError, 엔드포인트별 함수)
    ├── hooks/
    │   ├── useGameList.ts    # GET /games 폴링 (히스토리)
    │   └── useGameDetail.ts  # GET status(1회) + SSE stream(실시간)
    ├── lib/
    │   └── stages.ts         # Stage → 담당 모듈 매핑, 스텝 상태 계산
    └── components/
        ├── PromptForm.tsx     # 프롬프트 입력 (신규 생성/수정 공용)
        ├── GameHistoryList.tsx
        ├── GameDetail.tsx     # 선택된 게임의 상세 화면 조립
        ├── PipelineStatus.tsx # 파이프라인 스텝퍼 + 실시간 로그
        ├── ApprovalPanel.tsx  # 승인 게이트
        ├── ArtifactPanel.tsx  # 배포 결과 + 수정 요청
        ├── ErrorBanner.tsx    # last_error 표시
        └── StatusBadge.tsx    # JobStatus 배지
```

## 4. 데이터 흐름

```
        ┌────────────┐   REST (JSON)    ┌─────────────────────┐
        │  App.tsx   │ ───────────────► │ DeveloperAI backend  │
        │            │ ◄─────────────── │ (Src/DeveloperAI)     │
        └─────┬──────┘                   └──────────┬───────────┘
              │ selectedGameId                       │ SSE (job-events)
              ▼                                       ▼
      ┌───────────────┐                     ┌──────────────────┐
      │ useGameList   │                     │ useGameDetail     │
      │ (poll 4s)     │                     │ (status + stream) │
      └───────┬───────┘                     └─────────┬─────────┘
              ▼                                        ▼
      GameHistoryList                            GameDetail
                                        (PipelineStatus / ApprovalPanel /
                                         ArtifactPanel / ErrorBanner)
```

- **`api/client.ts`**가 유일한 HTTP 경계다. 컴포넌트/훅은 이 모듈만 통해
  백엔드를 호출하며, 실패는 `ApiError`(상태 코드 + `detail`)로 통일해서 던진다.
- **REST vs SSE 책임 분리**: `GameStatusResponse`(REST)는 특정 시점의 전체
  스냅샷(기획서, 기능 목록, 마지막 에러 등)을 담고, `JobEvent`(SSE, §app/utils/events.py)는
  "지금 막 어떤 노드가 어떤 상태로 전이했는지"만 담는 얇은 신호다. `useGameDetail`은
  SSE 이벤트를 받을 때마다 그걸 그대로 그리는 대신 `GET /games/{id}/status`를
  다시 호출해 최신 스냅샷으로 갱신한다 — 이벤트 페이로드 스키마가 바뀌어도
  프론트엔드가 깨지지 않도록 하기 위한 선택이다.
- **폴링은 히스토리에만 사용**한다 (4초 간격, `useGameList`). 개별 게임의
  실시간 진행 상황은 폴링 대신 SSE로 받는다 — 여러 모듈이 순차적으로 도는
  파이프라인 특성상 이벤트 기반이 지연/부하 모두에서 유리하다.

## 5. 파이프라인 ↔ 모듈 매핑 (`lib/stages.ts`)

`app/graph/state.py`의 `Stage` 상수와 `app/graph/graph.py`의 엣지 구성을 그대로
옮겨, 각 Stage가 실제로 어느 MCP 서버(=AI 모듈)의 책임인지 프론트엔드에서
보여준다:

| Stage | 담당 모듈 |
|---|---|
| Intake | Orchestrator |
| Planning | StrategicMcpServer |
| ApprovalGate | 사용자 승인 |
| CodeGen | UnityMcpServer (Unity Assistance) |
| AssetGen | AssetGenMcpServer |
| BuildPrototype / CompileCheck | UnityMcpServer (Unity Assistance) |
| StructureCompare / FunctionalTest | QaMcpServer |
| Deployment | GitMcpServer |

`ErrorCorrection`(컴파일 실패 재시도 루프)과 `HumanEscalation`(5회 초과
에스컬레이션)은 선형 경로 밖의 사이드 상태라 스텝퍼에 별도 칸을 두지 않고,
`ErrorCorrection`은 `CodeGen` 칸을 "진행 중"으로 유지한 채 배너로 안내하고,
`HumanEscalation`은 상태 배지 + 에러 배너로 표시한다 (`stepState()` 참고).

## 6. 상태 관리 방식

전역 상태 관리 라이브러리는 쓰지 않는다. 화면이 하나뿐이고 서버가 진실의
원천(source of truth)이라 로컬 캐시를 동기화할 이유가 없기 때문이다.

- `App.tsx`가 유일한 "라우팅"에 준하는 상태(`selectedGameId`)를 들고 있다.
- 게임 목록/상세는 각각 전용 훅(`useGameList`, `useGameDetail`)이 자체
  `useState`로 들고 있다가 컴포넌트에 그대로 넘긴다 (prop drilling은
  2단계 이내로 유지됨: `App → GameDetail → 하위 패널`).
- `GameDetail`에 `key={selectedGameId}`를 줘서 게임을 바꿀 때마다
  컴포넌트를 통째로 새로 마운트한다 — 이전 게임의 SSE 연결/로그가 다음
  게임으로 새는 것을 막는 가장 단순한 방법이다.

## 7. 알려진 한계

- **인증**: 백엔드에 `API_KEY`가 설정된 경우 `VITE_API_KEY`를 같은 값으로
  맞춰야 한다 (`api/client.ts`가 `X-API-Key` 헤더로 전송, SSE는 헤더를 못
  붙이므로 `?api_key=` 쿼리로 전송). 둘 다 비워두면 인증 없이 동작한다(로컬
  데모 전용).
- **체크포인터는 Postgres 기반**(`AsyncPostgresSaver`): 백엔드가 재시작해도
  진행 중이던 LangGraph 실행 상태가 유지된다. 단, 재시작 시점에 실행 중이던
  asyncio 태스크 자체는 자동 재개되지 않으므로 해당 게임은 다음 이벤트까지
  멈춘 것처럼 보일 수 있다.
- **SSE 연결은 백엔드가 먼저 끊지 않는다** (`app/utils/events.py`가 Redis
  구독을 무기한 유지): 프론트엔드가 컴포넌트 언마운트 시 `EventSource.close()`로
  정리하지만, 브라우저 탭을 열어둔 채 오래 방치하면 유휴 연결이 계속 유지된다.
- **페이지네이션 없음**: `GET /games`는 `limit`(기본 50)만 지원한다. 게임이
  많아지면 커서 기반 페이지네이션이 필요하다.
- **알림/사운드 없음**: 승인 대기·완료·에스컬레이션 시 브라우저 알림 등은
  구현하지 않았다. 탭을 보고 있어야 상태 변화를 알 수 있다.
