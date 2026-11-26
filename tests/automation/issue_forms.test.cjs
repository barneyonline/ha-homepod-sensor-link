"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {desiredLabels} = require("../../scripts/issue_automation.cjs");

const areaLabels = new Map([
  ["Discovery and onboarding", "area/setup"],
  ["Measurements and monitoring", "area/sensors"],
  ["Recovery and lifecycle", "area/recovery"],
  ["Documentation and automations", "area/docs"],
  ["Tests and developer tooling", "area/tooling"],
  ["Other or not sure", null],
]);

function formBody(form, answers = {}) {
  return form.body.filter(({type}) => type !== "markdown").map(({id, attributes}) => {
    const answer = answers[id] ?? attributes.options?.[0] ?? "Example response";
    const response = attributes.render && answer !== "_No response_" ? `\`\`\`${attributes.render}\n${answer}\n\`\`\`` : answer;
    return `### ${attributes.label}\n\n${response}`;
  }).join("\n\n");
}

test("checked-in issue forms supply the headings and choices used for labels", () => {
  const json = process.env.HOMEPOD_ISSUE_FORMS_JSON;
  assert.ok(json, "Run sh scripts/validate_automation.sh to supply the parsed issue forms");
  const forms = JSON.parse(json);
  const types = [
    ["bug_report", ["bug"], ["status/needs-diagnostics"]],
    ["feature_request", ["enhancement"], []],
    ["documentation", ["documentation", "area/docs"], []],
    ["support", ["question"], []],
  ];
  for (const [name, labels, status] of types) {
    const form = forms[name];
    assert.ok(form, `${name} form`);
    assert.deepEqual(desiredLabels({title: `${form.title}Example`}), [...labels, ...status], `${name} default title`);

    const area = form.body.find(({id}) => id === "area");
    assert.equal(Boolean(area), name !== "documentation", `${name} Area field`);
    if (area) assert.ok(area.attributes.options.length, `${name} Area choices`);
    for (const choice of area?.attributes.options ?? [null]) {
      if (choice) assert.ok(areaLabels.has(choice), `${name} Area choice ${choice}`);
      const topic = areaLabels.get(choice);
      const body = formBody(form, {area: choice});
      const expected = topic ? [...labels, topic, ...status] : [...labels, ...status];
      assert.deepEqual(desiredLabels({title: "Example", body}), expected, `${name} form with Area ${choice}`);
    }
  }

  const bugForm = forms.bug_report;
  const diagnostics = "Provided below after privacy review";
  assert.ok(bugForm.body.find(({id}) => id === "diagnostics")?.attributes.options.includes(diagnostics));
  const area = "Measurements and monitoring";
  const body = formBody(bugForm, {area, diagnostics, evidence: "_No response_"});
  assert.deepEqual(desiredLabels({title: "Example", body}), ["bug", "area/sensors", "status/needs-diagnostics"]);
  const withEvidence = formBody(bugForm, {area, diagnostics,
    evidence: "### Reproduction and expected/actual behaviour\n\nThe device is unavailable."});
  assert.deepEqual(desiredLabels({title: "Example", body: withEvidence}), ["bug", "area/sensors"]);

  const withoutDetails = {...bugForm, body: bugForm.body.filter(({id}) => id !== "evidence")};
  const edited = formBody(withoutDetails, {area, diagnostics,
    reproduction: "Example response\n\n### Diagnostic details\n\nIn the reproduction section."});
  assert.deepEqual(desiredLabels({title: "Example", body: edited}), ["bug", "area/sensors", "status/needs-diagnostics"]);
  const withoutReproduction = {...withoutDetails, body: withoutDetails.body.filter(({id}) => id !== "reproduction")};
  assert.deepEqual(desiredLabels({title: "Example", body: formBody(withoutReproduction, {area, diagnostics})}),
    ["bug", "area/sensors", "status/needs-diagnostics"]);
  const legacy = {...withoutDetails, body: [bugForm.body.find(({id}) => id === "reproduction"),
    ...withoutDetails.body.filter(({id}) => id !== "reproduction")]};
  const legacyBody = `${formBody(legacy, {area, diagnostics})}\n\n### Status category\n\nRead unavailable`;
  assert.deepEqual(desiredLabels({title: "Example", body: legacyBody}), ["bug", "area/sensors"]);
  const unclosedFence = formBody(legacy, {area, diagnostics, reproduction: "Example reproduction\n\n```text\nRead unavailable"});
  assert.deepEqual(desiredLabels({title: "Example", body: unclosedFence}), ["bug", "area/sensors"]);
  const legacyHeading = formBody(legacy, {area, diagnostics, reproduction: "### Diagnostic details\n\nRead unavailable"});
  assert.deepEqual(desiredLabels({title: "Example", body: legacyHeading}), ["bug", "area/sensors"]);
  assert.deepEqual(desiredLabels({title: "[Question]: Example", body: legacyHeading}), ["question", "area/sensors"]);

  const reproduction = formBody(bugForm, {area, reproduction: "### What do you need help with?\n\nExample response"});
  assert.deepEqual(desiredLabels({title: "Example", body: reproduction}), ["bug", "area/sensors", "status/needs-diagnostics"]);
  const support = formBody(forms.support, {area, question: "### Problem and proposed behaviour\n\nExample response"});
  assert.deepEqual(desiredLabels({title: "Example", body: support}), ["question", "area/sensors"]);
});
