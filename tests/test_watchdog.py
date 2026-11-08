"""Heartbeat supervision through real HA Repairs, timers and runtime checks."""

import asyncio
from datetime import timedelta
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.config_entries import ConfigEntries
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.homepod_sensor_link import async_remove_entry
from custom_components.homepod_sensor_link import coordinator
from custom_components.homepod_sensor_link import watchdog as watchdog_module
from custom_components.homepod_sensor_link.credentials import DOMAIN
from custom_components.homepod_sensor_link.engine import DeviceState, SafetyFailure
from custom_components.homepod_sensor_link.sensor import RecoveryStatus
from custom_components.homepod_sensor_link.watchdog import HeartbeatWatchdog, ISSUE_ID
import test_onboarding_v2 as fixtures


class WatchdogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.hass = HomeAssistant(self.directory.name)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        self.config = fixtures.entry({})
        self.hass.config_entries._entries[self.config.entry_id] = self.config
        self.now = 0
        self.devices = {"first", "second"}
        self.active = True
        self.watchdog = HeartbeatWatchdog(self.hass, self.config.entry_id, lambda: self.devices, lambda: self.active)
        self.watchdog.clock = lambda: self.now
        self.cancel = Mock()
        timer = patch.object(watchdog_module, "async_track_time_interval", return_value=self.cancel)
        self.timer = timer.start()
        self.addCleanup(timer.stop)
        create = patch.object(watchdog_module.persistent_notification, "async_create")
        self.create = create.start()
        self.addCleanup(create.stop)
        dismiss = patch.object(watchdog_module.persistent_notification, "async_dismiss")
        self.dismiss = dismiss.start()
        self.addCleanup(dismiss.stop)
        self.watchdog.start()

    async def asyncTearDown(self):
        self.watchdog.stop()
        await self.hass.async_stop(force=True)

    def issue(self):
        return ir.async_get(self.hass).async_get_issue(DOMAIN, ISSUE_ID)

    async def test_grace_missing_heartbeat_deduplication_and_recovery(self):
        self.now = 899
        self.watchdog.check()
        self.assertIsNone(self.issue())
        self.now = 900
        self.watchdog.completed("first")
        self.assertEqual(self.watchdog.reported, {"second"})
        self.assertEqual(self.issue().config_entry_id, self.config.entry_id)
        self.assertEqual(self.issue().severity, ir.IssueSeverity.WARNING)
        self.assertIn("1 HomePod.", self.create.call_args.args[1])
        self.watchdog.check()
        self.create.assert_called_once()
        self.watchdog.completed("second")
        self.assertIsNone(self.issue())
        self.dismiss.assert_called_with(self.hass, self.watchdog.notification_id)
        self.assertEqual(self.watchdog.reported, set())

    async def test_multiple_stalls_update_one_notification_and_new_devices_get_grace(self):
        self.now = 600
        self.watchdog.completed("first")
        self.now = 900
        self.watchdog.check()
        self.now = 1500
        self.watchdog.check()
        self.assertEqual(self.watchdog.reported, self.devices)
        self.assertEqual(self.create.call_count, 2)
        self.assertIn("2 HomePods.", self.create.call_args.args[1])
        self.devices = {"new"}
        self.watchdog.check()
        self.assertIsNone(self.issue())
        self.assertEqual(self.watchdog.progress, {"new": 1500})
        self.watchdog.completed("removed")
        self.assertNotIn("removed", self.watchdog.progress)
        self.now = 2400
        self.watchdog.check()
        self.assertEqual(self.watchdog.reported, {"new"})

    async def test_pause_clears_alerts_and_resume_resets_grace(self):
        self.now = 900
        self.watchdog.check()
        self.assertIsNotNone(self.issue())
        self.active = False
        self.watchdog.check()
        self.assertEqual(self.watchdog.progress, {})
        self.assertIsNone(self.issue())
        self.now = 10000
        self.active = True
        self.watchdog.check()
        self.now += 899
        self.watchdog.check()
        self.assertIsNone(self.issue())
        self.now += 1
        self.watchdog.check()
        self.assertIsNotNone(self.issue())

    async def test_timer_is_independent_idempotent_and_cancelled_on_stop(self):
        self.watchdog.start()
        self.timer.assert_called_once_with(self.hass, self.watchdog.check, timedelta(minutes=5))
        self.now = 900
        self.timer.call_args.args[1](None)
        self.assertIsNotNone(self.issue())
        self.watchdog.stop()
        self.watchdog.stop()
        self.cancel.assert_called_once()
        self.assertIsNone(self.issue())
        self.now = 10000
        self.watchdog.check()
        self.assertEqual(self.watchdog.progress, {})
        self.watchdog.start()
        self.assertEqual(self.watchdog.progress, {key: 10000 for key in self.devices})
        self.assertIsNone(self.issue())

    async def test_progress_uses_monotonic_time_not_wall_clock(self):
        with patch.object(coordinator.dt_util, "utcnow", return_value=dt_util.utcnow() + timedelta(days=365)):
            self.now = 1
            self.watchdog.check()
            self.assertIsNone(self.issue())
            self.now = 900
            self.watchdog.check()
            self.assertIsNotNone(self.issue())
            self.watchdog.completed("first")
            self.watchdog.completed("second")
            self.assertIsNone(self.issue())


class RuntimeWatchdogTests(unittest.IsolatedAsyncioTestCase):
    make_runtime = fixtures.OnboardingTests.make_runtime

    async def asyncSetUp(self):
        await fixtures.OnboardingTests.asyncSetUp(self)
        self.config, self.runtime = await self.make_runtime()
        self.runtime.records[self.key]["phase"] = "active"
        self.runtime.sync_targets()
        self.runtime.verify_native = AsyncMock()
        self.backend.read_ha.side_effect = None
        self.backend.read_ha.return_value = {"temperature": 21, "humidity": 45}
        self.watchdog = self.runtime.watchdog
        self.watchdog.clock = lambda: self.now[0]

    async def asyncTearDown(self):
        await self.runtime.stop()
        await fixtures.OnboardingTests.asyncTearDown(self)

    def issue(self):
        return ir.async_get(self.hass).async_get_issue(DOMAIN, ISSUE_ID)

    async def test_real_timer_alerts_while_device_check_is_hung_and_clears_on_completion(self):
        entered, release, alerted = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def read(_target):
            entered.set()
            await release.wait()
            return {"temperature": 21, "humidity": 45}

        def notification(*args, **kwargs):
            alerted.set()

        self.backend.read_ha.side_effect = read
        with (
            patch.object(watchdog_module, "INTERVAL", timedelta(milliseconds=10)),
            patch.object(watchdog_module.persistent_notification, "async_create", side_effect=notification),
        ):
            self.watchdog.start()
            task = asyncio.create_task(self.runtime.engine.check_all())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                state = self.runtime.engine.states[self.key]
                self.assertIsNotNone(state.last_checked)
                self.assertIsNone(state.last_completed)
                self.now[0] = 900
                await asyncio.wait_for(alerted.wait(), 1)
                self.assertFalse(task.done())
                self.assertIsNotNone(self.issue())
                release.set()
                await task
                self.assertIsNotNone(state.last_completed)
                self.assertIsNone(self.issue())
                self.runtime.coordinator.async_set_updated_data(self.runtime.engine.snapshot())
                diagnostic = RecoveryStatus(self.runtime, self.target)
                self.assertIsNotNone(diagnostic.extra_state_attributes["last_completed"])
                persisted = await self.runtime.store.async_load()
                self.assertEqual(persisted[self.key]["last_completed"], state.last_completed)
            finally:
                release.set()
                await task

    async def test_failed_but_completed_checks_refresh_heartbeat_without_pairing_changes(self):
        self.watchdog.start()
        self.now[0] = 900
        self.watchdog.check()
        self.backend.read_ha.side_effect = OSError()
        await self.runtime.engine.check_all()
        self.assertIsNone(self.issue())
        self.assertEqual(self.watchdog.progress[self.key], 900)
        self.assertEqual(self.runtime.engine.states[self.key].status, "unreachable")
        self.backend.restore_missing.assert_not_awaited()
        # Unchanged sensor values still complete fresh checks.
        self.backend.read_ha.side_effect = None
        for self.now[0] in (1200, 1500, 1800):
            await self.runtime.engine.check_all()
            self.watchdog.check()
            self.assertIsNone(self.issue())

    async def test_cancelled_check_does_not_refresh_heartbeat(self):
        entered = asyncio.Event()

        async def read(_target):
            entered.set()
            await asyncio.Event().wait()

        self.watchdog.start()
        self.backend.read_ha.side_effect = read
        task = asyncio.create_task(self.runtime.engine.check_all())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(self.runtime.engine.states[self.key].last_completed)
        self.assertEqual(self.watchdog.progress[self.key], 0)
        self.now[0] = 900
        self.watchdog.check()
        self.assertIsNotNone(self.issue())

    async def test_paused_blocked_and_removed_devices_are_excluded(self):
        self.watchdog.start()
        for field, value in (("cancelled", True), ("phase", "needs_credentials"), ("phase", "blocked")):
            self.now[0] += 900
            self.watchdog.check()
            self.assertIsNotNone(self.issue())
            self.runtime.records[self.key][field] = value
            self.runtime.sync_targets()
            self.assertIsNone(self.issue())
            self.assertEqual(self.watchdog.progress, {})
            self.runtime.records[self.key].update(phase="active", cancelled=False)
            self.runtime.sync_targets()
        self.runtime.engine.states[self.key].blocked = True
        self.watchdog.check()
        self.assertEqual(self.watchdog.progress, {})
        self.runtime.engine.states[self.key].blocked = False
        self.watchdog.check()
        self.now[0] += 900
        self.watchdog.check()
        self.assertIsNotNone(self.issue())
        await self.runtime.remove_device(self.key)
        self.assertIsNone(self.issue())
        self.assertEqual(self.watchdog.progress, {})
        self.backend.restore_missing.assert_not_awaited()

    async def test_ha_lifecycle_and_competing_recovery_suspend_monitoring(self):
        self.watchdog.start()
        for state in (CoreState.starting, CoreState.stopping):
            self.hass.state = state
            self.watchdog.check()
            self.assertEqual(self.watchdog.progress, {})
            self.hass.state = CoreState.running
            self.watchdog.check()
        with patch.object(coordinator, "conflicting_recovery", return_value=True):
            self.now[0] = 900
            self.watchdog.check()
            self.assertIsNone(self.issue())
            self.assertEqual(self.watchdog.progress, {})
        self.watchdog.check()
        self.now[0] = 1800
        self.watchdog.check()
        self.assertIsNotNone(self.issue())
        await self.runtime.stop()
        self.assertIsNone(self.issue())
        self.watchdog.check()
        self.assertEqual(self.watchdog.progress, {})

    async def test_pending_inspection_completes_heartbeat(self):
        self.runtime.records[self.key]["phase"] = "prepared"
        self.runtime.sync_targets()
        self.watchdog.start()
        self.now[0] = 900
        self.watchdog.check()
        self.assertIsNotNone(self.issue())
        await self.runtime.inspect_pending(self.key)
        self.assertIsNone(self.issue())
        self.assertEqual(self.watchdog.progress[self.key], 900)
        self.assertIsNotNone(self.runtime.engine.states[self.key].last_completed)

    async def test_removal_without_loaded_runtime_clears_old_alerts(self):
        self.watchdog.start()
        self.now[0] = 900
        self.watchdog.check()
        self.assertIsNotNone(self.issue())
        del self.config.runtime_data
        with patch.object(watchdog_module.persistent_notification, "async_dismiss") as dismiss:
            await async_remove_entry(self.hass, self.config)
        self.assertIsNone(self.issue())
        dismiss.assert_called_with(self.hass, self.watchdog.notification_id)

    async def test_invalid_persisted_completion_is_rejected(self):
        with self.assertRaises(SafetyFailure):
            DeviceState.restore({"last_completed": float("nan")})
