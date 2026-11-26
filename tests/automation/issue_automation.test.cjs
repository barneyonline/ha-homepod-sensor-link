"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {run, plan, apply, desiredLabels, MARKER, GUIDANCE, RESOLVED} = require("../../scripts/issue_automation.cjs");
const repo = {owner: "barneyonline", repo: "ha-homepod-sensor-link"};
const issue = overrides => ({number: 7, title: "[Bug]: Example", body: "", labels: [], assignees: [], ...overrides});
const bot = {login: "github-actions[bot]", type: "Bot"};
const provided = "### Sanitised diagnostic evidence\n\nProvided below after privacy review";
const reproduced = "### Reproduction and expected/actual behaviour\n\nExample reproduction\n\n";

function harness(items = [], comments = []) {
  const calls = [];
  const endpoint = name => async args => { calls.push([name, args]); return {data: items}; };
  const issues = Object.fromEntries(["addAssignees", "getLabel", "createLabel", "addLabels", "removeLabel",
    "listComments", "listEvents", "updateComment", "createComment", "get", "listForRepo"].map(name => [name, endpoint(name)]));
  const github = {rest: {issues, pulls: Object.fromEntries(["get", "list", "requestReviewers"].map(name => [name, endpoint(name)]))},
    paginate: async () => comments};
  const logs = [], failures = [];
  const core = {info: value => logs.push(value), setFailed: value => failures.push(value)};
  return {github, core, calls, logs, failures, context: {repo, eventName: "workflow_dispatch", ref: "refs/heads/main", payload: {}}};
}

test("labels derive only from exact form choices and preserve foreign labels", () => {
  const item = issue({labels: [{name: "area/recovery"}, "keep-me"],
    body: `${reproduced}### Area\n\nMeasurements and monitoring\n\n${provided}`});
  assert.deepEqual(desiredLabels(item), ["bug", "area/sensors"]);
  assert.deepEqual(plan("labels", item), [
    {type: "add-label", label: "bug"}, {type: "add-label", label: "area/sensors"},
    {type: "remove-label", label: "area/recovery"},
  ]);
  assert.deepEqual(desiredLabels(issue({title: "[Feature]: Test"})), ["enhancement"]);
  assert.equal(desiredLabels(issue({title: "Unstructured issue"})), null);
});

test("documentation and support issues get type and topic labels", () => {
  const body = "### Area\n\nRecovery and lifecycle";
  assert.deepEqual(desiredLabels(issue({title: "[Docs]: Example", body})), ["documentation", "area/docs"]);
  const support = issue({title: "[Support]: Example", body});
  assert.deepEqual(desiredLabels(support), ["question", "area/recovery"]);
  assert.deepEqual(desiredLabels({...support, body: "### Area\n\nOther or not sure"}), ["question"]);
  assert.deepEqual(plan("diagnostics", support, "opened"), []);
});

test("untrusted form content never becomes code, labels or log text", async () => {
  const payload = "untrusted-input-do-not-log";
  const item = issue({body: "### Area\n\n" + payload});
  assert.deepEqual(desiredLabels(item), ["bug", "status/needs-diagnostics"]);
  assert.deepEqual(desiredLabels(issue({body: "### Area\n\nconstructor"})), ["bug", "status/needs-diagnostics"]);
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
  const status = "status/needs-diagnostics";
  assert.deepEqual(plan("diagnostics", issue(), "opened"), [{type: "guidance", needed: true}]);
  assert.deepEqual(plan("diagnostics", issue(), "unlabeled", status), [{type: "guidance", needed: false}]);
  assert.deepEqual(plan("diagnostics", issue({labels: [status]}), "labeled", status),
    [{type: "guidance", needed: true}]);
  assert.deepEqual(plan("diagnostics", issue(), "labeled", "keep-me"), []);
  assert.deepEqual(plan("diagnostics", issue({title: "[Feature]: Test"})), []);
  const unclassified = issue({title: "Unstructured issue", comments: 1, labels: ["bug", status]});
  assert.deepEqual(plan("labels", unclassified, "edited"), []);
  assert.deepEqual(plan("diagnostics", unclassified, "edited"), []);
  assert.deepEqual(plan("diagnostics", {...unclassified, labels: ["bug"]}, "unlabeled", status),
    [{type: "guidance", needed: false}]);
});

test("current bug forms need diagnostic details and previous forms keep their evidence choice", () => {
  assert.deepEqual(desiredLabels(issue({body: reproduced + provided})), ["bug"]);
  assert.deepEqual(desiredLabels(issue({body: provided})), ["bug", "status/needs-diagnostics"]);
  const missing = ["", "\n\n_No response_", "\n\n```text\n```", "\n\n```text\n\n```", "\n\n```text\n_No response_\n```"];
  for (const details of missing) {
    assert.deepEqual(desiredLabels(issue({body: `${provided}\n\n### Diagnostic details${details}`})),
      ["bug", "status/needs-diagnostics"]);
  }
  const supplied = [
    "Example status",
    "### Connection status\n\nExample status",
    "### Area\n\nExample status",
    "### Diagnostic details\n\nExample status",
    "```text\n### Reproduction and expected/actual behaviour\nExample status\n```",
  ];
  for (const details of supplied) {
    const body = `${provided}\n\n### Diagnostic details\n\n${details}\n\n### Reproduction and expected/actual behaviour\n\nExample`;
    assert.deepEqual(desiredLabels(issue({body})), ["bug"]);
  }
});

test("fenced examples do not supply issue form headings", () => {
  const body = "### Area\n\nDiscovery and onboarding\n\n### What do you need help with?\n\n" +
    "````markdown\n```\n### Problem and proposed behaviour\n\nExample heading\n````";
  assert.deepEqual(desiredLabels(issue({title: "Example", body})), ["question", "area/setup"]);
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
  const own = {id: 5, user: bot, body: GUIDANCE};
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

test("label events reconcile the current issue and preserve manual labels", async () => {
  const current = issue({title: "[Support]: Example", body: "### Area\n\nDiscovery and onboarding",
    labels: ["bug", "area/recovery", "status/needs-diagnostics", "keep-me"]});
  const h = harness(current);
  h.context = {...h.context, eventName: "issues", payload: {issue: issue(), action: "edited"}};
  await run({...h, mode: "labels"});
  assert.deepEqual(h.failures, []);
  assert.deepEqual(h.calls.map(([name, args]) => [name, args.name || args.labels || args.issue_number]), [
    ["get", 7],
    ["getLabel", "question"], ["addLabels", ["question"]],
    ["getLabel", "area/setup"], ["addLabels", ["area/setup"]],
    ["removeLabel", "bug"], ["removeLabel", "area/recovery"], ["removeLabel", "status/needs-diagnostics"],
  ]);
});

test("reclassification removes stale shared labels only when Actions last applied them", async () => {
  const current = issue({title: "[Feature]: Example", labels: ["enhancement", "documentation", "question"]});
  const h = harness(current);
  h.context = {...h.context, eventName: "issues", payload: {issue: current, action: "edited"}};
  const labelEvent = (id, name, actor, event = "labeled") => ({
    id, event, label: {name}, actor, created_at: "2026-11-26T00:00:00Z",
  });
  h.github.paginate = async (endpoint, args) => {
    assert.equal(endpoint, h.github.rest.issues.listEvents);
    assert.equal(args.issue_number, current.number);
    return [
      labelEvent(4, "documentation", {login: "maintainer", type: "User"}),
      labelEvent(1, "documentation", bot), labelEvent(3, "documentation", bot, "unlabeled"),
      labelEvent(2, "question", bot),
    ];
  };
  await run({...h, mode: "labels"});
  assert.deepEqual(h.failures, []);
  assert.deepEqual(h.calls.map(([name, args]) => [name, args.name || args.issue_number]),
    [["get", 7], ["removeLabel", "question"]]);
});

test("diagnostic edit events clear old guidance using the current issue", async () => {
  for (const current of [issue({body: reproduced + provided}), issue({title: "[Support]: Example"})]) {
    const h = harness({...current, comments: 1}, [{id: 5, user: bot, body: GUIDANCE}]);
    h.context = {...h.context, eventName: "issues", payload: {issue: issue(), action: "edited"}};
    await run({...h, mode: "diagnostics"});
    assert.deepEqual(h.failures, []);
    assert.deepEqual(h.calls.map(([name]) => name), ["get", "updateComment"]);
    assert.equal(h.calls[1][1].body, RESOLVED);
  }
});
