"""Validate community metadata, action translations and read-only automation."""

import json
from pathlib import Path
import re
import unittest
import tempfile
import shutil
from types import SimpleNamespace

import yaml

from scripts.check_repository import check
from scripts.validate_quality_scale import validate
from custom_components.homepod_sensor_link.sensor import RecoveryStatus, PARALLEL_UPDATES

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/homepod_sensor_link"


class RepositoryContractTests(unittest.TestCase):
    def test_entity_enum_translations_icons_and_parallel_update_contract(self):
        strings = json.loads((COMPONENT / "strings.json").read_text())
        labels = strings["entity"]["sensor"]["sensor_access"]["state"]
        diagnostic = RecoveryStatus(
            SimpleNamespace(coordinator=SimpleNamespace(data={}), records={"key": {}}), {"entry_id": "key"}
        )
        self.assertEqual(set(labels), set(diagnostic.options))
        self.assertEqual(diagnostic.device_class, "enum")
        self.assertEqual(PARALLEL_UPDATES, 0)
        self.assertEqual(
            json.loads((COMPONENT / "icons.json").read_text())["entity"]["sensor"]["sensor_access"]["default"],
            "mdi:shield-sync",
        )

    def test_all_quality_rules_have_evidence_but_external_readiness_is_not_fabricated(self):
        counts, pending = validate(ROOT)
        self.assertEqual(counts, {"done": 49, "exempt": 3, "todo": 2})
        self.assertEqual(len(pending), 5)
        for mode in ("require_complete", "require_release_ready"):
            with self.assertRaises(ValueError):
                validate(ROOT, **{mode: True})

    def test_quality_gate_refuses_unreviewed_exemptions_missing_evidence_and_fake_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "devtools", root / "devtools")
            (root / "quality_scale.yaml").write_text((ROOT / "quality_scale.yaml").read_text())
            for category in ("custom_components", "tests", "docs"):
                shutil.copytree(ROOT / category, root / category, ignore=shutil.ignore_patterns("__pycache__"))
            for file in ("README.md", "SECURITY.md"):
                shutil.copyfile(ROOT / file, root / file)
            claims = yaml.safe_load((root / "quality_scale.yaml").read_text())
            for status in ("exempt", "unknown", "done"):
                changed = {"rules": {**claims["rules"], "brands": {"status": status, "comment": "Unsupported claim"}}}
                (root / "quality_scale.yaml").write_text(yaml.safe_dump(changed))
                with self.assertRaises(ValueError):
                    validate(root)
            (root / "quality_scale.yaml").write_text(yaml.safe_dump(claims))
            (root / "docs/DEPENDENCIES.md").unlink()
            with self.assertRaises(ValueError):
                validate(root)

    def test_quality_code_has_no_local_type_or_coverage_suppressions(self):
        for path in COMPONENT.rglob("*.py"):
            self.assertNotRegex(path.read_text(), r"type:\s*ignore|pragma:\s*no\s*(cover|branch)")
        config = (ROOT / "pyproject.toml").read_text()
        self.assertIn("strict = true", config)
        self.assertNotIn("ignore_errors", config)
        self.assertNotIn("ignore_missing_imports", config)
        self.assertNotIn("disable_error_code", config)

    def test_repository_metadata_and_local_links(self):
        check(ROOT)

    def test_every_action_has_matching_user_facing_translations(self):
        actions = yaml.safe_load((COMPONENT / "services.yaml").read_text())
        strings = json.loads((COMPONENT / "strings.json").read_text())["services"]
        self.assertEqual(set(actions), set(strings))
        for name in actions:
            for field in ("name", "description"):
                self.assertEqual(actions[name][field], strings[name][field])

    def test_workflows_are_bounded_read_only_and_pin_external_actions(self):
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            text = path.read_text()
            workflow = yaml.load(text, Loader=yaml.BaseLoader)
            with self.subTest(workflow=path.name):
                self.assertEqual(workflow["permissions"], {"contents": "read"})
                self.assertNotIn("pull_request_target", workflow["on"])
                self.assertNotIn("workflow_run", workflow["on"])
                self.assertNotIn("secrets.", text)
                self.assertNotIn(": write", text)
                for job in workflow["jobs"].values():
                    self.assertLessEqual(int(job["timeout-minutes"]), 15)
                    for step in job["steps"]:
                        if "uses" in step:
                            self.assertRegex(step["uses"], r"^[\w/-]+@[a-f0-9]{40}$")
                        if step.get("uses", "").startswith("actions/checkout@"):
                            self.assertEqual(step["with"]["persist-credentials"], "false")
                            self.assertEqual(step["with"]["fetch-depth"], "0")

    def test_community_forms_parse_and_never_request_secret_attachments(self):
        for path in (ROOT / ".github/ISSUE_TEMPLATE").glob("*.yml"):
            form = yaml.safe_load(path.read_text())
            if "body" in form:
                self.assertTrue(form["name"])
                ids = [item["id"] for item in form["body"] if "id" in item]
                self.assertEqual(len(ids), len(set(ids)))
                warning = form["body"][0]
                self.assertEqual(warning["type"], "markdown")
                self.assertRegex(warning["attributes"]["value"].lower(), r"(never|do not).*(keys|credentials)")

    def test_watchdog_requires_selected_diagnostics_without_household_ids(self):
        text = (ROOT / "monitoring/heartbeat-watchdog.yaml").read_text()
        blueprint = yaml.load(text, Loader=yaml.BaseLoader)
        self.assertEqual(blueprint["blueprint"]["domain"], "automation")
        self.assertIn("monitored_entities", blueprint["blueprint"]["input"])
        self.assertIn("last_checked", text)
        self.assertIn("900", text)
        self.assertNotRegex(text, r"sensor\.[a-z_]+")
        self.assertNotIn("legacy", text.lower())
        self.assertNotIn("homepod_sensor_link.check_now", text)
        self.assertEqual(blueprint["mode"], "single")

    def test_deliberate_runtime_inventory_matches_hacs(self):
        config = json.loads((ROOT / "devtools/validation.json").read_text())
        self.assertEqual(config["home_assistant"], ["2026.10.0", "2026.11.0"])
        self.assertEqual(config["aiohomekit"], "4.0.1")
        for version in config["home_assistant"]:
            self.assertTrue(re.fullmatch(r"\d{4}\.\d+\.\d+", version))
