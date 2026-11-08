"""Discovery through explicit exported address hints, never stale HAP ports."""

import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from zeroconf import DNSIncoming, ServiceStateChange
from homeassistant.data_entry_flow import FlowResultType

from custom_components.homepod_sensor_link import onboarding, config_flow
from custom_components.homepod_sensor_link.importer import parse_export
from custom_components.homepod_sensor_link.ui import KEY
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

    async def test_explicit_empty_answer_ptr_queries_recover_all_four_candidates(self):
        hosts = [f"192.0.2.{i}" for i in range(1, 5)]
        descriptions = {}
        callback = None
        browser = SimpleNamespace(async_cancel=AsyncMock())

        def browse(_zc, kind, *, handlers):
            nonlocal callback
            callback = handlers[0]
            return browser

        def send(query, host, port):
            packet = DNSIncoming(query.packets()[0])
            self.assertEqual(port, 5353)
            self.assertEqual(packet.questions[0].name, "_hap._tcp.local.")
            self.assertEqual(packet.questions[0].type, 12)
            self.assertEqual(packet.answers(), [])
            name = f"Device {hosts.index(host)}._hap._tcp.local."
            descriptions[name] = SimpleNamespace(
                id=f"candidate-{hosts.index(host)}", model="HomePod", name="Device", addresses=[host], port=23456
            )
            callback(zeroconf=None, service_type="_hap._tcp.local.", name=name, state_change=ServiceStateChange.Added)

        zc = SimpleNamespace(async_wait_for_start=AsyncMock(), async_send=Mock(side_effect=send))

        class Info:
            def __init__(self, kind, name):
                self.name = name

            async def async_request(self, zc, timeout):
                return True

        with (
            patch.object(
                onboarding.zeroconf, "async_get_async_instance", AsyncMock(return_value=SimpleNamespace(zeroconf=zc))
            ),
            patch.object(onboarding, "AsyncServiceBrowser", side_effect=browse),
            patch.object(onboarding, "AsyncServiceInfo", Info),
            patch.object(
                onboarding, "HomeKitService", SimpleNamespace(from_service_info=lambda info: descriptions[info.name])
            ),
            patch.object(onboarding, "DISCOVERY_WINDOW", 0),
        ):
            found = await onboarding.discover_devices(self.hass, hosts)
        self.assertEqual(len(found), 4)
        self.assertEqual(zc.async_send.call_count, 4)
        browser.async_cancel.assert_awaited_once()


class DirectedFlowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.OnboardingTests.asyncSetUp
    asyncTearDown = fixtures.OnboardingTests.asyncTearDown
    flow = flow_fixtures.FlowLifecycleTests.flow
    flow_environment = fixtures.OnboardingTests.flow_environment

    async def test_import_reviews_new_candidates_before_pairing_and_reuses_same_owner_import(self):
        flow = self.flow()
        async with self.flow_environment():
            await flow.async_step_user()
            imported = parse_export(json.dumps(pairing_dictionary()))
            self.hass.data[KEY].grant("flow", "owner")
            self.hass.data[KEY].pending["flow"]["data"] = imported
            found = {f"candidate-{i}": {"name": f"Device {i}"} for i in range(4)}
            with patch.object(config_flow, "discover_devices", AsyncMock(return_value=found)) as discover:
                result = await flow.async_step_credentials({})
            self.assertEqual(result["step_id"], "devices")
            self.assertEqual(result["data_schema"]({})["devices"], list(found))
            discover.assert_awaited_once_with(self.hass, hosts=["192.0.2.1"])
            flow._selected = list(found)
            with patch.object(flow, "_begin", AsyncMock(return_value={"type": FlowResultType.CREATE_ENTRY})) as begin:
                await flow.async_step_policy({"recovery_devices": list(found)})
            begin.assert_awaited_once()

    async def test_another_administrator_cannot_reuse_staged_credentials_or_hints(self):
        flow = self.flow()
        flow._imported = {"controllers": ["private"]}
        flow._import_owner = "another-owner"
        flow._discovery_hosts = ["192.0.2.1"]
        async with self.flow_environment():
            await flow._access()
        self.assertIsNone(flow._imported)
        self.assertIsNone(flow._import_owner)
        self.assertEqual(flow._discovery_hosts, [])

    async def test_direct_import_without_hints_reviews_devices_but_reauth_keeps_selection(self):
        for source, selected, expected in (("reconfigure", [], "devices"), ("reauth", ["selected"], "verify")):
            with self.subTest(source=source):
                flow = self.flow(source)
                flow._selected = selected
                flow._force_validate = source == "reauth"
                async with self.flow_environment():
                    await flow._access()
                    self.hass.data[KEY].grant("flow", "owner")
                    self.hass.data[KEY].pending["flow"]["data"] = {"controllers": [], "accessories": {}}
                    with patch.object(flow, "_begin", AsyncMock(return_value={"step_id": "verify"})):
                        result = await flow.async_step_credentials({})
                    self.assertEqual(result["step_id"], expected)
                    self.assertEqual(flow._selected, selected)
