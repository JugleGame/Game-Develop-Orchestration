# Unity Functional Test Contract Template

Copy this file into the feature spec or work record. If any `<required>` token remains, the
contract is incomplete and its QA status is `INCOMPLETE`. Follow
[`unity-functional-qa.md`](unity-functional-qa.md) for execution and status rules.

## Identity

- Contract ID: `<required: feature-id/ac-id>`
- Issue: `<required: #number>`
- Feature: `<required: feature name>`
- Acceptance Criterion: `<required: observable criterion>`
- Source revision: `<required: commit SHA or worktree identifier>`
- Owner: `<required: host agent or person>`

## Test selection

- Focal mode: `<required: EditMode | PlayMode>`
- Focal test names:
  - `<required: exact Unity test name>`
- Regression mode and names:
  - `<required: EditMode|PlayMode - exact Unity test name, or none with reason>`
- Additional gates:
  - `<required: inspect_project_layout, inspect_animator, screenshot, human review, or none>`

## Given

- Scene or prefab: `<required>`
- Initial state: `<required>`
- Test data or fixture: `<required>`
- Determinism controls (seed, clock, frame or physics step): `<required>`

## When

1. `<required: exact input or method call>`
2. `<required: count, order, and wait or frame limit>`

## Then / Test Oracle

| ID | Observable outcome | Expected value or state | Tolerance or deadline | Evidence field |
|---|---|---|---|---|
| T1 | `<required>` | `<required>` | `<required>` | `<required: assertion or result field>` |

Forbidden side effects:

- `<required: error, duplicate event, extra state transition, or none>`

## Execution record

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

## Decision

- QA status: `<required: PASS | PRODUCT_FAIL | TEST_FAIL | INFRA_ERROR | FLAKY | INCOMPLETE>`
- Evidence-based reason: `<required>`
- Retry count for the same failure: `<required: 0..3>`
- Next action: `<required>`

## Completion checklist

- [ ] Every `<required>` value is complete.
- [ ] Every focal requested name appears at least once in the actual results.
- [ ] The focal run executed at least one test and reported zero failures.
- [ ] PlayMode reported zero console errors.
- [ ] Impacted regression tests and additional gates ran.
- [ ] The final build ran last, after every preceding gate passed.
- [ ] Exactly one QA status was selected using the policy priority order.
