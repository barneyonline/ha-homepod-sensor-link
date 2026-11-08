"""HA flow boundaries: denied access, partial results and per-device management."""

from copy import deepcopy
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.data_entry_flow import FlowResultType

from custom_components.homepod_sensor_link import config_flow as flow_module
from custom_components.homepod_sensor_link.credentials import DOMAIN
from custom_components.homepod_sensor_link.importer import MAX_IMPORT
from custom_components.homepod_sensor_link.ui import current_request
import test_onboarding_v2 as fixtures
from test_boundary_failures import native_config
from test_recovery import fixture


class FlowLifecycleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.OnboardingTests.asyncSetUp
    asyncTearDown = fixtures.OnboardingTests.asyncTearDown
    make_runtime = fixtures.OnboardingTests.make_runtime
    flow_environment = fixtures.OnboardingTests.flow_environment
    finish_flow = fixtures.OnboardingTests.finish_flow

    def flow(self, source="user", entry=None):
        flow = flow_module.HomePodSensorLinkFlow()
        flow.hass, flow.flow_id, flow.handler = self.hass, "flow", DOMAIN
        flow.context = {"source": source, **({"entry_id": entry.entry_id} if entry else {})}
        return flow

    async def test_native_credential_menu_and_empty_forms(self):
        from homeassistant.helpers.selector import TextSelectorType

        async with self.flow_environment():
            flow = self.flow()
            result = await flow.async_step_credentials()
            self.assertEqual(result["type"], FlowResultType.MENU)
            self.assertEqual(result["menu_options"], ["credential_export", "credential_manual"])
            export = await flow.async_step_credential_export()
            schema = export["data_schema"].schema
            self.assertTrue(next(iter(schema.values())).config["multiline"])
            manual = await flow.async_step_credential_manual()
            fields = manual["data_schema"].schema
            self.assertEqual(fields["controller_private_key"].config["type"], TextSelectorType.PASSWORD)
            self.assertEqual(fields["controller_private_key"].config["autocomplete"], "off")
            self.assertIsNone(export.get("description_placeholders"))
            self.assertIsNone(manual.get("description_placeholders"))

    async def test_native_export_rejects_invalid_and_utf8_oversize_without_echo_then_retries(self):
        async with self.flow_environment():
            flow = self.flow()
            flow._selected = [self.target["accessory_id"]]
            for content in (
                "invalid " + self.source["iOSDeviceLTSK"],
                "x" * (MAX_IMPORT + 1),
                "é" * MAX_IMPORT,
                "\ud800",
            ):
                result = await flow.async_step_credential_export({"credential_export": content})
                self.assertEqual(result["errors"], {"base": "invalid_credential_export"})
                self.assertIsNone(flow._imported)
                self.assertNotIn(self.source["iOSDeviceLTSK"], str(result))
                self.assertNotIn("default", str(result["data_schema"].schema))
            with patch.object(flow, "_begin", AsyncMock(return_value={"type": "test"})) as begin:
                await flow.async_step_credential_export({"credential_export": json.dumps(self.export)})
            begin.assert_awaited_once()
            self.assertEqual(flow._imported["controllers"], [self.source])
            flow._committed = True
            flow.async_remove()
            self.assertIsNone(flow._imported)

    async def test_native_csv_export_is_parsed(self):
        csv = "Account,Password,App / Group\n"
        csv += f"{self.source['iOSPairingId']},{self.source['iOSDeviceLTPK']}{self.source['iOSDeviceLTSK']},com.apple.hap.pairing\n"
        csv += f"{self.target['accessory_id']},{self.target['accessory_key']},com.apple.hap.pairing\n"
        async with self.flow_environment():
            flow = self.flow()
            flow._selected = [self.target["accessory_id"]]
            with patch.object(flow, "_begin", AsyncMock(return_value={"type": "test"})):
                await flow.async_step_credential_export({"credential_export": csv})
            self.assertEqual(flow._imported["controllers"], [self.source])
            self.assertEqual(flow._imported["accessories"], {self.target["accessory_id"]: self.target["accessory_key"]})

    async def test_native_manual_validates_pairs_and_does_not_echo_secrets(self):
        valid = {"controller_id": self.source["iOSPairingId"], "controller_private_key": self.source["iOSDeviceLTSK"]}
        async with self.flow_environment():
            flow = self.flow()
            flow._selected = [self.target["accessory_id"]]
            for invalid in (
                {},
                {**valid, "accessory_id": "missing-pin"},
                {**valid, "accessory_public_key": "ab" * 32},
                {**valid, "controller_private_key": "not-a-key"},
            ):
                result = await flow.async_step_credential_manual(invalid)
                self.assertEqual(result["errors"], {"base": "invalid_credential_export"})
                self.assertIsNone(flow._imported)
                self.assertNotIn(self.source["iOSDeviceLTSK"], str(result))
                self.assertNotIn("default", str(result["data_schema"].schema))
            for value, accessories in (
                (valid, {}),
                (
                    {
                        **valid,
                        "accessory_id": self.target["accessory_id"],
                        "accessory_public_key": self.target["accessory_key"],
                    },
                    {self.target["accessory_id"]: self.target["accessory_key"]},
                ),
            ):
                flow._imported = None
                with patch.object(flow, "_begin", AsyncMock(return_value={"type": "test"})) as begin:
                    await flow.async_step_credential_manual(value)
                begin.assert_awaited_once()
                self.assertEqual(flow._imported["controllers"], [self.source])
                self.assertEqual(flow._imported["accessories"], accessories)

    async def test_native_forms_require_local_authenticated_admin_and_original_owner(self):
        async with self.flow_environment():
            flow = self.flow()
            await flow.async_step_credentials()
            for request, cloud in (
                (fixtures.Request(peer="198.51.100.1"), False),
                (fixtures.Request(peer="127.0.0.1", admin=False), False),
                (fixtures.Request(peer="127.0.0.1", authenticated=False), False),
                (fixtures.Request(peer="127.0.0.1", owner="different-admin"), False),
                (fixtures.Request(peer="127.0.0.1"), True),
                ({}, False),
            ):
                token = current_request.set(request)
                try:
                    with (
                        patch("custom_components.homepod_sensor_link.ui.is_cloud_connection", return_value=cloud),
                        patch.object(flow_module, "parse_export") as parse,
                    ):
                        for step in (flow.async_step_credential_export, flow.async_step_credential_manual):
                            result = await step({"credential_export": json.dumps(self.export)})
                            self.assertEqual(result["reason"], "local_admin_required")
                        parse.assert_not_called()
                finally:
                    current_request.reset(token)
            self.assertEqual(flow._owner, "owner")

    async def test_every_sensitive_step_requires_local_administrator(self):
        for name in (
            "user",
            "devices",
            "no_devices",
            "discovery_failed",
            "policy",
            "credentials",
            "import_missing",
            "credential_export",
            "credential_manual",
            "finish",
            "manage",
            "reconfigure",
        ):
            with self.subTest(step=name):
                result = await getattr(self.flow(), "async_step_" + name)()
                self.assertEqual(result["reason"], "local_admin_required")
        flow = self.flow("zeroconf")
        result = await flow.async_step_zeroconf(SimpleNamespace())
        self.assertEqual(result["step_id"], "user")
        self.assertEqual(flow.context["title_placeholders"], {"name": "HomePod sensors"})
        self.assertEqual(flow.unique_id, DOMAIN)

    async def test_discovery_failure_selection_validation_and_retained_journals(self):
        await self.vault.put(self.key, self.record)
        active = deepcopy(self.record)
        active["target"]["entry_id"], active["target"]["accessory_id"] = "active", "active-accessory"
        active["phase"] = "active"
        await self.vault.put("active", active)
        async with self.flow_environment():
            flow = self.flow()
            with patch.object(flow_module, "discover_devices", AsyncMock(side_effect=OSError())):
                result = await flow.async_step_devices()
                self.assertEqual(result["errors"], {"base": "discovery_failed"})
                self.assertIn(self.target["accessory_id"], flow._discovered)
                self.assertNotIn("active-accessory", flow._discovered)
                self.assertEqual(result["data_schema"]({})["devices"], [self.target["accessory_id"]])
            result = await flow.async_step_devices({"devices": ["not-discovered"]})
            self.assertEqual(result["errors"], {"base": "select_devices"})
            flow._selected = [self.target["accessory_id"]]
            self.assertEqual(
                (await flow.async_step_policy({"recovery_devices": ["unselected"]}))["reason"], "invalid_selection"
            )
            self.assertEqual(
                (await flow.async_step_credential_export({}))["errors"], {"base": "invalid_credential_export"}
            )

    async def test_all_discovered_devices_start_selected_and_can_be_deselected(self):
        found = {f"disposable-{index}": {"name": f"HomePod {index}"} for index in range(4)}
        for source in ("user", "reconfigure"):
            with self.subTest(source=source):
                flow = self.flow(source)
                async with self.flow_environment():
                    with patch.object(flow_module, "discover_devices", AsyncMock(return_value=found)):
                        result = await flow.async_step_devices()
                        self.assertEqual(result["data_schema"]({})["devices"], list(found))
                        selected = ["disposable-1", "disposable-3"]
                        policy = await flow.async_step_devices({"devices": selected})
                        self.assertEqual(policy["step_id"], "policy")
                        self.assertEqual(flow._selected, selected)
                        self.assertEqual(policy["data_schema"]({})["recovery_devices"], selected)

    async def test_invalid_device_submission_preserves_available_selection(self):
        found = {"first": {"name": "First HomePod"}, "second": {"name": "Second HomePod"}}
        async with self.flow_environment():
            with patch.object(flow_module, "discover_devices", AsyncMock(return_value=found)):
                for submitted, expected in (
                    ({"devices": []}, []),
                    ({}, []),
                    ({"devices": ["second", "no-longer-discovered"]}, ["second"]),
                    ({"devices": ["second", "second"]}, ["second"]),
                ):
                    with self.subTest(submitted=submitted):
                        result = await self.flow().async_step_devices(submitted)
                        self.assertEqual(result["errors"], {"base": "select_devices"})
                        self.assertEqual(result["data_schema"]({})["devices"], expected)

    async def test_empty_discovery_offers_retry_instead_of_empty_selector(self):
        async with self.flow_environment():
            for source in ("user", "reconfigure"):
                for submitted in (None, {}, {"devices": [self.target["accessory_id"]]}):
                    with self.subTest(source=source, submitted=submitted):
                        flow = self.flow(source)
                        with patch.object(flow_module, "discover_devices", AsyncMock(return_value={})):
                            result = await flow.async_step_devices(submitted)
                        self.assertEqual(result["type"], FlowResultType.MENU)
                        self.assertEqual(result["step_id"], "no_devices")
                        self.assertEqual(result["menu_options"], ["devices", "import_missing"])
                        self.assertEqual(flow._selected, [])
                        retry = result["data_schema"]({"next_step_id": "devices"})
                        result = await getattr(flow, "async_step_" + retry["next_step_id"])()
                        self.assertEqual(result["step_id"], "devices")
                        self.assertEqual(result["data_schema"]({})["devices"], [self.target["accessory_id"]])

    async def test_empty_discovery_failure_is_distinct_and_can_retry(self):
        async with self.flow_environment():
            flow = self.flow()
            with patch.object(flow_module, "discover_devices", AsyncMock(side_effect=OSError("private details"))):
                for submitted in (None, {"devices": []}):
                    result = await flow.async_step_devices(submitted)
                    self.assertEqual(result["type"], FlowResultType.MENU)
                    self.assertEqual(result["step_id"], "discovery_failed")
                    self.assertEqual(result["menu_options"], ["devices", "import_missing"])
                    self.assertNotIn("private details", str(result))
            self.assertEqual((await flow.async_step_devices())["step_id"], "devices")

    async def test_only_active_devices_shows_empty_state_and_export_can_find_missing_devices(self):
        active = deepcopy(self.record)
        active["phase"] = "active"
        await self.vault.put(self.key, active)
        async with self.flow_environment():
            flow = self.flow("reconfigure")
            result = await flow.async_step_devices()
            self.assertEqual(result["step_id"], "no_devices")
            choice = result["data_schema"]({"next_step_id": "import_missing"})
            result = await getattr(flow, "async_step_" + choice["next_step_id"])()
            self.assertEqual(result["step_id"], "credential_export")
            found = {"missing-homepod": {"name": "Missing HomePod"}}
            with patch.object(flow_module, "discover_devices", AsyncMock(return_value=found)):
                result = await flow.async_step_credential_export({"credential_export": json.dumps(self.export)})
            self.assertEqual(result["step_id"], "devices")
            self.assertEqual(result["data_schema"]({})["devices"], ["missing-homepod"])

    async def test_monitor_only_native_setup_needs_no_export(self):
        target, source_entry = fixture()
        native = native_config(source_entry.data)
        self.hass.config_entries._entries[native.entry_id] = native
        flow = self.flow()
        async with self.flow_environment():
            await flow.async_step_user()
            flow._selected = [target["accessory_id"]]
            flow._discovered = {target["accessory_id"]: {"name": "Native", "native_entry_id": native.entry_id}}
            result = await self.finish_flow(flow, await flow.async_step_policy({"recovery_devices": []}))
            self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY)
            self.assertEqual(result["data"]["device_keys"], [native.entry_id])
            self.assertNotIn("source", self.vault.records()[native.entry_id]["target"])

    async def test_partial_failure_retains_native_reference_and_prior_journal(self):
        target, source_entry = fixture()
        native = native_config(source_entry.data)
        self.hass.config_entries._entries[native.entry_id] = native
        await self.vault.put(self.key, self.record)
        async with self.flow_environment():
            flow = self.flow()
            await flow._access()
            flow._selected = [self.target["accessory_id"], "fresh-failure", target["accessory_id"]]
            # use a distinct device for native adoption
            flow._selected = [self.target["accessory_id"], "fresh-failure"]
            flow._recovery_ids = []
            flow._discovered = {identifier: {"name": "Pending"} for identifier in flow._selected}
            with patch.object(flow_module, "prepare", AsyncMock(side_effect=OSError())):
                keys = await flow._prepare()
            self.assertIn(self.key, keys)
            self.assertEqual(len(keys), 2)
            self.assertEqual(self.vault.records()[self.key]["target"]["ha_id"], self.target["ha_id"])
            fresh = next(record for key, record in self.vault.records().items() if key != self.key)
            self.assertEqual(fresh["phase"], "needs_credentials")
            flow._selected = [target["accessory_id"]]
            flow._discovered = {target["accessory_id"]: {"name": "Native"}}
            with patch.object(flow_module, "prepare", AsyncMock(side_effect=OSError())):
                keys = await flow._prepare()
            self.assertEqual(keys, [self.key])  # previous immutable journal wins

    async def test_controller_start_failure_and_empty_finish_are_safe(self):
        @asynccontextmanager
        async def broken(hass):
            raise OSError()
            yield

        async with self.flow_environment():
            flow = self.flow()
            await flow._access()
            flow._selected = []
            with patch.object(flow_module, "controller_session", broken):
                self.assertEqual(await flow._prepare(), [])
            flow._task = self.hass.loop.create_future()
            flow._task.set_result([])
            self.assertEqual((await flow.async_step_finish())["reason"], "verification_failed")
            self.assertEqual((await flow.async_step_verify())["reason"], "verification_failed")
            flow._committed = True
            flow.async_remove()

    async def test_manage_dispatch_validation_pending_import_and_failure(self):
        config, runtime = await self.make_runtime()
        try:
            async with self.flow_environment():
                self.assertEqual(
                    (await self.flow("reconfigure", config).async_step_reconfigure())["type"], FlowResultType.MENU
                )
                self.assertEqual((await self.flow("reconfigure", config).async_step_manage())["step_id"], "manage")
                self.assertEqual(
                    (
                        await self.flow("reconfigure", config).async_step_manage(
                            {"device": "foreign", "action": "remove"}
                        )
                    )["reason"],
                    "invalid_selection",
                )
                malformed = await self.flow("reconfigure", config).async_step_manage(
                    {"device": self.key, "action": "unknown"}
                )
                self.assertEqual(malformed["errors"], {"base": "device_action_failed"})
                for action, method, args in (
                    ("resume", "resume_device", (self.key,)),
                    ("cancel", "cancel_device", (self.key,)),
                    ("remove", "remove_device", (self.key,)),
                    ("recovery_off", "set_recovery", (self.key, False)),
                    ("recovery_on", "set_recovery", (self.key, True)),
                ):
                    with (
                        self.subTest(action=action),
                        patch.object(runtime, method, AsyncMock(return_value=True)) as call,
                    ):
                        result = await self.flow("reconfigure", config).async_step_manage(
                            {"device": self.key, "action": action}
                        )
                        self.assertEqual(result["reason"], "device_updated")
                        call.assert_awaited_once_with(*args)
                runtime.records[self.key]["phase"] = "blocked"
                result = await self.flow("reconfigure", config).async_step_manage(
                    {"device": self.key, "action": "resume"}
                )
                self.assertEqual(result["errors"], {"base": "safety_blocked"})
                runtime.records[self.key]["phase"] = "needs_credentials"
                runtime.records[self.key]["target"]["recovery"] = False
                result = await self.flow("reconfigure", config).async_step_manage(
                    {"device": self.key, "action": "resume"}
                )
                self.assertEqual(result["step_id"], "credentials")
                runtime.records[self.key]["target"]["recovery"] = True
                runtime.records[self.key]["phase"] = "active"
                for enabled in (False, True):
                    with patch.object(runtime, "set_recovery", AsyncMock(return_value=False)):
                        result = await self.flow("reconfigure", config).async_step_manage(
                            {"device": self.key, "action": "recovery_on"}
                        )
                        self.assertEqual(result["step_id"], "credentials")
                with patch.object(runtime, "remove_device", AsyncMock(side_effect=OSError())):
                    result = await self.flow("reconfigure", config).async_step_manage(
                        {"device": self.key, "action": "remove"}
                    )
                    self.assertEqual(result["errors"], {"base": "device_action_failed"})
                runtime.stopped = True
                self.assertEqual(
                    (await self.flow("reconfigure", config).async_step_manage())["reason"], "runtime_unavailable"
                )
        finally:
            await runtime.stop()

    async def test_reconfigure_rejects_public_pin_drift_and_updates_without_secrets(self):
        config, runtime = await self.make_runtime()
        try:
            async with self.flow_environment():
                flow = self.flow("reconfigure", config)
                flow._task = self.hass.loop.create_future()
                flow._task.set_result([self.key])
                self.hass.config_entries.async_update_entry(
                    config, data={**config.data, "pins": {self.key: {"changed": True}}}
                )
                self.assertEqual((await flow.async_step_finish())["reason"], "verification_failed")
                self.hass.config_entries.async_update_entry(
                    config, data={**config.data, "pins": {}, "admin_pins": {self.key: {"changed": True}}}
                )
                self.assertEqual((await flow.async_step_finish())["reason"], "verification_failed")
                self.hass.config_entries.async_update_entry(config, data={**config.data, "admin_pins": {}})
                with patch.object(self.hass.config_entries, "async_schedule_reload"):
                    result = await flow.async_step_finish()
                self.assertEqual(result["type"], FlowResultType.ABORT)
                flow.async_remove()
                self.assertTrue(flow._committed)
                runtime.stopped = True
                with patch.object(self.hass.config_entries, "async_schedule_reload") as reload:
                    await flow.async_step_finish()
                    reload.assert_called_once()
        finally:
            await runtime.stop()
