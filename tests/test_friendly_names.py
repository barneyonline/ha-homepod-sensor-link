"""Synthetic AirPlay discovery, conservative matching and HA name overrides."""

import asyncio
from copy import deepcopy
from datetime import timedelta
import ipaddress
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceInfo

from custom_components.homepod_sensor_link import names, onboarding
from custom_components.homepod_sensor_link.credentials import DOMAIN
from custom_components.homepod_sensor_link.sensor import Measurement
from test_boundary_failures import native_config
import test_onboarding_v2 as fixtures


def candidate(addresses):
    return {"name": "HomePodSensor disposable", "endpoint": {"AccessoryIPs": addresses}}


def wire_info(kind, label, address="192.0.2.1", identifier="one", model="HomePod"):
    return AsyncServiceInfo(
        kind,
        f"{label}.{kind}",
        server="test-device.local.",
        port=12345,
        addresses=[ipaddress.ip_address(address).packed],
        properties={"id": identifier, "md": model, "model": model},
    )


def fresh_services():
    return [wire_info(onboarding.HAP_TYPE, "HomePodSensor disposable")], [wire_info(names.AIRPLAY_TYPE, "Test Room")]


class MatchingTests(unittest.TestCase):
    def test_unique_addresses_support_ipv4_ipv6_and_multiple_interfaces(self):
        found = {"one": candidate(["192.0.2.1", "2001:db8::1"]), "two": candidate(["192.0.2.2"])}
        services = [
            {"name": "Test Room One", "addresses": {"2001:db8::1"}},
            {"name": "Test Room Two", "addresses": {"192.0.2.2"}},
        ]
        self.assertEqual(names.friendly_names(found, services), {"one": "Test Room One", "two": "Test Room Two"})

    def test_no_match_missing_endpoint_and_ambiguity_in_either_direction_keep_names(self):
        found = {
            "one": candidate(["192.0.2.1"]),
            "two": candidate(["192.0.2.2"]),
            "three": candidate(["192.0.2.2"]),
            "absent": candidate(["192.0.2.3"]),
            "native-only": {"name": "Native HomePod"},
        }
        services = [
            {"name": "First", "addresses": {"192.0.2.1"}},
            {"name": "Duplicate", "addresses": {"192.0.2.1"}},
            {"name": "Shared", "addresses": {"192.0.2.2"}},
        ]
        before = deepcopy(found)
        self.assertEqual(names.friendly_names(found, services), {})
        self.assertEqual(found, before)
        self.assertEqual(names.friendly_names(found, []), {})


class NameDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.OnboardingTests.asyncSetUp
    asyncTearDown = fixtures.OnboardingTests.asyncTearDown
    make_runtime = fixtures.OnboardingTests.make_runtime

    async def test_browse_resolves_only_homepods_and_closes_listener(self):
        browser = SimpleNamespace(async_cancel=AsyncMock())
        labels = ("Test Room", "unresolved", "invalid", "unrelated", "missing-model", "blank", "removed")
        services = {label: f"{label}.{names.AIRPLAY_TYPE}" for label in labels}
        services["blank"] = " ." + names.AIRPLAY_TYPE
        requested = set()

        def start(_zc, kind, *, handlers):
            self.assertEqual(kind, names.AIRPLAY_TYPE)
            for name in services.values():
                self.hass.loop.call_soon(handlers[0], None, kind, name, ServiceStateChange.Added)
            self.hass.loop.call_soon(handlers[0], None, kind, services["Test Room"], ServiceStateChange.Updated)
            self.hass.loop.call_soon(handlers[0], None, kind, services["removed"], ServiceStateChange.Removed)
            return browser

        class Info:
            def __init__(self, kind, name):
                self.name = name
                self.decoded_properties = {"model": "AudioAccessory5,1"}
                if name == services["unrelated"]:
                    self.decoded_properties = {"model": "AppleTV14,1"}
                if name == services["missing-model"]:
                    self.decoded_properties = {"model": None}

            async def async_request(self, _zc, timeout):
                requested.add(self.name)
                if self.name == services["invalid"]:
                    raise ValueError("untrusted advertisement")
                return self.name != services["unresolved"]

            def ip_addresses_by_version(self, version):
                return [ipaddress.ip_address("192.0.2.1")]

        with (
            patch.object(
                names.zeroconf, "async_get_async_instance", AsyncMock(return_value=SimpleNamespace(zeroconf=0))
            ),
            patch.object(names, "AsyncServiceBrowser", side_effect=start),
            patch.object(names, "AsyncServiceInfo", Info),
            patch.object(names, "DISCOVERY_WINDOW", 0),
        ):
            result = await names.discover_airplay(self.hass)
        self.assertEqual(result, [{"name": "Test Room", "addresses": {"192.0.2.1"}}])
        self.assertNotIn(services["removed"], requested)
        browser.async_cancel.assert_awaited_once()

        browser.async_cancel.side_effect = OSError("cleanup failed")
        with (
            patch.object(
                names.zeroconf, "async_get_async_instance", AsyncMock(return_value=SimpleNamespace(zeroconf=0))
            ),
            patch.object(names, "AsyncServiceBrowser", return_value=browser),
            patch.object(names, "DISCOVERY_WINDOW", 0),
        ):
            self.assertEqual(await names.discover_airplay(self.hass), [])
            with (
                patch.object(names.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError)),
                self.assertRaises(asyncio.CancelledError),
            ):
                await names.discover_airplay(self.hass)

    async def test_optional_discovery_failure_and_cancellation_cleanup(self):
        with patch.object(names.zeroconf, "async_get_async_instance", AsyncMock(side_effect=OSError())):
            self.assertEqual(await names.discover_airplay(self.hass), [])
        browser = SimpleNamespace(async_cancel=AsyncMock())
        with (
            patch.object(
                names.zeroconf, "async_get_async_instance", AsyncMock(return_value=SimpleNamespace(zeroconf=0))
            ),
            patch.object(names, "AsyncServiceBrowser", return_value=browser),
            patch.object(names.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError)),
            self.assertRaises(asyncio.CancelledError),
        ):
            await names.discover_airplay(self.hass)
        browser.async_cancel.assert_awaited_once()

    async def test_discovery_uses_fresh_addresses_without_native_adoption(self):
        native = native_config({"AccessoryPairingID": "one", "AccessoryIP": "192.0.2.99"})
        self.hass.config_entries._entries[native.entry_id] = native
        fresh = {"one": candidate(["192.0.2.1"]), "unmatched": candidate(["192.0.2.2"])}
        with (
            patch.object(onboarding, "_discover_candidates", AsyncMock(return_value=fresh)),
            patch.object(
                onboarding,
                "discover_airplay",
                AsyncMock(return_value=[{"name": "Test Room", "addresses": {"192.0.2.1"}}]),
            ),
            patch.object(names, "discover_hosts", AsyncMock(side_effect=fresh_services())),
        ):
            result = await onboarding.discover_devices(self.hass)
        self.assertEqual(result["one"]["name"], "Test Room")
        self.assertNotIn("native_entry_id", result["one"])
        self.assertEqual(result["unmatched"]["name"], "HomePodSensor disposable")

    async def test_cached_sensor_address_cannot_persist_another_homepods_name(self):
        await self.vault.put(self.key, self.record)
        records = self.vault.records()
        identifier = self.target["accessory_id"]
        hap, airplay = fresh_services()
        hap = [wire_info(onboarding.HAP_TYPE, "Other sensor", identifier="different-homepod")]
        with (
            patch.object(
                onboarding, "_discover_candidates", AsyncMock(return_value={identifier: candidate(["192.0.2.1"])})
            ),
            patch.object(
                onboarding,
                "discover_airplay",
                AsyncMock(return_value=[{"name": "Test Room", "addresses": {"192.0.2.1"}}]),
            ),
            patch.object(names, "discover_hosts", AsyncMock(side_effect=[hap, airplay])),
            patch.object(self.vault, "put", AsyncMock()) as save,
        ):
            await onboarding.refresh_names(self.hass, records, self.vault)
        save.assert_not_awaited()
        self.assertEqual(records[self.key]["name"], self.record["name"])
        self.assertEqual(self.vault.record(self.key)["name"], self.record["name"])

    async def test_confirmation_requires_both_current_services_and_keeps_ambiguity(self):
        candidates = {"one": candidate(["192.0.2.1"])}
        services = [{"name": "Test Room", "addresses": {"192.0.2.1"}}]
        hap, airplay = fresh_services()
        cases = (
            ([], airplay),
            (hap, []),
            ([wire_info(onboarding.HAP_TYPE, "Invalid", model="Other speaker")], airplay),
            ([AsyncServiceInfo(onboarding.HAP_TYPE, "Incomplete." + onboarding.HAP_TYPE)], airplay),
            (hap, [wire_info(names.AIRPLAY_TYPE, "Renamed Room")]),
            (hap, [wire_info(names.AIRPLAY_TYPE, "Other speaker", model="AppleTV14,1")]),
            (hap, airplay + [wire_info(names.AIRPLAY_TYPE, "Second Room")]),
            (hap + [wire_info(onboarding.HAP_TYPE, "Second sensor", identifier="two")], airplay),
        )
        for answers in cases:
            with (
                self.subTest(answers=len(answers[0])),
                patch.object(names, "discover_hosts", AsyncMock(side_effect=answers)),
            ):
                self.assertEqual(await names.confirmed_names(candidates, services), {})
        with patch.object(names, "discover_hosts", AsyncMock()) as discover:
            self.assertEqual(await names.confirmed_names(candidates, []), {})
            discover.assert_not_awaited()

    async def test_dual_stack_confirmation_merges_duplicate_services_and_bounds_requests(self):
        candidates = {"one": candidate(["192.0.2.1", "2001:db8::1"])}
        services = [{"name": "Test Room", "addresses": {"192.0.2.1"}}]
        hap, airplay = fresh_services()
        hap.append(wire_info(onboarding.HAP_TYPE, "HomePodSensor disposable", address="2001:db8::1"))
        airplay.append(wire_info(names.AIRPLAY_TYPE, "Test Room", address="2001:db8::1"))
        with patch.object(names, "discover_hosts", AsyncMock(side_effect=[hap, airplay])) as discover:
            self.assertEqual(await names.confirmed_names(candidates, services), {"one": "Test Room"})
            self.assertEqual(discover.await_args_list[0].args, (["192.0.2.1", "2001:db8::1"],))
            self.assertEqual(discover.await_args_list[1].args, (["192.0.2.1", "2001:db8::1"], names.AIRPLAY_TYPE))
        candidates["one"]["endpoint"]["AccessoryIPs"] = [f"192.0.2.{index}" for index in range(1, 100)]
        with patch.object(names, "discover_hosts", AsyncMock(return_value=[])) as discover:
            self.assertEqual(await names.confirmed_names(candidates, services), {})
            self.assertTrue(all(len(call.args[0]) == 64 for call in discover.await_args_list))

    async def test_refresh_persists_names_and_retains_them_when_no_longer_matched(self):
        await self.vault.put(self.key, self.record)
        records = self.vault.records()
        identifier = self.target["accessory_id"]
        discovered = {identifier: {"friendly_name": "Test Room"}}
        with patch.object(onboarding, "discover_devices", AsyncMock(return_value=discovered)):
            await onboarding.refresh_names(self.hass, records, self.vault)
        self.assertEqual(records[self.key]["name"], "Test Room")
        self.assertEqual(self.vault.record(self.key)["name"], "Test Room")
        with patch.object(self.vault, "put", AsyncMock()) as save:
            for result in (discovered, {identifier: {"name": "HomePodSensor disposable"}}, {}):
                with patch.object(onboarding, "discover_devices", AsyncMock(return_value=result)):
                    await onboarding.refresh_names(self.hass, records, self.vault)
            with patch.object(onboarding, "discover_devices", AsyncMock(side_effect=OSError())):
                await onboarding.refresh_names(self.hass, records, self.vault)
            save.assert_not_awaited()
        self.assertEqual(records[self.key]["name"], "Test Room")
        with (
            patch.object(onboarding, "discover_devices", AsyncMock(side_effect=asyncio.CancelledError)),
            self.assertRaises(asyncio.CancelledError),
        ):
            await onboarding.refresh_names(self.hass, records, self.vault)

    async def test_existing_entities_keep_manual_device_entity_names_and_ids(self):
        config, runtime = await self.make_runtime(recovery=False)
        registry, entities = dr.async_get(self.hass), er.async_get(self.hass)
        device = registry.async_get_or_create(
            config_entry_id=config.entry_id, identifiers={(DOMAIN, self.key)}, name="HomePodSensor disposable"
        )
        registry.async_update_device(device.id, name_by_user="My Test HomePod")
        entity = entities.async_get_or_create(
            "sensor",
            DOMAIN,
            self.key + "_temperature",
            config_entry=config,
            device_id=device.id,
            suggested_object_id="keep_temperature_history",
        )
        entities.async_update_entity(entity.entity_id, name="My Test Temperature")
        with patch.object(
            onboarding,
            "discover_devices",
            AsyncMock(return_value={self.target["accessory_id"]: {"friendly_name": "Test Room"}}),
        ):
            await onboarding.refresh_names(self.hass, runtime.records, self.vault)
        platform = EntityPlatform(
            hass=self.hass,
            logger=logging.getLogger(__name__),
            domain="sensor",
            platform_name=DOMAIN,
            platform=None,
            scan_interval=timedelta(seconds=300),
            entity_namespace=None,
        )
        platform.config_entry = config
        sensor = Measurement(runtime, self.target, "temperature")
        try:
            await platform.async_add_entities([sensor])
            self.assertEqual(sensor.entity_id, entity.entity_id)
            updated = registry.async_get(device.id)
            self.assertEqual(updated.name, "Test Room")
            self.assertEqual(updated.name_by_user, "My Test HomePod")
            self.assertEqual(entities.async_get(entity.entity_id).name, "My Test Temperature")
            self.assertEqual(self.hass.states.get(entity.entity_id).name, "My Test HomePod My Test Temperature")
        finally:
            await platform.async_reset()
            await runtime.stop()
