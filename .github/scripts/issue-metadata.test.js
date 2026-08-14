const test = require("node:test");
const assert = require("node:assert/strict");

const { FIELD_DEFAULTS, applyDefaults, contractFromTitle, metadataFor } = require("./issue-metadata");

function fixture(issue, currentFields = []) {
  const requests = [];
  return {
    requests,
    input: {
      context: {
        repo: { owner: "JugleGame", repo: "Game-Develop-Orchestration" },
        payload: { issue },
      },
      github: {
        request: async (route, request) => {
          requests.push({ route, request });
          return { data: route.startsWith("GET ") ? currentFields : {} };
        },
      },
    },
  };
}

test("every Issue receives an author, type, Priority, and Effort default", async () => {
  const scenario = fixture({
    number: 14,
    title: "제목 contract가 없는 일반 Issue",
    user: { login: "issue-author" },
    assignees: [],
    labels: [],
  });

  assert.equal(await applyDefaults(scenario.input), true);
  assert.deepEqual(scenario.requests.map(({ route }) => route), [
    "GET /repos/{owner}/{repo}/issues/{issue_number}/issue-field-values",
    "PATCH /repos/{owner}/{repo}/issues/{issue_number}",
    "POST /repos/{owner}/{repo}/issues/{issue_number}/issue-field-values",
  ]);
  assert.deepEqual(scenario.requests[1].request.assignees, ["issue-author"]);
  assert.deepEqual(scenario.requests[1].request.labels, []);
  assert.equal(scenario.requests[1].request.type, "Task");
  assert.deepEqual(scenario.requests[2].request.issue_field_values, FIELD_DEFAULTS);
});

test("existing metadata is preserved and only missing fields are added", async () => {
  const scenario = fixture(
    {
      number: 15,
      title: "[bug] 로그인 실패",
      user: { login: "reporter" },
      assignees: [{ login: "maintainer" }],
      labels: [{ name: "help wanted" }],
    },
    [{ issue_field_id: 45512566 }],
  );

  await applyDefaults(scenario.input);
  assert.deepEqual(scenario.requests[1].request.assignees, ["maintainer", "reporter"]);
  assert.deepEqual(scenario.requests[1].request.labels, ["help wanted", "bug"]);
  assert.equal(scenario.requests[1].request.type, "Bug");
  assert.deepEqual(scenario.requests[2].request.issue_field_values, [FIELD_DEFAULTS[1]]);
});

test("contract mapping never overrides an existing Issue type", () => {
  assert.equal(contractFromTitle("[feature] 새 기능"), "feature");
  assert.equal(contractFromTitle("대괄호가 없는 제목"), null);
  assert.equal(
    metadataFor({
      title: "[bug] 문서 오류",
      user: { login: "author" },
      type: { name: "Task" },
      assignees: [{ login: "author" }],
      labels: ["documentation"],
    }).type,
    "Task",
  );
});
