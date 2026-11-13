"""Discovery through explicit exported address hints, never stale HAP ports."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.data_entry_flow import FlowResultType

from custom_components.homepod_sensor_link import onboarding, config_flow
from custom_components.homepod_sensor_link.importer import parse_export
from custom_components.homepod_sensor_link.ui import current_request
from test_import_discovery_regressions import pairing_dictionary
import test_flow_lifecycle as flow_fixtures
import test_onboarding_v2 as fixtures


class DirectedImportTests(unittest.TestCase):
    def test_address_hints_are_deduplicated_and_stale_ports_are_not_imported(self):
        parsed = parse_export(json.dumps(pairing_dictionary()))
        self.assertEqual(parsed["discovery_hosts"], ["192.0.2.1"])
        self.assertNotIn("AccessoryPort", json.dumps(parsed))

    def test_only_literal_unicast_hints_are_used_with_a_bounded_query_budget(self):
        records = pairing_dictionary()
        record = records[next(iter(records))]
        record["AccessoryIP"] = None
        record["AccessoryIPs"] = [
            False,
            "invalid",
            "127.0.0.1",
            "0.0.0.0",
            "224.0.0.1",
            "fe80::1",
            "::1",
            "::",
            "2001:db8::1",
        ]
        self.assertEqual(parse_export(json.dumps(records))["discovery_hosts"], ["2001:db8::1", "192.0.2.1"])
        for value in records.values():
            value["AccessoryIP"] = False
            value["AccessoryIPs"] = "192.0.2.1"
        self.assertNotIn("discovery_hosts", parse_export(json.dumps(records)))
        record["AccessoryIPs"] = [f"192.0.2.{i}" for i in range(1, 100)]
        self.assertEqual(len(parse_export(json.dumps(records))["discovery_hosts"]), 64)


class DirectedDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.OnboardingTests.asyncSetUp
    asyncTearDown = fixtures.OnboardingTests.asyncTearDown

    async def test_unicast_replies_find_four_candidates_when_shared_browse_returns_none(self):
        hosts = [f"192.0.2.{i}" for i in range(1, 5)]
        browser = SimpleNamespace(async_cancel=AsyncMock())
        infos = [
            SimpleNamespace(name=f"Device {i}", id=f"candidate-{i}", model="HomePod", addresses=[host], port=23456)
            for i, host in enumerate(hosts)
        ]
        with (
            patch.object(
                onboarding.zeroconf,
                "async_get_async_instance",
                AsyncMock(return_value=SimpleNamespace(zeroconf=object())),
            ),
            patch.object(onboarding, "AsyncServiceBrowser", return_value=browser),
            patch.object(onboarding, "discover_hosts", AsyncMock(return_value=infos)) as discover,
            patch.object(onboarding, "HomeKitService", SimpleNamespace(from_service_info=lambda info: info)),
            patch.object(onboarding, "DISCOVERY_WINDOW", 0),
        ):
            found = await onboarding.discover_devices(self.hass, hosts)
        self.assertEqual(len(found), 4)
        discover.assert_awaited_once_with(hosts)
        browser.async_cancel.assert_awaited_once()


class DirectedFlowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.OnboardingTests.asyncSetUp
    asyncTearDown = fixtures.OnboardingTests.asyncTearDown
    flow = flow_fixtures.FlowLifecycleTests.flow
    flow_environment = fixtures.OnboardingTests.flow_environment
    make_runtime = fixtures.OnboardingTests.make_runtime

    async def test_managed_import_with_hints_keeps_the_selected_device_and_policy(self):
        config, runtime = await self.make_runtime(recovery=False)
        identifier = self.target["accessory_id"]
        runtime.records[self.key]["phase"] = "needs_credentials"
        found = {
            identifier: {"name": "Selected HomePod", "endpoint": {"AccessoryIP": "192.0.2.1"}},
            "other": {"name": "Other HomePod"},
        }
        try:
            async with self.flow_environment():
                flow = self.flow("reconfigure", config)
                await flow.async_step_manage({"device": self.key})
                await flow.async_step_manage_resume({})
                with (
                    patch.object(config_flow, "discover_devices", AsyncMock(return_value=found)) as discover,
                    patch.object(flow, "_begin", AsyncMock(return_value={"step_id": "verify"})),
                ):
                    result = await flow.async_step_credential_export(
                        {"credential_export": json.dumps(pairing_dictionary())}
                    )
                self.assertEqual(result["step_id"], "verify")
                discover.assert_awaited_once_with(self.hass, hosts=["192.0.2.1"])
                self.assertEqual(flow._selected, [identifier])
                self.assertEqual(flow._recovery_ids, [])
                self.assertEqual(flow._discovered[identifier], found[identifier])
        finally:
            await runtime.stop()

    async def test_empty_search_can_retry_with_same_export_or_import_corrected_hints(self):
        manager = self.hass.config_entries.flow
        async with self.flow_environment():
            for failure in (None, OSError()):
                with self.subTest(failure=failure):
                    with (
                        patch(
                            "homeassistant.config_entries._async_get_flow_handler",
                            AsyncMock(return_value=config_flow.HomePodSensorLinkFlow),
                        ),
                        patch.object(config_flow, "discover_devices", AsyncMock(return_value={})),
                    ):
                        result = await manager.async_init(config_flow.DOMAIN, context={"source": "user"})
                    flow_id = result["flow_id"]
                    flow = manager._progress[flow_id]
                    result = await manager.async_configure(flow_id, {"next_step_id": "import_missing"})
                    self.assertEqual(result["step_id"], "credential_export")
                    with patch.object(config_flow, "discover_devices", AsyncMock(return_value={}, side_effect=failure)):
                        result = await manager.async_configure(
                            flow_id, {"credential_export": json.dumps(pairing_dictionary())}
                        )
                        self.assertEqual(result["step_id"], "discovery_failed" if failure else "no_devices")
                        imported = flow._imported
                        result = await manager.async_configure(flow_id, {"next_step_id": "devices"})
                        self.assertIs(flow._imported, imported)
                        self.assertEqual(flow._discovery_hosts, ["192.0.2.1"])
                    result = await manager.async_configure(flow_id, {"next_step_id": "import_missing"})
                    self.assertEqual(result["step_id"], "credential_export")
                    self.assertIsNone(flow._imported)
                    self.assertEqual(flow._discovery_hosts, [])
                    result = await manager.async_configure(flow_id, {"credential_export": "invalid"})
                    self.assertEqual(result["errors"], {"base": "invalid_credential_export"})
                    corrected = pairing_dictionary()
                    for record in corrected.values():
                        record["AccessoryIP"] = "192.0.2.2"
                        record["AccessoryIPs"] = ["192.0.2.2"]
                    found = {"missing-homepod": {"name": "Missing HomePod"}}
                    with patch.object(config_flow, "discover_devices", AsyncMock(return_value=found)) as discover:
                        result = await manager.async_configure(flow_id, {"credential_export": json.dumps(corrected)})
                    discover.assert_awaited_once_with(self.hass, hosts=["192.0.2.2"])
                    self.assertEqual(result["step_id"], "devices")
                    self.assertEqual(result["data_schema"]({})["devices"], ["missing-homepod"])
                    self.assertFalse(hasattr(flow, "_task"))
                    self.assertEqual(self.vault.records(), {})
                    manager.async_abort(flow_id)

    async def test_import_missing_keeps_credentials_used_by_verification(self):
        flow = self.flow()
        imported = parse_export(json.dumps(pairing_dictionary()))
        flow._imported = imported
        flow._discovery_hosts = ["192.0.2.1"]
        flow._selected = ["selected-homepod"]
        flow._task = self.hass.loop.create_future()
        flow._task.set_result([])
        async with self.flow_environment():
            with patch.object(flow, "async_step_verify", AsyncMock(return_value={"step_id": "verify"})) as verify:
                result = await flow.async_step_import_missing()
            verify.assert_awaited_once()
            self.assertEqual(result["step_id"], "verify")
            self.assertIs(flow._imported, imported)
            self.assertEqual(flow._discovery_hosts, ["192.0.2.1"])
            self.assertEqual(flow._selected, ["selected-homepod"])

    async def test_import_reviews_new_candidates_before_pairing_and_reuses_same_owner_import(self):
        flow = self.flow()
        async with self.flow_environment():
            await flow.async_step_user()
            found = {f"candidate-{i}": {"name": f"Device {i}"} for i in range(4)}
            with patch.object(config_flow, "discover_devices", AsyncMock(return_value=found)) as discover:
                result = await flow.async_step_credential_export(
                    {"credential_export": json.dumps(pairing_dictionary())}
                )
            self.assertEqual(result["step_id"], "devices")
            self.assertEqual(result["data_schema"]({})["devices"], list(found))
            discover.assert_awaited_once_with(self.hass, hosts=["192.0.2.1"])
            flow._selected = list(found)
            with patch.object(flow, "_begin", AsyncMock(return_value={"type": FlowResultType.CREATE_ENTRY})) as begin:
                await flow.async_step_policy({"recovery_devices": list(found)})
            begin.assert_awaited_once()

    async def test_another_administrator_cannot_reuse_staged_credentials_or_hints(self):
        flow = self.flow()
        async with self.flow_environment():
            await flow._access()
            flow._imported = parse_export(json.dumps(pairing_dictionary()))
            flow._discovery_hosts = ["192.0.2.1"]
            token = current_request.set(fixtures.Request(peer="127.0.0.1", owner="another-owner"))
            try:
                result = await flow.async_step_credential_export({"credential_export": json.dumps(self.export)})
                self.assertEqual(result["reason"], "local_admin_required")
            finally:
                current_request.reset(token)
            self.assertEqual(flow._owner, "owner")
            self.assertIsNotNone(flow._imported)
            self.assertEqual(flow._discovery_hosts, ["192.0.2.1"])

    async def test_resubmitting_staged_credentials_reuses_the_import_from_either_form(self):
        flow = self.flow()
        imported = parse_export(json.dumps(pairing_dictionary()))
        flow._imported = imported
        async with self.flow_environment():
            with patch.object(config_flow, "parse_export") as parse:
                for step in (flow.async_step_credential_export, flow.async_step_credential_manual):
                    result = await step({})
                    self.assertEqual(result["step_id"], "devices")
                    self.assertIs(flow._imported, imported)
                parse.assert_not_called()

    async def test_direct_import_without_hints_reviews_devices_but_reauth_keeps_selection(self):
        for source, selected, expected in (("reconfigure", [], "devices"), ("reauth", ["selected"], "verify")):
            with self.subTest(source=source):
                flow = self.flow(source)
                flow._selected = selected
                flow._force_validate = source == "reauth"
                async with self.flow_environment():
                    await flow._access()
                    with patch.object(flow, "_begin", AsyncMock(return_value={"step_id": "verify"})):
                        result = await flow.async_step_credential_export({"credential_export": json.dumps(self.export)})
                    self.assertEqual(result["step_id"], expected)
                    self.assertEqual(flow._selected, selected)
