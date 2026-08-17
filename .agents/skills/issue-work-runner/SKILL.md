---
name: issue-work-runner
description: "Start, correct, and operate this repository's approval-gated Issue Work Runner. Use for explicit requests such as \"Issue #N 작업 시작\", \"이 Issue 구현 시작\", or \"Issue 작업 자동화\", and for underspecified repository-development requests that omit an Issue number or ask what to do after Issue creation in Game-Develop-Orchestration."
---

# Issue Work Runner

1. Read repository `README.md` and `AGENTS.md` first.
2. Resolve the Issue entry request before changing repository state:
   - If the user only asks to create an Issue, do not start the Runner. After creation, stop and
     provide the exact next request `Issue #<created-number> 작업 시작`.
   - If no Issue exists for a repository source change, do not edit files or create a branch. Tell
     the user to request `이 작업으로 Issue 생성해줘` or identify an existing Issue with
     `Issue #<number> 작업 시작`.
   - If an Issue exists but its number cannot be established from the current conversation, do not
     infer it from the current branch. Ask for `Issue #<number> 작업 시작`.
   - If the current conversation already established the open Issue and is continuing its work,
     proceed without making the user repeat the trigger.
3. Resolve the repository from `git remote`, then fetch the requested GitHub Issue and all comments
   with the GitHub connector. Require the Issue to be open and read its complete title and body. Do
   not update the Issue.
4. Form an exact JSON snapshot with only `number`, `title`, `body`, `state`, and `url`. Store it at
   `var/issue-snapshots/<number>.json`; `var/` is disposable and must not be committed.
5. Derive a branch matching `<issue-number>-<type>-<short-description>`, where type is one of
   `feat`, `fix`, `refactor`, `test`, `docs`, or `chore`. Use the Issue objective and contract type;
   do not invent a second objective.
6. Select the repository virtual-environment Python for the current OS: use
   `.venv\Scripts\python.exe` on Windows or `.venv/bin/python` on macOS/Linux. Run the following
   equivalent command in the Desktop built-in terminal from the repository root:

   ```powershell
   <venv-python> -m issue_runner start --snapshot-file var/issue-snapshots/<number>.json --branch <branch>
   ```

   Let the Runner reject a dirty worktree, a base other than `dev`, a closed or malformed Issue,
   a branch collision, or an invalid branch name. Never use checkout as an automatic trigger.
7. Show the run ID and analysis result to the user. Do not approve on the user's behalf. Continue
   only after explicit approval:

   ```powershell
   <venv-python> -m issue_runner approve <run-id>
   <venv-python> -m issue_runner resume <run-id>
   ```

8. On failure, inspect `status`, the phase `stderr.log`, `events.jsonl`, and `repository.diff` paths.
   Use `retry` only after the user accepts the risk of repeating that phase, then use `resume`.
9. Never commit, push, open or merge a PR, or close/update the Issue through this workflow. Obtain
   separate user approval and follow `docs/github-issue-workflow.md` for publication.
