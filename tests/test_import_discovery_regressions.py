"""Synthetic regressions for pairing dictionaries and delayed mDNS discovery."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from zeroconf import ServiceStateChange

from custom_components.homepod_sensor_link import onboarding
from custom_components.homepod_sensor_link.engine import SafetyFailure
from custom_components.homepod_sensor_link.importer import parse_export
import test_onboarding_v2 as fixtures


def pairing_dictionary():
    source, _, _ = fixtures.export_fixture()
    return {
        f"aa:bb:cc:dd:ee:{index:02x}": {
            **source,
            "AccessoryPairingID": f"aa:bb:cc:dd:ee:{index:02x}",
            "AccessoryLTPK": "ab" * 32,
            "AccessoryIP": "192.0.2.1",
            "AccessoryIPs": ["192.0.2.1"],
            "AccessoryPort": 12345,
            "Connection": "IP",
        }
        for index in range(4)
    }


class PairingDictionaryTests(unittest.TestCase):
    def test_four_pairings_share_one_controller_and_discard_transport_metadata(self):
        records = pairing_dictionary()
        parsed = parse_export(json.dumps(records))
        self.assertEqual(len(parsed["controllers"]), 1)
        self.assertEqual(set(parsed["accessories"]), set(records))
        self.assertNotIn("AccessoryIP", json.dumps(parsed))
        self.assertEqual(len(records), 4)

    def test_dictionary_rejects_mismatched_identity_unknown_fields_and_invalid_records(self):
        original = pairing_dictionary()
        first = next(iter(original))
        changed = deepcopy(original)
        changed[first]["AccessoryPairingID"] = "another-device"
        extra = deepcopy(original)
        extra[first]["unrelated_secret"] = "must-not-be-accepted"
        missing = deepcopy(original)
        del missing[first]["AccessoryLTPK"]
        invalid = [{}, {first: False}, changed, extra, missing, {str(i): {} for i in range(65)}]
        for value in invalid:
            with self.subTest(shape=len(value)), self.assertRaises(SafetyFailure) as error:
                parse_export(json.dumps(value))
            self.assertEqual(str(error.exception), "invalid_credential_export")

    def test_dictionary_preserves_existing_key_validation_and_derives_optional_public_key(self):
        original = pairing_dictionary()
        first = next(iter(original))
        record = original[first]
        del record["iOSDeviceLTPK"]
        parsed = parse_export(json.dumps({first.upper(): record}))
        self.assertEqual(len(parsed["controllers"]), 1)
        self.assertIn(first, parsed["accessories"])
        for field in ("iOSPairingId", "iOSDeviceLTSK", "AccessoryPairingID"):
            changed = deepcopy(original)
            del changed[first][field]
            with self.subTest(field=field), self.assertRaises(SafetyFailure):
                parse_export(json.dumps(changed))
        for field, value in (("iOSDeviceLTSK", "invalid"), ("iOSDeviceLTPK", "ab" * 32)):
            changed = deepcopy(original)
            changed[first][field] = value
            with self.subTest(field=field), self.assertRaises(SafetyFailure):
                parse_export(json.dumps(changed))


class ActiveDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.OnboardingTests.asyncSetUp
    asyncTearDown = fixtures.OnboardingTests.asyncTearDown
    flow_environment = fixtures.OnboardingTests.flow_environment

    async def test_active_browse_collects_later_responses_and_always_cancels(self):
        services = {f"Device {i}._hap._tcp.local.": i for i in range(4)}
        browser = SimpleNamespace(async_cancel=AsyncMock())

        def start(_zc, kind, *, handlers):
            self.assertEqual(kind, "_hap._tcp.local.")
            handler = handlers[0]
            for name in services:
                self.hass.loop.call_soon(handler, None, kind, name, ServiceStateChange.Added)
            return browser

        class Info:
            def __init__(self, kind, name):
                self.index = services[name]

            async def async_request(self, zc, timeout):
                return True

        def description(info):
            return SimpleNamespace(
                id=f"aa:bb:cc:dd:ee:{info.index:02x}",
                model="HomePod",
                name=f"Device {info.index}",
                addresses=["192.0.2.1"],
                port=12345,
            )

        async def snapshot():
            yield SimpleNamespace(description=description(SimpleNamespace(index=0)))

        @asynccontextmanager
        async def cached_session(hass):
            yield SimpleNamespace(async_discover=snapshot), None

        with (
            patch.object(onboarding, "controller_session", cached_session),
            patch.object(
                onboarding.zeroconf,
                "async_get_async_instance",
                AsyncMock(return_value=SimpleNamespace(zeroconf=object())),
            ),
            patch.object(onboarding, "AsyncServiceBrowser", side_effect=start, create=True),
            patch.object(onboarding, "AsyncServiceInfo", Info, create=True),
            patch.object(onboarding, "HomeKitService", SimpleNamespace(from_service_info=description), create=True),
            patch.object(onboarding, "DISCOVERY_WINDOW", 0, create=True),
        ):
            found = await onboarding.discover_devices(self.hass)
        self.assertEqual(len(found), 4)
        browser.async_cancel.assert_awaited_once()

    async def test_pairing_dictionary_reaches_the_native_credential_form(self):
        from custom_components.homepod_sensor_link.config_flow import HomePodSensorLinkFlow

        flow = HomePodSensorLinkFlow()
        flow.hass, flow.flow_id, flow.handler = self.hass, "flow", "homepod_sensor_link"
        async with self.flow_environment():
            with patch.object(flow, "_begin", AsyncMock(return_value={"type": "test"})):
                await flow.async_step_credential_export({"credential_export": json.dumps(pairing_dictionary())})
        self.assertEqual(len(flow._imported["controllers"]), 1)
        self.assertEqual(len(flow._imported["accessories"]), 4)

    async def test_removed_unresolved_unrelated_and_invalid_services_do_not_hide_healthy_homepod(self):
        browser = SimpleNamespace(async_cancel=AsyncMock())
        requested = set()

        def start(_zc, kind, *, handlers):
            change = handlers[0]
            for name in ("healthy", "unresolved", "unrelated", "invalid", "bad-endpoint", "removed"):
                change(None, kind, name, ServiceStateChange.Added)
            change(None, kind, "healthy", ServiceStateChange.Updated)
            change(None, kind, "removed", ServiceStateChange.Removed)
            return browser

        class Info:
            def __init__(self, kind, name):
                self.name = name

            async def async_request(self, zc, timeout):
                requested.add(self.name)
                return self.name != "unresolved"

        def description(info):
            if info.name == "invalid":
                raise ValueError("untrusted advertisement")
            return SimpleNamespace(
                id="disposable-homepod",
                model="Other speaker" if info.name == "unrelated" else "HomePod",
                name="Device",
                addresses=["192.0.2.1"],
                port=0 if info.name == "bad-endpoint" else 12345,
            )

        with (
            patch.object(
                onboarding.zeroconf,
                "async_get_async_instance",
                AsyncMock(return_value=SimpleNamespace(zeroconf=object())),
            ),
            patch.object(onboarding, "AsyncServiceBrowser", side_effect=start),
            patch.object(onboarding, "AsyncServiceInfo", Info),
            patch.object(onboarding, "HomeKitService", SimpleNamespace(from_service_info=description)),
            patch.object(onboarding, "DISCOVERY_WINDOW", 0),
        ):
            found = await onboarding.discover_devices(self.hass)
        self.assertEqual(set(found), {"disposable-homepod"})
        self.assertNotIn("removed", requested)
        browser.async_cancel.assert_awaited_once()

    async def test_cancelled_browse_releases_listener_without_resolving(self):
        browser = SimpleNamespace(async_cancel=AsyncMock())
        with (
            patch.object(
                onboarding.zeroconf,
                "async_get_async_instance",
                AsyncMock(return_value=SimpleNamespace(zeroconf=object())),
            ),
            patch.object(onboarding, "AsyncServiceBrowser", return_value=browser),
            patch.object(onboarding.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError)),
            self.assertRaises(asyncio.CancelledError),
        ):
            await onboarding.discover_devices(self.hass)
        browser.async_cancel.assert_awaited_once()
