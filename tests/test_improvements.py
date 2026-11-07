"""Regression checks for native reload recovery and credential audit scheduling."""

import copy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import yaml
from homeassistant.helpers.template import Template
from homeassistant.core import HomeAssistant, State
from homeassistant.util import dt as dt_util

from custom_components.homepod_sensor_link.audit import RecoveryAudit
from custom_components.homepod_sensor_link.engine import AuthenticationFailure, RecoveryEngine, SafetyFailure


class NativeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 0
        self.backend = SimpleNamespace(read_ha=AsyncMock(), restore_missing=AsyncMock(return_value=True))
        self.reload, self.verify, self.notify = AsyncMock(), AsyncMock(), AsyncMock()
        self.saved = []
        self.target = {"entry_id": "test"}
        self.engine = self.make_engine()

    async def save(self, data):
        self.saved.append(copy.deepcopy(data))

    def make_engine(self, saved=None):
        return RecoveryEngine(
            [self.target],
            self.backend,
            self.save,
            self.reload,
            self.notify,
            lambda: self.now,
            saved,
            verify_native=self.verify,
        )

    async def poll(self, *times):
        for self.now in times:
            await self.engine.check_all()

    async def missing_pairing(self):
        self.backend.read_ha.side_effect = [AuthenticationFailure()] * 3 + [None]
        await self.poll(0, 300, 600)
        self.backend.read_ha.side_effect = None

    async def test_reload_failure_remains_pending_then_retries_without_pairing_write(self):
        self.reload.side_effect = [OSError(), None]
        await self.missing_pairing()
        state = self.engine.states["test"]
        self.assertEqual(state.status, "native_reload_pending")
        self.assertTrue(state.native_pending)
        self.assertIsNone(state.last_verified)
        await self.poll(900)
        self.assertEqual(state.status, "healthy")
        self.assertFalse(state.native_pending)
        self.assertEqual(self.reload.await_count, 2)
        self.backend.restore_missing.assert_awaited_once()
        self.assertEqual(state.repairs, 1)
        self.assertEqual(state.last_repair, 900)

    async def test_pending_native_reload_survives_restart(self):
        self.reload.side_effect = OSError()
        await self.missing_pairing()
        self.engine = self.make_engine(self.saved[-1])
        self.reload.side_effect = None
        await self.poll(900)
        self.assertEqual(self.engine.states["test"].status, "healthy")
        self.assertEqual(self.engine.states["test"].repairs, 1)
        self.assertEqual(self.backend.restore_missing.await_count, 1)

    async def test_unavailable_native_entities_are_not_healthy_despite_successful_probe(self):
        self.verify.side_effect = OSError()
        await self.poll(0, 300, 600, 900)
        self.assertEqual(self.engine.states["test"].status, "native_reload_pending")
        self.assertIsNone(self.engine.states["test"].last_verified)
        self.assertEqual(self.reload.await_count, 3)
        self.backend.restore_missing.assert_not_called()

    async def test_native_retry_limit_survives_restart_and_expires(self):
        self.verify.side_effect = OSError()
        await self.poll(0, 300, 600)
        self.engine = self.make_engine(self.saved[-1])
        await self.poll(900)
        self.assertEqual(self.reload.await_count, 3)
        self.verify.side_effect = None
        await self.poll(3600)
        self.assertEqual(self.reload.await_count, 4)
        self.assertEqual(self.engine.states["test"].status, "healthy")

    async def test_changed_native_inventory_blocks_without_reload(self):
        self.verify.side_effect = SafetyFailure()
        await self.poll(0)
        self.assertTrue(self.engine.states["test"].blocked)
        self.reload.assert_not_called()

    async def test_successful_reload_does_not_clear_failed_native_verification(self):
        self.verify.side_effect = OSError()
        await self.missing_pairing()
        self.assertTrue(self.engine.states["test"].native_pending)
        self.assertEqual(self.engine.states["test"].repairs, 0)
        self.assertIsNone(self.engine.states["test"].last_verified)


class AuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 0
        self.backend = SimpleNamespace(
            read_ha=AsyncMock(),
            restore_missing=AsyncMock(),
            inspect_recovery=AsyncMock(return_value={"entry_id": "test", "recovery_admin_verified": True}),
        )
        self.save, self.notify = AsyncMock(), AsyncMock()
        self.engine = RecoveryEngine(
            [{"entry_id": "test"}], self.backend, self.save, AsyncMock(), AsyncMock(), lambda: self.now
        )
        self.audit = RecoveryAudit(self.engine, self.backend, self.save, self.notify, lambda: self.now)

    async def test_successful_access_audited_weekly(self):
        await self.audit.check()
        self.now = 3600
        self.assertEqual(await self.audit.check(), [])
        self.now = 7 * 86400
        await self.audit.check()
        self.assertEqual(self.backend.inspect_recovery.await_count, 2)
        self.assertEqual(self.engine.states["test"].recovery_status, "verified")
        self.backend.restore_missing.assert_not_called()

    async def test_rejected_credentials_retry_hourly_and_recover(self):
        self.backend.inspect_recovery.side_effect = SafetyFailure("private diagnostic detail")
        await self.audit.check()
        state = self.engine.states["test"]
        self.assertEqual(state.recovery_status, "rejected")
        self.assertEqual(state.recovery_error, "SafetyFailure")
        self.now = 300
        await self.audit.check()
        self.assertEqual(self.backend.inspect_recovery.await_count, 1)
        self.now = 3600
        self.backend.inspect_recovery.side_effect = None
        await self.audit.check()
        self.assertEqual(state.recovery_status, "verified")
        self.assertEqual(state.recovery_last_verified, 3600)
        self.assertFalse(state.blocked)
        self.backend.restore_missing.assert_not_called()

    async def test_network_failure_is_not_labelled_key_rejection(self):
        self.backend.inspect_recovery.side_effect = OSError("sensitive text must not be persisted")
        await self.audit.check()
        state = self.engine.states["test"]
        self.assertEqual(state.recovery_status, "unreachable")
        self.assertNotIn("sensitive", str(state.public()))

    async def test_audit_schedule_survives_engine_restart(self):
        await self.audit.check()
        restored = RecoveryEngine(
            self.engine.targets,
            self.backend,
            self.save,
            AsyncMock(),
            AsyncMock(),
            lambda: self.now,
            self.engine.snapshot(),
        )
        audit = RecoveryAudit(restored, self.backend, self.save, self.notify, lambda: self.now)
        self.now = 3600
        self.assertEqual(await audit.check(), [])
        await audit.check(force=True)
        self.assertEqual(self.backend.inspect_recovery.await_count, 2)


class WatchdogTemplateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hass = HomeAssistant("/tmp/homepod-monitor-template-tests")
        self.config = yaml.safe_load(
            (Path(__file__).parent.parent / "monitoring/heartbeat-watchdog.yaml")
            .read_text()
            .replace("!input monitored_entities", "[]")
        )
        self.entities = [
            "sensor.example_homepod_1_pairing_recovery",
            "sensor.example_homepod_2_pairing_recovery",
            "sensor.example_homepod_3_pairing_recovery",
            "sensor.example_homepod_4_pairing_recovery",
        ]
        self.template = Template(self.config["actions"][0]["variables"]["stalled"], self.hass)

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)

    def set_age(self, entity, minutes):
        self.hass.states.async_set(
            entity, "healthy", {"last_checked": (dt_util.utcnow() - timedelta(minutes=minutes)).isoformat()}
        )

    async def test_fresh_heartbeat_quiet_even_when_measurement_does_not_change(self):
        for entity in self.entities:
            self.set_age(entity, 5)
        self.assertEqual(self.template.async_render({"monitored_entities": self.entities}), [])

    async def test_stale_and_missing_heartbeat_detected(self):
        for entity in self.entities[:3]:
            self.set_age(entity, 5)
        self.set_age(self.entities[1], 16)
        self.assertEqual(
            self.template.async_render({"monitored_entities": self.entities}), [self.entities[1], self.entities[3]]
        )

    async def test_startup_grace_and_normal_monitoring(self):
        condition = Template(self.config["conditions"][0]["value_template"], self.hass)
        recent = State(
            "automation.homepod_sensor_link_watchdog", "on", last_changed=dt_util.utcnow() - timedelta(minutes=5)
        )
        old = State(
            "automation.homepod_sensor_link_watchdog", "on", last_changed=dt_util.utcnow() - timedelta(minutes=20)
        )
        self.assertFalse(condition.async_render({"this": recent}))
        self.assertTrue(condition.async_render({"this": old}))
