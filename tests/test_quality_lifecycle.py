"""Quality behavior through HA services, entity publication, Repairs and reauth."""

from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

from homeassistant.helpers import issue_registry as ir
from homeassistant.config_entries import ConfigEntry

from custom_components.homepod_sensor_link import sensor
from custom_components.homepod_sensor_link.config_flow import HomePodSensorLinkFlow
from custom_components.homepod_sensor_link.credentials import DOMAIN, public_targets
from custom_components.homepod_sensor_link.engine import DeviceState
from custom_components.homepod_sensor_link.importer import parse_export
from custom_components.homepod_sensor_link.provisioning import make_owned
from custom_components.homepod_sensor_link.ui import KEY
import test_onboarding_v2 as fixtures
from test_recovery import fixture


class QualityLifecycleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.OnboardingTests.asyncSetUp
    asyncTearDown = fixtures.OnboardingTests.asyncTearDown
    make_runtime = fixtures.OnboardingTests.make_runtime
    flow_environment = fixtures.OnboardingTests.flow_environment
    finish_flow = fixtures.OnboardingTests.finish_flow

    def flow(self, config, source="reauth"):
        flow = HomePodSensorLinkFlow()
        flow.hass, flow.flow_id, flow.handler = self.hass, "flow", DOMAIN
        flow.context = {"source": source, "entry_id": config.entry_id}
        return flow

    async def test_adding_selected_devices_creates_entities_without_reloading_ready_peers(self):
        config, runtime = await self.make_runtime()
        add = Mock()
        unload = []
        try:
            with patch.object(ConfigEntry, "async_on_unload", side_effect=unload.append):
                await sensor.async_setup_entry(self.hass, config, add)
            self.assertEqual(len(add.call_args.args[0]), 3)
            other = fixture(1)[0]
            target = make_owned(
                other["accessory_id"], other["accessory_key"], {"AccessoryIP": "192.0.2.2", "AccessoryPort": 12345}
            )
            target.update(source=self.source, recovery=True)
            key = target["entry_id"]
            await self.vault.put(key, {"target": target, "name": "New pending device", "phase": "prepared"})
            runtime.engine.states[self.key].attempts = [10]
            data = {
                "device_keys": [self.key, key],
                "pins": {t["entry_id"]: public_targets([t])[0] for t in (self.target, target)},
                "admin_pins": {},
            }
            with patch.object(self.hass.config_entries, "async_schedule_reload") as reload:
                await runtime.accept_inventory(data)
                reload.assert_not_called()
            self.assertEqual(add.call_count, 2)
            self.assertEqual(len(add.call_args.args[0]), 3)
            self.assertEqual(runtime.engine.states[self.key].attempts, [10])
            runtime.coordinator.async_set_updated_data(runtime.engine.snapshot())
            self.assertEqual(add.call_count, 2)
            pending = {
                "target": {"entry_id": "incomplete", "accessory_id": "incomplete", "owned": True},
                "name": "Pending",
                "phase": "needs_credentials",
            }
            await self.vault.put("incomplete", pending)
            data["device_keys"].append("incomplete")
            await runtime.accept_inventory(data)
            self.assertNotIn("incomplete", [target["entry_id"] for target in runtime.engine.targets])
            changed = deepcopy(self.record)
            changed["target"]["ha_key"] = "invalid"
            await self.vault.put(self.key, changed)
            await runtime.accept_inventory(data)
            self.assertEqual(runtime.records[self.key]["phase"], "blocked")
        finally:
            for cancel in unload:
                cancel()
            await runtime.stop()

    async def test_repairs_survive_unload_and_clear_on_recovery_or_device_removal(self):
        config, runtime = await self.make_runtime()
        state = DeviceState(status="blocked", reason="identity_validation_failed")
        try:
            await runtime.notify(self.target, state, False)
            registry = ir.async_get(self.hass)
            issue = registry.async_get_issue(DOMAIN, "access_" + self.key)
            self.assertIsNotNone(issue)
            self.assertEqual(issue.config_entry_id, config.entry_id)
            self.assertEqual(issue.translation_key, "sensor_access")
            self.assertEqual(issue.severity, ir.IssueSeverity.ERROR)
            self.assertFalse(issue.is_fixable)
            await runtime.stop()
            self.assertIsNotNone(registry.async_get_issue(DOMAIN, "access_" + self.key))
            state.status = "healthy"
            await runtime.notify(self.target, state, True)
            self.assertIsNone(registry.async_get_issue(DOMAIN, "access_" + self.key))
            state.recovery_status = "rejected"
            with patch.object(ConfigEntry, "async_start_reauth") as start:
                await runtime.notify_access(self.target, state, False)
                start.assert_called_once_with(self.hass)
            self.assertIsNotNone(registry.async_get_issue(DOMAIN, "administrator_" + self.key))
            state.recovery_status = "verified"
            await runtime.notify_access(self.target, state, True)
            self.assertIsNone(registry.async_get_issue(DOMAIN, "administrator_" + self.key))
            state.status = "unreachable"
            await runtime.notify(self.target, state, False)
            await runtime.remove_device(self.key)
            self.assertIsNone(registry.async_get_issue(DOMAIN, "access_" + self.key))
        finally:
            await runtime.stop()

    async def test_reauthentication_reuses_both_pins_and_does_not_reset_recovery_history(self):
        config, runtime = await self.make_runtime()
        runtime.records[self.key].update(phase="active", commissioned=True)
        await self.vault.put(self.key, runtime.records[self.key])
        runtime.engine.states[self.key].attempts = [10, 20]
        try:
            flow = self.flow(config)
            self.assertEqual((await flow.async_step_reauth({}))["step_id"], "reauth_confirm")
            self.assertEqual((await flow.async_step_reauth_confirm())["reason"], "local_admin_required")
            async with self.flow_environment() as backend:
                self.assertEqual((await flow.async_step_reauth_confirm({}))["step_id"], "credentials")
                self.hass.data[KEY].pending["flow"]["data"] = parse_export(json.dumps(self.export))
                result = await self.finish_flow(flow, await flow.async_step_credentials({}))
                self.assertEqual(result["reason"], "reauth_successful")
                backend.restore_missing.assert_not_called()
            record = self.vault.records()[self.key]
            self.assertEqual(record["target"]["ha_id"], self.target["ha_id"])
            self.assertEqual(record["source_pin"], self.record["source_pin"])
            self.assertEqual(runtime.engine.states[self.key].attempts, [10, 20])
            runtime.records[self.key]["target"]["recovery"] = False
            runtime.records[self.key]["phase"] = "active"
            await self.vault.put(self.key, runtime.records[self.key])
            async with self.flow_environment():
                self.assertEqual(
                    (await self.flow(config).async_step_reauth_confirm({}))["reason"], "reauth_not_required"
                )
        finally:
            await runtime.stop()

    async def test_reauthentication_failure_is_not_success_when_entry_is_unloaded(self):
        config, runtime = await self.make_runtime()
        try:
            record = self.vault.records()[self.key]
            record["phase"] = "needs_credentials"
            await self.vault.put(self.key, record)
            for stopped in (False, True):
                runtime.stopped = stopped
                flow = self.flow(config)
                flow._task = self.hass.loop.create_future()
                flow._task.set_result([self.key])
                async with self.flow_environment():
                    self.assertEqual((await flow.async_step_finish())["reason"], "verification_failed")
        finally:
            await runtime.stop()

    async def test_reconfigure_cannot_erase_public_or_administrator_pins_from_incomplete_storage(self):
        config, runtime = await self.make_runtime()
        try:
            for field in ("ha_key", "source_pin"):
                record = self.vault.records()[self.key]
                if field == "ha_key":
                    original = record["target"].pop(field)
                else:
                    original = record.pop(field)
                    self.hass.config_entries.async_update_entry(
                        config, data={**config.data, "admin_pins": {self.key: original}}
                    )
                await self.vault.put(self.key, record)
                flow = self.flow(config, "reconfigure")
                flow._task = self.hass.loop.create_future()
                flow._task.set_result([self.key])
                async with self.flow_environment():
                    self.assertEqual((await flow.async_step_finish())["reason"], "verification_failed")
                if field == "ha_key":
                    record["target"][field] = original
                else:
                    record[field] = original
                await self.vault.put(self.key, record)
        finally:
            await runtime.stop()
