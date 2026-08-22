# GitHub Issue workflow

## At a glance

An Issue is a **work contract**, a branch is a **private work desk**, and a
Pull Request (PR) is **evidence that the work is complete**.

This contract applies to source changes and Pull Requests in this orchestration repository. Creating
or operating a user's external Unity project is an operational action, not a repository work
contract: it has no orchestration-repository diff, PR, or independent rollback state. It therefore
must not create a GitHub Issue merely because a project was created. An Issue is appropriate only
when the user explicitly requests Issue-backed tracking or the work also changes this repository as
an independently verifiable outcome.

```text
Create pipeline Issue #123
  -> Create branch 123-fix-issue-contract-check
  -> Open a feature PR: branch -> dev, with Refs #123
  -> Change, test, review, and merge into dev
  -> Open an integration PR: dev -> main, with Closes #123
  -> Merge into main, close the Issue, and delete the branch
```

## Everyday rules

1. Normal pipeline maintenance that changes this repository starts from one open Issue.
2. Write Issue and PR titles and explanatory prose in Korean so reviewers
   share one working language. Keep the bracketed Issue contract type in its exact
   English form, for example `[pipeline]`; do not translate it. Also preserve
   code identifiers, file paths, commands, and required GitHub keywords.
3. Read the full Issue. Focus on what may change (Scope), what must not change
   (Out of Scope), and the observable finish list (Acceptance Criteria).
4. Use `<number>-<type>-<short-description>` for branches, for example
   `123-fix-issue-contract-check`. The allowed types are `feat`, `fix`,
   `refactor`, `test`, `docs`, and `chore`.
5. A feature PR must target `dev`, use the Issue branch name, and contain
   exactly one matching `Refs #123` reference. It does not close the Issue.
6. Only `dev` may target `main`. Its integration PR must contain one or more
   `Closes #123` references for the open work Issues included in the release.
   GitHub automatically closes those Issues when that PR merges into `main`.
7. Keep one Issue's work in one feature PR. Create a new Issue for a different
   problem.
8. Merge only after the automated tests and contract check are green.

## Agent entry guidance

An agent must establish an open Issue number in the current conversation before editing repository
source, creating a work branch, or starting an Issue runner. When the request is incomplete, stop
before repository mutation and return a copyable correction:

- If no Issue exists, ask the user to request `이 작업으로 Issue 생성해줘`.
- If an Issue exists but its number is missing or ambiguous, ask for
  `Issue #<number> 작업 시작`.
- After creating an Issue, provide `Issue #<created-number> 작업 시작` as the exact next request;
  Issue creation alone must not start implementation.

Do not infer consent from the checked-out branch. Once the current conversation has established the
open Issue, normal follow-up requests may continue without repeating the trigger.

## How to size an Issue

Use **one independently verifiable outcome** as the boundary of an Issue:

- Keep the implementation steps, tests, documentation, and configuration
  changes required to deliver that outcome in the same Issue, even when they
  happen in sequence.
- Create a new Issue when discovered work has a separate functional
  responsibility, external dependency, failure or completion state, or reason
  to roll it back independently.
- Adjust Scope only for work required to complete the existing Objective. Add
  a new Objective as a new Issue instead. Update the Issue before proceeding
  when a required Scope adjustment is discovered during implementation.

In short: group the procedure, split the responsibility. File count, line
count, and implementation order do not define the boundary.

## Small exception

For changes submitted to this repository, the only no-Issue exception is a documentation typo. Use a
`docs/<short-description>` branch and include this line in the PR body:

```text
No-Issue-Reason: Typo-only documentation change
```

Automation fails this exception when it changes anything outside `README.md`
and `docs/`. Code, configuration, and dependency changes require an Issue,
even when small.

## Applied automatically

- `.github/ISSUE_TEMPLATE/work-item.yml` requires a complete pipeline-maintenance Issue.
- `.github/pull_request_template.md` asks for the Issue link, change reason, tests, and acceptance evidence.
- The `Issue metadata defaults` workflow applies to every new Issue. It adds
  the author as an assignee, preserves existing metadata, fills a missing type,
  Priority, and Effort, and maps known English title contracts to existing
  labels and types: `bug`, `docs`, `feature`, and `pipeline`. Priority and
  Effort default to `Medium`. These are reviewable defaults; a human may change them.
  Projects, milestones, relationships, and development links are not invented
  when no real target or relationship exists.
- The `Issue contract` check validates the complete `feature -> dev -> main`
  path. It checks a feature branch, exactly one matching `Refs #<number>`, and
  an open work Issue for feature PRs. For `dev -> main`, it requires one or
  more valid `Closes #<number>` references and rejects every other main PR.
- The `Tests` check runs the existing Python tests and MCP contract check.
- The `Tests` check also runs automated tests for the Issue-contract rules.

## GitHub settings a repository administrator must apply

Repository files cannot block a merge on their own. An administrator must set
the following in GitHub:

1. Go to **Settings > Rules > Rulesets > New branch ruleset**.
2. Create one ruleset named, for example, `Protect dev`, targeting the `dev`
   branch, and another named `Protect main`, targeting the `main` branch.
3. Enable **Require a pull request before merging**.
4. Enable **Require status checks to pass**, then require:
   - `Validate issue, branch, and PR`
   - `Python tests and contract check`
5. Enable **Require approvals** and normally require at least one approval.
6. Enable **Block force pushes**.
7. Enable **Automatically delete head branches**.

The Issue-contract check only permits Issue branches and the documentation
exception to target `dev`. It only permits `dev` to target `main`; the main
ruleset must require that check so this rule cannot be bypassed.

Do not block the GitHub Actions `GITHUB_TOKEN` from reading Issues. If an
organization policy restricts it, allow `issues: read`, `pull-requests: read`,
and `contents: read`. These workflows need neither write permission nor an
external secret.

## Notes

- Automation verifies that an Issue exists and is **open**. This repository has
  no agreed label for a more detailed ready-to-work state. Add a check later
  only after agreeing on a label such as `ready`.
- The check reruns when the PR description changes.
- GitHub automatically closes an Issue only when a PR with `Closes #<number>`
  merges into the default branch. In this flow, put that text in the
  `dev -> main` integration PR, not in the feature PR to `dev`.
