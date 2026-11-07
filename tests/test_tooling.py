"""Failure-path checks for the local repository and coverage gates."""

from copy import deepcopy
from contextlib import redirect_stdout
import io
from pathlib import Path
import json
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from scripts.build_wiki import build
from scripts.check_coverage import check
from scripts.check_repository import check_links
from scripts.run_tests import ROOT, run


class CoverageGateTests(unittest.TestCase):
    def setUp(self):
        self.modules = {"first.py", "second.py"}
        self.report = {
            "meta": {"branch_coverage": True},
            "files": {name: {"summary": {"missing_lines": 0, "missing_branches": 0}} for name in self.modules},
        }

    def test_exact_zero_counts_pass(self):
        check(self.report, self.modules)
        self.report["files"]["first.py"]["summary"].update(missing_lines=0, missing_branches=0)
        check(self.report, self.modules)

    def test_each_module_and_metric_is_gated_independently(self):
        for metric in ("missing_lines", "missing_branches"):
            with self.subTest(metric=metric):
                report = deepcopy(self.report)
                report["files"]["first.py"]["summary"][metric] += 1
                report["files"]["first.py"]["summary"]["percent_covered"] = (
                    100.0  # rounded display cannot hide one miss
                )
                with self.assertRaises(ValueError):
                    check(report, self.modules)

    def test_missing_extra_and_statement_only_reports_fail(self):
        for report in (
            {**self.report, "meta": {"branch_coverage": False}},
            {**self.report, "files": {}},
            {**self.report, "files": {**self.report["files"], "unexpected.py": {}}},
        ):
            with self.assertRaises(ValueError):
                check(report, self.modules)
        with self.assertRaises(ValueError):
            check(self.report, set())

    def test_invalid_counts_fail_closed(self):
        for invalid in (-1, True, float("nan"), "0"):
            report = deepcopy(self.report)
            report["files"]["first.py"]["summary"]["missing_lines"] = invalid
            with self.assertRaises(ValueError):
                check(report, self.modules)

    def test_excluding_code_cannot_hide_a_regression(self):
        self.report["files"]["first.py"]["summary"]["excluded_lines"] = 1
        with self.assertRaises(ValueError):
            check(self.report, self.modules)


class DocumentationGateTests(unittest.TestCase):
    def test_missing_and_escaping_links_fail_without_following_external_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "README.md"
            source.write_text("[External](https://example.invalid/never-requested) [Section](#section)")
            self.assertEqual(check_links(root), [])
            source.write_text("[Missing](missing.md) [Escape](../outside.md)")
            self.assertEqual(len(check_links(root)), 2)

    def test_wiki_drift_is_detected_and_unexpected_work_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.md"
            source.write_text("# Start\n\n[Self](source.md#start)\n")
            with patch("scripts.build_wiki.PAGES", {"Home": "source.md"}):
                build(root)
                build(root, check=True)
                page = root / "wiki/Home.md"
                self.assertIn("(Home.md#start)", page.read_text())
                source.write_text("# Changed\n")
                with self.assertRaises(ValueError):
                    build(root, check=True)
                extra = root / "wiki/Unrelated.md"
                extra.write_text("Preserve this work")
                with self.assertRaises(ValueError):
                    build(root)
                self.assertEqual(extra.read_text(), "Preserve this work")


class TestRunnerSafetyTests(unittest.TestCase):
    def test_reports_are_exported_with_network_disabled_and_source_read_only(self):
        payload = {"coverage": {"totals": {"percent_covered": 100}}, "xml": "<coverage/>", "tests": 1, "success": True}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "scripts.run_tests.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps(payload))
            ) as docker,
        ):
            with redirect_stdout(io.StringIO()):
                run("2026.11.0", directory)
            self.assertTrue((Path(directory) / "coverage.xml").exists())
            command = docker.call_args.args[0]
            self.assertEqual(command[command.index("--network") + 1], "none")
            self.assertIn(str(ROOT) + ":/project:ro", command)

    def test_unapproved_matrix_version_and_source_report_output_are_refused(self):
        with patch("scripts.run_tests.subprocess.run") as docker:
            for version, output in (("2099.1.0", "/tmp/test-reports"), ("2026.11.0", ROOT / "reports")):
                with self.assertRaises(ValueError):
                    run(version, output)
            docker.assert_not_called()

    def test_a_failed_suite_cannot_be_reported_as_success(self):
        payload = {"coverage": {"totals": {"percent_covered": 100}}, "xml": "<coverage/>", "tests": 1, "success": False}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("scripts.run_tests.subprocess.run", return_value=SimpleNamespace(stdout=json.dumps(payload))),
        ):
            with self.assertRaises(SystemExit) as error, redirect_stdout(io.StringIO()):
                run("2026.11.0", directory)
            self.assertEqual(error.exception.code, 1)
