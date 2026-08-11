# 새 컴퓨터에서 시작하기

명령 세 줄이면 끝난다. 나머지는 왜 그런지에 대한 설명이다.

```bash
git clone <이 저장소> && cd Game-Developer-AI
python scripts/bootstrap.py
# .env 를 열어 자격증명을 채운다
```

`bootstrap.py` 는 표준 라이브러리만 쓰므로 아무 환경 없이 바로 돈다. 하는 일:

1. `Src/DeveloperAI/.venv` 에 가상환경을 만든다 — **저장소 전체가 쓰는 단 하나의 venv 다.**
2. 오케스트레이터와 MCP 서버의 의존성을 **둘 다** 그 안에 설치한다.
3. `.env.example` 을 `.env` 로 복사한다 (이미 있으면 건드리지 않는다).
4. 필요한 패키지가 실제로 import 되는지, 어떤 자격증명이 비어 있는지 보고한다.

이미 구성된 머신에서 상태만 보려면:

```bash
python scripts/bootstrap.py --check
```

## 사전 요구사항

| | 무엇 | 없으면 |
|---|---|---|
| 필수 | Python 3.12+ | 아무것도 안 된다 |
| 필수 | Git | `git` MCP 서버가 실패한다 |
| 선택 | Docker Desktop | 오케스트레이터(경로 A)의 Postgres·Redis 가 없다 |
| 선택 | Unity 6 Editor | `unity` 서버가 뜨긴 하지만 실제 작업 도구가 실패한다 |
| 선택 | Node 18+ | `Src/Web` 대시보드를 못 띄운다 |

경로 B(Claude Code)로만 파이프라인을 검증할 거라면 Python 과 Git 만 있으면 된다.

## 설정은 `.env` 한 장이다

루트의 `.env` 하나가 저장소 전체를 설정한다. 읽는 쪽은 둘이다.

| 읽는 쪽 | 어떻게 |
|---|---|
| 오케스트레이터 (`Src/DeveloperAI`) | `app/config/settings.py` 의 `env_file` |
| MCP 서버 5대 (`Src/McpServers`) | `common/env.py` — `import common` 시점에 적용 |

규칙 둘:

- **쉘에 export 된 값이 항상 이긴다.** `.env` 는 비어 있는 변수만 채운다.
  CI 나 일회성 실행(`GIT_ROOT=/tmp/x python -m gitmcp.server`)이 그대로 동작한다.
- **빈 값은 "설정 안 함" 이다.** `.env.example` 의 `UNITY_BUILD_OUTPUT=` 같은 줄은
  코드의 기본값을 덮어쓰지 않는다.

실제로 채워야 하는 건 셋뿐이고, 셋 다 안 해도 무언가는 돈다.

| 변수 | 없으면 | 언제 필요한가 |
|---|---|---|
| `ANTHROPIC_API_KEY` | 경로 A 의 LLM 호출이 실패한다 | 경로 B(Claude Code)는 **필요 없다** |
| `RESEARCH_DSN` | `strategic` 서버만 기동 직후 종료한다 | 기획 단계를 서버로 돌릴 때 |
| `UNITY_PROJECT_PATH` | `unity` 서버의 작업 도구가 실패한다 | 실제 Unity 프로젝트에 코드를 넣을 때 |

`.env` 는 커밋되지 않는다(`.gitignore`). 자격증명은 `.mcp.json` 에 적지 않는다 — 그 파일은 커밋된다.

## 의미 검색은 선택 설치다 (권장)

카드 검색은 **의미 임베딩 + 트라이그램**을 순위로 융합한다. 임베딩 모델이 없으면
서버는 죽지 않고 트라이그램만으로 강등되는데, 한국어에서 그건 의미 검색이 아니라
글자 모양 비교다. 반례 유사도 하한선도 임베딩 모드에서만 걸리므로, 없으면 주제와
무관한 실패 사례가 반례 자리를 채운다.

```bash
Src/DeveloperAI/.venv/Scripts/python -m pip install -e "Src/McpServers[semantic-search]"
```

torch 를 끌고 오는 무거운 의존성이라 `bootstrap.py` 의 기본 설치에는 없다. 강등
여부는 검색 결과의 `searchMode` 로 확인한다 (`vector+trigram` 이면 정상,
`trigram` 이면 강등).

## 에셋 생성에는 `PIXELLAB_API_KEY` 가 필요하다 (폴백 없음)

`asset` 서버의 생성 도구는 전부 PixelLab v2 를 부른다. 키가 없거나 호출이
실패하면 **에러를 반환한다** — 대신 그려주는 경로가 없다. 반환값의
`generatedBy` 는 항상 `"pixellab"` 이다.

키는 https://pixellab.ai 콘솔에서 발급해 `.env` 의 `PIXELLAB_API_KEY` 에 넣는다.

## venv 가 하나인 이유

`.mcp.json` 과 `.vscode/*.json` 이 `Src/DeveloperAI/.venv` 를 이름으로 지목한다.
그래서 그 경로에 있어야 하고, MCP 서버의 의존성도 **같은** venv 에 들어간다.
`Src/McpServers` 는 `asset/` `common/` `qa/` … 처럼 최상위 패키지가 여럿인 평면
구조라 `pip install -e .` 로는 설치되지 않는다 — `serve_all.py` 와 `.mcp.json` 이
하듯 `PYTHONPATH` 로 import 하고, 의존성만 설치한다. `bootstrap.py` 가 그렇게 한다.

## Windows 가 아닌 머신

한 줄만 추가하면 된다. `.mcp.json` 의 `command` 가 이 변수를 읽는다.

```bash
export GDAI_PYTHON='../DeveloperAI/.venv/bin/python'
```

**Claude Code 를 켜기 전에** 쉘에서 export 한다 — Claude Code 는 `.env` 를 읽지
않으므로 이 변수만은 `.env` 에 넣어도 소용이 없다. `bootstrap.py` 가 Windows 가
아닌 것을 감지하면 이 줄을 그대로 출력한다.

## 잘 됐는지 확인

```bash
python scripts/bootstrap.py --check          # 패키지·설정 상태
cd Src/McpServers && python -m pytest        # MCP 서버 테스트
cd Src/DeveloperAI && python -m pytest       # 오케스트레이터 테스트
```

파이프라인까지 확인하려면:

```bash
cd Src/McpServers && python serve_all.py       # 5대 기동 (Ctrl-C 로 종료)
cd Src/McpServers && python verify_contract.py # §03 계약과 실제 도구 목록 대조
```

`verify_contract.py` 는 서버가 떠 있어야 한다 — `serve_all.py` 가 먼저다.

경로 A 를 끝까지 돌릴 거라면 Postgres·Redis 도 필요하다.

```bash
cd Src/DeveloperAI && docker compose up -d
```

## 자주 나오는 실패

| 증상 | 원인 |
|---|---|
| `.mcp.json` 의 서버가 안 뜬다 | venv 가 없거나(`--check`), Windows 밖에서 `GDAI_PYTHON` 미설정 |
| `ModuleNotFoundError: mcp` | 시스템 파이썬으로 실행했다. venv 파이썬을 쓴다 |
| `strategic` 만 기동 직후 종료 | `RESEARCH_DSN` 이 비었다 — 정상 동작이다 |
| 모든 API 요청이 401 | 백엔드 `API_KEY` 와 `Src/Web/.env` 의 `VITE_API_KEY` 가 다르다 |
| `verify_contract.py` 가 connection refused | `serve_all.py` 를 안 띄웠다 |
| 검색 결과의 `searchMode` 가 `trigram` | `sentence-transformers` 미설치 — 위 "의미 검색은 선택 설치다" 참고 |
| 에셋 생성이 `errorCode 3000` 으로 실패한다 | `PIXELLAB_API_KEY` 가 없거나 호출이 실패했다 — 폴백 경로는 없다 |
| `run_evals.py` 가 전부 `SKIP` | 키·DSN 미설정. 자원 미설정은 실패가 아니라 건너뛴다(의도된 동작) |
