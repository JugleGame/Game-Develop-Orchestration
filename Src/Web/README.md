# DeveloperAI Console (Web Front-End)

DeveloperAI 오케스트레이터(`Src/DeveloperAI`)를 위한 React 프론트엔드.
`02_시스템_설계명세서` §1의 `Web Front-End (React/Next.js)`에 해당하며,
SSR이 필요 없는 단일 대시보드라 가벼운 Vite + React + TypeScript로 구성했다.
아키텍처/구조에 대한 상세 설명은 [ARCHITECTURE.md](./ARCHITECTURE.md) 참고.

## 1. 사전 요구사항

- Node.js 18 이상, npm
- 백엔드(`Src/DeveloperAI`)가 실행 중이어야 한다 (아래 "전체 실행 순서" 참고)

## 2. 설치

```bash
cd Src/Web
npm install
```

## 3. 환경변수

```bash
cp .env.example .env
```

| 변수 | 설명 | 기본값 |
|---|---|---|
| `VITE_API_BASE_URL` | DeveloperAI 백엔드 API 주소 | `http://localhost:8000` |

## 4. 실행

```bash
npm run dev
```

http://localhost:5173 에서 열린다.

## 5. 타입 체크

```bash
npm run lint   # tsc --noEmit
```

## 6. 전체 실행 순서 (바로 테스트하는 방법)

프론트엔드 혼자서는 아무것도 보여줄 게 없다 — 백엔드와, 백엔드가 호출하는 5개의
MCP 서버(Strategic/Unity/QA/Asset/Git)가 함께 떠 있어야 실제 파이프라인이 돈다.
Strategic/Unity/QA/Asset/GitMcpServer는 이 저장소 범위 밖의 별도 컴포넌트이므로,
로컬에서 전체 흐름을 눈으로 보고 싶다면 이 저장소에 포함된 **목(mock) MCP 서버**로
대신한다. 총 4개의 터미널이 필요하다.

**터미널 1 — 인프라 (Postgres/Redis)**

```bash
cd Src/DeveloperAI
docker compose up -d
```

**터미널 2 — 목 MCP 서버 (Strategic/Unity/QA/Asset/Git, 포트 9001~9005)**

```bash
cd Src/DeveloperAI
source .venv/Scripts/activate   # PowerShell: .venv\Scripts\Activate.ps1
python scripts/mock_mcp_servers.py
```

항상 성공하는 해피 패스만 흉내낸다 (컴파일 에러 없음, QA 항상 PASS). 자세한 내용은
[Src/DeveloperAI/scripts/mock_mcp_servers.py](../DeveloperAI/scripts/mock_mcp_servers.py) 참고.
실제 Strategic/Unity/QA/Asset/GitMcpServer가 준비되면 `.env`의 `*_MCP_URL`을
그쪽으로 바꾸기만 하면 된다.

**터미널 3 — DeveloperAI 백엔드**

```bash
cd Src/DeveloperAI
source .venv/Scripts/activate
uvicorn app.main:app --reload --port 8000
```

기본 CORS 설정(`CORS_ALLOW_ORIGINS`)이 `http://localhost:5173`을 허용하도록
되어 있어 별도 설정 없이 프론트엔드에서 바로 호출된다.

**터미널 4 — 프론트엔드**

```bash
cd Src/Web
npm run dev
```

http://localhost:5173 접속 후:

1. 왼쪽 "새 게임 만들기"에 프롬프트 입력 후 제출 (예: "더블 점프가 있는 플랫포머 게임을 만들어줘").
2. 파이프라인 단계가 `Planning`까지 진행되면 화면에 기획서 요약 + 기능 목록과 함께
   승인/재기획 버튼이 뜬다. "승인하고 개발 시작"을 누른다.
3. `CodeGen → AssetGen → BuildPrototype → CompileCheck → StructureCompare →
   FunctionalTest → Deployment` 단계가 실시간(SSE)으로 진행되는 것을 확인한다.
4. 완료되면 "배포 결과"에 Git 저장소 이름/커밋/태그가 표시된다.
5. "이 게임 수정하기"를 눌러 수정 프롬프트를 보내면, 같은 게임/같은 저장소로
   다시 파이프라인이 도는 것을 확인할 수 있다 (§app/graph/nodes/planning.py의
   `repo_name` 고정 로직).
6. 왼쪽 히스토리 목록에서 지금까지 만든 게임들을 확인할 수 있다.

## 7. 백엔드 테스트

프론트엔드가 호출하는 새 엔드포인트(`GET /games`, `POST /games/{id}/revise`)를
포함한 백엔드 전체 테스트는 `Src/DeveloperAI`에서:

```bash
cd Src/DeveloperAI
python -m pytest -q
```
