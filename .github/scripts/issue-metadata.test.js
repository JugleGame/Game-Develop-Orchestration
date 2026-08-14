const test = require("node:test");
const assert = require("node:assert/strict");

const { DEFAULTS, applyDefaults, isPipelineIssue } = require("./issue-metadata");

test("pipeline Issue receives the review metadata defaults", async () => {
  const requests = [];
  const applied = await applyDefaults({
    context: {
      repo: { owner: "JugleGame", repo: "Game-Develop-Orchestration" },
      payload: { issue: { number: 14, title: "[pipeline] metadata 자동 입력" } },
    },
    github: {
      request: async (route, input) => requests.push({ route, input }),
    },
  });

  assert.equal(applied, true);
  assert.equal(requests[0].route, "PATCH /repos/{owner}/{repo}/issues/{issue_number}");
  assert.deepEqual(requests[0].input.assignees, DEFAULTS.assignees);
  assert.deepEqual(requests[0].input.labels, DEFAULTS.labels);
  assert.equal(requests[0].input.type, "Task");
  assert.deepEqual(requests[0].input.issue_field_values, DEFAULTS.issue_field_values);
});

test("only the exact English pipeline contract type is handled", async () => {
  assert.equal(isPipelineIssue("[pipeline] 유효한 제목"), true);
  assert.equal(isPipelineIssue("[파이프라인] 번역된 제목"), false);
  assert.equal(isPipelineIssue("[pipeline]공백 없는 제목"), false);

  const applied = await applyDefaults({
    context: { payload: { issue: { number: 15, title: "[feature] 다른 Issue" } } },
    github: { request: async () => assert.fail("metadata request must not run") },
  });
  assert.equal(applied, false);
});
