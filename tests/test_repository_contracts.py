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
        self.assertEqual(set(pending), {"git-provenance", "controller-extraction", "hardware", "distribution"})
        for mode in ("require_complete", "require_release_ready"):
            with self.assertRaises(ValueError):
                validate(ROOT, **{mode: True})

    def test_quality_gate_refuses_unreviewed_exemptions_missing_evidence_and_fake_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(ROOT / "devtools", root / "devtools")
            (root / "quality_scale.yaml").write_text((ROOT / "quality_scale.yaml").read_text())
            for category in ("custom_components", "tests"):
                shutil.copytree(ROOT / category, root / category, ignore=shutil.ignore_patterns("__pycache__"))
            for file in ("README.md", "SECURITY.md", "CONTRIBUTING.md"):
                shutil.copyfile(ROOT / file, root / file)
            claims = yaml.safe_load((root / "quality_scale.yaml").read_text())
            for status in ("exempt", "unknown", "done"):
                changed = {"rules": {**claims["rules"], "brands": {"status": status, "comment": "Unsupported claim"}}}
                (root / "quality_scale.yaml").write_text(yaml.safe_dump(changed))
                with self.assertRaises(ValueError):
                    validate(root)
            (root / "quality_scale.yaml").write_text(yaml.safe_dump(claims))
            evidence_path = root / "devtools/quality-evidence.json"
            evidence = json.loads(evidence_path.read_text())
            original_docs = evidence["rules"]["dependency-transparency"]["docs"]
            for reference in (
                "https://example.invalid/evidence",
                "http://github.com/barneyonline/ha-homepod-sensor-link/wiki/Getting-Started",
                "https://github.com/barneyonline/ha-homepod-sensor-link/wiki/Repository-Standards",
                "https://github.com/barneyonline/ha-homepod-sensor-link/wiki/Getting-Started?redirect=elsewhere",
            ):
                evidence["rules"]["dependency-transparency"]["docs"] = [reference]
                evidence_path.write_text(json.dumps(evidence))
                with self.assertRaises(ValueError):
                    validate(root)
            evidence["rules"]["dependency-transparency"]["docs"] = original_docs
            for category in ("code", "tests"):
                original = evidence["rules"]["dependency-transparency"][category]
                evidence["rules"]["dependency-transparency"][category] = [
                    "https://github.com/barneyonline/ha-homepod-sensor-link/wiki/Getting-Started"
                ]
                evidence_path.write_text(json.dumps(evidence))
                with self.assertRaises(ValueError):
                    validate(root)
                evidence["rules"]["dependency-transparency"][category] = original
            evidence_path.write_text(json.dumps(evidence))
            (root / "CONTRIBUTING.md").unlink()
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

    def test_workflows_isolate_trusted_metadata_writes_and_pin_external_actions(self):
        metadata = {
            "auto_assign.yml",
            "auto_assign_new_issues.yml",
            "auto_label_issues.yml",
            "comment_missing_diagnostics.yml",
        }
        for path in (ROOT / ".github/workflows").glob("*.yml"):
            text = path.read_text()
            workflow = yaml.load(text, Loader=yaml.BaseLoader)
            with self.subTest(workflow=path.name):
                self.assertEqual(workflow["permissions"], {"contents": "read"})
                if "pull_request_target" in workflow["on"]:
                    self.assertEqual(path.name, "auto_assign.yml")
                self.assertNotIn("workflow_run", workflow["on"])
                for name, job in workflow["jobs"].items():
                    if "uses" in job:
                        self.assertEqual(path.name, "release-assets.yml")
                        self.assertEqual(job["uses"], "./.github/workflows/release-candidate.yml")
                        self.assertNotIn("secrets", job)
                        self.assertNotIn("write", job.get("permissions", {}).values())
                        builder = yaml.load(
                            (ROOT / ".github/workflows/release-candidate.yml").read_text(), Loader=yaml.BaseLoader
                        )
                        self.assertNotEqual(workflow["concurrency"]["group"], builder["concurrency"]["group"])
                        continue
                    if path.name in metadata:
                        self.assertEqual(name, "metadata")
                        permissions = {"contents": "read", "issues": "write"}
                        if path.name == "auto_assign.yml":
                            permissions["pull-requests"] = "write"
                        self.assertEqual(job["permissions"], permissions)
                        self.assertIn("github.repository == 'barneyonline/ha-homepod-sensor-link'", job["if"])
                        self.assertEqual(job["steps"][0]["with"]["ref"], "main")
                        self.assertNotIn("github.event.pull_request.head", json.dumps(job))
                        self.assertNotIn("github.head_ref", json.dumps(job))
                    else:
                        self.assertNotIn("write", job.get("permissions", {}).values())
                    if name != "codecov":
                        self.assertNotIn("secrets.", json.dumps(job))
                    self.assertLessEqual(int(job["timeout-minutes"]), 15)
                    for step in job["steps"]:
                        if "uses" in step:
                            self.assertRegex(step["uses"], r"^[\w/-]+@[a-f0-9]{40}$")
                        if step.get("uses", "").startswith("actions/checkout@"):
                            self.assertEqual(step["with"]["persist-credentials"], "false")
                            self.assertEqual(step["with"]["fetch-depth"], "0")

    def test_codecov_secret_is_isolated_to_validated_main_reports(self):
        workflow = yaml.load((ROOT / ".github/workflows/validate.yml").read_text(), Loader=yaml.BaseLoader)
        job = workflow["jobs"]["codecov"]
        self.assertEqual(job["needs"], "validate")
        self.assertEqual(
            job["if"],
            "${{ github.repository == 'barneyonline/ha-homepod-sensor-link' "
            "&& github.event_name != 'pull_request' && github.ref == 'refs/heads/main' }}",
        )
        self.assertNotIn("env", job)
        self.assertEqual(json.dumps(workflow).count("secrets."), 1)
        self.assertNotIn("secrets.", json.dumps(workflow["jobs"]["validate"]))
        secret_steps = [step for step in job["steps"] if "secrets." in json.dumps(step)]
        self.assertEqual(len(secret_steps), 1)
        upload = secret_steps[0]
        self.assertTrue(upload["uses"].startswith("codecov/codecov-action@"))
        self.assertEqual(upload["with"]["token"], "${{ secrets.CODECOV_TOKEN }}")
        self.assertEqual(upload["with"]["slug"], "barneyonline/ha-homepod-sensor-link")
        versions = json.loads((ROOT / "devtools/validation.json").read_text())["home_assistant"]
        self.assertEqual(
            upload["with"]["files"].split(","), [f"coverage/{version}/coverage.xml" for version in versions]
        )
        for option in ("disable_search", "disable_file_fixes", "disable_safe_directory", "fail_ci_if_error"):
            self.assertEqual(upload["with"][option], "true")
        self.assertEqual(upload["with"]["plugins"], "noop")
        self.assertNotIn("skip_validation", upload["with"])
        self.assertNotIn("use_oidc", upload["with"])
        download = next(step for step in job["steps"] if step["uses"].startswith("actions/download-artifact@"))
        self.assertEqual(
            download["with"], {"name": "homepod-link-coverage-${{ github.run_attempt }}", "path": "coverage"}
        )
        self.assertTrue(all("uses" in step and "run" not in step for step in job["steps"]))
        settings = yaml.safe_load((ROOT / "codecov.yml").read_text())
        for status in ("project", "patch"):
            self.assertEqual(settings["coverage"]["status"][status]["default"], {"target": "100%", "threshold": "0%"})
        self.assertFalse(settings["comment"])

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

    def test_deliberate_runtime_inventory_matches_hacs(self):
        config = json.loads((ROOT / "devtools/validation.json").read_text())
        self.assertEqual(config["home_assistant"], ["2026.11.0"])
        self.assertEqual(set(config["coverage"]), set(config["home_assistant"]))
        self.assertEqual(config["aiohomekit"], "4.0.1")
        for version in config["home_assistant"]:
            self.assertTrue(re.fullmatch(r"\d{4}\.\d+\.\d+", version))
