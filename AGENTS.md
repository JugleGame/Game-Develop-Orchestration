# Codex Adapter

Read `README.md` first, then only the document required by the task:

- Structure: `docs/architecture.md`
- MCP tools or ownership: `docs/contracts.md`
- Setup or environment: `docs/operations.md`
- Open work: `docs/backlog.md`

Never add model calls inside MCP servers. Use native Git only within user-approved scope.
Treat `var/` as disposable output, never source.

## GitHub Issue workflow

Before starting normal development work, identify the related GitHub Issue and read it **in full**.
Pay particular attention to Objective, Scope, Out of Scope, Acceptance Criteria, and Test.

1. Do not start normal development work without a valid, open Issue.
2. Use `<issue-number>-<type>-<short-description>` for branch names.
   `type` is one of `feat`, `fix`, `refactor`, `test`, `docs`, or `chore`; the description uses lowercase letters and hyphens only.
3. One branch and PR normally address one Issue. Report any unrelated problem as a new Issue candidate.
4. Do not change files outside the Issue Scope or within its Out of Scope section.
5. Verify every Acceptance Criterion and run the required tests.
6. Include exactly `Closes #<issue-number>` in the PR body, plus the changes, reason, test result, and how the Acceptance Criteria were met.
7. The only no-Issue exception is a documentation typo. Use a `docs/<short-description>` branch and include
   `No-Issue-Reason: Typo-only documentation change` in the PR body.

Automation validates these links, but human review and GitHub branch-protection settings are also required.
Follow `docs/github-issue-workflow.md` for the repository configuration.
