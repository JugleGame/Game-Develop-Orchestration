const ISSUE_BRANCH = /^(\d+)-(feat|fix|refactor|test|docs|chore)-[a-z0-9]+(?:-[a-z0-9]+)*$/;
const DOCS_BRANCH = /^docs\/[a-z0-9]+(?:-[a-z0-9]+)*$/;

function references(body, pattern) {
  return [...body.matchAll(pattern)].map((reference) => Number(reference[1]));
}

async function validateOpenWorkIssue({ github, owner, repo, issueNumber }) {
  try {
    const { data: issue } = await github.rest.issues.get({
      owner,
      repo,
      issue_number: issueNumber,
    });

    if (issue.pull_request) {
      return `#${issueNumber} is a Pull Request, not a work Issue.`;
    }
    if (issue.state !== "open") {
      return `#${issueNumber} is not open and cannot be used for work.`;
    }
  } catch (error) {
    if (error.status === 404) {
      return `The Issue referenced by the PR, #${issueNumber}, does not exist.`;
    }
    throw error;
  }

  return undefined;
}

async function validateFeaturePullRequest({ context, github, core, branch, body }) {
  if (DOCS_BRANCH.test(branch)) {
    if (!/^No-Issue-Reason:\s*Typo-only documentation change\s*$/mi.test(body)) {
      core.setFailed("A no-Issue documentation typo PR requires the exact No-Issue-Reason.");
      return;
    }

    const files = await github.paginate(github.rest.pulls.listFiles, {
      owner: context.repo.owner,
      repo: context.repo.repo,
      pull_number: context.issue.number,
      per_page: 100,
    });
    const invalid = files.map((file) => file.filename)
      .filter((path) => path !== "README.md" && !path.startsWith("docs/"));
    if (invalid.length) {
      core.setFailed(`A docs/ exception PR may change only README.md and docs/: ${invalid.join(", ")}`);
    }
    return;
  }

  const match = branch.match(ISSUE_BRANCH);
  if (!match) {
    core.setFailed("Feature PR branches must use <issue-number>-<type>-<short-description>.");
    return;
  }

  const issueNumber = Number(match[1]);
  const issueReferences = references(body, /\brefs?\s+#(\d+)\b/gi);
  if (issueReferences.length !== 1 || issueReferences[0] !== issueNumber) {
    core.setFailed(`The PR body must contain exactly one matching reference: Refs #${issueNumber}.`);
    return;
  }

  const error = await validateOpenWorkIssue({
    github,
    owner: context.repo.owner,
    repo: context.repo.repo,
    issueNumber,
  });
  if (error) {
    core.setFailed(error);
  }
}

async function validateIntegrationPullRequest({ context, github, core, body }) {
  const issueNumbers = [...new Set(references(body, /\bcloses\s+#(\d+)\b/gi))];
  if (!issueNumbers.length) {
    core.setFailed("A dev-to-main PR must contain at least one closing reference: Closes #<issue-number>.");
    return;
  }

  for (const issueNumber of issueNumbers) {
    const error = await validateOpenWorkIssue({
      github,
      owner: context.repo.owner,
      repo: context.repo.repo,
      issueNumber,
    });
    if (error) {
      core.setFailed(error);
    }
  }
}

async function validate({ context, github, core }) {
  const pullRequest = context.payload.pull_request;
  const branch = pullRequest.head.ref;
  const base = pullRequest.base.ref;
  const body = pullRequest.body || "";

  if (base === "dev") {
    await validateFeaturePullRequest({ context, github, core, branch, body });
    return;
  }

  if (base === "main") {
    if (branch !== "dev") {
      core.setFailed("Only the dev branch may open a Pull Request to main.");
      return;
    }
    await validateIntegrationPullRequest({ context, github, core, body });
    return;
  }

  core.setFailed("Pull Requests must target dev, or target main from dev.");
}

module.exports = { validate };
