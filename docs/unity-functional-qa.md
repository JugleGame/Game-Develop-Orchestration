# Unity Functional QA Policy

## 목적

이 문서는 Unity 게임 기능을 구현하는 host agent가 따라야 하는 authoritative QA policy다.
빌드는 "게임 파일을 포장할 수 있는가"를 확인할 뿐, 점프·공격·UI 같은 기능이 맞게
동작하는지는 확인하지 않는다. 따라서 **`build_project` 성공은 기능 테스트 통과가 아니며,
`run_playmode_test` 성공만으로도 기능 통과를 선언할 수 없다.**

이 문서에서 **필수**는 생략할 수 없는 gate이고, **금지**는 해당 증거로 완료를 선언하면
안 된다는 뜻이다. 실제 테스트 계약은
[`unity-functional-test-template.md`](unity-functional-test-template.md)를 복사해 작성한다.

## 완료 불변식

Unity 기능 하나의 완료 순서는 다음과 같다.

```text
Acceptance Criterion
  -> Given-When-Then 계약
  -> 명명된 테스트 존재 확인 또는 작성
  -> 기능 구현
  -> compile 확인
  -> 변경 기능의 named test
  -> PlayMode console smoke
  -> 영향 범위 regression tests
  -> layout 등 기능별 추가 gate
  -> final build
  -> QA status 판정
```

다음 규칙을 항상 적용한다.

1. 기능 구현 직후, 다른 기능 구현으로 넘어가기 전에 해당 기능의 named test를 실행한다.
2. Acceptance Criterion마다 observable outcome과 이를 검증하는 test name이 하나 이상 있어야 한다.
3. 필요한 테스트가 없으면 먼저 테스트를 작성한다. 테스트가 실행되기 전까지 기능은
   `INCOMPLETE`다.
4. `run_named_tests`가 실제로 1개 이상의 테스트를 완료했다는 증거가 없으면 통과가 아니다.
5. `run_playmode_test`는 runtime console error를 찾는 smoke test일 뿐 functional test가 아니다.
6. 모든 기능 및 regression gate가 통과하기 전에는 `build_project`를 호출하지 않는다.
7. 실패 후에는 원인을 수정하고 **같은 기능 테스트 -> regression tests -> final build** 순서로
   다시 실행한다. 실패한 gate를 건너뛰어 build로 바로 이동하지 않는다.
8. 같은 원인에 대한 자동 재시도는 최대 3회다. 그 후에는 증거와 함께 사용자에게 알린다.

## Given-When-Then 계약

테스트 코드를 작성하기 전에 다음 네 항목을 고정한다.

- **Given**: scene, prefab, save data, seed, 시간, player/enemy 상태 등 시작 조건
- **When**: 입력 또는 호출, 횟수, 순서, 대기 시간
- **Then**: 외부에서 관찰할 값, 허용 오차, 제한 시간, 금지되는 부작용
- **Evidence**: test name과 결과에서 읽을 field

`Then`에 "정상 동작한다", "문제가 없다", "화면이 자연스럽다"처럼 기계적으로 판정할 수
없는 문장을 쓰지 않는다. 다음처럼 수치나 상태를 사용한다.

```text
Given: Player가 Ground layer 위에 있고 velocity.y == 0이다.
When: Jump 입력을 한 번 보내고 physics frame을 한 번 진행한다.
Then: 0.1초 안에 velocity.y > 0이고, 공중에서 두 번째 Jump 입력은 무시된다.
Evidence: Test_Player_JumpsOnceAndRejectsAirJump
```

애니메이션, rendering, audio처럼 상태 Assert만으로 충분하지 않은 기준은 screenshot, frame,
audio capture 또는 사람 검수를 추가할 수 있다. 이 추가 증거는 named test를 대체하지 않는다.

## 필수 실행 절차

### 1. 계약과 테스트를 준비한다

1. Issue와 feature spec의 Acceptance Criteria를 읽는다.
2. template의 모든 `<required>` 값을 채운다.
3. 변경 기능을 직접 검증할 EditMode 또는 PlayMode test name을 정한다.
4. 대상 Unity project에서 그 테스트를 찾는다.
5. 없으면 Unity Test Framework test를 작성하고 test name을 계약과 spec에 기록한다.
6. 변경이 영향을 줄 기존 regression test names를 기록한다.

테스트 파일이 존재한다는 사실만으로 5단계를 통과 처리하지 않는다. Unity Test Runner가 그
테스트를 실제로 발견하고 실행해야 한다.

### 2. 기능을 구현하고 compile을 확인한다

기능 구현 직후 `get_compile_errors(gameId)`를 호출한다. `errors`가 빈 배열이어야 다음
gate로 이동한다.

- production source의 compile error: `PRODUCT_FAIL`
- test source 또는 fixture의 결함임이 확인된 compile error: `TEST_FAIL`
- 원인을 특정할 증거가 부족함: `INCOMPLETE`

### 3. 변경 기능의 named test를 실행한다

Acceptance Criterion에 기록된 names를 명시해 `run_named_tests`를 호출한다. 빈
`testNames`로 전체 suite를 실행한 결과는 focal feature test의 대체 증거로 사용하지 않는다.

다음 조건을 **모두** 확인한다.

| Field | 필수 조건 |
|---|---|
| `status` | 정확히 `completed` |
| `passed` | `true` |
| `testCount` | 1 이상 |
| `failedCount` | 0 |
| `requested` | 계약에 기록한 names가 누락되지 않음 |
| `results` | 길이가 `testCount`와 같고 모든 `status`가 `Passed` |
| `failures` | 빈 배열 |

각 requested name은 `results[].name` 또는 `results[].fullName`에서 최소 한 번 확인되어야
한다. 필터가 0개와 일치하거나 일부 requested name의 결과가 없으면 `INCOMPLETE`다.

- `status == timeout`: 신뢰할 완료 결과가 없으므로 `INFRA_ERROR`
- `PipelineTestReporter` 부재, Unity bridge/transport 실패: `INFRA_ERROR`
- 완료된 신뢰 가능한 Assert가 production behavior 때문에 실패: `PRODUCT_FAIL`
- 잘못된 fixture, setup 또는 oracle 때문에 실패했음이 확인됨: `TEST_FAIL`

### 4. PlayMode console smoke를 실행한다

`run_playmode_test(gameId)`를 호출하고 다음을 모두 확인한다.

- 호출이 정상 반환되어 PlayMode 시작과 종료가 끝났다.
- `passed == true`
- `errorCount == 0`
- `errors`가 빈 배열이다.

이 도구에는 named test의 `status`나 `testCount`가 없다. 일반 log와 warning도 반환하지 않고
수집된 error/exception만 보고한다. 따라서 결과가 성공이어도 "실행 중 console error가
없었다"는 증거일 뿐, 기능 행동이 맞다는 증거로 사용하지 않는다.

### 5. regression과 구조 gate를 실행한다

계약에 기록한 영향 범위의 EditMode/PlayMode regression names를 `run_named_tests`로 실행하고
3단계와 같은 field를 검증한다. 기능이 scene, prefab, reference, Animator 또는 asset layout을
변경했다면 `inspect_project_layout`, `inspect_animator` 등 해당 계약의 추가 gate도 실행한다.

### 6. final build를 마지막에 실행한다

앞선 모든 gate가 통과한 뒤에만 `build_project(gameId)`를 호출한다. 호출 성공, artifact path,
target, `totalErrors == 0`을 기록한다. Build가 성공해도 앞선 기능 증거가 없으면 최종 상태는
`PASS`가 아니라 `INCOMPLETE`다.

## QA status 판정

한 기능에는 아래 status 중 정확히 하나만 부여한다. 여러 조건이 동시에 보이면 아래 순서를
위에서 아래로 적용해 첫 번째로 일치하는 status를 선택한다.

| 우선순위 | Status | 판정 조건 | 다음 행동 |
|---:|---|---|---|
| 1 | `INFRA_ERROR` | bridge, transport, reporter, domain reload, timeout 또는 실행 환경 때문에 신뢰 가능한 결과를 얻지 못함 | 환경을 복구하고 같은 호출을 다시 실행 |
| 2 | `TEST_FAIL` | test source, fixture, setup 또는 oracle 결함이 증거로 확인됨 | 테스트를 고치고 같은 named test 재실행 |
| 3 | `FLAKY` | 같은 revision, Given, input으로 같은 test를 반복했는데 최소 한 번 PASS와 한 번 FAIL이 관찰됨 | 원인을 안정화하고 같은 test를 2회 연속 통과시킨 뒤 regression 실행 |
| 4 | `PRODUCT_FAIL` | 신뢰 가능한 compile, Assert, console, layout 또는 build 증거가 production 결함을 가리킴 | production을 고치고 같은 named test 재실행 |
| 5 | `INCOMPLETE` | 계약, test, requested result, 실행 개수, 필수 gate 또는 증거가 빠졌고 위 실패 원인은 확인되지 않음 | 누락 항목을 만들거나 수집; 완료 선언 금지 |
| 6 | `PASS` | 계약의 모든 named tests, console smoke, regressions, 추가 gate와 final build가 모두 통과함 | Acceptance Criterion evidence 기록 |

`FLAKY`는 "한 번 재시도하니 통과했다"는 이유로 자동 부여하지 않는다. revision과 입력이
같았다는 증거 및 서로 다른 결과가 모두 있어야 한다. 원인이 확인되기 전에는 `PASS`로
승격하지 않는다.

## 실패 복구 순서

실패할 때 다음 loop를 그대로 따른다.

```text
실패 증거 보존
  -> QA status 분류
  -> product / test / infrastructure 중 확인된 원인만 수정
  -> 동일한 Given-When-Then named test 재실행
  -> PlayMode console smoke 재실행
  -> 영향 범위 regression tests 재실행
  -> 추가 gate 재실행
  -> final build
  -> 최종 status 판정
```

추측으로 product와 test를 동시에 바꾸지 않는다. 그러면 어느 수정이 결과를 바꿨는지 알 수
없다. 변경하지 않은 revision에서 결과가 뒤집히면 `FLAKY`로 분류해 먼저 안정화한다.

## 완료 보고에 포함할 증거

host agent의 완료 보고와 PR에는 최소한 다음을 남긴다.

- Issue, feature/Acceptance Criterion, source revision
- Given-When-Then 계약 경로 또는 본문
- focal 및 regression test names와 mode
- `run_named_tests`의 `status`, `testCount`, `failedCount`, failures 요약
- `run_playmode_test`의 `errorCount`와 errors 요약
- 추가 gate 결과
- final build의 target, artifact path, `totalErrors`
- 최종 QA status와 해당 판정 근거

MCP는 raw evidence만 반환하고 final status를 선언하지 않는다. 위 규칙에 따른 최종 판정은
host agent의 책임이다.
