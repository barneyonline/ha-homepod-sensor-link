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


def dump_fixture(*, controllers=1, accessories=1):
    """Disposable bytes for format tests; native tests also verify actual key pairs."""
    bundle, blocks = {"controllers": [], "accessories": []}, []
    for number in range(controllers):
        public, seed = secrets.token_hex(32), secrets.token_hex(32)
        account = "synthetic-Controller-" + str(number)
        bundle["controllers"].append({"iOSPairingId": account, "iOSDeviceLTPK": public, "iOSDeviceLTSK": seed})
        blocks.append(
            "HomeKit Pairing Identity (com.apple.hap.pairing)\nAccount: " + account + "\nKey: " + public + seed
        )
    for number in range(accessories):
        account, public = "synthetic-Accessory-" + str(number), secrets.token_hex(32)
        bundle["accessories"].append({"AccessoryPairingID": account, "AccessoryLTPK": public})
        blocks.append("Paired HomeKit Accessory (com.apple.hap.pairing)\nAccount: " + account + "\nKey: " + public)
    return bundle, ("\n\n".join(blocks) + "\n").encode()


class WizardTests(unittest.TestCase):
    def test_private_reads_reject_a_fifo_without_waiting_for_a_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic-fifo"
            os.mkfifo(path, 0o600)
            original_open = os.open

            def nonblocking_open(path, flags):
                self.assertTrue(flags & os.O_NONBLOCK)
                return original_open(path, flags)

            with patch.object(wizard.os, "open", side_effect=nonblocking_open), self.assertRaises(wizard.WizardError):
                wizard.private_read(path)

    def test_private_reads_reject_growth_beyond_the_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic-file"
            wizard.private_write(path.parent, path.name, b"small")
            original_fstat = os.fstat

            def grow_after_stat(descriptor):
                info = original_fstat(descriptor)
                with path.open("ab") as stream:
                    stream.write(b" beyond the limit")
                return info

            with patch.object(wizard.os, "fstat", side_effect=grow_after_stat), self.assertRaises(wizard.WizardError):
                wizard.private_read(path, limit=5)

    def test_snapshot_requires_directory_sync_after_saving_original_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary).resolve()
            expected = saved_state()

            def sync_saved_snapshot(location):
                self.assertEqual(location, directory)
                self.assertEqual(wizard.load_state(location), expected)
                raise OSError("synthetic directory sync failure")

            with (
                patch.object(wizard, "check_initial"),
                patch.object(wizard, "boot_arguments", return_value=(None, b"")),
                patch.object(wizard, "sync_directory", side_effect=sync_saved_snapshot) as sync,
                self.assertRaises(OSError),
            ):
                wizard.snapshot(directory)
            sync.assert_called_once_with(directory)
            self.assertEqual(wizard.load_state(directory), expected)

    def test_prepared_tool_must_remain_in_its_private_source_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            directory = wizard.private_directory(root / "private")
            source = directory / "build.synthetic" / "HomeKitExport"
            binary = source / "homekit-export"
            binary.parent.mkdir(parents=True)
            binary.write_text("synthetic fixture, never executed")
            binary.chmod(0o700)
            wizard.private_write(
                source,
                "HomeKitExport.entitlements",
                plistlib.dumps({"keychain-access-groups": ["com.apple.hap.pairing"]}),
            )
            wizard.private_write(directory, "tool-source.txt", str(source).encode())
            self.assertEqual(wizard.tool_source(directory), source)
            wizard.private_write(
                source, "HomeKitExport.entitlements", plistlib.dumps({"keychain-access-groups": ["*"]})
            )
            with self.assertRaises(wizard.WizardError):
                wizard.tool_source(directory)
            wizard.private_write(
                source,
                "HomeKitExport.entitlements",
                plistlib.dumps({"keychain-access-groups": ["com.apple.hap.pairing"]}),
            )
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

    def test_private_dump_is_parsed_without_prompting_for_or_echoing_keys(self):
        bundle, dump = dump_fixture()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(wizard.parse_dump(dump), bundle)
            self.assertEqual(wizard.parse_dump(dump.replace(b"\n", b"\r\n")), bundle)
            self.assertEqual(wizard.parse_dump(dump + b"\n" + dump), bundle)
        self.assertEqual(output.getvalue(), "")
        for invalid in ("64 bytes (binary)", "not-a-key", secrets.token_hex(31)):
            with self.assertRaises(wizard.WizardError):
                wizard.hexadecimal(invalid, 64)
        for invalid in ("", "line\nbreak", "hidden\x7fcontrol", "x" * 257):
            with self.assertRaises(wizard.WizardError):
                wizard.identifier(invalid)

    def test_incomplete_unrelated_oversized_and_conflicting_dumps_are_refused(self):
        bundle, dump = dump_fixture()
        first = bundle["controllers"][0]
        accessory = bundle["accessories"][0]
        for invalid in (
            b"",
            b"\xff",
            b"x" * 1048577,
            dump * 257,
            dump.replace(b"com.apple.hap.pairing", b"unrelated.group"),
            dump.replace(b"Account:", b"Unexpected:"),
            dump.replace(first["iOSDeviceLTSK"].encode(), b"32 bytes (binary)"),
            dump.replace(b"Key:", b"Key: \nExtra:"),
            dump_fixture(controllers=0)[1],
            dump_fixture(accessories=0)[1],
            dump + b"\n" + dump.replace(accessory["AccessoryLTPK"].encode(), secrets.token_hex(32).encode()),
            dump
            + b"\n"
            + dump.replace(accessory["AccessoryPairingID"].encode(), accessory["AccessoryPairingID"].upper().encode()),
        ):
            with self.subTest(size=len(invalid)), self.assertRaises(wizard.WizardError) as error:
                wizard.parse_dump(invalid)
            self.assertNotIn(first["iOSDeviceLTSK"], str(error.exception))

    def test_numbered_selection_keeps_keys_hidden_and_handles_single_records(self):
        bundle, _ = dump_fixture(accessories=2)
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(
                wizard.select_records(bundle["accessories"], "Accessories", "AccessoryPairingID", 64, lambda _: "2"),
                bundle["accessories"][1:],
            )
            self.assertEqual(
                wizard.select_records(bundle["accessories"], "Accessories", "AccessoryPairingID", 64, lambda _: "all"),
                bundle["accessories"],
            )
            self.assertEqual(
                wizard.select_records(
                    bundle["accessories"], "Accessories", "AccessoryPairingID", 64, lambda _: "2,1,2"
                ),
                bundle["accessories"],
            )
            with patch("builtins.input", side_effect=AssertionError("Single records need no selection prompt")):
                self.assertEqual(
                    wizard.select_records(bundle["controllers"], "Controllers", "iOSPairingId", 3),
                    bundle["controllers"],
                )
        for record in bundle["accessories"]:
            self.assertIn(record["AccessoryPairingID"], output.getvalue())
            self.assertNotIn(record["AccessoryLTPK"], output.getvalue())
        for invalid in ("", "zero", "0", "3", "-1", "all"):
            with redirect_stdout(io.StringIO()), self.assertRaises(wizard.WizardError):
                wizard.select_records(bundle["accessories"], "Accessories", "AccessoryPairingID", 1, lambda _: invalid)
        too_many, _ = dump_fixture(controllers=4)
        with redirect_stdout(io.StringIO()), self.assertRaises(wizard.WizardError):
            wizard.select_records(too_many["controllers"], "Controllers", "iOSPairingId", 3, lambda _: "all")

    def test_invalid_dump_never_creates_an_import_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = wizard.private_directory(Path(temporary).resolve() / "private")
            wizard.private_write(directory, "homekit-keys.txt", b"Failed extraction; no usable records.")
            with (
                patch.object(wizard, "check_restored"),
                redirect_stdout(io.StringIO()),
                self.assertRaises(wizard.WizardError),
            ):
                wizard.export_credentials(directory, saved_state(phase="restore"))
            self.assertFalse(list(directory.glob("homepod-import-*.json")))

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
        bundle, dump = dump_fixture(accessories=2)
        selected = {**bundle, "accessories": bundle["accessories"][1:]}
        with tempfile.TemporaryDirectory() as temporary:
            directory = wizard.private_directory(Path(temporary).resolve() / "private")
            wizard.private_write(directory, "homekit-keys.txt", dump)
            wizard.private_write(directory, "unrelated-certificate.txt", b"synthetic certificate placeholder")
            state = saved_state(phase="restore")
            with (
                patch.object(wizard, "check_restored", side_effect=wizard.WizardError("locked")),
                patch.object(wizard, "private_read") as read,
                self.assertRaises(wizard.WizardError),
            ):
                wizard.export_credentials(directory, state)
            read.assert_not_called()
            with (
                patch.object(wizard, "check_restored"),
                redirect_stdout(io.StringIO()) as output,
            ):
                wizard.export_credentials(directory, state, lambda _: "2")
                wizard.completed_export(directory, wizard.load_state(directory))
            files = list(directory.glob("homepod-import-*.json"))
            self.assertEqual(len(files), 1)
            for path in files:
                self.assertEqual(json.loads(wizard.private_read(path)), selected)
            self.assertEqual(wizard.load_state(directory)["phase"], "complete")
            self.assertNotIn(bundle["controllers"][0]["iOSDeviceLTSK"], output.getvalue())
            self.assertFalse((directory / "homekit-keys.txt").exists())
            self.assertEqual(
                wizard.private_read(directory / "unrelated-certificate.txt"), b"synthetic certificate placeholder"
            )

    def test_failed_write_state_save_or_readback_keeps_the_original_dump(self):
        for failure in ("write", "state", "directory", "readback"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                directory = wizard.private_directory(Path(temporary).resolve() / "private")
                _, dump = dump_fixture()
                wizard.private_write(directory, "homekit-keys.txt", dump)
                original_read = wizard.private_read

                def readback(path, limit=131072):
                    data = original_read(path, limit)
                    return b"mismatched JSON" if Path(path).name.startswith("homepod-import-") else data

                targets = {
                    "write": patch.object(wizard.os, "fsync", side_effect=OSError("synthetic write failure")),
                    "state": patch.object(wizard, "save_state", side_effect=OSError("synthetic state failure")),
                    "directory": patch.object(
                        wizard, "sync_directory", side_effect=OSError("synthetic directory sync failure")
                    ),
                    "readback": patch.object(wizard, "private_read", side_effect=readback),
                }
                with (
                    patch.object(wizard, "check_restored"),
                    targets[failure],
                    redirect_stdout(io.StringIO()),
                    self.assertRaises((OSError, wizard.WizardError)),
                ):
                    wizard.export_credentials(directory, saved_state(phase="restore"))
                self.assertEqual(wizard.private_read(directory / "homekit-keys.txt"), dump)

    def test_cleanup_retry_checks_both_files_and_reuses_the_completed_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = wizard.private_directory(Path(temporary).resolve() / "private")
            _, dump = dump_fixture()
            wizard.private_write(directory, "homekit-keys.txt", dump)
            with (
                patch.object(wizard, "check_restored"),
                patch.object(Path, "unlink", side_effect=OSError("synthetic cleanup failure")),
                redirect_stdout(io.StringIO()),
                self.assertRaises(wizard.WizardError),
            ):
                wizard.export_credentials(directory, saved_state(phase="restore"))
            state = wizard.load_state(directory)
            path = directory / state["export_file"]
            original_json = wizard.private_read(path)
            self.assertEqual(wizard.private_read(directory / "homekit-keys.txt"), dump)
            wizard.private_write(directory, path.name, b"changed JSON")
            with patch.object(wizard, "check_restored"), self.assertRaises(wizard.WizardError):
                wizard.completed_export(directory, state)
            self.assertTrue((directory / "homekit-keys.txt").exists())
            wizard.private_write(directory, path.name, original_json)
            wizard.private_write(directory, "homekit-keys.txt", dump + b"\n")
            with patch.object(wizard, "check_restored"), self.assertRaises(wizard.WizardError):
                wizard.completed_export(directory, state)
            self.assertTrue((directory / "homekit-keys.txt").exists())
            wizard.private_write(directory, "homekit-keys.txt", dump)
            with patch.object(wizard, "check_restored"), redirect_stdout(io.StringIO()):
                self.assertEqual(wizard.completed_export(directory, state), path)
            self.assertFalse((directory / "homekit-keys.txt").exists())
            self.assertEqual(wizard.private_read(path), original_json)
            self.assertEqual(len(list(directory.glob("homepod-import-*.json"))), 1)
            wizard.private_write(
                directory, wizard.STATE_FILE, json.dumps({**state, "export_file": "../elsewhere.json"}).encode()
            )
            with self.assertRaises(wizard.WizardError):
                wizard.load_state(directory)
