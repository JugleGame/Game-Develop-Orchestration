const API_VERSION = "2026-03-10";

const FIELD_DEFAULTS = Object.freeze([
  { field_id: 45512566, value: "Medium" }, // JugleGame Priority
  { field_id: 45512569, value: "Medium" }, // JugleGame Effort
]);

const CONTRACTS = Object.freeze({
  bug: { type: "Bug", label: "bug" },
  docs: { type: "Task", label: "documentation" },
  feature: { type: "Feature", label: "feature" },
  pipeline: { type: "Task", label: "pipeline" },
});

function contractFromTitle(title = "") {
  return /^\[([a-z]+)\]\s+/.exec(title)?.[1] ?? null;
}

function metadataFor(issue) {
  const contract = CONTRACTS[contractFromTitle(issue.title)];
  const assignees = new Set((issue.assignees ?? []).map(({ login }) => login));
  const labels = new Set((issue.labels ?? []).map((label) => label.name ?? label));

  assignees.add(issue.user.login);
  if (contract?.label) labels.add(contract.label);

  return {
    assignees: [...assignees],
    labels: [...labels],
    type: issue.type?.name ?? contract?.type ?? "Task",
  };
}

async function applyDefaults({ context, github }) {
  const issue = context.payload.issue;
  if (!issue) return false;

  const request = {
    ...context.repo,
    issue_number: issue.number,
    headers: { "X-GitHub-Api-Version": API_VERSION },
  };
  const { data: currentFields } = await github.request(
    "GET /repos/{owner}/{repo}/issues/{issue_number}/issue-field-values",
    request,
  );
  const existingFieldIds = new Set(currentFields.map(({ issue_field_id }) => issue_field_id));
  const missingFields = FIELD_DEFAULTS.filter(({ field_id }) => !existingFieldIds.has(field_id));

  await github.request("PATCH /repos/{owner}/{repo}/issues/{issue_number}", {
    ...request,
    ...metadataFor(issue),
  });

  if (missingFields.length) {
    await github.request(
      "POST /repos/{owner}/{repo}/issues/{issue_number}/issue-field-values",
      { ...request, issue_field_values: missingFields },
    );
  }
  return true;
}

module.exports = { CONTRACTS, FIELD_DEFAULTS, applyDefaults, contractFromTitle, metadataFor };
