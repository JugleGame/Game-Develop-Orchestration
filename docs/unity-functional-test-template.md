# Unity Functional Test Contract Template

이 파일을 feature spec 또는 작업 기록에 복사한다. `<required>`가 하나라도 남아 있으면
계약은 미완성이며 QA status는 `INCOMPLETE`다. 실행 규칙과 status 정의는
[`unity-functional-qa.md`](unity-functional-qa.md)를 따른다.

## 식별

- Contract ID: `<required: feature-id/ac-id>`
- Issue: `<required: #number>`
- Feature: `<required: feature name>`
- Acceptance Criterion: `<required: observable criterion>`
- Source revision: `<required: commit SHA or worktree identifier>`
- Owner: `<required: host agent or person>`

## 테스트 선택

- Focal mode: `<required: EditMode | PlayMode>`
- Focal test names:
  - `<required: exact Unity test name>`
- Regression mode and names:
  - `<required: EditMode|PlayMode - exact Unity test name, or none with reason>`
- Additional gates:
  - `<required: inspect_project_layout, inspect_animator, screenshot, human review, or none>`

## Given

- Scene/prefab: `<required>`
- Initial state: `<required>`
- Test data/fixture: `<required>`
- Determinism controls (seed, clock, frame/physics step): `<required>`

## When

1. `<required: exact input or method call>`
2. `<required: count, order, and wait/frame limit>`

## Then / Test Oracle

| ID | Observable outcome | Expected value/state | Tolerance or deadline | Evidence field |
|---|---|---|---|---|
| T1 | `<required>` | `<required>` | `<required>` | `<required: Assert/result field>` |

금지되는 부작용:

- `<required: error, duplicate event, extra state transition, or none>`

## 실행 기록

### Compile

- `get_compile_errors.errors`: `<required: [] or exact evidence>`

### Focal named tests

- Call: `run_named_tests(gameId=<required>, testNames=<required>, mode=<required>)`
- `status`: `<required>`
- `requested`: `<required>`
- `testCount`: `<required>`
- `failedCount`: `<required>`
- `results`: `<required: name/fullName, status, durationSeconds, message>`
- `failures`: `<required>`

### PlayMode console smoke

- Call: `run_playmode_test(gameId=<required>)`
- `passed`: `<required>`
- `errorCount`: `<required>`
- `errors`: `<required>`

### Regression and additional gates

- Regression results: `<required>`
- Additional gate results: `<required>`

### Final build

- Call: `build_project(gameId=<required>)`
- Target: `<required>`
- Artifact path: `<required>`
- `totalErrors`: `<required>`

## 판정

- QA status: `<required: PASS | PRODUCT_FAIL | TEST_FAIL | INFRA_ERROR | FLAKY | INCOMPLETE>`
- Evidence-based reason: `<required>`
- Retry count for the same failure: `<required: 0..3>`
- Next action: `<required>`

## 완료 checklist

- [ ] 모든 `<required>` 값을 채웠다.
- [ ] 모든 focal requested name이 실제 results에서 1회 이상 확인되었다.
- [ ] focal test 실행 개수는 1 이상이고 failure는 0이다.
- [ ] PlayMode console error는 0이다.
- [ ] 영향 범위 regression과 additional gate를 실행했다.
- [ ] final build는 앞선 gate가 통과한 뒤 마지막에 실행했다.
- [ ] QA status는 policy의 우선순위로 정확히 하나만 선택했다.
