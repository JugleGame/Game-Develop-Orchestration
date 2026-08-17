# Phase Runner 사용 안내

Phase Runner는 게임 개발 작업을 기획, Unity 구현, 선택적 2D·3D 에셋 생성, 최종
Unity 통합 단계로 나누어 실행하는 로컬 CLI입니다. 각 단계는 필요한 MCP 하나만 활성화한
새 `codex exec` 스레드에서 실행됩니다.

Phase Runner 자체는 AI가 아닙니다. 다음 단계를 선택하고 사람의 승인을 확인하며 실행 상태를
저장하는 결정론적 컨트롤러입니다. 단계 간에는 전체 채팅 대신 크기가 제한된 JSON 결과와
artifact 경로만 전달합니다.

> 현재 인터페이스는 CLI입니다. 일반 사용자용 GUI는
> [Issue #51](https://github.com/JugleGame/Game-Develop-Orchestration/issues/51)에서 별도로
> 개발합니다. GUI가 추가된 뒤에도 이 CLI는 자동화와 장애 복구용 기준 인터페이스로 유지됩니다.

## 준비

- Python 3.12 이상
- 설치 및 로그인이 완료된 Codex CLI
- Unity 단계 실행 시 Unity 6 Editor와 Unity MCP relay
- 사용하는 단계에 필요한 `.env` 값
  - Research: `RESEARCH_DSN`
  - 2D Asset: `PIXELLAB_API_KEY`
  - 3D Asset: `MESHY_API_KEY`, 필요 시 `BLENDER_PATH`

저장소 루트에서 환경을 준비합니다.

```powershell
python scripts/bootstrap.py
```

Windows에서는 Phase Runner가 자식 프로세스로 실행할 수 있는 standalone Codex CLI가
필요합니다. 보호된 `WindowsApps`의 데스크톱 앱 실행 파일만 발견되면 시작 전에 실패합니다.
CLI를 설치하고 로그인한 뒤 실제 실행 가능 여부를 확인합니다.

```powershell
npm install -g @openai/codex
codex --version
codex
python scripts/bootstrap.py --check
```

PATH에서 올바른 CLI를 자동 탐지할 수 없으면 명시적으로 지정할 수 있습니다. 지정한 명령이
실행 불가능한 경우 다른 PATH 후보로 우회하지 않습니다.

```powershell
$env:GDAI_CODEX_COMMAND = Join-Path (npm prefix -g) "codex.cmd"
```

Phase Runner 명령은 저장소 루트에서 실행해야 합니다. 이후 예시는 Windows PowerShell을
기준으로 합니다.

## 빠른 시작

### 1. 최초 요청 작성

예를 들어 `request.md`를 다음과 같이 준비합니다.

```markdown
2D 탑다운 액션 게임의 플레이 가능한 프로토타입을 만들어 주세요.
플레이어 이동, 근접 공격, 적 1종, 체력 UI가 필요합니다.
각 기능은 Unity 기능 QA 절차로 검증해 주세요.
```

### 2. run 생성과 기획 실행

```powershell
.venv\Scripts\python.exe -m phase_runner start --prompt-file request.md
```

`start`는 새 run을 만들고 `research` 프로필로 기획 단계만 실행합니다. 출력의 `runId`를
이후 명령에 사용합니다. 짧은 요청은 파일 대신 직접 전달할 수도 있습니다.

```powershell
.venv\Scripts\python.exe -m phase_runner start --prompt "2D 퍼즐 게임을 만들어 주세요."
```

### 3. 상태와 기획 결과 확인

```powershell
.venv\Scripts\python.exe -m phase_runner status <run-id>
Get-Content -Raw var\runs\<run-id>\phases\planning\result.json
```

기획 결과의 `visualDimension`, `assetsRequired`, `summary`, `handoff`, `artifactPaths`를
확인합니다. 승인 전에는 Unity 구현이 시작되지 않습니다.

### 4. 기획 승인 후 진행

```powershell
.venv\Scripts\python.exe -m phase_runner approve <run-id> planning
.venv\Scripts\python.exe -m phase_runner resume <run-id>
```

에셋이 필요하지 않으면 Unity 구현과 최종 통합까지 진행한 뒤 완료됩니다. 에셋이 필요하면
Unity 구현 후 `asset-generation` 승인 지점에서 멈춥니다.

### 5. 에셋 생성과 검수 승인

에셋 공급자 비용과 외부 변경을 확인한 뒤 생성 단계를 승인합니다.

```powershell
.venv\Scripts\python.exe -m phase_runner approve <run-id> asset-generation
.venv\Scripts\python.exe -m phase_runner resume <run-id>
```

`visualDimension`에 따라 `asset2d`, `asset3d` 또는 두 프로필이 순차 실행됩니다. 생성된
artifact를 직접 검수한 뒤 최종 Unity 통합을 승인합니다.

```powershell
.venv\Scripts\python.exe -m phase_runner approve <run-id> asset-review
.venv\Scripts\python.exe -m phase_runner resume <run-id>
```

최종 상태가 `completed`인지 확인합니다.

```powershell
.venv\Scripts\python.exe -m phase_runner status <run-id>
```

## 단계와 승인 흐름

```text
planning (research)
  └─ planning 승인
      └─ unity_implementation (unity)
          ├─ assetsRequired=false
          │   └─ unity_integration (unity) → completed
          └─ assetsRequired=true
              └─ asset-generation 승인
                  └─ asset2d_generation 및/또는 asset3d_generation
                      └─ asset-review 승인
                          └─ unity_integration (unity) → completed
```

| 승인 이름 | 확인할 내용 | 승인 후 실행 가능한 단계 |
|---|---|---|
| `planning` | 기획, 시각 차원, 기능 범위 | Unity 구현 |
| `asset-generation` | 공급자 비용, 외부 변경, 에셋 명세 | 2D·3D 에셋 생성 |
| `asset-review` | 생성 결과의 기술·시각 검수 | 최종 Unity 통합 |

승인하지 않으려면 현재 대기 중인 gate를 이유와 함께 거절합니다.

```powershell
.venv\Scripts\python.exe -m phase_runner reject <run-id> planning --reason "기획 수정 필요"
```

거절된 run은 terminal 상태입니다. 수정된 요청은 새 run으로 시작합니다.

## 상태 읽기

대표 run 상태는 다음과 같습니다.

| 상태 | 의미 | 다음 행동 |
|---|---|---|
| `ready` | 다음 단계를 실행할 수 있음 | `resume` |
| `running` | 외부 단계가 실행 중이거나 중단 흔적이 있음 | 프로세스를 확인하고 필요 시 명시적 `retry` |
| `awaiting-planning-approval` | 기획 검토 대기 | `approve ... planning` 또는 `reject` |
| `awaiting-asset-generation-approval` | 비용·외부 변경 승인 대기 | `approve ... asset-generation` 또는 `reject` |
| `awaiting-asset-review` | 생성 에셋 검수 대기 | `approve ... asset-review` 또는 `reject` |
| `failed` | 단계 실행 또는 결과 검증 실패 | 원인 확인 후 `retry` |
| `rejected` | 사용자가 승인 gate에서 거절 | 새 run 시작 |
| `completed` | 모든 단계 완료 | 결과와 QA 증거 확인 |

각 phase는 별도로 `pending`, `running`, `completed`, `failed` 상태와 시도 횟수, MCP
프로필, 새 Codex thread ID, 결과 경로, 오류를 기록합니다.

## 실패와 중단 복구

먼저 상태와 해당 단계의 `stderr.log`, `events.jsonl`을 확인합니다.

```powershell
.venv\Scripts\python.exe -m phase_runner status <run-id>
Get-Content -Raw var\runs\<run-id>\phases\<phase>\stderr.log
```

Codex 실행 실패 시 상태 오류에는 JSONL에서 추출한 bounded 진단과 로그 경로가 포함됩니다.
재시도는 이전 `result.json`과 로그를 제거한 뒤 시작하므로 실패한 새 실행이 오래된 결과를
완료 결과로 재사용하지 않습니다. Unity 단계는 기능 QA 결과가 `PASS`가 아니면 실패 상태로
중단됩니다.

실패 원인을 해결한 뒤 명시적으로 재시도합니다.

```powershell
.venv\Scripts\python.exe -m phase_runner retry <run-id>
.venv\Scripts\python.exe -m phase_runner resume <run-id>
```

프로세스나 PC가 단계 실행 중 종료되면 phase가 `running`으로 남을 수 있습니다. 이 경우에도
자동으로 다시 실행하지 않습니다. `retry`는 외부 변경이 반복될 가능성을 사용자가 확인했다는
명시적 신호입니다. 공급자 작업이나 Unity 변경이 이미 적용되었는지 먼저 확인하세요.

완료된 phase는 `resume`이나 프로세스 재시작으로 중복 실행되지 않습니다. 알 수 없는 상태,
잘못된 JSON 결과, 허용 크기를 넘은 인계 데이터는 안전하게 실패 처리됩니다.

## 저장 파일

모든 실행 상태는 Git에서 무시되는 `var/runs/<run-id>/` 아래에 저장됩니다.

```text
var/runs/<run-id>/
├─ state.json
├─ .lock
└─ phases/
   └─ <phase>/
      ├─ output-schema.json
      ├─ result.json
      ├─ events.jsonl
      └─ stderr.log
```

- `state.json`: run 상태, 승인, 시도 횟수, thread ID와 결과 경로
- `result.json`: 다음 단계에 전달되는 bounded 구조화 결과
- `events.jsonl`: Codex의 전체 JSONL lifecycle 및 도구 이벤트
- `stderr.log`: Codex 진행 로그와 실패 진단 정보
- `.lock`: 같은 run을 두 프로세스가 동시에 실행하지 못하게 하는 OS lock 파일

최초 프롬프트는 재개를 위해 `state.json`에 저장됩니다. 비밀키나 토큰을 프롬프트에 넣지
마세요. 전체 로그와 `var/` 결과는 소스가 아니며 커밋하지 않습니다.

## 안전 원칙

- `asset-generation`은 예상 비용과 외부 변경을 확인하기 전에는 승인하지 않습니다.
- `asset-review`는 결과 파일을 직접 검수하기 전에는 승인하지 않습니다.
- 같은 Unity 프로젝트를 수정하는 phase는 순차 실행합니다.
- Phase Runner 실행 중 `.mcp.json`과 `.codex/config.toml`은 현재 역할 프로필로 전환됩니다.
  최초 전환 시 기존 설정은 `.bak`으로 보존됩니다.
- 선택된 Codex MCP는 `required = true`입니다. 필요한 MCP가 시작되지 않으면 해당 phase도
  실패하며 MCP 없이 계속 진행하지 않습니다.
- `var/runs/`를 삭제하면 재개 기록을 잃습니다. 컨텍스트 비용을 줄이기 위한 목적으로
  `var/`를 삭제하지 않습니다.

## 명령 요약

```powershell
# 새 run과 planning 실행
.venv\Scripts\python.exe -m phase_runner start --prompt-file request.md

# 상태
.venv\Scripts\python.exe -m phase_runner status <run-id>

# 승인과 거절
.venv\Scripts\python.exe -m phase_runner approve <run-id> planning
.venv\Scripts\python.exe -m phase_runner approve <run-id> asset-generation
.venv\Scripts\python.exe -m phase_runner approve <run-id> asset-review
.venv\Scripts\python.exe -m phase_runner reject <run-id> <gate> --reason "거절 이유"

# 진행과 복구
.venv\Scripts\python.exe -m phase_runner resume <run-id>
.venv\Scripts\python.exe -m phase_runner retry <run-id>
```

도움말은 다음과 같이 확인합니다.

```powershell
.venv\Scripts\python.exe -m phase_runner --help
.venv\Scripts\python.exe -m phase_runner start --help
```

CLI의 `--root`로 저장소 루트를, `--runs-root`로 run 저장 위치를 지정할 수 있습니다.
전역 옵션은 subcommand 앞에 둡니다.

```powershell
.venv\Scripts\python.exe -m phase_runner --root C:\dev\Game-Develop-Orchestration status <run-id>
```

## 종료 코드

| 코드 | 의미 |
|---|---|
| `0` | 명령 성공 |
| `1` | 입력, 상태, 실행 또는 결과 검증 오류 |
| `2` | 승인 대기, 완료·거절 상태, 동시 실행 등 의도된 진행 차단 |

## 검증

기본 테스트는 fake executor를 사용하므로 실제 모델이나 유료 에셋 공급자를 호출하지 않습니다.

```powershell
.venv\Scripts\python.exe -m pytest Src/McpServers/tests/test_phase_runner.py
```

실제 Codex smoke test는 명시적으로 opt-in한 경우에만 실행합니다.

```powershell
$env:GDAI_RUN_CODEX_SMOKE = "1"
.venv\Scripts\python.exe -m pytest Src/McpServers/tests/test_phase_runner.py -k smoke
Remove-Item Env:GDAI_RUN_CODEX_SMOKE
```

전체 설치, 환경 변수, 수동 MCP 프로필 전환과 Unity 운영 절차는
[운영 문서](../docs/operations.md)를 참고하세요.
