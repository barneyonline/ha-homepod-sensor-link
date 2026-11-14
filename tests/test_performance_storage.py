"""Count offline polling I/O and credential copies without weakening checkpoints."""

from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant

from custom_components.homepod_sensor_link import vault as vault_module
from custom_components.homepod_sensor_link.audit import AUDIT_INTERVAL, AUDIT_RETRY, RecoveryAudit
from custom_components.homepod_sensor_link.engine import AuthenticationFailure, INTERVAL, RecoveryEngine, SafetyFailure
from custom_components.homepod_sensor_link.provisioning import make_owned
from custom_components.homepod_sensor_link.vault import DATA_FILE, Vault
import test_onboarding_v2 as fixtures


class AuditOperationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = 0
        self.backend = SimpleNamespace(inspect_recovery=AsyncMock(return_value={"verified": True}))
        self.save = AsyncMock()
        self.engine = self.make_engine([{"entry_id": "disposable", "recovery": True}])
        self.audit = self.make_audit(self.engine)

    def make_engine(self, targets, states=None):
        return RecoveryEngine(targets, self.backend, self.save, AsyncMock(), AsyncMock(), lambda: self.now, states)

    def make_audit(self, engine):
        return RecoveryAudit(engine, self.backend, self.save, AsyncMock(), lambda: self.now)

    async def test_seven_days_of_verified_polling_commit_only_two_due_audits(self):
        polls = 0
        for self.now in range(0, AUDIT_INTERVAL + 1, INTERVAL):
            polls += 1
            await self.audit.check()
        self.assertEqual(polls, 2017)
        self.assertEqual(self.backend.inspect_recovery.await_count, 2)
        self.assertEqual(self.save.await_count, 2)
        self.assertEqual(self.save.call_args.args[0]["disposable"]["recovery_last_checked"], AUDIT_INTERVAL)

    async def test_seven_days_of_rejected_access_commit_every_hourly_attempt(self):
        self.backend.inspect_recovery.side_effect = SafetyFailure()
        for self.now in range(0, AUDIT_INTERVAL + 1, INTERVAL):
            await self.audit.check()
        self.assertEqual(self.backend.inspect_recovery.await_count, 169)
        self.assertEqual(self.save.await_count, 169)
        state = self.save.call_args.args[0]["disposable"]
        self.assertEqual(state["recovery_last_checked"], AUDIT_INTERVAL)
        self.assertEqual(state["recovery_status"], "rejected")

    async def test_forced_audits_bypass_schedule_and_monitor_only_never_commits(self):
        await self.audit.check()
        self.now = INTERVAL
        self.assertEqual(await self.audit.check(), [])
        self.assertEqual(self.save.await_count, 1)
        await self.audit.check(force=True)
        self.assertEqual(self.save.await_count, 2)
        self.assertEqual(self.backend.inspect_recovery.await_count, 2)
        monitor = self.make_audit(self.make_engine([{"entry_id": "monitor", "recovery": False}]))
        self.save.reset_mock()
        self.backend.inspect_recovery.reset_mock()
        for force in (False, True):
            self.assertEqual(await monitor.check(force=force), [])
        self.save.assert_not_awaited()
        self.backend.inspect_recovery.assert_not_awaited()

    async def test_restart_preserves_success_and_failure_schedules_without_skip_commits(self):
        for failure, interval in ((False, AUDIT_INTERVAL), (True, AUDIT_RETRY)):
            with self.subTest(failure=failure):
                self.now = 0
                self.save.reset_mock()
                self.backend.inspect_recovery.side_effect = SafetyFailure() if failure else None
                engine = self.make_engine(self.engine.targets)
                await self.make_audit(engine).check()
                persisted = self.save.call_args.args[0]
                restored = self.make_audit(self.make_engine(engine.targets, persisted))
                self.now = interval - INTERVAL
                self.assertEqual(await restored.check(), [])
                self.assertEqual(self.save.await_count, 1)
                self.now = interval
                self.assertEqual(len(await restored.check()), 1)
                self.assertEqual(self.save.await_count, 2)


class ProtectedStorageOperationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.hass = HomeAssistant(self.directory.name)
        self.vault = await Vault(self.hass).load()

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)

    async def test_state_only_save_does_not_copy_credentials_and_still_commits(self):
        for count in (1, 16, 64):
            copies = []

            class CountedRecord(dict):
                def __deepcopy__(self, memo):
                    copies.append(True)
                    return deepcopy(dict(self), memo)

            self.vault.data["records"] = {
                str(index): CountedRecord(target={"entry_id": str(index)}, phase="active") for index in range(count)
            }
            state = {"disposable": {"attempts": [1], "last_checked": 2}}
            with patch.object(vault_module.os, "fsync", wraps=vault_module.os.fsync) as fsync:
                await self.vault.save_states(state)
                self.assertEqual(copies, [])
                self.assertEqual(fsync.call_count, 2)
            state["disposable"]["attempts"].append(3)
            self.assertEqual(self.vault.data["states"]["disposable"]["attempts"], [1])
            reloaded = await Vault(self.hass).load()
            self.assertEqual(len(reloaded.records()), count)
            self.assertEqual(reloaded.data["states"]["disposable"]["attempts"], [1])

    async def test_skipped_audit_performs_no_protected_journal_io_or_fsync(self):
        now = [0]
        backend = SimpleNamespace(inspect_recovery=AsyncMock(return_value={"verified": True}))
        engine = RecoveryEngine(
            [{"entry_id": "disposable"}],
            backend,
            self.vault.save_states,
            AsyncMock(),
            AsyncMock(),
            lambda: now[0],
        )
        audit = RecoveryAudit(engine, backend, self.vault.save_states, AsyncMock(), lambda: now[0])
        location = Path(self.directory.name) / DATA_FILE
        with patch.object(vault_module.os, "fsync", wraps=vault_module.os.fsync) as fsync:
            await audit.check()
            self.assertEqual(fsync.call_count, 2)
            persisted = location.read_bytes()
            self.assertEqual(location.stat().st_mode & 0o777, 0o600)
            self.assertEqual(location.parent.stat().st_mode & 0o777, 0o700)
            now[0] = INTERVAL
            with patch.object(vault_module, "_write", wraps=vault_module._write) as write:
                self.assertEqual(await audit.check(), [])
                write.assert_not_called()
            self.assertEqual(fsync.call_count, 2)
            self.assertEqual(location.read_bytes(), persisted)
            await audit.check(force=True)
            self.assertEqual(fsync.call_count, 4)
        reloaded = await Vault(self.hass).load()
        self.assertEqual(reloaded.data["states"]["disposable"]["recovery_last_checked"], INTERVAL)

    async def test_unchanged_checkpoint_recreates_missing_file_and_rejects_unsafe_mode(self):
        record = {"target": {"entry_id": "disposable"}, "phase": "prepared"}
        await self.vault.put("disposable", record)
        location = Path(self.directory.name) / DATA_FILE
        location.unlink()
        with patch.object(vault_module.os, "fsync", wraps=vault_module.os.fsync) as fsync:
            await self.vault.put("disposable", record)
            self.assertEqual(fsync.call_count, 2)
        self.assertEqual((await Vault(self.hass).load()).record("disposable"), record)
        location.chmod(0o644)
        with self.assertRaises(ValueError):
            await self.vault.put("disposable", record)

    async def test_sixty_four_individual_reads_copy_sixty_four_records_and_preserve_isolation(self):
        copies = []

        class CountedRecord(dict):
            def __deepcopy__(self, memo):
                copies.append(self["target"]["entry_id"])
                return deepcopy(dict(self), memo)

        self.vault.data["records"] = {
            str(index): CountedRecord(
                target={"entry_id": str(index), "pairing": {"metadata": ["disposable"]}}, phase="prepared"
            )
            for index in range(64)
        }
        for key in self.vault.data["records"]:
            self.vault.records()[key]
        self.assertEqual(len(copies), 4096)
        copies.clear()
        for key in self.vault.data["records"]:
            record = self.vault.record(key)
            record["target"]["pairing"]["metadata"].append("caller mutation")
            self.assertEqual(self.vault.data["records"][key]["target"]["pairing"]["metadata"], ["disposable"])
        self.assertEqual(len(copies), 64)
        self.assertEqual(set(copies), set(self.vault.data["records"]))
        with self.assertRaises(KeyError):
            self.vault.record("missing")


class PendingStorageOperationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.OnboardingTests.asyncSetUp
    asyncTearDown = fixtures.OnboardingTests.asyncTearDown
    make_runtime = fixtures.OnboardingTests.make_runtime

    async def test_unchanged_pending_failures_commit_only_the_normal_journal(self):
        cases = (
            (OSError, "device_unreachable", False),
            (AuthenticationFailure, "resume_required", False),
            (AuthenticationFailure, "recovery_pending", True),
        )
        _, runtime = await self.make_runtime(recovery=False)
        try:
            for failure, error, commissioned in cases:
                for count in (1, 16, 64):
                    with self.subTest(devices=count, error=error):
                        await self.check_pending_poll_counts(runtime, count, failure, error, commissioned)
        finally:
            await runtime.stop()

    async def check_pending_poll_counts(self, runtime, count, failure, error, commissioned):
        records = {}
        for index in range(count):
            target = make_owned(f"synthetic-{index}", self.target["accessory_key"], {})
            target["recovery"] = False
            records[target["entry_id"]] = {
                "target": target,
                "phase": "prepared",
                "error": error,
                "commissioned": commissioned,
            }
        self.vault.data["records"] = deepcopy(records)
        await self.vault.save_states({})
        runtime.records = records
        runtime.engine.states.clear()
        runtime.saved.clear()
        runtime.sync_targets()
        self.backend.read_ha.side_effect = failure
        for _ in range(2):
            self.backend.read_ha.reset_mock()
            with (
                patch.object(self.vault, "put", wraps=self.vault.put) as put,
                patch.object(vault_module, "_write", wraps=vault_module._write) as write,
                patch.object(vault_module.os, "fsync", wraps=vault_module.os.fsync) as fsync,
            ):
                await runtime.update()
                self.assertEqual(self.backend.read_ha.await_count, count)
                put.assert_not_awaited()
                write.assert_called_once()
                self.assertEqual(fsync.call_count, 2)
            self.assertEqual((await Vault(self.hass).load()).records(), records)
        self.backend.restore_missing.assert_not_awaited()

    async def test_auth_rejection_persists_consumed_authorisation_and_endpoint(self):
        self.record.update(error="resume_required", authorised_boot="previous-boot")
        await self.vault.put(self.key, self.record)
        self.backend.read_ha.side_effect = AuthenticationFailure()
        with patch.object(self.vault, "put", wraps=self.vault.put) as put:
            await self.provisioner.inspect(self.key)
            put.assert_awaited_once()
        self.assertNotIn("authorised_boot", (await Vault(self.hass).load()).record(self.key))

        async def moved_then_rejected(target):
            target["pairing"]["AccessoryIP"] = "192.0.2.2"
            raise AuthenticationFailure()

        self.backend.read_ha.side_effect = moved_then_rejected
        with patch.object(self.vault, "put", wraps=self.vault.put) as put:
            await self.provisioner.inspect(self.key)
            put.assert_awaited_once()
        self.assertEqual(
            (await Vault(self.hass).load()).record(self.key)["target"]["pairing"]["AccessoryIP"], "192.0.2.2"
        )
        self.backend.restore_missing.assert_not_awaited()

    async def test_first_failure_changed_endpoint_and_explicit_resume_commit(self):
        await self.vault.put(self.key, self.record)
        self.backend.read_ha.side_effect = OSError()
        with patch.object(self.vault, "put", wraps=self.vault.put) as put:
            await self.provisioner.inspect(self.key)
            put.assert_awaited_once()
        self.assertEqual(self.vault.record(self.key)["error"], "device_unreachable")

        async def moved_then_failed(target):
            target["pairing"]["AccessoryIP"] = "192.0.2.2"
            raise OSError()

        self.backend.read_ha.side_effect = moved_then_failed
        with patch.object(self.vault, "put", wraps=self.vault.put) as put:
            await self.provisioner.inspect(self.key)
            put.assert_awaited_once()
        reloaded = await Vault(self.hass).load()
        self.assertEqual(reloaded.record(self.key)["target"]["pairing"]["AccessoryIP"], "192.0.2.2")

        paused = reloaded.record(self.key)
        paused["cancelled"] = True
        await self.vault.put(self.key, paused)
        self.backend.read_ha.side_effect = OSError()
        with patch.object(self.vault, "put", wraps=self.vault.put) as put:
            await self.provisioner.inspect(self.key, resume=True)
            put.assert_awaited_once()
        self.assertFalse((await Vault(self.hass).load()).record(self.key)["cancelled"])
        self.backend.restore_missing.assert_not_awaited()
