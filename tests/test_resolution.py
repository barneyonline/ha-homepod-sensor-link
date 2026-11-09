"""Bound discovery tasks without dropping advertisements or leaking work."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from zeroconf import ServiceStateChange

from custom_components.homepod_sensor_link import names, onboarding
from custom_components.homepod_sensor_link.resolution import RESOLVE_WORKERS, resolve_all
import test_onboarding_v2 as fixtures


class ResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_inventory_does_no_work(self):
        resolve = AsyncMock()
        self.assertEqual(await resolve_all([], resolve), [])
        resolve.assert_not_awaited()

    async def test_all_results_preserve_order_with_at_most_thirty_two_tasks(self):
        for count in (1, 32, 64, 1024):
            with self.subTest(services=count):
                workers = set()
                active = peak = 0
                inputs = list(range(count))

                async def resolve(value):
                    nonlocal active, peak
                    workers.add(asyncio.current_task())
                    active += 1
                    peak = max(peak, active)
                    await asyncio.sleep(0)
                    active -= 1
                    return value * 2

                self.assertEqual(await resolve_all(inputs, resolve), [value * 2 for value in inputs])
                self.assertEqual(len(workers), min(count, RESOLVE_WORKERS))
                self.assertEqual(peak, min(count, RESOLVE_WORKERS))
                self.assertTrue(all(task.done() for task in workers))

    async def test_browser_mutation_during_resolution_does_not_change_inventory(self):
        inputs = {"first", "second"}

        async def resolve(value):
            inputs.add("later")
            await asyncio.sleep(0)
            return value

        self.assertEqual(set(await resolve_all(inputs, resolve)), {"first", "second"})

    async def test_parent_cancellation_drains_every_started_resolver(self):
        entered = asyncio.Event()
        workers = set()
        finished = []

        async def resolve(value):
            workers.add(asyncio.current_task())
            if len(workers) == RESOLVE_WORKERS:
                entered.set()
            try:
                await asyncio.Future()
            finally:
                finished.append(value)

        task = asyncio.create_task(resolve_all(range(64), resolve))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(finished), RESOLVE_WORKERS)
        self.assertTrue(all(worker.done() for worker in workers))


class DiscoveryResolutionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.OnboardingTests.asyncSetUp
    asyncTearDown = fixtures.OnboardingTests.asyncTearDown

    async def test_hap_and_airplay_use_bounded_requests_without_dropping_services(self):
        for module, discover, service_type in (
            (onboarding, onboarding._discover_candidates, onboarding.HAP_TYPE),
            (names, names.discover_airplay, names.AIRPLAY_TYPE),
        ):
            with self.subTest(service=service_type):
                browser = SimpleNamespace(async_cancel=AsyncMock())
                workers = set()
                requested = set()
                active = peak = 0

                def start(_zc, kind, *, handlers):
                    for index in range(128):
                        handlers[0](None, kind, f"Speaker {index}.{kind}", ServiceStateChange.Added)
                    return browser

                class Info:
                    def __init__(self, kind, name):
                        self.name = name
                        self.decoded_properties = {"model": "HomePod"}

                    async def async_request(self, zc, timeout):
                        nonlocal active, peak
                        workers.add(asyncio.current_task())
                        requested.add(self.name)
                        active += 1
                        peak = max(peak, active)
                        await asyncio.sleep(0)
                        active -= 1
                        return True

                    def ip_addresses_by_version(self, version):
                        return ["192.0.2.1"]

                def description(info):
                    return SimpleNamespace(
                        id=info.name,
                        name=info.name,
                        model="HomePod",
                        addresses=["192.0.2.1"],
                        port=12345,
                    )

                with (
                    patch.object(
                        module.zeroconf,
                        "async_get_async_instance",
                        AsyncMock(return_value=SimpleNamespace(zeroconf=object())),
                    ),
                    patch.object(module, "AsyncServiceBrowser", side_effect=start),
                    patch.object(module, "AsyncServiceInfo", Info),
                    patch.object(module, "HomeKitService", SimpleNamespace(from_service_info=description)),
                    patch.object(module, "DISCOVERY_WINDOW", 0),
                ):
                    result = await discover(self.hass)
                self.assertEqual(len(result), 128)
                self.assertEqual(len(requested), 128)
                self.assertEqual(len(workers), RESOLVE_WORKERS)
                self.assertEqual(peak, RESOLVE_WORKERS)
                self.assertTrue(all(worker.done() for worker in workers))
                browser.async_cancel.assert_awaited_once()

    async def test_resolver_failure_cancels_and_drains_peers(self):
        entered = asyncio.Event()
        workers = set()
        finished = []

        async def resolve(value):
            workers.add(asyncio.current_task())
            if len(workers) == RESOLVE_WORKERS:
                entered.set()
            try:
                await entered.wait()
                if value == 0:
                    raise OSError("synthetic resolution failure")
                await asyncio.Future()
            finally:
                finished.append(value)

        with self.assertRaisesRegex(OSError, "synthetic resolution failure"):
            await resolve_all(range(64), resolve)
        self.assertEqual(len(finished), RESOLVE_WORKERS)
        self.assertTrue(all(worker.done() for worker in workers))

    async def test_resolver_cancellation_propagates_and_drains_peers(self):
        entered = asyncio.Event()
        workers = set()
        finished = []

        async def resolve(value):
            workers.add(asyncio.current_task())
            if len(workers) == RESOLVE_WORKERS:
                entered.set()
            try:
                await entered.wait()
                if value == 0:
                    raise asyncio.CancelledError
                await asyncio.Future()
            finally:
                finished.append(value)

        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(resolve_all(range(64), resolve), timeout=1)
        self.assertEqual(len(finished), RESOLVE_WORKERS)
        self.assertTrue(all(worker.done() for worker in workers))
