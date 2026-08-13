# GitHub Issue workflow

## At a glance

An Issue is a **work contract**, a branch is a **private work desk**, and a
Pull Request (PR) is **evidence that the work is complete**.

```text
Create pipeline Issue #123
  -> Create branch 123-fix-issue-contract-check
  -> Change and test
  -> Write Closes #123 in the PR
  -> Automated checks and human review
  -> Merge, close the Issue, and delete the branch
```

## Everyday rules

1. Normal pipeline maintenance starts from one open Issue.
2. Read the full Issue. Focus on what may change (Scope), what must not change
   (Out of Scope), and the observable finish list (Acceptance Criteria).
3. Use `<number>-<type>-<short-description>` for branches, for example
   `123-fix-issue-contract-check`. The allowed types are `feat`, `fix`,
   `refactor`, `test`, `docs`, and `chore`.
4. Put exactly one matching reference such as `Closes #123` in the PR body.
5. Keep one Issue's work in one PR. Create a new Issue for a different problem.
6. Merge only after the automated tests and contract check are green.

## Small exception

The only no-Issue exception is a documentation typo. Use a
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
- The `Issue contract` check validates the branch name, Issue existence and open state, and matching `Closes #<number>` reference.
- The `Tests` check runs the existing Python tests and MCP contract check.

## GitHub settings a repository administrator must apply

Repository files cannot block a merge on their own. An administrator must set
the following in GitHub:

1. Go to **Settings > Rules > Rulesets > New branch ruleset**.
2. Name it, for example, `Protect main`, and target the `main` branch.
3. Enable **Require a pull request before merging**.
4. Enable **Require status checks to pass**, then require:
   - `Validate issue, branch, and PR`
   - `Python tests and contract check`
5. Enable **Require approvals** and normally require at least one approval.
6. Enable **Block force pushes**.
7. Enable **Automatically delete head branches**.

Do not block the GitHub Actions `GITHUB_TOKEN` from reading Issues. If an
organization policy restricts it, allow `issues: read`, `pull-requests: read`,
and `contents: read`. These workflows need neither write permission nor an
external secret.

## Notes

- Automation verifies that an Issue exists and is **open**. This repository has
  no agreed label for a more detailed ready-to-work state. Add a check later
  only after agreeing on a label such as `ready`.
- The check reruns when the PR description changes.
- GitHub automatically closes the linked Issue when the PR merges into the
  default branch. Removing `Closes #<number>` prevents that automatic close.
