"""Owned controller isolation and explicit transition, using disposable records."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.helpers import issue_registry as ir

from custom_components import homepod_sensor_link as integration
from custom_components.homepod_sensor_link import config_flow, onboarding, sensor
from custom_components.homepod_sensor_link.backend import HomeKitBackend, check_current
from custom_components.homepod_sensor_link.credentials import DOMAIN, public_targets, validate_target
from custom_components.homepod_sensor_link.engine import DeviceState, SafetyFailure
from custom_components.homepod_sensor_link.provisioning import prepare
from custom_components.homepod_sensor_link.vault import Vault
import test_onboarding_v2 as fixtures
import test_flow_lifecycle as lifecycle
from test_recovery import fixture, keypair


class ControllerIsolationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.OnboardingTests.asyncSetUp
    asyncTearDown = fixtures.OnboardingTests.asyncTearDown
    make_runtime = fixtures.OnboardingTests.make_runtime
    flow_environment = fixtures.OnboardingTests.flow_environment
    finish_flow = fixtures.OnboardingTests.finish_flow
    flow = lifecycle.FlowLifecycleTests.flow

    async def legacy_inventory(self, *, include_owned=True):
        legacy, old = fixture(1)
        legacy.pop("owned")
        legacy.pop("pairing")
        legacy["ha_permissions"] = 1
        key = legacy["entry_id"]
        record = {
            "target": legacy,
            "name": "Previously adopted HomePod",
            "phase": "active",
            "source_pin": {"old": True},
        }
        await self.vault.put(key, record)
        if include_owned:
            self.record["phase"] = "active"
            await self.vault.put(self.key, self.record)
        records = self.vault.records()
        config = fixtures.entry(
            {
                "device_keys": list(records),
                "pins": {key: public_targets([r["target"]])[0] for key, r in records.items()},
                "admin_pins": {key: r.get("source_pin", {}) for key, r in records.items()},
            }
        )
        self.hass.config_entries._entries[config.entry_id] = config
        self.hass.config_entries.async_update_entry(config, version=2)
        return config, key, legacy, old

    async def test_migration_preserves_owned_keys_and_never_reads_or_reloads_native_entries(self):
        config, key, legacy, _ = await self.legacy_inventory()
        owned = deepcopy(self.vault.record(self.key))
        await self.vault.save_states(
            {key: {"attempts": [1], "repairs": 2, "native_pending": True}, self.key: {"attempts": [2]}}
        )
        core = Path(self.directory.name) / ".storage/core.config_entries"
        core.write_text("unrelated synthetic native storage")
        with (
            patch.object(self.hass.config_entries, "async_entries", side_effect=AssertionError("foreign lookup")),
            patch.object(self.hass.config_entries, "async_get_entry", side_effect=AssertionError("foreign lookup")),
            patch.object(self.hass.config_entries, "async_reload", AsyncMock()) as reload,
            patch.object(HomeKitBackend, "session", side_effect=AssertionError("network session")),
        ):
            self.assertTrue(await integration.async_migrate_entry(self.hass, config))
        reload.assert_not_called()
        self.assertEqual(config.version, 3)
        self.assertEqual(self.vault.record(self.key), owned)
        self.assertEqual(config.data["pins"][self.key], public_targets([self.target])[0])
        self.assertNotIn(key, config.data["pins"])
        self.assertNotIn(key, config.data["admin_pins"])
        paused = self.vault.record(key)
        self.assertEqual(paused["phase"], "transition_required")
        self.assertEqual(paused["onboarding_attempts"], [1])
        self.assertNotIn("source_pin", paused)
        self.assertNotIn(legacy["ha_id"], json.dumps(paused))
        self.assertEqual(self.vault.data["states"][key], {"attempts": [1], "repairs": 2})
        self.assertEqual(core.read_text(), "unrelated synthetic native storage")
        self.assertEqual((await Vault(self.hass).load()).record(key), paused)

    async def test_interrupted_config_migration_is_idempotent_and_unknown_versions_fail(self):
        config, key, _, _ = await self.legacy_inventory(include_owned=False)
        with patch.object(self.hass.config_entries, "async_update_entry", side_effect=OSError("update interrupted")):
            with self.assertRaises(OSError):
                await integration.async_migrate_entry(self.hass, config)
        paused = self.vault.record(key)
        self.assertEqual(config.version, 2)
        self.assertTrue(await integration.async_migrate_entry(self.hass, config))
        self.assertEqual(self.vault.record(key), paused)
        self.assertFalse(await integration.async_migrate_entry(self.hass, config))

    async def test_migration_write_failure_keeps_config_and_previous_journal(self):
        config, _, _, _ = await self.legacy_inventory()
        before = deepcopy(self.vault.data)
        with patch.object(self.vault, "_commit", AsyncMock(side_effect=OSError())):
            with self.assertRaises(OSError):
                await integration.async_migrate_entry(self.hass, config)
        self.assertEqual(config.version, 2)
        self.assertEqual(self.vault.data, before)

    async def test_paused_devices_do_not_probe_and_cannot_resume_or_enable_recovery(self):
        config, key, _, _ = await self.legacy_inventory(include_owned=False)
        await integration.async_migrate_entry(self.hass, config)
        target = self.vault.record(key)["target"]
        runtime = integration.Runtime(
            self.hass, config, [target], SimpleNamespace(async_stop=AsyncMock()), None, self.vault
        )
        runtime.backend = self.backend
        await runtime.initialize()
        config.runtime_data = runtime
        try:
            self.assertEqual(runtime.engine.targets, [])
            self.assertEqual(runtime.monitored_devices(), set())
            self.assertIsNotNone(ir.async_get(self.hass).async_get_issue(DOMAIN, "controller_transition"))
            await runtime.preflight()
            await runtime.update()
            self.backend.read_ha.assert_not_called()
            self.backend.restore_missing.assert_not_called()
            for action in (runtime.resume_device(key), runtime.set_recovery(key, True)):
                with self.assertRaises(SafetyFailure):
                    await action
            async with self.flow_environment():
                flow = self.flow("reconfigure", config)
                self.assertIn("transition", (await flow.async_step_reconfigure())["menu_options"])
                flow._managed_device = key
                self.assertEqual((await flow.async_step_manage_actions())["menu_options"], ["manage_remove", "manage"])
                self.assertEqual((await flow.async_step_manage_resume({}))["reason"], "explicit_transition_required")
                self.assertEqual((await flow.async_step_devices())["step_id"], "devices")
            await runtime.remove_device(key)
            self.assertIsNone(ir.async_get(self.hass).async_get_issue(DOMAIN, "controller_transition"))
        finally:
            await runtime.stop()

    async def test_transition_requires_local_admin_valid_selection_and_explicit_confirmation(self):
        config, key, _, _ = await self.legacy_inventory(include_owned=False)
        await integration.async_migrate_entry(self.hass, config)
        flow = self.flow("reconfigure", config)
        with patch.object(flow, "_access", AsyncMock(return_value=None)):
            self.assertEqual((await flow.async_step_transition())["reason"], "local_admin_required")
        async with self.flow_environment():
            self.assertEqual((await self.flow().async_step_transition())["reason"], "invalid_selection")
            form = await flow.async_step_transition()
            self.assertEqual(form["data_schema"]({})["confirm"], False)
            for selected in ([], ["unknown"], [key, key]):
                form = await flow.async_step_transition({"devices": selected, "confirm": True})
                self.assertEqual(form["errors"], {"base": "select_devices"})
            self.assertEqual(
                (await flow.async_step_transition({"devices": [key]}))["errors"], {"base": "confirm_transition"}
            )
            self.assertEqual(self.vault.record(key)["phase"], "transition_required")
            self.assertFalse(flow._transition_confirmed)
            self.assertEqual(
                (await flow.async_step_transition({"devices": [key], "confirm": True}))["step_id"], "credentials"
            )
            self.assertEqual(self.vault.record(key)["phase"], "transition_required")

    async def test_retained_unconfigured_native_journal_also_requires_explicit_transition(self):
        config, key, _, _ = await self.legacy_inventory()
        self.hass.config_entries.async_update_entry(
            config,
            data={
                "device_keys": [self.key],
                "pins": {self.key: config.data["pins"][self.key]},
                "admin_pins": {},
            },
        )
        await integration.async_migrate_entry(self.hass, config)
        async with self.flow_environment():
            flow = self.flow("reconfigure", config)
            form = await flow.async_step_transition()
            self.assertEqual(form["step_id"], "transition")
            result = await flow.async_step_transition({"devices": [key], "confirm": True})
            self.assertEqual(result["step_id"], "credentials")
            self.assertEqual(self.vault.record(key)["phase"], "transition_required")

    async def test_transition_cannot_be_bypassed_and_preparation_uses_a_fresh_user_controller(self):
        config, key, legacy, old = await self.legacy_inventory(include_owned=False)
        await integration.async_migrate_entry(self.hass, config)
        identifier = legacy["accessory_id"]
        imported = {"controllers": [self.source], "accessories": {identifier: legacy["accessory_key"]}}
        discovered = {
            identifier: {"name": "Candidate", "endpoint": {"AccessoryIP": "192.0.2.1", "AccessoryPort": 12345}}
        }
        self.backend.read_ha.side_effect = None
        with self.assertRaises(SafetyFailure):
            await prepare(self.hass, self.vault, self.backend, [identifier], [], imported, discovered)
        with (
            patch.object(self.hass.config_entries, "async_entries", side_effect=AssertionError("foreign lookup")),
            patch.object(self.hass.config_entries, "async_get_entry", side_effect=AssertionError("foreign lookup")),
        ):
            keys = await prepare(
                self.hass, self.vault, self.backend, [identifier], [], imported, discovered, transition_confirmed=True
            )
        target = self.vault.record(key)["target"]
        self.assertEqual(keys, [key])
        self.assertEqual(target["ha_permissions"], 0)
        self.assertTrue(target["owned"])
        self.assertFalse(target["recovery"])
        self.assertNotEqual(target["ha_id"], legacy["ha_id"])
        self.assertNotEqual(target["pairing"]["iOSDeviceLTSK"], old.data["iOSDeviceLTSK"])
        self.backend.restore_missing.assert_not_called()

    async def test_confirmed_flow_import_creates_new_connection_without_touching_native_identity(self):
        config, key, legacy, _ = await self.legacy_inventory(include_owned=False)
        await integration.async_migrate_entry(self.hass, config)
        exported = deepcopy(self.export)
        exported["accessories"][0]["AccessoryPairingID"] = legacy["accessory_id"]
        async with self.flow_environment():
            flow = self.flow("reconfigure", config)
            await flow.async_step_transition({"devices": [key], "confirm": True})
            with (
                patch.object(config_flow, "discover_devices", AsyncMock(return_value={})),
                patch.object(self.hass.config_entries, "async_schedule_reload"),
            ):
                await self.finish_flow(
                    flow, await flow.async_step_credential_export({"credential_export": json.dumps(exported)})
                )
        target = self.vault.record(key)["target"]
        self.assertNotEqual(target["ha_id"], legacy["ha_id"])
        self.assertFalse(target["recovery"])

    async def test_transition_failure_stays_visible_without_success_or_native_activity(self):
        config, key, legacy, _ = await self.legacy_inventory(include_owned=False)
        await integration.async_migrate_entry(self.hass, config)
        target = self.vault.record(key)["target"]
        runtime = integration.Runtime(
            self.hass, config, [target], SimpleNamespace(async_stop=AsyncMock()), None, self.vault
        )
        runtime.backend = self.backend
        await runtime.initialize()
        config.runtime_data = runtime
        exported = deepcopy(self.export)
        exported["accessories"][0]["AccessoryPairingID"] = legacy["accessory_id"]
        try:
            async with self.flow_environment():
                flow = self.flow("reconfigure", config)
                await flow.async_step_transition({"devices": [key], "confirm": True})
                with (
                    patch.object(config_flow, "discover_devices", AsyncMock(side_effect=OSError())),
                    patch.object(config_flow, "prepare", AsyncMock(side_effect=OSError())),
                ):
                    result = await self.finish_flow(
                        flow, await flow.async_step_credential_export({"credential_export": json.dumps(exported)})
                    )
                self.assertEqual(result["reason"], "verification_failed")
                self.assertEqual(self.vault.record(key)["phase"], "transition_required")
                self.backend.restore_missing.assert_not_called()
        finally:
            await runtime.stop()

    async def test_discovery_and_backend_never_accept_native_entry_credentials(self):
        with (
            patch.object(self.hass.config_entries, "async_entries", side_effect=AssertionError("foreign lookup")),
            patch.object(onboarding, "_discover_candidates", AsyncMock(return_value={})),
            patch.object(onboarding, "discover_airplay", AsyncMock(return_value=[])),
        ):
            self.assertEqual(await onboarding.discover_devices(self.hass), {})
        controller = Mock()
        backend = HomeKitBackend(controller, None)
        with self.assertRaises(SafetyFailure):
            async with backend.session({**self.target, "owned": False}):
                self.fail("Unowned session opened")
        controller.assert_not_called()
        invalid = deepcopy(self.target)
        invalid["pairing"]["AccessoryLTPK"] = invalid["accessory_key"] = "ab"
        with self.assertRaises(SafetyFailure):
            validate_target(invalid)
        # Private/public source matching is independent of all core entries.
        seed, public = keypair()
        invalid = {**self.target, "source": {**self.source, "iOSDeviceLTSK": seed, "iOSDeviceLTPK": public}}
        with self.assertRaises(SafetyFailure):
            validate_target(invalid, {"iOSPairingId": "different"})
        self.assertEqual(check_current(self.target)["iOSPairingId"], self.target["ha_id"])

    async def test_old_owned_publication_state_keeps_pending_counters(self):
        restored = DeviceState.restore({"native_pending": True, "native_attempts": [1], "attempts": [2]})
        self.assertTrue(restored.publication_pending)
        self.assertEqual(restored.publication_attempts, [1])
        self.assertEqual(restored.attempts, [2])
        for old, new in (
            ("native_reload_pending", "publication_pending"),
            ("native_unavailable", "publication_unavailable"),
        ):
            self.assertEqual(DeviceState.restore({"status": old}).status, new)

    async def test_transition_adds_measurements_only_after_an_owned_identity_exists(self):
        config, key, _, _ = await self.legacy_inventory(include_owned=False)
        await integration.async_migrate_entry(self.hass, config)
        target = self.vault.record(key)["target"]
        runtime = integration.Runtime(
            self.hass, config, [target], SimpleNamespace(async_stop=AsyncMock()), None, self.vault
        )
        runtime.backend = self.backend
        await runtime.initialize()
        config.runtime_data = runtime
        add, unload = Mock(), []
        try:
            with patch.object(type(config), "async_on_unload", side_effect=unload.append):
                await sensor.async_setup_entry(self.hass, config, add)
            self.assertEqual(len(add.call_args.args[0]), 1)
            owned = deepcopy(self.record)
            owned["target"]["entry_id"] = key
            await self.vault.put(key, owned)
            await runtime.accept_inventory({"device_keys": [key], "pins": {}, "admin_pins": {}})
            self.assertEqual(len(add.call_args.args[0]), 2)
            self.assertTrue(all(isinstance(entity, sensor.Measurement) for entity in add.call_args.args[0]))
        finally:
            for cancel in unload:
                cancel()
            await runtime.stop()
