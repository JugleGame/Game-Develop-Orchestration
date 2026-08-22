# Issue Work Runner 사용 안내

Issue Work Runner는 이 저장소의 GitHub Issue 유지보수를 분석, 구현, 검증, 검토로 나누어
실행하는 로컬 CLI입니다. 각 단계는 새 `codex exec` 스레드를 사용하고, 단계 사이에는 크기가
제한된 JSON 결과와 artifact/diff 경로만 전달합니다. Runner는 모델이 아니라 승인과 상태 전이를
관리하는 결정론적 컨트롤러입니다.

## 시작

새 Desktop 세션에서 `Issue #54 작업 시작`처럼 요청하면 저장소 로컬 `issue-work-runner`
Skill이 GitHub connector로 열린 Issue 전체를 조회하고 다음 형식의 snapshot을
`var/issue-snapshots/54.json`에 저장합니다.

Issue 번호 없이 저장소 변경을 요청하면 파일이나 branch를 변경하지 않고 먼저
`이 작업으로 Issue 생성해줘` 또는 `Issue #<number> 작업 시작` 형식을 안내합니다. Issue를 방금
생성한 경우에도 Runner를 곧바로 시작하지 않고 `Issue #<created-number> 작업 시작`을 다음 요청으로
제시합니다. 이미 현재 대화에서 열린 Issue가 확정된 후속 작업에는 같은 문구를 반복 요구하지
않습니다.

```json
{
  "number": 54,
  "title": "[pipeline] ...",
  "body": "Issue 본문 전체",
  "state": "open",
  "url": "https://github.com/.../issues/54"
}
```

Skill은 현재 브랜치가 clean `dev`인지 확인하고 Issue 계약에 맞는 작업 브랜치 이름을 정한 뒤
Desktop 내장 터미널에서 명령을 실행합니다. Windows에서는 `.venv\Scripts\python.exe`,
macOS/Linux에서는 `.venv/bin/python`을 `<venv-python>`으로 사용합니다.

```powershell
<venv-python> -m issue_runner start --snapshot-file var/issue-snapshots/54.json --branch 54-feat-issue-work-runner
```

`start`는 snapshot의 필수 필드와 열린 상태, base/work branch 계약, clean worktree, branch
충돌을 검증합니다. 조건이 맞을 때만 `dev`에서 새 브랜치를 만들고 analysis 한 단계만 실행합니다.
브랜치를 직접 전환하는 것만으로 Runner나 모델이 시작되지는 않습니다.

## 승인과 실행

```powershell
.venv\Scripts\python.exe -m issue_runner status <run-id> # Windows
.venv/bin/python -m issue_runner status <run-id>          # macOS/Linux
Get-Content -Raw var\issue-runs\<run-id>\phases\analysis\result.json
.venv\Scripts\python.exe -m issue_runner approve <run-id>
.venv\Scripts\python.exe -m issue_runner resume <run-id>
```

analysis 결과에는 Objective, Scope, Out of Scope, Acceptance Criteria, Test와 구현 계획이 모두
포함됩니다. 승인 전에는 implementation이 실행되지 않습니다. 승인하지 않으려면 다음처럼
종료합니다.

```powershell
.venv\Scripts\python.exe -m issue_runner reject <run-id> --reason "범위 조정 필요"
```

승인 후 `resume`은 implementation, verification, review를 각각 별도의 fresh thread로 실행합니다.
review는 Scope 밖 변경이 없고 모든 Acceptance Criteria와 Test에 정확히 대응하는 PASS 증거가
있어야 완료됩니다. Runner는 commit, push, PR, merge 또는 GitHub Issue 변경을 수행하지 않습니다.
analysis와 review는 read-only sandbox를 사용합니다. verification은 테스트의 ignored 출력 생성을
위해 workspace-write를 사용하지만 실행 전후 repository fingerprint가 달라지면 실패합니다.
모든 phase에서 HEAD 변경을 거부하며, 제출된 artifact 경로는 실제로 존재하는 저장소 상대 POSIX
경로여야 합니다.

## 복구

상태와 단계별 `stderr.log`, `events.jsonl`, `repository.diff`를 확인합니다. 실패나 실행 중 중단
흔적은 자동 재실행되지 않습니다.

```powershell
.venv\Scripts\python.exe -m issue_runner status <run-id>
.venv\Scripts\python.exe -m issue_runner retry <run-id>
.venv\Scripts\python.exe -m issue_runner resume <run-id>
```

`retry`는 실패했거나 `running`으로 남은 정확히 한 단계만 다시 실행합니다. 완료 단계는 재실행하지
않으며 같은 run의 동시 프로세스는 OS lock으로 차단합니다. 상태와 결과는
`var/issue-runs/<run-id>/`에 원자적으로 저장되고 Git에서 무시됩니다.

## 검증

기본 테스트는 fake executor와 임시 Git 저장소를 사용하며 실제 Codex나 GitHub write를 호출하지
않습니다.

```powershell
.venv\Scripts\python.exe -m pytest Src/McpServers/tests/test_issue_runner.py
```

실제 Codex smoke test는 명시적 opt-in입니다.

```powershell
$env:GDAI_RUN_CODEX_SMOKE = "1"
.venv\Scripts\python.exe -m pytest Src/McpServers/tests/test_issue_runner.py -k smoke
Remove-Item Env:GDAI_RUN_CODEX_SMOKE
```
