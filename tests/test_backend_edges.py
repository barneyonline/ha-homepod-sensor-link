"""Fresh read, pairing-table and endpoint failures never become write authority."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from custom_components.homepod_sensor_link.backend import HomeKitBackend
from custom_components.homepod_sensor_link.engine import (
    SafetyFailure,
    RecoveryPaused,
    canonical,
    DeviceState,
    RecoveryEngine,
    AuthenticationFailure,
)
from custom_components.homepod_sensor_link.audit import RecoveryAudit
from test_recovery import fixture


class BackendEdgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.target, self.entry = fixture()
        self.target["owned"] = True
        self.target["pairing"] = self.entry.data
        self.pairing = SimpleNamespace(
            list_accessories_and_characteristics=AsyncMock(),
            get_characteristics=AsyncMock(),
            list_pairings=AsyncMock(),
            add_pairing=AsyncMock(),
        )
        self.backend = HomeKitBackend(Mock(), Mock(), lambda _: self.entry)

        @asynccontextmanager
        async def session(*args, **kwargs):
            yield self.pairing

        self.backend.session = session

    def layout(self, extra=()):
        return [
            {
                "aid": 1,
                "services": [
                    {
                        "characteristics": [
                            {"iid": 2, "type": "10", "perms": ["pr"]},
                            {"iid": 3, "type": "11", "perms": ["pr"]},
                            *extra,
                        ]
                    }
                ],
            }
        ]

    async def test_fresh_sensor_layout_values_and_ambiguous_measurements_fail_closed(self):
        self.pairing.list_accessories_and_characteristics.return_value = []
        with self.assertRaises(SafetyFailure):
            await self.backend.read_ha(self.target)
        self.pairing.list_accessories_and_characteristics.return_value = self.layout(
            [{"iid": 4, "type": "25", "perms": ["pr"]}]
        )
        for values in (
            {},
            {(1, 2): {"value": True}, (1, 3): {"value": 20}},
            {(1, 2): {"value": 40, "status": -1}, (1, 3): {"value": 20}},
            {(1, 2): {"value": float("nan")}, (1, 3): {"value": 20}},
        ):
            self.pairing.get_characteristics.return_value = values
            with self.assertRaises(OSError):
                await self.backend.read_ha(self.target)
        self.pairing.list_accessories_and_characteristics.return_value = self.layout(
            [{"iid": 4, "type": "11", "perms": ["pr"]}]
        )
        self.pairing.get_characteristics.return_value = {
            (1, 2): {"value": 40},
            (1, 3): {"value": 20},
            (1, 4): {"value": 21},
        }
        with self.assertRaises(SafetyFailure):
            await self.backend.read_ha(self.target)
        self.target["owned"] = False
        self.assertEqual((await self.backend.read_ha(self.target))["temperature"], 21)
        self.target["expected_pairings"] = [
            {"pairingId": self.target["ha_id"], "publicKey": self.target["ha_key"], "permissions": 1}
        ]
        self.backend.inspect_recovery = AsyncMock(return_value={"ha_identity_present": False})
        with self.assertRaises(OSError):
            await self.backend.read_ha(self.target)
        self.pairing.add_pairing.assert_not_called()

    async def test_admin_and_ha_table_pins_are_validated_and_accepted_checkpoint_clears(self):
        admin = {
            "pairingId": self.target["source"]["iOSPairingId"],
            "publicKey": self.target["source"]["iOSDeviceLTPK"],
            "permissions": 1,
        }
        own = {"pairingId": self.target["ha_id"], "publicKey": self.target["ha_key"], "permissions": 1}
        for rows in ([{**admin, "permissions": 0}], [admin, {**own, "permissions": 0}]):
            self.pairing.list_pairings.return_value = rows
            with self.assertRaises(SafetyFailure):
                await self.backend.inspect_recovery(self.target)
        self.pairing.list_pairings.return_value = [admin]
        self.assertFalse(await self.backend.pairing_present(self.target))
        self.target["expected_pairings"] = [admin, own]
        self.backend.write_allowed = lambda: False
        with self.assertRaises(RecoveryPaused):
            await self.backend.restore_missing(self.target)
        self.pairing.list_pairings.return_value = [admin, own]
        self.backend.after_write = AsyncMock()
        self.assertFalse(await self.backend.restore_missing(self.target))
        self.assertNotIn("expected_pairings", self.target)
        self.backend.after_write.assert_awaited_once()
        self.assertTrue(await self.backend.pairing_present(self.target))
        self.pairing.add_pairing.assert_not_called()

    async def test_discovery_endpoint_checks_pin_before_falling_back_or_connecting(self):
        self.entry.data.update(AccessoryIPs=["192.0.2.1"], AccessoryPort=12345)
        self.backend.controller.async_find = AsyncMock(
            return_value=SimpleNamespace(description=SimpleNamespace(name="candidate"))
        )
        info = SimpleNamespace(async_request=AsyncMock(return_value=True))
        description = SimpleNamespace(id="changed", addresses=["192.0.2.2"], port=12345)
        with (
            patch("custom_components.homepod_sensor_link.backend.AsyncServiceInfo", return_value=info),
            patch(
                "custom_components.homepod_sensor_link.backend.HomeKitService.from_service_info",
                return_value=description,
            ),
        ):
            with self.assertRaises(SafetyFailure):
                await self.backend.endpoint(self.target)
            description.id = self.target["accessory_id"]
            self.assertEqual((await self.backend.endpoint(self.target))["AccessoryIP"], "192.0.2.2")
            info.async_request.return_value = False
            self.assertEqual((await self.backend.endpoint(self.target))["AccessoryIP"], "192.0.2.1")


class EngineEdgeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.backend = SimpleNamespace(read_ha=AsyncMock(), restore_missing=AsyncMock(), inspect_recovery=AsyncMock())
        self.save, self.notify, self.reload, self.verify = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
        self.now = 0
        self.active = True
        self.engine = RecoveryEngine(
            [{"entry_id": "device"}],
            self.backend,
            self.save,
            self.reload,
            self.notify,
            lambda: self.now,
            verify_native=self.verify,
            is_active=lambda: self.active,
        )

    async def test_invalid_persisted_and_remote_pairing_shapes_are_refused(self):
        with self.assertRaises(SafetyFailure):
            DeviceState.restore([])
        with self.assertRaises(SafetyFailure):
            RecoveryEngine([], self.backend, self.save, self.reload, self.notify, lambda: 0, states=[])
        for rows in ([], {}, [{"pairingId": "x", "publicKey": "ab" * 32, "permissions": False}]):
            with self.assertRaises(SafetyFailure):
                canonical(rows)

    async def test_pause_at_outer_task_and_after_probe_never_permits_repair(self):
        self.active = False
        await self.engine.check_all()
        self.backend.read_ha.assert_not_called()
        self.assertEqual(await RecoveryAudit(self.engine, self.backend, self.save, self.notify, lambda: 0).check(), [])
        # Ownership may change after the lock is acquired but before queued work.
        calls = iter([True, False])
        self.engine.is_active = lambda: next(calls)
        await self.engine.check_all()
        self.backend.read_ha.assert_not_called()
        self.engine.is_active = lambda: self.active
        self.active = True

        async def failed(target):
            self.active = False
            raise AuthenticationFailure

        self.backend.read_ha.side_effect = failed
        await self.engine.check_all()
        self.backend.restore_missing.assert_not_called()

    async def test_monitor_only_pending_and_native_safety_failures(self):
        state = self.engine.states["device"]
        self.engine.automatic_recovery = False
        self.verify.side_effect = OSError()
        await self.engine.check_all()
        self.assertEqual(state.status, "native_unavailable")
        state.native_pending = True
        self.verify.side_effect = None
        await self.engine.check_all()
        self.assertEqual(state.reason, "automatic_recovery_disabled")
        self.engine.automatic_recovery = True
        self.reload.side_effect = SafetyFailure()
        await self.engine.check_all()
        self.assertTrue(state.blocked)

    async def test_accepted_pairing_rejection_and_repair_safety_do_not_readd(self):
        state = self.engine.states["device"]
        self.backend.read_ha.side_effect = AuthenticationFailure()
        state.pending_pairing_added = True
        await self.engine.check_all()
        self.assertEqual(state.reason, "accepted_pairing_authentication_failed")
        state.pending_pairing_added = False
        self.backend.restore_missing.side_effect = SafetyFailure()
        for self.now in (300, 600):
            await self.engine.check_all()
        self.assertTrue(state.blocked)

    async def test_admin_audit_skips_monitor_only_device(self):
        self.engine.targets[0]["recovery"] = False
        audit = RecoveryAudit(self.engine, self.backend, self.save, self.notify, lambda: 0)
        self.assertEqual(await audit.check(force=True), [])
        self.backend.inspect_recovery.assert_not_called()

    async def test_brief_network_outage_does_not_notify_until_last_success_is_stale(self):
        self.engine.states["device"].last_verified = 0
        self.backend.read_ha.side_effect = OSError()
        self.now = 1
        await self.engine.check_all()
        self.notify.assert_not_called()
        self.now = 600
        await self.engine.check_all()
        self.notify.assert_awaited_once()
        self.backend.restore_missing.assert_not_called()
