"use strict";

// Event data is input data, never shell arguments, source code or log output.
const REPOSITORY = "ha-homepod-sensor-link";
const MAINTAINER = "barneyonline";
const MARKER = "<!-- homepod-sensor-link:diagnostic-guidance -->";
const ISSUE_TYPES = [
  {prefixes: ["[Bug]:"], label: "bug", headings: ["Integration version", "Home Assistant version and installation type"]},
  {prefixes: ["[Feature]:"], label: "enhancement", headings: ["Area", "Problem and proposed behaviour"]},
  {prefixes: ["[Docs]:"], label: "documentation", headings: ["Page and section", "What should the documentation explain or correct?"]},
  {prefixes: ["[Support]:", "[Question]:"], label: "question", headings: ["Area", "What do you need help with?"]},
];
const REPRODUCTION = "Reproduction and expected/actual behaviour";
const EVIDENCE_CHOICE = "Sanitised diagnostic evidence";
const EVIDENCE_DETAILS = "Diagnostic details";
const FORM_HEADINGS = new Set([
  ...ISSUE_TYPES.flatMap(({headings}) => headings),
  EVIDENCE_CHOICE, EVIDENCE_DETAILS, REPRODUCTION,
]);
const AREAS = new Map([
  ["Discovery and onboarding", "area/setup"],
  ["Measurements and monitoring", "area/sensors"],
  ["Recovery and lifecycle", "area/recovery"],
  ["Documentation and automations", "area/docs"],
  ["Tests and developer tooling", "area/tooling"],
]);
const DIAGNOSTICS = "status/needs-diagnostics";
const SHARED_LABELS = new Set(["documentation", "question"]);
const MANAGED = new Set([...ISSUE_TYPES.map(type => type.label), ...AREAS.values(), DIAGNOSTICS]);
const GUIDANCE = MARKER + "\nAdditional sanitised evidence would help triage this report.\n\n" +
  "Describe the steps, expected/actual behaviour, integration and Home Assistant versions, " +
  "and controlled status/error categories. If you share this integration's diagnostics, review them first.\n\n" +
  "Never attach controller keys, pairing exports, backups, HA storage, addresses, device/controller IDs " +
  "or unreviewed logs. Do not post vulnerability details publicly; see SECURITY.md in the repository.";
const RESOLVED = MARKER + "\nThe missing-evidence status has been cleared. " +
  "Keep any further evidence sanitised as described in SECURITY.md.";

function labelNames(item) {
  return (item.labels || []).map(label => typeof label === "string" ? label : label.name);
}

function closedFenceStarts(markers) {
  const starts = new Set();
  const closingLengths = new Map();
  // Only closed fences hide headings. An unclosed fence can come from an earlier form answer.
  for (let index = markers.length - 1; index >= 0; index--) {
    const match = markers[index];
    if (!match) continue;
    const [, marker, suffix] = match;
    const character = marker[0];
    const longestClosing = closingLengths.get(character) || 0;
    if (marker.length <= longestClosing && (character === "~" || !suffix.includes("`"))) starts.add(index);
    if (!suffix.trim()) closingLengths.set(character, Math.max(longestClosing, marker.length));
  }
  return starts;
}

function formFields(body) {
  const fields = new Map();
  const lines = body.split(/\r?\n/);
  const markers = lines.map(line => /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(line));
  const fenceStarts = closedFenceStarts(markers);
  let currentHeading;
  let response;
  let fence = "";
  for (const [index, line] of lines.entries()) {
    const heading = line.startsWith("### ") ? line.slice(4).trimEnd() : "";
    // The bug form places reproduction immediately after the free-form evidence.
    const expectsDetails = fields.has(EVIDENCE_CHOICE) && !fields.has(REPRODUCTION);
    if (!fence && FORM_HEADINGS.has(heading) && !fields.has(heading) &&
        (heading !== EVIDENCE_DETAILS || expectsDetails) &&
        (currentHeading !== EVIDENCE_DETAILS || heading === REPRODUCTION)) {
      currentHeading = heading;
      response = [];
      fields.set(heading, response);
    } else if (response) {
      response.push(line);
    }

    const match = markers[index];
    if (match) {
      const [, marker, suffix] = match;
      if (!fence && fenceStarts.has(index)) {
        fence = marker;
      } else if (marker[0] === fence[0] && marker.length >= fence.length && !suffix.trim()) {
        fence = "";
      }
    }
  }
  return new Map(Array.from(fields, ([heading, lines]) => [heading, lines.join("\n").trim()]));
}

function selection(fields, heading) {
  return fields.get(heading)?.split(/\n### /, 1)[0].trim();
}

function hasDiagnosticEvidence(fields) {
  if (selection(fields, EVIDENCE_CHOICE) !== "Provided below after privacy review") return false;
  const headings = [...fields.keys()];
  const choice = headings.indexOf(EVIDENCE_CHOICE);
  const reproduction = headings.indexOf(REPRODUCTION);
  const details = headings.indexOf(EVIDENCE_DETAILS);
  // The legacy form asked for reproduction before the choice and had no dedicated details field.
  if (reproduction >= 0 && reproduction < choice) return true;
  if (reproduction > choice && (details < choice || details > reproduction)) return false;
  const response = fields.get(EVIDENCE_DETAILS)?.replace(/^(`{3,})text\n(?:([\s\S]*?)\n)?\1$/, "$2").trim();
  return Boolean(response) && response !== "_No response_";
}

function desiredLabels(item) {
  const body = String(item.body || "");
  if (body.length > 65536) throw new Error("Issue form exceeds the supported budget");
  const title = String(item.title || "");
  const fields = formFields(body);
  let type = ISSUE_TYPES.find(({prefixes}) => prefixes.some(prefix => title.startsWith(prefix)));
  if (!type) {
    // The form's identifying headings precede headings contributed in its long answers.
    const seen = new Set();
    for (const heading of fields.keys()) {
      seen.add(heading);
      type = ISSUE_TYPES.find(({headings}) => headings.at(-1) === heading && headings.every(name => seen.has(name)));
      if (type) break;
    }
  }
  if (!type) return null;
  const labels = [type.label];
  const area = type.label === "documentation" ? "area/docs" : AREAS.get(selection(fields, "Area"));
  if (area) labels.push(area);
  if (type.label === "bug" && !hasDiagnosticEvidence(fields)) labels.push(DIAGNOSTICS);
  return labels;
}

function plan(mode, item, eventName, eventLabel) {
  if (!Number.isSafeInteger(item.number) || item.number < 1) throw new Error("Invalid issue number");
  const labels = labelNames(item);
  if (labels.includes("automation/hal")) return [];
  if (mode === "pr") {
    if (!item.pull_request && !item.head) return [];
    const actions = (item.assignees || []).some(user => user.login === MAINTAINER) ? [] : [{type: "assign"}];
    const managed = /^codex\/(?:issue-|bugfix-|release-|autonomous-maintenance(?:$|\/))/.test(item.head?.ref || "");
    if (!item.draft && item.user?.login !== MAINTAINER && !managed &&
        !(item.requested_reviewers || []).some(user => user.login === MAINTAINER)) {
      actions.push({type: "review"});
    }
    return actions;
  }
  if (item.pull_request) return [];
  if (mode === "assign") {
    return (item.assignees || []).some(user => user.login === MAINTAINER) ? [] : [{type: "assign"}];
  }
  const desired = desiredLabels(item);
  if (mode === "labels" && desired) {
    return [
      ...desired.filter(label => !labels.includes(label)).map(label => ({type: "add-label", label})),
      ...labels.filter(label => MANAGED.has(label) && !desired.includes(label)).map(label => ({type: "remove-label", label})),
    ];
  }
  if (mode === "diagnostics") {
    const labelEvent = ["labeled", "unlabeled"].includes(eventName);
    if (labelEvent && eventLabel !== DIAGNOSTICS) return [];
    if (!desired?.includes("bug")) {
      // An issue that was reclassified can still have a request from its bug report.
      const hasUnclassifiedStatus = !desired && labels.includes(DIAGNOSTICS);
      return item.comments > 0 && !hasUnclassifiedStatus ? [{type: "guidance", needed: false}] : [];
    }
    // Token-generated label events do not trigger other workflows. Open/edit
    // events independently derive the same status; human label events use it.
    const needed = (labelEvent ? labels : desired).includes(DIAGNOSTICS);
    return [{type: "guidance", needed}];
  }
  return [];
}

function isActionsBot(user) {
  return user?.login === "github-actions[bot]" && user.type === "Bot";
}

async function preserveManualLabels({github, context}, item, actions) {
  const shared = actions.filter(action => action.type === "remove-label" && SHARED_LABELS.has(action.label));
  if (!shared.length) return actions;
  const events = await github.paginate(github.rest.issues.listEvents, {...context.repo, issue_number: item.number, per_page: 100});
  const keep = shared.filter(action => {
    const latest = events.filter(event => event.label?.name === action.label && ["labeled", "unlabeled"].includes(event.event))
      .sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id - a.id)[0];
    return latest?.event !== "labeled" || !isActionsBot(latest.actor);
  });
  return actions.filter(action => !keep.includes(action));
}

async function apply({github, context}, item, actions) {
  const common = {...context.repo, issue_number: item.number};
  for (const action of actions) {
    if (action.type === "assign") {
      await github.rest.issues.addAssignees({...common, assignees: [MAINTAINER]});
    } else if (action.type === "review") {
      await github.rest.pulls.requestReviewers({...context.repo, pull_number: item.number, reviewers: [MAINTAINER]});
    } else if (action.type === "add-label") {
      try {
        await github.rest.issues.getLabel({...context.repo, name: action.label});
      } catch (error) {
        if (error.status !== 404) throw error;
        try {
          await github.rest.issues.createLabel({...context.repo, name: action.label, color: "1d76db"});
        } catch (race) {
          if (race.status !== 422) throw race;
          await github.rest.issues.getLabel({...context.repo, name: action.label});
        }
      }
      await github.rest.issues.addLabels({...common, labels: [action.label]});
    } else if (action.type === "remove-label") {
      try {
        await github.rest.issues.removeLabel({...common, name: action.label});
      } catch (error) {
        if (error.status !== 404) throw error;
      }
    } else if (action.type === "guidance") {
      const comments = await github.paginate(github.rest.issues.listComments, {...common, per_page: 100});
      const existing = comments.find(comment => isActionsBot(comment.user) && comment.body?.startsWith(MARKER));
      const body = action.needed ? GUIDANCE : RESOLVED;
      if (existing && existing.body !== body) {
        await github.rest.issues.updateComment({...context.repo, comment_id: existing.id, body});
      } else if (!existing && action.needed) {
        await github.rest.issues.createComment({...common, body});
      }
    }
  }
}

async function run({github, context, core, mode}) {
  try {
    if (context.repo.owner !== MAINTAINER || context.repo.repo !== REPOSITORY ||
        !["pr", "assign", "labels", "diagnostics"].includes(mode)) throw new Error("Unsupported repository or mode");
    const preview = context.eventName === "workflow_dispatch";
    let items;
    if (preview) {
      if (context.ref !== "refs/heads/main") throw new Error("Manual previews require main");
      const number = context.payload.inputs?.item_number || "";
      if (number) {
        if (!/^[1-9][0-9]{0,8}$/.test(number)) throw new Error("Invalid preview number");
        const result = mode === "pr" ?
          await github.rest.pulls.get({...context.repo, pull_number: Number(number)}) :
          await github.rest.issues.get({...context.repo, issue_number: Number(number)});
        items = [result.data];
      } else {
        const result = mode === "pr" ?
          await github.rest.pulls.list({...context.repo, state: "open", per_page: 100}) :
          await github.rest.issues.listForRepo({...context.repo, state: "open", per_page: 100});
        items = result.data;
      }
    } else {
      let item = context.payload.pull_request || context.payload.issue;
      if (!item) throw new Error("Missing event item");
      if (["labels", "diagnostics"].includes(mode) && !item.pull_request && context.payload.issue) {
        // Queued edit events may carry an older title, body or label list.
        const {data} = await github.rest.issues.get({...context.repo, issue_number: item.number});
        item = data;
      }
      items = [item];
    }
    let count = 0;
    for (const item of items) {
      const planned = plan(mode, item, context.payload.action, context.payload.label?.name);
      const actions = await preserveManualLabels({github, context}, item, planned);
      count += actions.length;
      if (!preview) await apply({github, context}, item, actions);
    }
    core.info((preview ? "Read-only preview" : "Event reconciliation") + ": " +
      items.length + " items, " + count + " planned actions. No issue text or attachments logged.");
  } catch {
    core.setFailed("Metadata automation failed; inspect permissions and supported form fields. No issue payload was logged.");
  }
}

module.exports = {run, plan, apply, desiredLabels, MARKER, GUIDANCE, RESOLVED};
