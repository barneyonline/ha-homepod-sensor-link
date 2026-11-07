"""Policy and preservation regressions, using generated keys and fake devices."""

from contextlib import asynccontextmanager
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from aiohomekit.exceptions import AccessoryNotFoundError

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from custom_components.homepod_sensor_link.engine import (
    AuthenticationFailure,
    DeviceState,
    RecoveryEngine,
    SafetyFailure,
)
from custom_components.homepod_sensor_link.backend import HomeKitBackend, check_current
from custom_components.homepod_sensor_link.credentials import validate_target
from custom_components.homepod_sensor_link.secure_storage import read_private
from custom_components.homepod_sensor_link.vault import DATA_FILE as KEY_FILE


def keypair():
    private = Ed25519PrivateKey.generate()
    return (
        private.private_bytes_raw().hex(),
        private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw).hex(),
    )


def fixture(index=0):
    secret, public = keypair()
    source_secret, source_public = keypair()
    target = {
        "entry_id": f"entry-{index}",
        "accessory_id": f"aa:bb:cc:dd:ee:{index:02x}",
        "accessory_key": "ab" * 32,
        "ha_id": "ha-controller",
        "ha_key": public,
        "source": {"iOSPairingId": "apple-controller", "iOSDeviceLTPK": source_public, "iOSDeviceLTSK": source_secret},
    }
    data = {
        "Connection": "IP",
        "AccessoryPairingID": target["accessory_id"],
        "AccessoryLTPK": target["accessory_key"],
        "iOSPairingId": target["ha_id"],
        "iOSDeviceLTPK": public,
        "iOSDeviceLTSK": secret,
    }
    entry = SimpleNamespace(entry_id=target["entry_id"], data=data, domain="homekit_controller", disabled_by=None)
    return target, entry


class PolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.target, _ = fixture()
        self.now = 0
        self.saved = []
        self.backend = SimpleNamespace(read_ha=AsyncMock(), restore_missing=AsyncMock(return_value=True))
        self.reload = AsyncMock()
        self.notify = AsyncMock()

    async def save(self, data):
        self.saved.append(copy.deepcopy(data))

    def engine(self, saved=None):
        return RecoveryEngine([self.target], self.backend, self.save, self.reload, self.notify, lambda: self.now, saved)

    async def advance(self, engine, times=(0, 300, 600)):
        for self.now in times:
            await engine.check_all()

    async def test_healthy_never_accesses_recovery_identity(self):
        engine = self.engine()
        await self.advance(engine)
        self.backend.restore_missing.assert_not_called()
        self.reload.assert_not_called()
        self.assertEqual(engine.states["entry-0"].status, "healthy")

    async def test_network_failure_never_writes_pairing(self):
        self.backend.read_ha.side_effect = OSError()
        await self.advance(self.engine())
        self.backend.restore_missing.assert_not_called()
        self.reload.assert_not_called()

    async def test_three_auth_failures_then_repair_verify_reload(self):
        self.backend.read_ha.side_effect = [AuthenticationFailure()] * 3 + [None]
        engine = self.engine()
        await self.advance(engine)
        self.backend.restore_missing.assert_awaited_once_with(self.target)
        self.reload.assert_awaited_once_with(self.target)
        state = engine.states["entry-0"]
        self.assertEqual((state.status, state.repairs, state.last_repair), ("healthy", 1, 600))
        self.assertTrue(
            any(s["entry-0"]["status"] == "repairing" and s["entry-0"]["attempts"] == [600] for s in self.saved)
        )

    async def test_restart_preserves_attempts_and_observes_startup_grace(self):
        self.backend.read_ha.side_effect = AuthenticationFailure()
        engine = self.engine()
        self.backend.restore_missing.side_effect = OSError()
        await self.advance(engine)
        restored = self.engine(self.saved[-1])
        await self.advance(restored, (900, 1200, 1500))
        self.assertEqual(self.backend.restore_missing.await_count, 1)
        self.assertEqual(restored.states["entry-0"].status, "cooldown")

    async def test_mismatch_blocks_without_writing(self):
        self.backend.read_ha.side_effect = SafetyFailure()
        engine = self.engine()
        await self.advance(engine)
        self.backend.restore_missing.assert_not_called()
        self.assertTrue(engine.states["entry-0"].blocked)
        self.assertEqual(self.backend.read_ha.await_count, 1)

    async def test_failed_post_repair_verification_does_not_reload(self):
        self.backend.read_ha.side_effect = AuthenticationFailure()
        engine = self.engine()
        await self.advance(engine)
        self.reload.assert_not_called()
        self.assertEqual(engine.states["entry-0"].status, "repair_failed")

    async def test_healthy_resets_authentication_streak(self):
        self.backend.read_ha.side_effect = [AuthenticationFailure(), None, AuthenticationFailure()]
        engine = self.engine()
        await self.advance(engine)
        self.assertEqual(engine.states["entry-0"].auth_failures, 1)
        self.backend.restore_missing.assert_not_called()

    async def test_three_attempts_maximum_per_day(self):
        self.backend.read_ha.side_effect = AuthenticationFailure()
        self.backend.restore_missing.side_effect = OSError()
        engine = self.engine()
        await self.advance(engine, (0, 300, 600, 4200, 7800, 11400, 15000))
        self.assertEqual(self.backend.restore_missing.await_count, 3)
        self.assertEqual(engine.states["entry-0"].status, "cooldown")
        self.now = 87001
        await engine.check_all()
        self.assertEqual(self.backend.restore_missing.await_count, 4)

    async def test_overlapping_checks_are_skipped(self):
        engine = self.engine()
        async with engine.lock:
            await engine.check_all()
        self.backend.read_ha.assert_not_called()

    async def test_backwards_clock_cannot_bypass_cooldown(self):
        self.backend.read_ha.side_effect = AuthenticationFailure()
        state = DeviceState(auth_since=-2000, auth_failures=3, attempts=[10000])
        engine = self.engine({"entry-0": state.public()})
        await self.advance(engine)
        self.backend.restore_missing.assert_not_called()


class PairingPreservationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.target, self.entry = fixture()
        self.rows = [
            {"pairingId": "apple-controller", "publicKey": self.target["source"]["iOSDeviceLTPK"], "permissions": 1},
            {"pairingId": "other-apple-controller", "publicKey": "cd" * 32, "permissions": 1},
        ]
        self.pairing = SimpleNamespace(
            list_pairings=AsyncMock(side_effect=lambda: copy.deepcopy(self.rows)),
            add_pairing=AsyncMock(side_effect=self.add),
        )
        self.backend = HomeKitBackend(None, None, lambda _: self.entry)

        @asynccontextmanager
        async def session(*_args, **_kwargs):
            yield self.pairing

        self.backend.session = session

    async def add(self, identifier, public_key, permission):
        self.assertEqual((identifier, public_key, permission), (self.target["ha_id"], self.target["ha_key"], "Admin"))
        self.rows.append({"pairingId": identifier, "publicKey": public_key, "permissions": 1})

    async def test_only_known_missing_public_key_added_and_others_preserved(self):
        original = copy.deepcopy(self.rows)
        self.assertTrue(await self.backend.restore_missing(self.target))
        self.assertEqual(self.rows[:2], original)
        self.pairing.add_pairing.assert_awaited_once()

    async def test_existing_matching_pairing_is_noop(self):
        await self.add(self.target["ha_id"], self.target["ha_key"], "Admin")
        self.assertFalse(await self.backend.restore_missing(self.target))
        self.pairing.add_pairing.assert_not_called()

    async def test_existing_conflicting_public_key_is_never_overwritten(self):
        self.rows.append({"pairingId": self.target["ha_id"], "publicKey": "ef" * 32, "permissions": 1})
        with self.assertRaises(SafetyFailure):
            await self.backend.restore_missing(self.target)
        self.pairing.add_pairing.assert_not_called()

    async def test_changed_ha_permissions_fail_closed_without_readding(self):
        self.rows.append({"pairingId": self.target["ha_id"], "publicKey": self.target["ha_key"], "permissions": 0})
        with self.assertRaises(SafetyFailure):
            await self.backend.restore_missing(self.target)
        self.pairing.add_pairing.assert_not_called()

    async def test_non_admin_recovery_identity_cannot_add(self):
        self.rows[0]["permissions"] = 0
        with self.assertRaises(SafetyFailure):
            await self.backend.restore_missing(self.target)
        self.pairing.add_pairing.assert_not_called()

    async def test_unrelated_pairing_change_is_detected(self):
        async def bad_add(*args):
            await self.add(*args)
            self.rows[1]["permissions"] = 0

        self.pairing.add_pairing.side_effect = bad_add
        with self.assertRaises(SafetyFailure):
            await self.backend.restore_missing(self.target)


class CredentialTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.records = [fixture(i) for i in range(4)]
        self.entries = {t["entry_id"]: e for t, e in self.records}
        self.file = Path(self.directory.name) / KEY_FILE
        self.file.parent.mkdir(mode=0o700, parents=True)
        self.write()

    def write(self):
        self.file.write_text(json.dumps({"version": 1, "targets": [t for t, _ in self.records]}))
        self.file.chmod(0o600)

    def load(self):
        targets = read_private(self.file)["targets"]
        for target in targets:
            validate_target(target, self.entries.get(target["entry_id"]))
        return targets

    def test_multiple_native_target_validation(self):
        self.assertEqual(len(self.load()), 4)

    def test_world_readable_file_rejected(self):
        self.file.chmod(0o644)
        with self.assertRaises(ValueError):
            self.load()

    def test_non_private_directory_rejected(self):
        self.file.parent.chmod(0o755)
        with self.assertRaises(ValueError):
            self.load()

    def test_symlink_rejected(self):
        actual = self.file.with_name("elsewhere.json")
        self.file.rename(actual)
        self.file.symlink_to(actual)
        with self.assertRaises(OSError):
            self.load()

    def test_source_private_public_mismatch_rejected(self):
        self.records[0][0]["source"]["iOSDeviceLTSK"] = "ab" * 32
        self.write()
        with self.assertRaises(SafetyFailure):
            self.load()

    def test_ha_private_public_mismatch_rejected(self):
        self.records[0][1].data["iOSDeviceLTSK"] = "ab" * 32
        with self.assertRaises(SafetyFailure):
            self.load()

    def test_replaced_accessory_or_controller_blocked(self):
        for key in ("AccessoryPairingID", "AccessoryLTPK", "iOSPairingId", "iOSDeviceLTPK"):
            with self.subTest(key=key):
                original = self.records[0][1].data[key]
                self.records[0][1].data[key] = "different"
                with self.assertRaises(SafetyFailure):
                    self.load()
                self.records[0][1].data[key] = original

    def test_disabled_or_removed_native_entry_blocked(self):
        target, entry = self.records[0]
        entry.disabled_by = "user"
        with self.assertRaises(SafetyFailure):
            check_current(target, entry)
        with self.assertRaises(SafetyFailure):
            check_current(target, None)


class EndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.target, self.entry = fixture()
        self.entry.data.update(AccessoryIP="192.0.2.18", AccessoryIPs=["192.0.2.18"], AccessoryPort=12345)
        self.controller = SimpleNamespace(async_find=AsyncMock(side_effect=AccessoryNotFoundError("missing PTR")))
        self.backend = HomeKitBackend(self.controller, SimpleNamespace(zeroconf=object()), lambda _: self.entry)

    async def test_missing_ptr_uses_current_native_endpoint(self):
        endpoint = await self.backend.endpoint(self.target)
        self.assertEqual(endpoint["AccessoryIP"], "192.0.2.18")
        self.assertEqual(endpoint["AccessoryPort"], 12345)
        self.entry.data["AccessoryPort"] = 23456
        self.assertEqual((await self.backend.endpoint(self.target))["AccessoryPort"], 23456)

    async def test_fallback_still_rejects_changed_native_accessory_identity(self):
        self.entry.data["AccessoryLTPK"] = "cd" * 32
        with self.assertRaises(SafetyFailure):
            await self.backend.endpoint(self.target)

    async def test_advertised_wrong_identity_never_falls_back(self):
        self.controller.async_find.side_effect = None
        self.controller.async_find.return_value = SimpleNamespace(
            description=SimpleNamespace(name="HomePodSensor test")
        )
        info = SimpleNamespace(async_request=AsyncMock(return_value=True))
        with (
            patch("custom_components.homepod_sensor_link.backend.AsyncServiceInfo", return_value=info),
            patch(
                "custom_components.homepod_sensor_link.backend.HomeKitService.from_service_info",
                return_value=SimpleNamespace(id="different"),
            ),
        ):
            with self.assertRaises(SafetyFailure):
                await self.backend.endpoint(self.target)

    async def test_discovery_refresh_failure_uses_current_native_endpoint(self):
        self.controller.async_find.side_effect = None
        self.controller.async_find.return_value = SimpleNamespace(
            description=SimpleNamespace(name="HomePodSensor test")
        )
        info = SimpleNamespace(async_request=AsyncMock(return_value=False))
        with patch("custom_components.homepod_sensor_link.backend.AsyncServiceInfo", return_value=info):
            self.assertEqual((await self.backend.endpoint(self.target))["AccessoryPort"], 12345)

    async def test_unusable_stored_endpoint_fails_without_connection(self):
        for addresses, port in [
            ([], 12345),
            (["not-an-ip"], 12345),
            (["0.0.0.0"], 12345),
            (["192.0.2.18"], 0),
            (["192.0.2.18"], 65536),
        ]:
            with self.subTest(addresses=addresses, port=port):
                with self.assertRaises(OSError):
                    self.backend._endpoint(addresses, port)


if __name__ == "__main__":
    unittest.main()
