"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {run, plan, apply, desiredLabels, MARKER, GUIDANCE, RESOLVED} = require("../../scripts/issue_automation.cjs");
const repo = {owner: "barneyonline", repo: "ha-homepod-sensor-link"};
const issue = overrides => ({number: 7, title: "[Bug]: Example", body: "", labels: [], assignees: [], ...overrides});

function harness(items = [], comments = []) {
  const calls = [];
  const endpoint = name => async args => { calls.push([name, args]); return {data: items}; };
  const issues = Object.fromEntries(["addAssignees", "getLabel", "createLabel", "addLabels", "removeLabel",
    "listComments", "updateComment", "createComment", "get", "listForRepo"].map(name => [name, endpoint(name)]));
  const github = {rest: {issues, pulls: Object.fromEntries(["get", "list", "requestReviewers"].map(name => [name, endpoint(name)]))},
    paginate: async () => comments};
  const logs = [], failures = [];
  const core = {info: value => logs.push(value), setFailed: value => failures.push(value)};
  return {github, core, calls, logs, failures, context: {repo, eventName: "workflow_dispatch", ref: "refs/heads/main", payload: {}}};
}

test("labels derive only from exact form choices and preserve foreign labels", () => {
  const item = issue({labels: [{name: "area/recovery"}, "keep-me"],
    body: "### Area\n\nMeasurements and monitoring\n\n### Sanitised diagnostic evidence\n\nProvided below after privacy review"});
  assert.deepEqual(desiredLabels(item), ["bug", "area/sensors"]);
  assert.deepEqual(plan("labels", item), [
    {type: "add-label", label: "bug"}, {type: "add-label", label: "area/sensors"},
    {type: "remove-label", label: "area/recovery"},
  ]);
  assert.deepEqual(desiredLabels(issue({title: "[Feature]: Test"})), ["enhancement"]);
  assert.equal(desiredLabels(issue({title: "Unstructured issue"})), null);
});

test("untrusted form content never becomes code, labels or log text", async () => {
  const payload = "untrusted-input-do-not-log";
  const item = issue({body: "### Area\n\n" + payload});
  assert.deepEqual(desiredLabels(item), ["bug", "status/needs-diagnostics"]);
  const h = harness([item]);
  await run({...h, mode: "labels"});
  assert.equal(h.failures.length, 0);
  assert.equal(h.calls.length, 1);
  assert.ok(h.logs.every(line => !line.includes(payload)));
  assert.throws(() => desiredLabels(issue({body: "x".repeat(65537)})));
});

test("manual previews perform reads only for all four modes", async () => {
  for (const mode of ["pr", "assign", "labels", "diagnostics"]) {
    const h = harness([issue({head: {ref: "contribution"}, user: {login: "contributor"}})]);
    await run({...h, mode});
    assert.equal(h.failures.length, 0);
    assert.equal(h.calls.length, 1);
    assert.ok(["list", "listForRepo"].includes(h.calls[0][0]));
    assert.match(h.logs[0], /Read-only preview/);
  }
});

test("wrong repository, branch, mode, preview number and missing event fail closed", async () => {
  for (const change of [
    {repo: {owner: "untrusted", repo: repo.repo}},
    {ref: "refs/heads/other"},
    {payload: {inputs: {item_number: "$(untrusted)"}}},
    {eventName: "issues", payload: {}},
  ]) {
    const h = harness();
    await run({...h, context: {...h.context, ...change}, mode: "labels"});
    assert.equal(h.failures.length, 1);
    assert.equal(h.calls.length, 0);
  }
  const h = harness();
  await run({...h, mode: "unsupported"});
  assert.equal(h.failures.length, 1);
  assert.throws(() => plan("labels", issue({number: -1})));
});

test("assignment and review preserve maintainers and avoid self/draft/managed review", () => {
  assert.deepEqual(plan("assign", issue({assignees: [{login: "barneyonline"}]})), []);
  assert.deepEqual(plan("assign", issue({assignees: [{login: "someone-else"}]})), [{type: "assign"}]);
  for (const properties of [{draft: true}, {user: {login: "barneyonline"}}, {head: {ref: "codex/issue-7"}},
    {requested_reviewers: [{login: "barneyonline"}]}]) {
    assert.deepEqual(plan("pr", issue({head: {ref: "normal"}, ...properties})), [{type: "assign"}]);
  }
  assert.deepEqual(plan("pr", issue({head: {ref: "normal"}, user: {login: "contributor"}})),
    [{type: "assign"}, {type: "review"}]);
  assert.deepEqual(plan("labels", issue({pull_request: {}})), []);
  assert.deepEqual(plan("assign", issue({labels: ["automation/hal"]})), []);
});

test("diagnostics follows form events without depending on token-generated label events", () => {
  assert.deepEqual(plan("diagnostics", issue(), "opened"), [{type: "guidance", needed: true}]);
  assert.deepEqual(plan("diagnostics", issue(), "unlabeled"), [{type: "guidance", needed: false}]);
  assert.deepEqual(plan("diagnostics", issue({labels: ["status/needs-diagnostics"]}), "labeled"),
    [{type: "guidance", needed: true}]);
  assert.deepEqual(plan("diagnostics", issue({title: "[Feature]: Test"})), []);
});

test("apply adds only controlled metadata and handles missing/racing labels", async () => {
  const h = harness();
  let exists = false;
  h.github.rest.issues.getLabel = async () => { if (!exists) throw {status: 404}; };
  h.github.rest.issues.createLabel = async () => { exists = true; throw {status: 422}; };
  h.github.rest.issues.removeLabel = async () => { throw {status: 404}; };
  await apply(h, issue(), [{type: "assign"}, {type: "review"}, {type: "add-label", label: "bug"},
    {type: "remove-label", label: "status/needs-diagnostics"}]);
  assert.deepEqual(h.calls.map(call => call[0]), ["addAssignees", "requestReviewers", "addLabels"]);
});

test("forged comment markers are preserved and only the bot's own guidance is reconciled", async () => {
  const forged = {id: 4, user: {login: "contributor", type: "User"}, body: GUIDANCE};
  let h = harness([], [forged]);
  await apply(h, issue(), [{type: "guidance", needed: true}]);
  assert.equal(h.calls[0][0], "createComment");
  assert.equal(h.calls[0][1].body, GUIDANCE);
  assert.match(GUIDANCE, /Never attach controller keys/);
  const own = {id: 5, user: {login: "github-actions[bot]", type: "Bot"}, body: GUIDANCE};
  h = harness([], [forged, own]);
  await apply(h, issue(), [{type: "guidance", needed: false}]);
  assert.deepEqual(h.calls.map(call => call[0]), ["updateComment"]);
  assert.equal(h.calls[0][1].comment_id, 5);
  assert.equal(h.calls[0][1].body, RESOLVED);
  assert.ok(RESOLVED.startsWith(MARKER));
  h = harness([], [own]);
  await apply(h, issue(), [{type: "guidance", needed: true}]);
  assert.equal(h.calls.length, 0);
  await apply(h, issue(), [{type: "guidance", needed: false}]);
  assert.ok(!h.calls.some(call => call[0] === "deleteComment"));
});

test("event reconciliation executes the plan; API errors reveal no raw payload", async () => {
  const h = harness();
  h.context = {...h.context, eventName: "issues", payload: {issue: issue(), action: "opened"}};
  await run({...h, mode: "assign"});
  assert.equal(h.calls[0][0], "addAssignees");
  h.github.rest.issues.addAssignees = async () => { throw new Error("private-api-details"); };
  await run({...h, mode: "assign"});
  assert.equal(h.failures.length, 1);
  assert.ok(!h.failures[0].includes("private-api-details"));
});
