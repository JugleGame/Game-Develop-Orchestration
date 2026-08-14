const PIPELINE_PREFIX = "[pipeline] ";

const DEFAULTS = Object.freeze({
  labels: ["pipeline"],
  type: "Task",
  issue_field_values: [
    { field_id: 45512566, value: "Medium" }, // JugleGame Priority
    { field_id: 45512569, value: "Medium" }, // JugleGame Effort
  ],
});

function isPipelineIssue(title = "") {
  return title.startsWith(PIPELINE_PREFIX);
}

async function applyDefaults({ context, github }) {
  const issue = context.payload.issue;
  if (!issue || !isPipelineIssue(issue.title)) return false;

  await github.request("PATCH /repos/{owner}/{repo}/issues/{issue_number}", {
    ...context.repo,
    issue_number: issue.number,
    ...DEFAULTS,
    assignees: [issue.user.login],
    headers: { "X-GitHub-Api-Version": "2026-03-10" },
  });
  return true;
}

module.exports = { DEFAULTS, applyDefaults, isPipelineIssue };
