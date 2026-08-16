# Unity Functional QA Policy

## Purpose

This document is the authoritative QA policy for a host agent that implements Unity gameplay
features. A build proves that Unity can package the game. It does not prove that jumping, combat,
UI, or any other feature behaves correctly. Therefore, **a successful `build_project` call is not
a functional test pass, and a successful `run_playmode_test` call alone cannot prove a feature
works.**

In this document, **MUST** marks a gate that cannot be skipped and **MUST NOT** marks evidence that
cannot justify feature completion. Create each concrete test contract by copying
[`unity-functional-test-template.md`](unity-functional-test-template.md).

## Completion invariant

Every Unity feature MUST complete this sequence:

```text
Acceptance Criterion
  -> Given-When-Then contract
  -> confirm or create named tests
  -> implement the feature
  -> verify compilation
  -> run focal named tests
  -> run PlayMode console smoke
  -> run impacted regression tests
  -> run feature-specific gates such as layout inspection
  -> run the final build
  -> assign one QA status
```

Always apply these rules:

1. Immediately after implementing a feature, run its named tests before implementing another
   feature.
2. Every Acceptance Criterion MUST define at least one observable outcome and at least one test
   name that verifies it.
3. If a required test does not exist, create it first. The feature remains `INCOMPLETE` until the
   test has executed.
4. A feature cannot pass without evidence that `run_named_tests` completed at least one test.
5. `run_playmode_test` is a runtime console-error smoke test, not a functional test.
6. Do not call `build_project` until all focal and regression gates have passed.
7. After a failure, fix the cause and rerun **the same focal tests -> regression tests -> final
   build**. Never skip a failed gate and jump directly to a build.
8. Retry the same failure cause at most three times automatically. Then report the evidence and
   escalate to the user.

## Given-When-Then contract

Fix these four elements before writing the test code:

- **Given**: the initial scene, prefab, save data, seed, clock, and player or enemy state
- **When**: the exact input or call, count, order, and wait duration
- **Then**: externally observable values, tolerances, deadlines, and forbidden side effects
- **Evidence**: the test name and the exact result field that proves the outcome

Do not write untestable outcomes such as "works correctly," "has no problems," or "looks
natural." Use measurable states instead:

```text
Given: Player is on the Ground layer and velocity.y == 0.
When: Send one Jump input and advance one physics frame.
Then: velocity.y > 0 within 0.1 seconds, and a second Jump input in the air is ignored.
Evidence: Test_Player_JumpsOnceAndRejectsAirJump
```

Animation, rendering, and audio criteria may require screenshots, frame capture, audio capture, or
human review in addition to state assertions. Supplemental evidence never replaces a named test.

## Required execution procedure

### 1. Prepare the contract and tests

1. Read the Issue and the feature spec Acceptance Criteria.
2. Fill every `<required>` value in the template.
3. Choose the exact EditMode or PlayMode test names that directly verify the changed feature.
4. Locate those tests in the target Unity project.
5. If any test is missing, write a Unity Test Framework test and record its exact name in the
   contract and feature spec.
6. Record existing regression test names that the change can affect.

The existence of a test file does not satisfy step 5. Unity Test Runner must discover and execute
the test.

### 2. Implement the feature and verify compilation

Immediately after implementation, call `get_compile_errors(gameId)`. The returned `errors` array
MUST be empty before proceeding.

- A compile error in production source: `PRODUCT_FAIL`
- A compile error proven to originate from test source or its fixture: `TEST_FAIL`
- Insufficient evidence to identify the source: `INCOMPLETE`

### 3. Run the focal named tests

Call `run_named_tests` with the exact names recorded in the Acceptance Criterion. An unfiltered
suite run with empty `testNames` cannot replace focal feature evidence.

Verify **all** of these conditions:

| Field | Required condition |
|---|---|
| `status` | Exactly `completed` |
| `passed` | `true` |
| `testCount` | At least 1 |
| `failedCount` | 0 |
| `requested` | Contains every name recorded in the contract |
| `results` | Length equals `testCount`; every `status` is `Passed` |
| `failures` | Empty array |

Every requested name MUST appear at least once in `results[].name` or `results[].fullName`. A
zero-match filter or a missing requested result is `INCOMPLETE`.

- `status == timeout`: `INFRA_ERROR` because no trusted completion result exists
- Missing `PipelineTestReporter`, Unity bridge failure, or transport failure: `INFRA_ERROR`
- A completed, trustworthy assertion that fails because of production behavior: `PRODUCT_FAIL`
- A failure proven to come from an invalid fixture, setup, or oracle: `TEST_FAIL`

### 4. Run the PlayMode console smoke test

Call `run_playmode_test(gameId)` and verify all of these conditions:

- The call returned normally after PlayMode started and stopped.
- `passed == true`.
- `errorCount == 0`.
- `errors` is an empty array.

This tool has no named-test `status` or `testCount`. It also does not return ordinary logs or
warnings; it returns collected errors and exceptions. A successful result proves only that no
collected console error occurred during the smoke window. It does not prove feature behavior.

### 5. Run regression and structural gates

Run the impacted EditMode and PlayMode regression names from the contract with `run_named_tests`.
Validate the same fields as in step 3. If the feature changes scenes, prefabs, references,
Animators, or asset layout, also run the applicable contract gates such as
`inspect_project_layout` or `inspect_animator`.

### 6. Run the final build last

Call `build_project(gameId)` only after every preceding gate has passed. Record successful return,
artifact path, target, and `totalErrors == 0`. A successful build without the preceding functional
evidence is `INCOMPLETE`, not `PASS`.

## QA status decision

Assign exactly one status to a feature. If several conditions appear to apply, evaluate this table
from top to bottom and select the first matching status.

| Priority | Status | Decision condition | Required next action |
|---:|---|---|---|
| 1 | `INFRA_ERROR` | Bridge, transport, reporter, domain reload, timeout, or execution environment prevents a trustworthy result | Restore the environment and repeat the same call |
| 2 | `TEST_FAIL` | Evidence proves a defect in test source, fixture, setup, or oracle | Fix the test and rerun the same named test |
| 3 | `FLAKY` | The same test on the same revision, Given state, and input produces at least one pass and one failure | Stabilize the cause and pass the same test twice consecutively before regression |
| 4 | `PRODUCT_FAIL` | Trustworthy compile, assertion, console, layout, or build evidence identifies a production defect | Fix production and rerun the same named test |
| 5 | `INCOMPLETE` | A contract, test, requested result, execution count, required gate, or evidence is missing and no higher-priority failure is proven | Create or collect the missing item; do not declare completion |
| 6 | `PASS` | Every contracted named test, console smoke, regression, additional gate, and final build passed | Record Acceptance Criterion evidence |

Do not assign `FLAKY` merely because one retry passed. It requires proof that the revision and input
were unchanged and that both outcomes occurred. Do not promote it to `PASS` until the cause is
stabilized.

## Failure recovery order

Follow this loop exactly after a failure:

```text
preserve failure evidence
  -> classify one QA status
  -> change only the proven product, test, or infrastructure cause
  -> rerun the same Given-When-Then named tests
  -> rerun PlayMode console smoke
  -> rerun impacted regression tests
  -> rerun additional gates
  -> run the final build
  -> assign the final status
```

Do not change production and test code together based on a guess. Doing so removes the evidence of
which change affected the outcome. If an unchanged revision produces different outcomes, classify
it as `FLAKY` and stabilize it first.

## Required completion evidence

The host agent completion report and PR MUST include at least:

- Issue, feature or Acceptance Criterion, and source revision
- Given-When-Then contract path or body
- focal and regression test names and modes
- `run_named_tests` `status`, `testCount`, `failedCount`, and failure summary
- `run_playmode_test` `errorCount` and error summary
- additional gate results
- final build target, artifact path, and `totalErrors`
- final QA status and its evidence-based reason

The MCP server returns raw evidence and never declares final `PASS`. The host agent owns the final
decision under this policy.
