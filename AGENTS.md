# Codex Adapter

Read `README.md` first, then only the document required by the task:

- Structure: `docs/architecture.md`
- MCP tools or ownership: `docs/contracts.md`
- Setup or environment: `docs/operations.md`
- Open work: `docs/backlog.md`
- Unity feature implementation or QA: `docs/unity-functional-qa.md`

Before implementing or completing any Unity gameplay feature, read and follow
`docs/unity-functional-qa.md`. Never treat `build_project` or `run_playmode_test` alone as
functional PASS.

Never add model calls inside MCP servers. Use native Git only within user-approved scope.
Treat `var/` as disposable output, never source.

## GitHub Issue workflow

This workflow governs source changes in this orchestration repository. Creating or operating an
external Unity project does not by itself require or create a GitHub Issue. Open one only when the
user explicitly requests Issue-backed tracking or the work changes this repository.

Before starting normal development work, identify the related GitHub Issue and read it **in full**.
Pay particular attention to Objective, Scope, Out of Scope, Acceptance Criteria, and Test.

For a new repository source-change request, do not edit files, create a branch, or start a runner
until an open Issue number is established in the current conversation. If no Issue exists, tell the
user to request `이 작업으로 Issue 생성해줘`. If an Issue exists but its number is missing or the
start request is ambiguous, tell the user to request `Issue #<number> 작업 시작`. After creating an
Issue, stop and provide that exact next request with the created number. Do not repeat this gate for
follow-up work after the current conversation has already established the open Issue.

1. Do not start normal development work without a valid, open Issue.
2. Write Issue and PR titles and explanatory prose in Korean. Keep the bracketed Issue contract type in exact English, such as `[pipeline]`; do not translate it. Preserve code identifiers, file paths, commands, and required GitHub keywords exactly.
3. Use `<issue-number>-<type>-<short-description>` for branch names.
   `type` is one of `feat`, `fix`, `refactor`, `test`, `docs`, or `chore`; the description uses lowercase letters and hyphens only.
4. Treat one independently verifiable outcome as one Issue. Keep its required implementation steps, tests, documentation, and configuration together.
5. Create a new Issue candidate when discovered work has a separate functional responsibility, external dependency, failure or completion state, or independent rollback reason.
6. Adjust Scope only for work required to complete the existing Objective, and update the Issue before proceeding. A new Objective requires a new Issue.
7. Do not change files within the Issue's Out of Scope section.
8. Verify every Acceptance Criterion and run the required tests.
9. For a feature PR to `dev`, include exactly `Refs #<issue-number>`. For a `dev`-to-`main` integration PR, include one or more `Closes #<issue-number>` references. Also record the changes, reason, test result, and Acceptance Criteria evidence.
10. The only no-Issue exception is a documentation typo. Use a `docs/<short-description>` branch and include
   `No-Issue-Reason: Typo-only documentation change` in the PR body.

Automation validates these links, but human review and GitHub branch-protection settings are also required.
Follow `docs/github-issue-workflow.md` for the repository configuration.
