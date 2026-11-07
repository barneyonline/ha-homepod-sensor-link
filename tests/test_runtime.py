"""Run the coordinator, storage and diagnostic entities inside real HA classes."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import CoreState, HomeAssistant

from custom_components.homepod_sensor_link import Runtime, async_remove_entry
from custom_components.homepod_sensor_link.vault import DATA_FILE as KEY_FILE
from custom_components.homepod_sensor_link.diagnostics import async_get_config_entry_diagnostics
from custom_components.homepod_sensor_link.engine import AuthenticationFailure
from custom_components.homepod_sensor_link.sensor import RecoveryStatus

from test_recovery import fixture


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.hass = HomeAssistant(self.directory.name)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        self.hass.state = CoreState.running
        self.entry = ConfigEntry(
            domain="homepod_sensor_link",
            title="Recovery",
            data={},
            options={},
            source="user",
            unique_id="homepod_sensor_link",
            version=2,
            minor_version=2,
            discovery_keys={},
            subentries_data=None,
        )
        self.records = [fixture(i) for i in range(4)]
        self.targets = [t for t, _ in self.records]
        self.native_entries = {t["entry_id"]: e for t, e in self.records}
        self.lookup_patch = patch.object(
            self.hass.config_entries, "async_get_entry", side_effect=self.native_entries.get
        )
        self.lookup_patch.start()
        self.addCleanup(self.lookup_patch.stop)
        self.controller = SimpleNamespace(async_stop=AsyncMock())
        self.runtime = Runtime(self.hass, self.entry, self.targets, self.controller, None)
        self.runtime.backend = SimpleNamespace(
            pairing_present=AsyncMock(return_value=False),
            read_ha=AsyncMock(),
            restore_missing=AsyncMock(return_value=True),
            inspect_recovery=AsyncMock(
                side_effect=lambda t: {"entry_id": t["entry_id"], "recovery_admin_verified": True}
            ),
        )
        self.runtime.reload_entry = AsyncMock()
        self.runtime.notify = AsyncMock()
        self.runtime.verify_native = AsyncMock()
        self.runtime.notify_access = AsyncMock()
        await self.runtime.initialize()
        self.entry.runtime_data = self.runtime

    async def asyncTearDown(self):
        await self.runtime.stop()
        await self.hass.async_stop(force=True)

    async def test_real_coordinator_persists_public_state_only(self):
        await self.runtime.coordinator.async_refresh()
        data = json.loads((Path(self.directory.name) / ".storage/homepod_sensor_link.state").read_text())
        self.assertEqual(len(data["data"]), 4)
        self.assertTrue(all(s["status"] == "healthy" for s in data["data"].values()))
        diagnostic = await async_get_config_entry_diagnostics(self.hass, self.entry)
        public = json.dumps(diagnostic)
        for target in self.targets:
            self.assertNotIn(target["source"]["iOSDeviceLTSK"], public)
            self.assertNotIn(target["ha_key"], public)
            for identity in (
                target["entry_id"],
                target["accessory_id"],
                target["accessory_key"],
                target["ha_id"],
                target["source"]["iOSPairingId"],
            ):
                self.assertNotIn(identity, public)
        with patch.object(
            self.hass.config_entries,
            "async_get_entry",
            return_value=SimpleNamespace(title="Example HomePod Sensor Link"),
        ):
            sensor = RecoveryStatus(self.runtime, self.targets[0])
        self.assertEqual(sensor.native_value, "healthy")
        self.assertIsNotNone(sensor.extra_state_attributes["last_verified"])

    async def test_real_storage_survives_restart_after_failed_repair(self):
        engine = self.runtime.engine
        now = [0]
        engine.clock, engine.started = lambda: now[0], 0
        self.runtime.backend.read_ha.side_effect = AuthenticationFailure()
        self.runtime.backend.restore_missing.side_effect = OSError()
        for now[0] in (0, 300, 600):
            await engine.check_all()
        persisted = await self.runtime.store.async_load()
        self.assertTrue(all(s["attempts"] == [600] for s in persisted.values()))
        await self.runtime.initialize()
        self.assertTrue(all(s.attempts == [600] for s in self.runtime.engine.states.values()))

    async def test_removing_integration_deletes_owned_secret_and_preserves_native_storage(self):
        secret = Path(self.directory.name) / KEY_FILE
        secret.parent.mkdir(mode=0o700, parents=True)
        secret.write_text("dummy-secret")
        secret.chmod(0o600)
        unrelated = secret.parent.parent / "core.config_entries"
        unrelated.write_text("preserve-native-entries")
        await async_remove_entry(self.hass, self.entry)
        self.assertFalse(secret.exists())
        self.assertEqual(unrelated.read_text(), "preserve-native-entries")

    async def test_native_verification_requires_loaded_entry_and_two_numeric_entities(self):
        ids = ["sensor.test_temperature", "sensor.test_humidity"]
        self.runtime.native_sensor_ids = lambda _: ids
        native = SimpleNamespace(disabled_by=None, state=SimpleNamespace(value="loaded"))
        with patch.object(self.hass.config_entries, "async_get_entry", return_value=native):
            self.hass.states.async_set(ids[0], "22.5", {"device_class": "temperature"})
            self.hass.states.async_set(ids[1], "50", {"device_class": "humidity"})
            await Runtime.verify_native(self.runtime, self.targets[0])
            for value in ("unavailable", "unknown", "nan", "infinity"):
                self.hass.states.async_set(ids[0], value, {"device_class": "temperature"})
                with self.assertRaises(OSError):
                    await Runtime.verify_native(self.runtime, self.targets[0])
            self.hass.states.async_set(ids[0], "22.5", {"device_class": "temperature"})
            native.state.value = "setup_retry"
            with self.assertRaises(OSError):
                await Runtime.verify_native(self.runtime, self.targets[0])

    async def test_native_reload_requires_new_reports_from_actual_sensors(self):
        target, native = fixture()
        ids = ["sensor.test_temperature", "sensor.test_humidity"]
        self.runtime.native_sensor_ids = lambda _: ids

        async def reload(_):
            self.hass.states.async_set(ids[0], "22.5", {"device_class": "temperature"})
            self.hass.states.async_set(ids[1], "50", {"device_class": "humidity"})
            return True

        with (
            patch.object(self.hass.config_entries, "async_get_entry", return_value=native),
            patch.object(self.hass.config_entries, "async_reload", side_effect=reload),
        ):
            await Runtime.reload_entry(self.runtime, target)
        self.runtime.verify_native.assert_awaited_once_with(target)


if __name__ == "__main__":
    unittest.main()
