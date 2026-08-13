const test = require("node:test");
const assert = require("node:assert/strict");

const { validate } = require("./issue-contract");

function fixture({ head, base, body, issues = {} }) {
  const failures = [];
  return {
    failures,
    input: {
      context: {
        repo: { owner: "example", repo: "project" },
        issue: { number: 99 },
        payload: { pull_request: { head: { ref: head }, base: { ref: base }, body } },
      },
      github: {
        rest: {
          issues: {
            get: async ({ issue_number: issueNumber }) => {
              if (!issues[issueNumber]) {
                const error = new Error("Not found");
                error.status = 404;
                throw error;
              }
              return { data: issues[issueNumber] };
            },
          },
          pulls: { listFiles: async () => ({ data: [] }) },
        },
        paginate: async () => [],
      },
      core: { setFailed: (message) => failures.push(message) },
    },
  };
}

test("feature PR to dev requires one matching Refs reference", async () => {
  const scenario = fixture({
    head: "4-feat-dev-main-issue-linking",
    base: "dev",
    body: "Refs #4",
    issues: { 4: { state: "open" } },
  });

  await validate(scenario.input);
  assert.deepEqual(scenario.failures, []);
});

test("feature PR to dev rejects a closing reference", async () => {
  const scenario = fixture({
    head: "4-feat-dev-main-issue-linking",
    base: "dev",
    body: "Closes #4",
    issues: { 4: { state: "open" } },
  });

  await validate(scenario.input);
  assert.match(scenario.failures[0], /Refs #4/);
});

test("dev-to-main PR accepts multiple open closing references", async () => {
  const scenario = fixture({
    head: "dev",
    base: "main",
    body: "Closes #4\nCloses #7",
    issues: { 4: { state: "open" }, 7: { state: "open" } },
  });

  await validate(scenario.input);
  assert.deepEqual(scenario.failures, []);
});

test("dev-to-main PR rejects missing or invalid closing references", async () => {
  const missing = fixture({ head: "dev", base: "main", body: "Refs #4" });
  await validate(missing.input);
  assert.match(missing.failures[0], /Closes/);

  const nonexistent = fixture({ head: "dev", base: "main", body: "Closes #404" });
  await validate(nonexistent.input);
  assert.match(nonexistent.failures[0], /does not exist/);

  const closed = fixture({
    head: "dev",
    base: "main",
    body: "Closes #4",
    issues: { 4: { state: "closed" } },
  });
  await validate(closed.input);
  assert.match(closed.failures[0], /not open/);

  const pullRequest = fixture({
    head: "dev",
    base: "main",
    body: "Closes #4",
    issues: { 4: { state: "open", pull_request: {} } },
  });
  await validate(pullRequest.input);
  assert.match(pullRequest.failures[0], /Pull Request/);
});

test("only dev may target main", async () => {
  const scenario = fixture({
    head: "4-feat-dev-main-issue-linking",
    base: "main",
    body: "Closes #4",
    issues: { 4: { state: "open" } },
  });

  await validate(scenario.input);
  assert.match(scenario.failures[0], /Only the dev branch/);
});
