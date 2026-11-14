"""Keep guided setup free of unused inventory copies and key bookkeeping."""

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from custom_components.homepod_sensor_link import async_remove_config_entry_device
from custom_components.homepod_sensor_link import vault as vault_module
from custom_components.homepod_sensor_link.config_flow import HomePodSensorLinkFlow
from custom_components.homepod_sensor_link.credentials import DOMAIN
from custom_components.homepod_sensor_link.vault import Vault
import test_onboarding_v2 as fixtures


class SetupOperationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.OnboardingTests.asyncSetUp
    asyncTearDown = fixtures.OnboardingTests.asyncTearDown
    flow_environment = fixtures.OnboardingTests.flow_environment
    make_runtime = fixtures.OnboardingTests.make_runtime

    async def test_reusing_prepared_devices_copies_only_each_selected_record(self):
        for count in (1, 16, 64):
            with self.subTest(devices=count):
                copies = []
                inspected = []

                class CountedId(str):
                    def lower(self):
                        inspected.append(self)
                        return super().lower()

                class CountedRecord(dict):
                    def __deepcopy__(self, memo):
                        copies.append(self["target"]["entry_id"])
                        return deepcopy(dict(self), memo)

                self.vault.data["records"] = {
                    f"disposable-{index}": CountedRecord(
                        target={"entry_id": f"disposable-{index}", "accessory_id": CountedId(f"accessory-{index}")},
                        phase="prepared",
                    )
                    for index in range(count)
                }
                flow = HomePodSensorLinkFlow()
                flow.hass, flow.flow_id, flow.handler = self.hass, "flow", DOMAIN
                flow._vault = self.vault
                flow._selected = [f"accessory-{index}" for index in range(count)]
                flow._recovery_ids = []
                flow._discovered = {}
                flow._imported = {}
                async with self.flow_environment() as backend:
                    with patch.object(self.vault, "records", wraps=self.vault.records) as records:
                        results = await flow._prepare()
                self.assertEqual(results, list(self.vault.data["records"]))
                records.assert_not_called()
                self.assertEqual(copies, results)
                self.assertEqual(inspected, flow._selected)
                self.assertIsNone(flow._imported)
                backend.read_ha.assert_not_awaited()
                backend.inspect_recovery.assert_not_awaited()
                backend.restore_missing.assert_not_awaited()

    async def test_accessory_lookup_returns_last_matching_record_in_isolation(self):
        first = {"target": {"entry_id": "first", "accessory_id": "synthetic"}, "phase": "needs_credentials"}
        last = {
            "target": {"entry_id": "last", "accessory_id": "SYNTHETIC", "pairing": {"metadata": ["fixture"]}},
            "phase": "prepared",
        }
        await self.vault.put("first", first)
        self.assertEqual(self.vault.record_for_accessory("Synthetic"), first)
        await self.vault.put("last", last)
        self.assertIsNone(self.vault.record_for_accessory("missing"))
        matched = self.vault.record_for_accessory("Synthetic")
        self.assertEqual(matched, last)
        matched["target"]["pairing"]["metadata"].clear()
        self.assertEqual(self.vault.record("last"), last)
        self.assertEqual(self.vault.record_for_accessory("Synthetic", preferred_keys=frozenset({"first"})), first)
        await self.vault.remove("last")
        self.assertEqual(self.vault.record_for_accessory("Synthetic"), first)
        moved = {**first, "target": {**first["target"], "accessory_id": "different"}}
        await self.vault.put("first", moved)
        self.assertIsNone(self.vault.record_for_accessory("Synthetic"))
        self.assertEqual(self.vault.record_for_accessory("different"), moved)
        await self.vault.purge()
        self.assertIsNone(self.vault.record_for_accessory("different"))

    async def test_accessory_lookups_reuse_the_index_across_new_record_commits(self):
        inspected = []

        class CountedId(str):
            def lower(self):
                inspected.append(self)
                return super().lower()

        for index in range(16):
            identifier = f"accessory-{index}"
            self.assertIsNone(self.vault.record_for_accessory(identifier))
            key = f"disposable-{index}"
            record = {"target": {"entry_id": key, "accessory_id": CountedId(identifier)}, "phase": "prepared"}
            await self.vault.put(key, record)
            self.assertEqual(self.vault.record_for_accessory(identifier), record)
        self.assertEqual(inspected, [f"accessory-{index}" for index in range(16)])

    async def test_active_and_unloaded_removal_commit_credentials_and_state_once(self):
        for active in (True, False):
            with self.subTest(active=active):
                config, runtime = await self.make_runtime()
                peer = "disposable-peer"
                peer_record = {"target": {"entry_id": peer}, "phase": "needs_credentials"}
                peer_state = {"attempts": [10, 20]}
                await self.vault.put(peer, peer_record)
                await runtime.store.async_save({self.key: {"attempts": [30]}, peer: peer_state})
                if not active:
                    await runtime.stop()
                    del config.runtime_data
                try:
                    with (
                        patch.object(vault_module, "_write", wraps=vault_module._write) as write,
                        patch.object(vault_module.os, "fsync", wraps=vault_module.os.fsync) as fsync,
                    ):
                        device = SimpleNamespace(identifiers={(DOMAIN, self.key)})
                        self.assertTrue(await async_remove_config_entry_device(self.hass, config, device))
                        self.assertEqual(write.call_count, 1)
                        self.assertEqual(fsync.call_count, 2)
                    restored = await Vault(self.hass).load()
                    self.assertEqual(restored.records(), {peer: peer_record})
                    self.assertEqual(restored.data["states"], {peer: peer_state})
                    self.assertEqual(config.data["device_keys"], [])
                    self.assertEqual(config.data["pins"], {})
                    if active:
                        self.assertNotIn(self.key, runtime.saved)
                        self.assertNotIn(self.key, runtime.engine.states)
                finally:
                    await runtime.stop()
