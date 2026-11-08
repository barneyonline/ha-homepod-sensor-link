"""Audit all quality-rule evidence; optionally require full/release readiness."""

import argparse
import json
from pathlib import Path

import yaml

try:
    from .wiki import is_wiki_reference
except ImportError:
    from wiki import is_wiki_reference

ROOT = Path(__file__).resolve().parents[1]
EXEMPTIONS = {"docs-conditions", "docs-triggers", "inject-websession"}


def validate(root=ROOT, *, require_complete=False, require_release_ready=False):
    inventory = json.loads((root / "devtools/quality-rules.json").read_text())["rules"]
    evidence = json.loads((root / "devtools/quality-evidence.json").read_text())
    rules = yaml.safe_load((root / "quality_scale.yaml").read_text())["rules"]
    if len(inventory) != 54 or set(rules) != set(inventory) or set(evidence["rules"]) != set(inventory):
        raise ValueError("Quality-rule inventory differs from the reviewed 54-rule snapshot")
    counts = {"done": 0, "todo": 0, "exempt": 0}
    for name, claim in rules.items():
        status = claim["status"]
        if status not in counts or not claim.get("comment"):
            raise ValueError("Quality status/comment is invalid")
        counts[status] += 1
        record = evidence["rules"][name]
        if not record.get("comment") or not all(record.get(key) for key in ("code", "tests", "docs")):
            raise ValueError("Every rule needs explicit source, test and documentation evidence")
        for category in ("code", "tests", "docs"):
            for reference in record[category]:
                # Wiki content is maintained separately. This checks references,
                # not remote content or completion of independent release gates.
                if category == "docs" and is_wiki_reference(reference):
                    continue
                if "://" in reference:
                    raise ValueError("External evidence is limited to declared Wiki documentation")
                path = (root / reference).resolve()
                if root.resolve() not in path.parents or not path.is_file() or (root / reference).is_symlink():
                    raise ValueError("Quality evidence references missing or unsafe local content")
        if status == "exempt" and name not in EXEMPTIONS:
            raise ValueError("Unreviewed quality exemption")
        if name in {"brands", "integration-owner"} and status == "done" and not record.get("external_verification"):
            raise ValueError("External quality claims require recorded independent verification")
    if (
        rules["docs-conditions"]["status"] == "exempt"
        and (root / "custom_components/homepod_sensor_link/condition.py").exists()
    ):
        raise ValueError("Condition exemption conflicts with an implemented platform")
    if (
        rules["docs-triggers"]["status"] == "exempt"
        and (root / "custom_components/homepod_sensor_link/trigger.py").exists()
    ):
        raise ValueError("Trigger exemption conflicts with an implemented platform")
    if (root / "custom_components/homepod_sensor_link/manifest.json").is_file():
        manifest = json.loads((root / "custom_components/homepod_sensor_link/manifest.json").read_text())
        if manifest.get("quality_scale") == "platinum":
            raise ValueError("A custom manifest must not claim externally awarded Platinum")
    for gate in evidence["release_gates"].values():
        if gate["status"] not in {"pending", "done"} or not gate.get("comment"):
            raise ValueError("Invalid release evidence gate")
        if gate["status"] == "done" and not gate.get("evidence"):
            raise ValueError("Completed release gates require independent evidence")
    pending = [name for name, gate in evidence["release_gates"].items() if gate["status"] != "done"]
    if require_complete and counts["todo"]:
        raise ValueError(
            "Platinum target incomplete: " + ", ".join(name for name, rule in rules.items() if rule["status"] == "todo")
        )
    if require_release_ready and (counts["todo"] or pending):
        raise ValueError("Release readiness remains blocked by quality/external evidence gates")
    print(
        f"Quality evidence valid: {counts['done']} done, {counts['exempt']} scoped exemptions, {counts['todo']} todos; {len(pending)} external release gates pending. Official status: custom."
    )
    return counts, pending


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--require-release-ready", action="store_true")
    args = parser.parse_args()
    try:
        validate(require_complete=args.require_complete, require_release_ready=args.require_release_ready)
    except ValueError as error:
        raise SystemExit(str(error)) from None
