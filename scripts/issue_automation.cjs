"use strict";

// Event data is input data, never shell arguments, source code or log output.
const REPOSITORY = "ha-homepod-sensor-link";
const MAINTAINER = "barneyonline";
const MARKER = "<!-- homepod-sensor-link:diagnostic-guidance -->";
const AREAS = {
  "Discovery and onboarding": "area/setup",
  "Measurements and monitoring": "area/sensors",
  "Recovery and lifecycle": "area/recovery",
  "Documentation and automations": "area/docs",
  "Tests and developer tooling": "area/tooling",
};
const MANAGED = new Set(["bug", "enhancement", "status/needs-diagnostics", ...Object.values(AREAS)]);
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

function field(body, heading) {
  const prefix = "### " + heading + "\n";
  const text = String(body || "").replace(/\r\n/g, "\n");
  const start = text.indexOf(prefix);
  return start < 0 ? "" : text.slice(start + prefix.length).split("\n### ")[0].trim();
}

function desiredLabels(item) {
  if (String(item.body || "").length > 65536) throw new Error("Issue form exceeds the supported budget");
  const bug = String(item.title || "").startsWith("[Bug]:");
  const feature = String(item.title || "").startsWith("[Feature]:");
  if (!bug && !feature) return null;
  const labels = [bug ? "bug" : "enhancement"];
  const area = AREAS[field(item.body, "Area")];
  if (area) labels.push(area);
  if (bug && field(item.body, "Sanitised diagnostic evidence") !== "Provided below after privacy review") {
    labels.push("status/needs-diagnostics");
  }
  return labels;
}

function plan(mode, item, eventName) {
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
  if (!desired) return [];
  if (mode === "labels") {
    return [
      ...desired.filter(label => !labels.includes(label)).map(label => ({type: "add-label", label})),
      ...labels.filter(label => MANAGED.has(label) && !desired.includes(label)).map(label => ({type: "remove-label", label})),
    ];
  }
  if (mode === "diagnostics" && String(item.title || "").startsWith("[Bug]:")) {
    // Token-generated label events do not trigger other workflows. Open/edit
    // events independently derive the same status; human label events use it.
    const needed = ["labeled", "unlabeled"].includes(eventName) ?
      labels.includes("status/needs-diagnostics") : desired.includes("status/needs-diagnostics");
    return [{type: "guidance", needed}];
  }
  return [];
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
      const existing = comments.find(comment => comment.user?.login === "github-actions[bot]" &&
        comment.user?.type === "Bot" && comment.body?.startsWith(MARKER));
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
      const item = context.payload.pull_request || context.payload.issue;
      if (!item) throw new Error("Missing event item");
      items = [item];
    }
    let count = 0;
    for (const item of items) {
      const actions = plan(mode, item, context.payload.action);
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
