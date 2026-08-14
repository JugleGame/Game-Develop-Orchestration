# Claude Code Adapter

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
2. Write the Issue title and explanatory prose in Korean. Preserve code identifiers, file paths, commands, and required GitHub keywords exactly.
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
