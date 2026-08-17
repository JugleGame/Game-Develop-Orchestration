---
name: issue-work-runner
description: Start and operate this repository's approval-gated Issue Work Runner. Use for requests such as "Issue #N 작업 시작", "이 Issue 구현 시작", or "Issue 작업 자동화" that ask Codex Desktop to begin or resume GitHub Issue-backed maintenance in Game-Develop-Orchestration.
---

# Issue Work Runner

1. Read repository `README.md` and `AGENTS.md` first.
2. Resolve the repository from `git remote`, then fetch the requested GitHub Issue and all comments
   with the GitHub connector. Require the Issue to be open and read its complete title and body. Do
   not update the Issue.
3. Form an exact JSON snapshot with only `number`, `title`, `body`, `state`, and `url`. Store it at
   `var/issue-snapshots/<number>.json`; `var/` is disposable and must not be committed.
4. Derive a branch matching `<issue-number>-<type>-<short-description>`, where type is one of
   `feat`, `fix`, `refactor`, `test`, `docs`, or `chore`. Use the Issue objective and contract type;
   do not invent a second objective.
5. Run the following in the Desktop built-in terminal from the repository root:

   ```powershell
   .venv\Scripts\python.exe -m issue_runner start --snapshot-file var\issue-snapshots\<number>.json --branch <branch>
   ```

   Let the Runner reject a dirty worktree, a base other than `dev`, a closed or malformed Issue,
   a branch collision, or an invalid branch name. Never use checkout as an automatic trigger.
6. Show the run ID and analysis result to the user. Do not approve on the user's behalf. Continue
   only after explicit approval:

   ```powershell
   .venv\Scripts\python.exe -m issue_runner approve <run-id>
   .venv\Scripts\python.exe -m issue_runner resume <run-id>
   ```

7. On failure, inspect `status`, the phase `stderr.log`, `events.jsonl`, and `repository.diff` paths.
   Use `retry` only after the user accepts the risk of repeating that phase, then use `resume`.
8. Never commit, push, open or merge a PR, or close/update the Issue through this workflow. Obtain
   separate user approval and follow `docs/github-issue-workflow.md` for publication.
