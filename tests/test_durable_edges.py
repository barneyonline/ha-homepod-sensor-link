"""Drain-on-cancel and retry guards across persisted onboarding transactions."""

import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from custom_components.homepod_sensor_link import vault as vault_module
from custom_components.homepod_sensor_link.coordinator import validate_state_location
from custom_components.homepod_sensor_link.engine import AuthenticationFailure, RecoveryPaused, SafetyFailure
from custom_components.homepod_sensor_link.importer import parse_export
from custom_components.homepod_sensor_link.provisioning import prepare
import test_onboarding_v2 as fixtures
from test_recovery import keypair


class DurableEdgeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.OnboardingTests.asyncSetUp
    asyncTearDown = fixtures.OnboardingTests.asyncTearDown

    async def test_cancelled_write_drains_before_purge_can_acquire_lock(self):
        entered, release = threading.Event(), threading.Event()
        write = vault_module._write

        def blocked(*args):
            entered.set()
            release.wait(5)
            write(*args)

        with patch.object(vault_module, "_write", blocked):
            task = asyncio.create_task(self.vault.put(self.key, self.record))
            while not entered.is_set():
                await asyncio.sleep(0)
            task.cancel()
            purge = asyncio.create_task(self.vault.purge())
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(purge.done())
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await purge
        self.assertFalse((Path(self.directory.name) / vault_module.DATA_FILE).exists())
        self.assertEqual(self.vault.records(), {})

    async def test_cancelled_purge_drains_and_cannot_restore_old_memory(self):
        await self.vault.put(self.key, self.record)
        entered, release = threading.Event(), threading.Event()
        fsync = os.fsync

        def delayed(fd):
            entered.set()
            release.wait(5)
            fsync(fd)

        with patch.object(vault_module.os, "fsync", delayed):
            task = asyncio.create_task(self.vault.purge())
            while not entered.is_set():
                await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.vault.records(), {})
        self.assertFalse((Path(self.directory.name) / vault_module.DATA_FILE).exists())
        await self.vault.purge()  # repeat removal with no directory is safe

    async def test_pause_only_matching_unfinished_records_and_preserves_existing_pause(self):
        self.record.update(flow_id="flow", cancelled=True)
        self.record.pop("authorised_boot", None)
        await self.vault.put(self.key, self.record)
        for key, flow, phase in (("active", "flow", "active"), ("foreign", "other", "prepared")):
            record = deepcopy(self.record)
            record.update(flow_id=flow, phase=phase, cancelled=False)
            await self.vault.put(key, record)
        await self.vault.pause_flow("flow")
        self.assertTrue(self.vault.records()[self.key]["cancelled"])
        self.assertFalse(self.vault.records()["active"]["cancelled"])
        self.assertFalse(self.vault.records()["foreign"]["cancelled"])

    async def test_purge_refuses_foreign_owner_and_storage_symlink(self):
        with tempfile.TemporaryDirectory() as other:
            storage = Path(self.directory.name) / ".storage"
            storage.mkdir(exist_ok=True)
            private = storage / "homepod_sensor_link"
            private.symlink_to(other)
            with self.assertRaises(SafetyFailure):
                await self.vault.purge()
            private.unlink()
        await self.vault.put(self.key, self.record)
        fstat = os.fstat

        def foreign(fd):
            info = fstat(fd)
            return SimpleNamespace(st_uid=info.st_uid + 1)

        with patch.object(vault_module.os, "fstat", foreign), self.assertRaises(SafetyFailure):
            await self.vault.purge()
        self.assertIn(self.key, self.vault.records())

    async def test_legacy_fallback_state_location_does_not_follow_symlink_or_accept_public_mode(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other:
            storage = Path(directory) / ".storage"
            storage.symlink_to(other)
            with self.assertRaises(SafetyFailure):
                validate_state_location(directory)
            storage.unlink()
            storage.mkdir()
            file = storage / "homepod_sensor_link.state"
            file.write_text("{}")
            file.chmod(0o644)
            with self.assertRaises(SafetyFailure):
                validate_state_location(directory)
            file.chmod(0o600)
            validate_state_location(directory)

    async def test_retry_reuses_known_identity_and_partial_stub_key(self):
        await self.vault.put(self.key, self.record)
        found = {
            self.target["accessory_id"]: {
                "name": "Device",
                "endpoint": {"AccessoryIP": "192.0.2.1", "AccessoryPort": 12345},
            }
        }
        imported = parse_export(json.dumps(self.export))
        backend = SimpleNamespace(
            read_ha=AsyncMock(return_value={"temperature": 20, "humidity": 40}), inspect_recovery=AsyncMock()
        )
        keys = await prepare(self.hass, self.vault, backend, list(found), list(found), imported, found)
        self.assertEqual(keys, [self.key])
        backend.inspect_recovery.assert_not_called()
        self.record["phase"] = "needs_credentials"
        await self.vault.put(self.key, self.record)
        source = {**self.source, "iOSPairingId": self.target["ha_id"]}
        imported["controllers"] = [source, self.source]
        await prepare(self.hass, self.vault, backend, list(found), list(found), imported, found)
        self.assertEqual(backend.inspect_recovery.await_count, 1)
        self.assertEqual(self.vault.records()[self.key]["target"]["ha_id"], self.target["ha_id"])
        stub = {
            "target": {"entry_id": self.key, "accessory_id": self.target["accessory_id"], "owned": True},
            "phase": "needs_credentials",
        }
        await self.vault.put(self.key, stub)
        keys = await prepare(self.hass, self.vault, backend, list(found), list(found), imported, found)
        self.assertEqual(keys, [self.key])

    async def test_import_cannot_replace_source_pin_and_unmatched_source_stays_pending(self):
        self.record["phase"] = "needs_credentials"
        await self.vault.put(self.key, self.record)
        backend = SimpleNamespace(
            read_ha=AsyncMock(return_value={"temperature": 20, "humidity": 40}), inspect_recovery=AsyncMock()
        )
        found = {self.target["accessory_id"]: {"name": "Device"}}
        seed, public = keypair()
        imported = {
            "controllers": [{"iOSPairingId": "other-controller", "iOSDeviceLTSK": seed, "iOSDeviceLTPK": public}]
        }
        with self.assertRaises(SafetyFailure):
            await prepare(self.hass, self.vault, backend, list(found), list(found), imported, found)
        backend.inspect_recovery.side_effect = SafetyFailure()
        keys = await prepare(self.hass, self.vault, backend, list(found), list(found), imported, found)
        self.assertEqual(self.vault.records()[keys[0]]["phase"], "needs_credentials")
        self.assertNotIn("source", self.vault.records()[keys[0]]["target"])

    async def test_read_only_inspection_safety_network_and_missing_admin_never_write(self):
        for error, expected in ((SafetyFailure(), "identity_validation_failed"), (OSError(), "device_unreachable")):
            await self.vault.put(self.key, self.record)
            self.backend.read_ha.side_effect = error
            record, values = await self.provisioner.inspect(self.key)
            self.assertEqual(record["error"], expected)
            self.assertIsNone(values)
        self.record["target"].pop("source")
        await self.vault.put(self.key, self.record)
        self.backend.read_ha.side_effect = AuthenticationFailure()
        record, _ = await self.provisioner.inspect(self.key, resume=True)
        self.assertEqual(record["error"], "resume_required")
        self.backend.restore_missing.assert_not_called()
        self.record["target"]["source"] = self.source
        await self.vault.put(self.key, self.record)
        self.backend.restore_missing.side_effect = RecoveryPaused()
        record, _ = await self.provisioner.inspect(self.key)
        self.assertEqual(record["error"], "competing_recovery")

    async def test_conflicting_valid_controller_rows_are_rejected(self):
        seed, public = keypair()
        export = deepcopy(self.export)
        export["controllers"].append({**self.source, "iOSDeviceLTSK": seed, "iOSDeviceLTPK": public})
        with self.assertRaises(SafetyFailure):
            parse_export(json.dumps(export))
