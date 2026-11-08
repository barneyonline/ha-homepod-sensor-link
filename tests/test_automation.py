"""Artifact integrity and source attribution regressions for read-only workflows."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZipInfo

from scripts.build_release_artifact import build, verify

SHA = "a" * 40


class ArtifactTests(unittest.TestCase):
    def fixture(self, root, extra=None):
        archive = root / "homepod_sensor_link-2.0.0.zip"
        with ZipFile(archive, "w") as zipped:
            contents = {
                "custom_components/homepod_sensor_link/manifest.json": json.dumps(
                    {"domain": "homepod_sensor_link", "version": "2.0.0"}
                ),
                "LICENSE": "                    GNU GENERAL PUBLIC LICENSE\n",
            }
            if extra:
                contents.update(extra)
            for name, content in contents.items():
                entry = ZipInfo(name)
                entry.external_attr = 0o100644 << 16
                zipped.writestr(entry, content)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        (root / (archive.name + ".sha256")).write_text(f"{digest}  {archive.name}\n")
        (root / "package-evidence.json").write_text(
            json.dumps(
                {
                    "source_sha": SHA,
                    "version": "2.0.0",
                    "archive": archive.name,
                    "sha256": digest,
                    "scope": "development-artifact",
                    "release_ready": False,
                    "pending_release_gates": ["hardware"],
                }
            )
        )
        return archive

    def test_valid_development_artifact_has_explicit_source_and_unmet_release_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.fixture(root)
            result = verify(root, SHA)
            self.assertFalse(result["release_ready"])
            self.assertEqual(result["pending_release_gates"], ["hardware"])

    def test_changed_source_checksum_scope_or_filename_is_rejected(self):
        for field, value in (
            ("source_sha", "b" * 40),
            ("sha256", "0" * 64),
            ("release_ready", True),
            ("scope", "published-release"),
            ("archive", "../outside.zip"),
            ("version", "../outside"),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                self.fixture(root)
                path = root / "package-evidence.json"
                evidence = json.loads(path.read_text())
                evidence[field] = value
                path.write_text(json.dumps(evidence))
                with self.assertRaises(ValueError):
                    verify(root, SHA)

    def test_unsafe_entries_and_foreign_components_fail_even_with_matching_checksums(self):
        for name in ("../outside", "/absolute", "custom_components/foreign/manifest.json", "folder\\entry"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                self.fixture(root, {name: "disposable"})
                with self.assertRaises(ValueError):
                    verify(root, SHA)

    def test_corrupted_archive_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = self.fixture(root)
            archive.write_bytes(archive.read_bytes() + b"changed")
            with self.assertRaisesRegex(ValueError, "checksum"):
                verify(root, SHA)

    def test_dirty_wrong_commit_and_invalid_input_cannot_create_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "artifact"
            for values in (["b" * 40, ""], [SHA, " M README.md"]):
                with patch("scripts.build_release_artifact.subprocess.check_output", side_effect=values):
                    with self.assertRaises(ValueError):
                        build(output, SHA)
            with self.assertRaises(ValueError):
                build(output, "main")
            self.assertFalse(output.exists())
