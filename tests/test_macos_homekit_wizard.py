"""Synthetic checks for wizard restoration gates and private credential handling."""

import base64
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import plistlib
import secrets
import stat
from subprocess import CompletedProcess
import tempfile
import unittest
from unittest.mock import patch

from scripts import macos_homekit_export as wizard


def saved_state(original=None, phase="extract", *, binary=False):
    value = original.encode() if binary and original is not None else original
    raw = plistlib.dumps({"boot-args": value}) if original is not None else b""
    return {"phase": phase, "original": original, "plist": base64.b64encode(raw).decode()}


class WizardTests(unittest.TestCase):
    def test_prepared_tool_must_remain_in_its_private_source_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            directory = wizard.private_directory(root / "private")
            source = directory / "build.synthetic" / "KeychainKit"
            binary = source / ".build/debug/KeychainTool"
            binary.parent.mkdir(parents=True)
            binary.write_text("synthetic fixture, never executed")
            binary.chmod(0o700)
            wizard.private_write(directory, "tool-source.txt", str(source).encode())
            self.assertEqual(wizard.tool_source(directory), source)
            binary.chmod(0o600)
            with self.assertRaises(wizard.WizardError):
                wizard.tool_source(directory)
            binary.unlink()
            outside = root / "outside"
            outside.write_text("synthetic fixture, never executed")
            outside.chmod(0o700)
            binary.symlink_to(outside)
            with self.assertRaises(wizard.WizardError):
                wizard.tool_source(directory)

    def test_unrecognised_or_unreadable_security_status_blocks_progress(self):
        for sip, kernel in (
            (
                CompletedProcess([], 0, b"System Integrity Protection status: enabled.", b""),
                CompletedProcess([], 0, b"", b""),
            ),
            (CompletedProcess([], 0, b"custom configuration", b""), CompletedProcess([], 0, b"", b"")),
            (CompletedProcess([], 1, b"", b""), CompletedProcess([], 0, b"", b"")),
            (
                CompletedProcess([], 0, b"System Integrity Protection status: enabled.", b""),
                CompletedProcess([], 1, b"", b""),
            ),
        ):
            with patch.object(wizard, "system_command", side_effect=(sip, kernel)):
                if sip.returncode == 0 and kernel.returncode == 0 and b"enabled." in sip.stdout:
                    self.assertEqual(wizard.security_status(), ("enabled", []))
                else:
                    with self.assertRaises(wizard.WizardError):
                        wizard.security_status()

    def test_boot_argument_reads_distinguish_absence_errors_and_xml_types(self):
        for original in ("", "existing=setting", b"existing=setting"):
            raw = plistlib.dumps({"boot-args": original})
            with patch.object(wizard, "system_command", return_value=CompletedProcess([], 0, raw, b"")):
                value, saved = wizard.boot_arguments()
                self.assertEqual(value, original.decode() if isinstance(original, bytes) else original)
                self.assertEqual(saved, raw)
        for code, output, error, absent in (
            (1, b"", b"(iokit/common) data was not found", True),
            (1, b"", b"permission denied", False),
            (0, b"broken xml", b"", False),
            (0, plistlib.dumps({"unrelated": "setting"}), b"", False),
            (0, plistlib.dumps({"boot-args": b"bad\x00setting"}), b"", False),
        ):
            with patch.object(wizard, "system_command", return_value=CompletedProcess([], code, output, error)):
                if absent:
                    self.assertEqual(wizard.boot_arguments(), (None, b""))
                else:
                    with self.assertRaises(wizard.WizardError):
                        wizard.boot_arguments()

    def test_initial_checks_reject_existing_security_overrides(self):
        for arguments, sip, kernel in (
            (None, "disabled", []),
            (wizard.TEMPORARY_ARGUMENT, "enabled", []),
            (None, "enabled", [wizard.TEMPORARY_ARGUMENT]),
        ):
            with (
                patch.object(wizard, "boot_arguments", return_value=(arguments, b"")),
                patch.object(wizard, "security_status", return_value=(sip, kernel)),
                self.assertRaises(wizard.WizardError),
            ):
                wizard.check_initial()

    def test_snapshot_is_private_and_cannot_be_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = wizard.private_directory(Path(temporary).resolve() / "private")
            original = b"existing=setting"
            raw = plistlib.dumps({"boot-args": original})
            with (
                patch.object(wizard, "boot_arguments", return_value=(original.decode(), raw)),
                patch.object(wizard, "security_status", return_value=("enabled", [])),
            ):
                wizard.snapshot(directory)
                state = wizard.load_state(directory)
                with self.assertRaises(wizard.WizardError):
                    wizard.snapshot(directory)
            self.assertEqual(base64.b64decode(state["plist"]), raw)
            self.assertEqual(stat.S_IMODE((directory / wizard.STATE_FILE).stat().st_mode), 0o600)
            output = io.StringIO()
            with redirect_stdout(output):
                wizard.restoration_instructions(directory, state)
            self.assertIn("nvram -x -f", output.getvalue())
            self.assertEqual(plistlib.loads((directory / "boot-args-before.plist").read_bytes())["boot-args"], original)
            with redirect_stdout(output):
                wizard.restoration_instructions(directory, saved_state())
            self.assertIn("nvram -d boot-args", output.getvalue())

    def test_extraction_requires_both_stored_and_running_temporary_settings(self):
        for original in (None, "", "existing=setting"):
            expected = (original + " " if original is not None else "") + wizard.TEMPORARY_ARGUMENT
            for sip, kernel, arguments, phase, accepted in (
                ("disabled", [wizard.TEMPORARY_ARGUMENT], expected, "extract", True),
                ("disabled", [], expected, "extract", False),
                ("enabled", [wizard.TEMPORARY_ARGUMENT], expected, "extract", False),
                ("disabled", [wizard.TEMPORARY_ARGUMENT], "different", "extract", False),
                ("disabled", [wizard.TEMPORARY_ARGUMENT], expected, "restore", False),
            ):
                with (
                    patch.object(wizard, "boot_arguments", return_value=(arguments, b"")),
                    patch.object(wizard, "security_status", return_value=(sip, kernel)),
                ):
                    if accepted:
                        wizard.check_extraction(saved_state(original, phase))
                    else:
                        with self.assertRaises(wizard.WizardError):
                            wizard.check_extraction(saved_state(original, phase))

    def test_import_stays_locked_until_restoration_and_restart(self):
        for original in (None, "", "existing=setting"):
            state = saved_state(original, "restore", binary=True)
            raw = base64.b64decode(state["plist"])
            for sip, kernel, current, accepted in (
                ("enabled", [], original, True),
                ("disabled", [], original, False),
                ("enabled", [wizard.TEMPORARY_ARGUMENT], original, False),
                ("enabled", [], "changed", False),
            ):
                with (
                    patch.object(wizard, "boot_arguments", return_value=(current, raw)),
                    patch.object(wizard, "security_status", return_value=(sip, kernel)),
                ):
                    if accepted:
                        wizard.check_restored(state)
                    else:
                        with self.assertRaises(wizard.WizardError):
                            wizard.check_restored(state)
        with (
            patch.object(wizard, "boot_arguments", return_value=("", plistlib.dumps({"boot-args": ""}))),
            patch.object(wizard, "security_status", return_value=("enabled", [])),
            self.assertRaises(wizard.WizardError),
        ):
            wizard.check_restored(saved_state("", binary=True))

    def test_private_storage_rejects_links_loose_permissions_and_invalid_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            directory = wizard.private_directory(root / "private")
            linked = root / "linked"
            linked.symlink_to(directory, target_is_directory=True)
            with self.assertRaises(wizard.WizardError):
                wizard.private_directory(linked)
            directory.chmod(0o755)
            with self.assertRaises(wizard.WizardError):
                wizard.private_directory(directory)
            directory.chmod(0o700)
            target = directory / "record"
            target.write_text("private synthetic data")
            target.chmod(0o644)
            with self.assertRaises(wizard.WizardError):
                wizard.private_read(target)
            target.chmod(0o600)
            (directory / "linked-record").symlink_to(target)
            with self.assertRaises(OSError):
                wizard.private_read(directory / "linked-record")
            os.link(target, directory / "hard-link")
            with self.assertRaises(wizard.WizardError):
                wizard.private_read(target)
            wizard.private_write(directory, wizard.STATE_FILE, b'{"phase": "complete"}')
            with self.assertRaises(wizard.WizardError):
                wizard.load_state(directory)

    def test_selected_keys_are_normalised_without_echoing_them(self):
        public, seed, accessory = (secrets.token_hex(32) for _ in range(3))
        values = iter(
            ("Synthetic-Controller", "“" + public + "+" + seed + "”", "Synthetic-Accessory", "0x" + accessory)
        )
        output = io.StringIO()
        with redirect_stdout(output):
            bundle = wizard.collect_credentials(secret=lambda _prompt: next(values), prompt=lambda _prompt: "1")
        self.assertEqual(bundle["controllers"][0]["iOSDeviceLTSK"], seed)
        self.assertEqual(bundle["controllers"][0]["iOSDeviceLTPK"], public)
        self.assertEqual(bundle["accessories"][0]["AccessoryLTPK"], accessory)
        for value in (public, seed, accessory):
            self.assertNotIn(value, output.getvalue())
        for invalid in ("64 bytes (binary)", "not-a-key", seed[:-1]):
            with self.assertRaises(wizard.WizardError):
                wizard.hexadecimal(invalid, 64)
        for invalid in ("", "line\nbreak", "x" * 257):
            with self.assertRaises(wizard.WizardError):
                wizard.identifier(invalid)
        for invalid in ("zero", "0", "4"):
            with self.assertRaises(wizard.WizardError):
                wizard.collect_credentials(prompt=lambda _prompt: invalid)

    def test_cli_keeps_format_errors_and_export_input_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve() / "private"
            with (
                patch("sys.argv", ["helper", "status", str(directory)]),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(wizard.main(), 0)
            self.assertEqual(output.getvalue().strip(), "new")
            wizard.save_state(directory, saved_state(phase="restore"))
            with (
                patch("sys.argv", ["helper", "export", str(directory)]),
                patch("sys.stdin.isatty", return_value=False),
                patch.object(wizard, "export_credentials") as export,
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(wizard.main(), 1)
            export.assert_not_called()
            self.assertIn("interactively", output.getvalue())

    def test_export_checks_restoration_before_prompting_and_writes_private_json(self):
        bundle = {
            "controllers": [{"iOSPairingId": "synthetic-controller", "iOSDeviceLTSK": secrets.token_hex(32)}],
            "accessories": [{"AccessoryPairingID": "synthetic-accessory", "AccessoryLTPK": secrets.token_hex(32)}],
        }
        with tempfile.TemporaryDirectory() as temporary:
            directory = wizard.private_directory(Path(temporary).resolve() / "private")
            state = saved_state(phase="restore")
            with (
                patch.object(wizard, "check_restored", side_effect=wizard.WizardError("locked")),
                patch.object(wizard, "collect_credentials") as collect,
                self.assertRaises(wizard.WizardError),
            ):
                wizard.export_credentials(directory, state)
            collect.assert_not_called()
            with (
                patch.object(wizard, "check_restored"),
                patch.object(wizard, "collect_credentials", return_value=bundle),
                redirect_stdout(io.StringIO()) as output,
            ):
                wizard.export_credentials(directory, state)
                wizard.export_credentials(directory, state)
            files = list(directory.glob("homepod-import-*.json"))
            self.assertEqual(len(files), 2)
            for path in files:
                self.assertEqual(json.loads(wizard.private_read(path)), bundle)
            self.assertEqual(wizard.load_state(directory)["phase"], "complete")
            self.assertNotIn(bundle["controllers"][0]["iOSDeviceLTSK"], output.getvalue())
