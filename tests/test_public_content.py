"""Exercise publication guards with synthetic strings, never real identities."""

import unittest

from scripts.check_public_content import PUBLIC_IMAGE_PINS, scan_path, scan_text


class PublicContentTests(unittest.TestCase):
    def test_only_exact_verified_public_image_references_are_exempted(self):
        for reference in PUBLIC_IMAGE_PINS:
            with self.subTest(image=reference.split("@")[0]):
                self.assertEqual(scan_text("FROM " + reference), [])
                self.assertIn((1, "literal-key-material"), scan_text(reference.split("sha256:")[1]))
                self.assertIn((1, "literal-key-material"), scan_text(reference + " key=" + "ab" * 32))
        self.assertIn((1, "literal-key-material"), scan_text("ghcr.io/hacs/action@sha256:" + "ab" * 32))

    def test_documentation_address_is_allowed(self):
        self.assertEqual(scan_text("example endpoint 192.0.2.18"), [])

    def test_household_address_is_rejected(self):
        text = "192" + ".168" + ".10.20"
        self.assertIn((1, "household-ip"), scan_text(text))

    def test_private_key_header_is_rejected(self):
        text = "-----BEGIN " + "PRIVATE KEY-----"
        self.assertIn((1, "private-key-header"), scan_text(text))

    def test_literal_key_material_is_rejected(self):
        self.assertIn((1, "literal-key-material"), scan_text("ab" * 32))

    def test_generated_identity_code_is_not_a_stored_identity(self):
        self.assertEqual(scan_text("Ed25519PrivateKey.generate()"), [])

    def test_runtime_files_are_rejected(self):
        for name in (".storage/state", "exports/result.json", "keys.json", ".env", "private.key"):
            with self.subTest(name=name):
                self.assertTrue(scan_path(name))

    def test_component_source_is_allowed(self):
        self.assertFalse(scan_path("custom_components/homepod_sensor_link/credentials.py"))


class RepositoryAuditTests(unittest.TestCase):
    def test_working_index_and_reachable_history_are_scanned_independently(self):
        import os
        from pathlib import Path
        import shutil
        import subprocess
        import tempfile
        from scripts.check_public_content import audit

        if not shutil.which("git"):
            self.skipTest("Git is validated by the host public-content test stage")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                **os.environ,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_AUTHOR_NAME": "Disposable Test",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Disposable Test",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
            }

            def git(*args):
                return subprocess.run(
                    [
                        "git",
                        "-C",
                        str(root),
                        "-c",
                        "commit.gpgsign=false",
                        "-c",
                        "core.hooksPath=" + str(root / "no-hooks"),
                        *args,
                    ],
                    env=environment,
                    capture_output=True,
                    check=True,
                )

            git("init")
            sample = root / "sample.txt"
            sample.write_text("ab" * 32)
            git("add", "sample.txt")
            git("commit", "-m", "Synthetic historical sample")
            sample.write_text("clean")
            git("add", "sample.txt")
            git("commit", "-m", "Remove synthetic historical sample")
            sample.write_text("cd" * 32)
            git("add", "sample.txt")
            sample.write_text("clean working copy")
            (root / "working.txt").write_text("ef" * 32)
            findings, _, is_repo = audit(root, history=True)
            self.assertTrue(is_repo)
            self.assertEqual({f[0] for f in findings}, {"file", "index", "history"})
            self.assertTrue(all(f[3] == "literal-key-material" for f in findings))
            self.assertNotIn("ab" * 32, str(findings))

    def test_history_checks_forbidden_names_even_when_blob_content_is_shared(self):
        import os
        from pathlib import Path
        import shutil
        import subprocess
        import tempfile
        from scripts.check_public_content import audit

        if not shutil.which("git"):
            self.skipTest("Git is validated by the host public-content test stage")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                **os.environ,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_AUTHOR_NAME": "Disposable Test",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Disposable Test",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
            }

            def git(*args):
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(root),
                        "-c",
                        "commit.gpgsign=false",
                        "-c",
                        "core.hooksPath=" + str(root / "no-hooks"),
                        *args,
                    ],
                    env=environment,
                    capture_output=True,
                    check=True,
                )

            git("init")
            (root / "safe.md").write_text("same harmless content")
            git("add", ".")
            git("commit", "-m", "Synthetic initial file")
            git("mv", "safe.md", "keys.json")
            git("commit", "-m", "Synthetic forbidden path")
            git("rm", "keys.json")
            git("commit", "-m", "Remove forbidden path")
            findings, _, _ = audit(root, history=True)
            self.assertIn(("history", "keys.json", 0, "runtime-or-credential-file"), findings)

    def test_directory_and_broken_symlinks_are_refused(self):
        from pathlib import Path
        import tempfile
        from scripts.check_public_content import audit

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "directory-link").symlink_to(root, target_is_directory=True)
            (root / "broken-link").symlink_to(root / "missing")
            findings, _, is_repo = audit(root)
            self.assertFalse(is_repo)
            self.assertEqual({f[1] for f in findings}, {"directory-link", "broken-link"})
            self.assertTrue(all(f[3] == "symlink" for f in findings))


class PackagePreflightTests(unittest.TestCase):
    def test_unexpected_credential_file_or_secret_prevents_archive_creation(self):
        from pathlib import Path
        import tempfile
        from unittest.mock import patch
        from scripts import package

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            output = Path(directory) / "local.zip"
            for domain in package.COMPONENTS:
                component = root / "custom_components" / domain
                component.mkdir(parents=True)
                (component / "manifest.json").write_text("{}")
            for name in (
                "README.md",
                "SECURITY.md",
                "CONTRIBUTING.md",
                "CHANGELOG.md",
                "LICENSE",
                "hacs.json",
                "quality_scale.yaml",
                "blueprints/automation/homepod_sensor_link/heartbeat-watchdog.yaml",
            ):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("safe")
            (root / "docs").mkdir()
            unexpected = root / "custom_components" / package.COMPONENTS[0] / "keys.json"
            unexpected.write_text("safe contents, forbidden filename")
            with patch.object(package, "ROOT", root), self.assertRaises(ValueError):
                package.build(output)
            self.assertFalse(output.exists())
            unexpected.unlink()
            secret = root / "docs" / "sample.md"
            secret.write_text("ab" * 32)
            with patch.object(package, "ROOT", root), self.assertRaises(ValueError):
                package.build(output)
            self.assertFalse(output.exists())

    def test_package_is_reproducible_and_includes_only_the_new_component(self):
        from pathlib import Path
        import struct
        import tempfile
        from zipfile import ZipFile
        from scripts import package

        with tempfile.TemporaryDirectory() as directory:
            first, second = (Path(directory) / name for name in ("first.zip", "second.zip"))
            package.build(first)
            package.build(second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with ZipFile(first) as archive:
                self.assertIsNone(archive.testzip())
                names = archive.namelist()
                self.assertFalse(any(name.startswith("wiki/") for name in names))
                blueprint = "blueprints/automation/homepod_sensor_link/heartbeat-watchdog.yaml"
                self.assertEqual(archive.read(blueprint), (package.ROOT / blueprint).read_bytes())
                from scripts.build_wiki import PAGES

                for page in PAGES:
                    for theme in ("light", "dark"):
                        banner = f"docs/images/wiki/{page}-{theme}.jpg"
                        self.assertEqual(archive.read(banner), (package.ROOT / banner).read_bytes())
                self.assertNotIn("docs/images/wiki/prompts.json", names)
                self.assertIn(b"GNU GENERAL PUBLIC LICENSE", archive.read("LICENSE"))
                self.assertIn("docs/images/readme-banner.svg", names)
                for theme in ("light", "dark"):
                    banner = f"docs/images/readme-banner-{theme}.png"
                    self.assertIn(banner, names)
                    self.assertEqual(archive.read(banner), (package.ROOT / banner).read_bytes())
                    self.assertIn(banner, archive.read("README.md").decode())
                brand = "custom_components/homepod_sensor_link/brand/"
                expected = {
                    f"{kind}{scale}.png" for kind in ("icon", "dark_icon", "logo", "dark_logo") for scale in ("", "@2x")
                }
                self.assertEqual({name.removeprefix(brand) for name in names if name.startswith(brand)}, expected)
                for name in expected:
                    png = archive.read(brand + name)
                    self.assertEqual(png, (package.ROOT / brand / name).read_bytes())
                    self.assertEqual(png[:16], b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
                    side = 512 if "@2x" in name else 256
                    self.assertEqual(struct.unpack(">II", png[16:24]), (side, side))
                    self.assertEqual(png[25], 6)  # RGBA, including the transparent background
                self.assertFalse(any("brand-source" in name or "icon-concepts" in name for name in names))
                components = {name.split("/")[1] for name in names if name.startswith("custom_components/")}
                self.assertEqual(components, {"homepod_sensor_link"})
