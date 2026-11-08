"""Controller import, persisted commissioning and owned sensor regressions."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntry, ConfigEntries
from homeassistant.core import HomeAssistant, CoreState
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.data_entry_flow import FlowResultType

from custom_components.homepod_sensor_link import (
    Runtime,
    OWNER,
    async_remove_entry,
    async_remove_config_entry_device,
    async_setup_entry,
    async_setup,
    async_unload_entry,
)
from custom_components.homepod_sensor_link.backend import HomeKitBackend
from custom_components.homepod_sensor_link.config_flow import HomePodSensorLinkFlow
from custom_components.homepod_sensor_link.credentials import DOMAIN, public_targets
from custom_components.homepod_sensor_link.engine import AuthenticationFailure, SafetyFailure
from custom_components.homepod_sensor_link.importer import parse_export, MAX_IMPORT
from custom_components.homepod_sensor_link.provisioning import make_owned, Provisioner, prepare, boot_id
from custom_components.homepod_sensor_link.sensor import Measurement, RecoveryStatus
from custom_components.homepod_sensor_link.ui import current_request
from custom_components.homepod_sensor_link.vault import get_vault, DATA_FILE
from test_recovery import fixture, keypair
from scripts.macos_homekit_export import parse_dump


def entry(data=None):
    return ConfigEntry(
        domain=DOMAIN,
        title="HomePod Sensor Link",
        data=data or {},
        options={},
        source="user",
        unique_id=DOMAIN,
        version=2,
        minor_version=1,
        discovery_keys={},
        subentries_data=None,
    )


def export_fixture():
    seed, public = keypair()
    accessory = fixture()[0]
    source = {"iOSPairingId": "disposable-owner", "iOSDeviceLTSK": seed, "iOSDeviceLTPK": public}
    return (
        source,
        accessory,
        {
            "controllers": [source],
            "accessories": [
                {"AccessoryPairingID": accessory["accessory_id"], "AccessoryLTPK": accessory["accessory_key"]}
            ],
        },
    )


class ImportParserTests(unittest.TestCase):
    def test_automatically_converted_macos_dump_is_accepted_without_key_reentry(self):
        source, accessory, _ = export_fixture()
        dump = (
            "HomeKit Pairing Identity (com.apple.hap.pairing)\n"
            f"Account: {source['iOSPairingId']}\nKey: {source['iOSDeviceLTPK']}{source['iOSDeviceLTSK']}\n\n"
            "Paired HomeKit Accessory (com.apple.hap.pairing)\n"
            f"Account: {accessory['accessory_id']}\nKey: {accessory['accessory_key']}\n"
        )
        parsed = parse_export(json.dumps(parse_dump(dump.encode())))
        self.assertEqual(parsed["controllers"], [source])
        self.assertEqual(parsed["accessories"], {accessory["accessory_id"]: accessory["accessory_key"]})

    def test_json_derives_public_key_and_rejects_mismatch(self):
        source, accessory, export = export_fixture()
        export["controllers"][0].pop("iOSDeviceLTPK")
        parsed = parse_export(json.dumps(export))
        self.assertEqual(len(parsed["controllers"][0]["iOSDeviceLTPK"]), 64)
        self.assertEqual(parsed["accessories"][accessory["accessory_id"]], accessory["accessory_key"])
        export["controllers"][0]["iOSDeviceLTPK"] = "ab" * 32
        with self.assertRaises(SafetyFailure):
            parse_export(json.dumps(export))

    def test_selected_backup_csv_mapping(self):
        source, accessory, _ = export_fixture()
        csv = "Title,Date,Account,Password,App / Group,Body\n"
        csv += f"Identity,,{source['iOSPairingId']},{source['iOSDeviceLTPK']}{source['iOSDeviceLTSK']},com.apple.hap.pairing,\n"
        csv += f"Accessory,,{accessory['accessory_id']},{accessory['accessory_key']},com.apple.hap.pairing,\n"
        parsed = parse_export(csv.encode())
        self.assertEqual(parsed["controllers"], [source])
        self.assertEqual(parsed["accessories"], {accessory["accessory_id"]: accessory["accessory_key"]})

    def test_binary_placeholder_unrelated_passwords_duplicates_and_size_refused(self):
        invalid = [
            "x" * (MAX_IMPORT + 1),
            '{"controllers":[],"controllers":[],"accessories":[]}',
            "Account,Password,App / Group\nidentity,32 bytes (binary),com.apple.hap.pairing\n",
            "Account,Password,App / Group\nidentity,password,unrelated.group\n",
        ]
        for value in invalid:
            with self.subTest(length=len(value)), self.assertRaises(SafetyFailure) as error:
                parse_export(value)
            self.assertEqual(str(error.exception), "invalid_credential_export")

    def test_public_pins_never_contain_owned_private_key(self):
        target = make_owned("disposable", "ab" * 32, {"AccessoryIP": "192.0.2.1", "AccessoryPort": 12345})
        pins = json.dumps(public_targets([target]))
        self.assertNotIn(target["pairing"]["iOSDeviceLTSK"], pins)
        self.assertNotIn("pairing", pins)


class Request(dict):
    def __init__(self, *, peer="192.0.2.1", admin=True, owner="owner", authenticated=True):
        super().__init__(hass_user=SimpleNamespace(id=owner, is_admin=admin), ha_authenticated=authenticated)
        self.remote = peer


class OnboardingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for path in (
            "custom_components.homepod_sensor_link.onboarding.discover_airplay",
            "custom_components.homepod_sensor_link.refresh_names",
        ):
            boundary = patch(path, AsyncMock(return_value=[]))
            boundary.start()
            self.addCleanup(boundary.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.hass = HomeAssistant(self.directory.name)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        await self.hass.config_entries.async_initialize()
        dr.async_setup(self.hass)
        await dr.async_load(self.hass)
        await er.async_load(self.hass)
        self.hass.state = CoreState.running
        self.vault = await get_vault(self.hass)
        self.source, self.accessory, self.export = export_fixture()
        self.target = make_owned(
            self.accessory["accessory_id"],
            self.accessory["accessory_key"],
            {"AccessoryIP": "192.0.2.1", "AccessoryPort": 12345},
        )
        self.target.update(source=self.source, recovery=True)
        self.record = {
            "target": self.target,
            "name": "Disposable HomePod",
            "phase": "prepared",
            "cancelled": False,
            "authorised_boot": boot_id(self.hass),
            "error": None,
            "source_pin": {k: v for k, v in self.source.items() if k != "iOSDeviceLTSK"},
        }
        self.key = self.target["entry_id"]
        self.backend = SimpleNamespace(
            read_ha=AsyncMock(side_effect=AuthenticationFailure()),
            pairing_present=AsyncMock(return_value=False),
            restore_missing=AsyncMock(return_value=True),
            inspect_recovery=AsyncMock(return_value={"recovery_admin_verified": True}),
        )
        self.now = [0]
        self.provisioner = Provisioner(self.vault, self.backend, lambda: self.now[0], boot_id(self.hass))

    async def asyncTearDown(self):
        await self.hass.async_stop(force=True)

    async def test_initial_write_is_persisted_first_and_has_user_permission(self):
        await self.vault.put(self.key, self.record)

        async def restore(target):
            saved = self.vault.records()[self.key]
            self.assertEqual(saved["phase"], "write_uncertain")
            self.assertEqual(saved["onboarding_attempts"], [0])
            self.assertEqual(target["ha_permissions"], 0)
            return True

        self.backend.restore_missing.side_effect = restore
        self.backend.read_ha.side_effect = [AuthenticationFailure(), {"temperature": 22.5, "humidity": 48}]
        record, values = await self.provisioner.inspect(self.key)
        self.assertEqual(record["phase"], "verified")
        self.assertEqual(values["humidity"], 48)
        self.assertEqual(record["target"]["ha_id"], self.target["ha_id"])
        self.backend.restore_missing.assert_awaited_once()

    async def test_restart_inspects_accepted_key_but_never_writes_without_resume(self):
        await self.vault.put(self.key, self.record)
        restarted = Provisioner(self.vault, self.backend, lambda: 0, "another-boot")
        record, values = await restarted.inspect(self.key)
        self.assertEqual(record["error"], "resume_required")
        self.backend.restore_missing.assert_not_called()
        self.backend.read_ha.side_effect = None
        self.backend.read_ha.return_value = {"temperature": 20, "humidity": 40}
        record, values = await restarted.inspect(self.key)
        self.assertEqual(record["phase"], "verified")
        self.assertEqual(values["temperature"], 20)
        self.backend.restore_missing.assert_not_called()

    async def test_present_but_rejected_pairing_fails_closed_and_no_readd(self):
        await self.vault.put(self.key, self.record)
        self.backend.pairing_present.return_value = True
        record, _ = await self.provisioner.inspect(self.key)
        self.assertEqual(record["phase"], "blocked")
        self.backend.restore_missing.assert_not_called()
        await self.provisioner.inspect(self.key, resume=True)
        self.backend.restore_missing.assert_not_called()

    async def test_ambiguous_write_keeps_same_identity_and_rate_limit(self):
        await self.vault.put(self.key, self.record)
        self.backend.restore_missing.side_effect = OSError("private transport detail")
        record, _ = await self.provisioner.inspect(self.key)
        self.assertEqual(record["phase"], "write_uncertain")
        self.now[0] = 60
        record, _ = await self.provisioner.inspect(self.key, resume=True)
        self.assertEqual(record["error"], "pairing_rate_limit")
        self.assertEqual(self.backend.restore_missing.await_count, 1)
        self.assertEqual(record["target"]["pairing"], self.target["pairing"])
        self.assertNotIn("private transport", str(record.get("error")))

    async def test_explicit_cancellation_stops_all_probes_until_resume(self):
        await self.vault.put(self.key, self.record)
        await self.provisioner.cancel(self.key)
        await self.provisioner.inspect(self.key)
        self.backend.read_ha.assert_not_called()
        self.backend.read_ha.side_effect = None
        self.backend.read_ha.return_value = {"temperature": 20, "humidity": 50}
        await self.provisioner.inspect(self.key, resume=True)
        self.backend.read_ha.assert_awaited_once()

    async def test_user_pairing_add_preserves_every_prior_row(self):
        rows = [{"pairingId": self.source["iOSPairingId"], "publicKey": self.source["iOSDeviceLTPK"], "permissions": 1}]
        before = list(rows)

        async def add(identifier, public, permission):
            self.assertEqual(permission, "User")
            rows.append({"pairingId": identifier, "publicKey": public, "permissions": 0})

        pairing = SimpleNamespace(
            list_pairings=AsyncMock(side_effect=lambda: list(rows)), add_pairing=AsyncMock(side_effect=add)
        )

        @asynccontextmanager
        async def scope(*args, **kwargs):
            yield pairing

        backend = HomeKitBackend(None, None, lambda _: None)
        backend.session = scope
        self.assertTrue(await backend.restore_missing(self.target))
        self.assertEqual(rows[:1], before)
        self.assertFalse(await backend.restore_missing(self.target))
        pairing.add_pairing.assert_awaited_once()

    async def test_read_only_matching_is_bounded_and_no_pairing_writes(self):
        other = fixture(1)[0]["source"]
        imported = {
            "controllers": [other, self.source],
            "accessories": {self.target["accessory_id"]: self.target["accessory_key"]},
        }
        backend = SimpleNamespace(
            read_ha=AsyncMock(return_value={"temperature": 20, "humidity": 40}),
            inspect_recovery=AsyncMock(side_effect=[SafetyFailure(), {"recovery_admin_verified": True}]),
            restore_missing=AsyncMock(),
        )
        found = {
            self.target["accessory_id"]: {
                "name": "Disposable HomePod",
                "endpoint": {"AccessoryIP": "192.0.2.1", "AccessoryPort": 12345},
            }
        }
        keys = await prepare(self.hass, self.vault, backend, list(found), list(found), imported, found)
        record = self.vault.records()[keys[0]]
        self.assertEqual(record["target"]["source"], self.source)
        self.assertTrue(record["target"]["recovery"])
        backend.restore_missing.assert_not_called()
        private = Path(self.directory.name) / DATA_FILE
        self.assertEqual(private.stat().st_mode & 0o777, 0o600)
        self.assertEqual(private.parent.stat().st_mode & 0o777, 0o700)

    async def make_runtime(self, recovery=True):
        self.record["target"]["recovery"] = recovery
        self.record["phase"] = "verified"
        await self.vault.put(self.key, self.record)
        config = entry({"device_keys": [self.key], "pins": {self.key: public_targets([self.target])[0]}})
        self.hass.config_entries._entries[config.entry_id] = config
        runtime = Runtime(self.hass, config, [self.target], SimpleNamespace(async_stop=AsyncMock()), None, self.vault)
        runtime.backend = self.backend
        await runtime.initialize()
        config.runtime_data = runtime
        return config, runtime

    async def test_real_sensor_publication_and_administrator_discard(self):
        config, runtime = await self.make_runtime(recovery=False)
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
        entities = [Measurement(runtime, self.target, kind) for kind in ("temperature", "humidity")]
        try:
            await platform.async_add_entities(entities)
            runtime.publish(self.target, {"temperature": 22.5, "humidity": 48})
            await runtime.finish_publication(self.key)
            self.assertEqual(runtime.records[self.key]["phase"], "active")
            self.assertNotIn("source", self.vault.records()[self.key]["target"])
            for sensor in entities:
                self.assertTrue(sensor.available)
                self.assertNotIn(self.source["iOSDeviceLTSK"], str(sensor.extra_state_attributes))
                self.assertIsNotNone(self.hass.states.get(sensor.entity_id))
        finally:
            await platform.async_reset()
            await runtime.stop()

    async def test_device_and_entry_removal_automatically_delete_owned_data_only(self):
        config, runtime = await self.make_runtime()
        await runtime.store.async_save({self.key: {"repairs": 2}})
        native = Path(self.directory.name) / ".storage/core.config_entries"
        native.write_text("unrelated native entries")
        device = SimpleNamespace(identifiers={(DOMAIN, self.key)})
        self.assertTrue(await async_remove_config_entry_device(self.hass, config, device))
        self.assertEqual(self.vault.records(), {})
        self.assertEqual(await runtime.store.async_load(), {})
        self.assertEqual(config.data["device_keys"], [])
        self.assertEqual(native.read_text(), "unrelated native entries")
        await async_remove_entry(self.hass, config)
        self.assertFalse((Path(self.directory.name) / DATA_FILE).exists())
        self.assertFalse((Path(self.directory.name) / ".storage/homepod_sensor_link.state").exists())
        self.assertEqual(native.read_text(), "unrelated native entries")

    async def test_public_diagnostics_omit_all_identity_and_private_values(self):
        _, runtime = await self.make_runtime()
        output = json.dumps(runtime.public_snapshot())
        for value in (
            self.key,
            self.target["accessory_id"],
            self.target["ha_key"],
            self.source["iOSPairingId"],
            self.source["iOSDeviceLTSK"],
            self.target["pairing"]["iOSDeviceLTSK"],
        ):
            self.assertNotIn(value, output)
        self.assertTrue(runtime.records[self.key]["target"]["recovery"])
        await runtime.stop()

    async def test_conflict_guard_has_no_legacy_transition(self):
        config = entry({"device_keys": []})
        legacy = SimpleNamespace(disabled_by=None, state=SimpleNamespace(value="loaded"))
        with patch.object(self.hass.config_entries, "async_entries", return_value=[legacy]):
            with self.assertRaises(Exception):
                await async_setup_entry(self.hass, config)
        self.assertNotIn(OWNER, self.hass.data)
        self.assertFalse((Path(__file__).parents[1] / "custom_components/homepod_recovery").exists())

    @asynccontextmanager
    async def flow_environment(self, backend=None):
        request_token = current_request.set(Request(peer="127.0.0.1"))
        found = {
            self.target["accessory_id"]: {
                "name": "Disposable HomePod",
                "endpoint": {"AccessoryIP": "192.0.2.1", "AccessoryPort": 12345},
            }
        }

        @asynccontextmanager
        async def scope(_hass):
            yield SimpleNamespace(), None

        backend = backend or SimpleNamespace(
            inspect_recovery=AsyncMock(return_value={"recovery_admin_verified": True}),
            read_ha=AsyncMock(return_value={"temperature": 22, "humidity": 45}),
            restore_missing=AsyncMock(),
        )
        for method in ("read_ha", "restore_missing", "inspect_recovery", "pairing_present"):
            if not hasattr(backend, method):
                setattr(backend, method, AsyncMock())
        try:
            with (
                patch(
                    "custom_components.homepod_sensor_link.config_flow.discover_devices", AsyncMock(return_value=found)
                ),
                patch("custom_components.homepod_sensor_link.config_flow.controller_session", scope),
                patch("custom_components.homepod_sensor_link.config_flow.HomeKitBackend", return_value=backend),
            ):
                yield backend
        finally:
            current_request.reset(request_token)

    async def finish_flow(self, flow, result):
        if result["type"] == FlowResultType.SHOW_PROGRESS:
            await result["progress_task"]
            result = await flow.async_step_verify()
            self.assertEqual(result["type"], FlowResultType.SHOW_PROGRESS_DONE)
            result = await flow.async_step_finish()
        return result

    async def test_guided_flow_defaults_recovery_on_and_never_returns_credentials(self):
        flow = HomePodSensorLinkFlow()
        flow.hass, flow.context, flow.flow_id, flow.handler = self.hass, {"source": "user"}, "flow", DOMAIN
        async with self.flow_environment() as backend:
            self.assertEqual((await flow.async_step_user())["step_id"], "devices")
            policy = await flow.async_step_devices({"devices": [self.target["accessory_id"]]})
            self.assertEqual(policy["step_id"], "policy")
            self.assertEqual(policy["data_schema"]({})["recovery_devices"], [self.target["accessory_id"]])
            credentials = await flow.async_step_policy({"recovery_devices": [self.target["accessory_id"]]})
            self.assertEqual(credentials["type"], FlowResultType.MENU)
            result = await self.finish_flow(
                flow, await flow.async_step_credential_export({"credential_export": json.dumps(self.export)})
            )
            self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY)
            output = json.dumps(result["data"])
            self.assertNotIn(self.source["iOSDeviceLTSK"], output)
            key = result["data"]["device_keys"][0]
            self.assertTrue(self.vault.records()[key]["target"]["recovery"])
            self.assertEqual(self.vault.records()[key]["target"]["ha_permissions"], 0)
            self.assertEqual(self.vault.records()[key]["phase"], "prepared")
            backend.restore_missing.assert_not_called()
            self.assertIsNone(flow._imported)

    async def test_partial_matching_failure_keeps_ready_device_and_pending_result(self):
        flow = HomePodSensorLinkFlow()
        flow.hass, flow.context, flow.flow_id, flow.handler = self.hass, {"source": "user"}, "partial", DOMAIN
        good, bad = self.target["accessory_id"], "unexported-device"
        async with self.flow_environment():
            await flow.async_step_user()
            flow._discovered[bad] = {
                "name": "Unexported candidate",
                "endpoint": {"AccessoryIP": "192.0.2.2", "AccessoryPort": 12345},
            }
            flow._selected, flow._recovery_ids = [good, bad], [good, bad]
            flow._imported = parse_export(json.dumps(self.export))
            result = await self.finish_flow(flow, await flow._begin())
        self.assertEqual(len(result["data"]["device_keys"]), 2)
        records = self.vault.records()
        self.assertEqual({r["phase"] for r in records.values()}, {"prepared", "needs_credentials"})
        self.assertEqual(len({r["target"]["entry_id"] for r in records.values()}), 2)

    async def concurrent_credential_submissions(self, step, user_input):
        from custom_components.homepod_sensor_link import config_flow

        probe_started, release_probe = asyncio.Event(), asyncio.Event()
        submissions_waiting = asyncio.Event()

        async def inspect(_target):
            probe_started.set()
            await release_probe.wait()
            return {"recovery_admin_verified": True}

        waiting = 0

        async def blocked_access(hass):
            nonlocal waiting
            waiting += 1
            if waiting == 2:
                submissions_waiting.set()
            return await get_vault(hass)

        backend = SimpleNamespace(
            inspect_recovery=AsyncMock(side_effect=inspect),
            read_ha=AsyncMock(return_value={"temperature": 22, "humidity": 45}),
        )
        manager = self.hass.config_entries.flow
        async with self.flow_environment(backend):
            with patch(
                "homeassistant.config_entries._async_get_flow_handler", AsyncMock(return_value=HomePodSensorLinkFlow)
            ):
                result = await manager.async_init(DOMAIN, context={"source": "user"})
            flow_id = result["flow_id"]
            await manager.async_configure(flow_id, {"devices": [self.target["accessory_id"]]})
            await manager.async_configure(flow_id, {"recovery_devices": [self.target["accessory_id"]]})
            await manager.async_configure(flow_id, {"next_step_id": step})
            flow = manager._progress[flow_id]
            preparation_tasks = []
            prepare_flow = flow._prepare

            async def tracked_prepare():
                preparation_tasks.append(asyncio.current_task())
                return await prepare_flow()

            results = []
            try:
                with (
                    patch.object(config_flow, "get_vault", side_effect=blocked_access),
                    patch.object(config_flow, "parse_export", wraps=parse_export) as parse,
                    patch.object(flow, "_prepare", side_effect=tracked_prepare),
                ):
                    async with self.vault.lock:
                        submissions = [
                            asyncio.create_task(manager.async_configure(flow_id, user_input)) for _ in range(2)
                        ]
                        await asyncio.wait_for(submissions_waiting.wait(), 1)
                    results = await asyncio.gather(*submissions)
                    await asyncio.wait_for(probe_started.wait(), 1)
                    progress = await flow._begin()
                    self.assertIs(progress["progress_task"], flow._task)
            finally:
                release_probe.set()
                await asyncio.gather(*preparation_tasks)
            self.assertEqual([result["type"] for result in results], [FlowResultType.SHOW_PROGRESS] * 2)
            self.assertEqual(len(preparation_tasks), 1)
            self.assertIs(flow._task, preparation_tasks[0])
            self.assertEqual(parse.call_count, 1)
            backend.inspect_recovery.assert_awaited_once()
            records = self.vault.records()
            self.assertEqual(len(records), 1)
            self.assertEqual(set(flow._task.result()), set(records))
            record = next(iter(records.values()))
            self.assertEqual(record["source_pin"]["iOSPairingId"], self.source["iOSPairingId"])

    async def test_concurrent_native_export_submissions_share_one_preparation_task(self):
        await self.concurrent_credential_submissions(
            "credential_export", {"credential_export": json.dumps(self.export)}
        )

    async def test_concurrent_native_manual_submissions_share_one_preparation_task(self):
        await self.concurrent_credential_submissions(
            "credential_manual",
            {
                "controller_id": self.source["iOSPairingId"],
                "controller_private_key": self.source["iOSDeviceLTSK"],
                "accessory_id": self.target["accessory_id"],
                "accessory_public_key": self.target["accessory_key"],
            },
        )

    async def test_real_flow_manager_cancellation_closes_resources_and_discards_import(self):
        entered, closed = asyncio.Event(), asyncio.Event()

        @asynccontextmanager
        async def scope(_):
            try:
                yield SimpleNamespace(), None
            finally:
                closed.set()

        async def block(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        backend = SimpleNamespace(inspect_recovery=AsyncMock(side_effect=block))
        manager = self.hass.config_entries.flow
        async with self.flow_environment(backend):
            with (
                patch(
                    "homeassistant.config_entries._async_get_flow_handler",
                    AsyncMock(return_value=HomePodSensorLinkFlow),
                ),
                patch("custom_components.homepod_sensor_link.config_flow.controller_session", scope),
            ):
                result = await manager.async_init(DOMAIN, context={"source": "user"})
                flow_id = result["flow_id"]
                await manager.async_configure(flow_id, {"devices": [self.target["accessory_id"]]})
                await manager.async_configure(flow_id, {"recovery_devices": [self.target["accessory_id"]]})
                await manager.async_configure(flow_id, {"next_step_id": "credential_export"})
                result = await manager.async_configure(flow_id, {"credential_export": json.dumps(self.export)})
                self.assertEqual(result["type"], FlowResultType.SHOW_PROGRESS)
                await entered.wait()
                manager.async_abort(flow_id)
                await asyncio.wait_for(closed.wait(), 1)
        self.assertEqual(manager.async_progress(), [])
        self.assertEqual(self.vault.records(), {})

    async def test_native_adoption_without_recovery_creates_no_owned_measurements(self):
        target, native = fixture()
        native.title = "Disposable native entry"
        backend = SimpleNamespace(read_ha=AsyncMock(), inspect_recovery=AsyncMock(), restore_missing=AsyncMock())
        found = {target["accessory_id"]: {"name": native.title, "native_entry_id": native.entry_id}}
        with patch.object(self.hass.config_entries, "async_entries", return_value=[native]):
            keys = await prepare(
                self.hass, self.vault, backend, list(found), [], {"controllers": [], "accessories": {}}, found
            )
        record = self.vault.records()[keys[0]]
        self.assertFalse(record["target"].get("owned", False))
        self.assertFalse(record["target"]["recovery"])
        self.assertEqual(record["target"]["ha_id"], target["ha_id"])
        self.assertNotIn("source", record["target"])
        backend.inspect_recovery.assert_not_called()
        backend.restore_missing.assert_not_called()

    async def test_owned_pairing_retains_exact_exported_identifier_case(self):
        export = self.export
        raw = self.target["accessory_id"].upper()
        export["accessories"][0]["AccessoryPairingID"] = raw
        parsed = parse_export(json.dumps(export))
        identifier = raw.lower()
        backend = SimpleNamespace(inspect_recovery=AsyncMock(), read_ha=AsyncMock())
        found = {identifier: {"name": "Disposable", "endpoint": {"AccessoryIP": "192.0.2.1", "AccessoryPort": 12345}}}
        keys = await prepare(self.hass, self.vault, backend, [identifier], [identifier], parsed, found)
        record = self.vault.records()[keys[0]]
        self.assertEqual(record["target"]["accessory_id"], identifier)
        self.assertEqual(record["target"]["pairing"]["AccessoryPairingID"], raw)

    async def test_cancelled_flow_journal_cannot_start_pairing_on_same_boot(self):
        self.record["flow_id"] = "cancelled-flow"
        await self.vault.put(self.key, self.record)
        await self.vault.pause_flow("cancelled-flow")
        await self.provisioner.inspect(self.key)
        self.backend.read_ha.assert_not_called()
        self.backend.restore_missing.assert_not_called()
        self.assertNotIn("authorised_boot", self.vault.records()[self.key])

    async def test_active_monitor_resume_does_not_bypass_recovery_thresholds(self):
        config, runtime = await self.make_runtime()
        runtime.records[self.key]["phase"] = "active"
        await self.vault.put(self.key, runtime.records[self.key])
        await runtime.resume_device(self.key)
        self.backend.restore_missing.assert_not_called()
        self.backend.read_ha.assert_not_called()
        await runtime.stop()

    async def test_default_recovery_keeps_verified_admin_after_publication(self):
        config, runtime = await self.make_runtime()
        runtime.verify_native = AsyncMock()
        await runtime.finish_publication(self.key)
        self.assertIn("source", self.vault.records()[self.key]["target"])
        status = RecoveryStatus(runtime, self.target)
        self.assertTrue(status.extra_state_attributes["automatic_recovery"])
        self.assertNotIn(self.source["iOSDeviceLTSK"], str(status.extra_state_attributes))
        await runtime.stop()

    async def test_pairing_permission_drift_is_blocked_on_load(self):
        self.target["ha_permissions"] = 1
        _, runtime = await self.make_runtime()
        self.assertEqual(runtime.records[self.key]["phase"], "blocked")
        self.assertEqual(runtime.engine.targets, [])
        await runtime.stop()

    async def test_delayed_flow_pause_cannot_recreate_purged_credentials(self):
        self.record["flow_id"] = "removed"
        await self.vault.put(self.key, self.record)
        await self.vault.purge()
        await self.vault.pause_flow("removed")
        self.assertFalse((Path(self.directory.name) / DATA_FILE).exists())
        self.assertEqual(self.vault.records(), {})

    async def test_cleanup_removes_corrupt_owned_file_without_following_symlinks(self):
        await self.vault.put(self.key, self.record)
        path = Path(self.directory.name) / DATA_FILE
        path.write_text("invalid JSON")
        path.chmod(0o644)
        await self.vault.purge()
        self.assertFalse(path.exists())
        path.parent.mkdir(mode=0o700)
        outside = Path(self.directory.name) / "preserve.json"
        outside.write_text("unrelated data")
        path.symlink_to(outside)
        await self.vault.purge()
        self.assertEqual(outside.read_text(), "unrelated data")
        self.assertFalse(path.is_symlink())

    async def test_new_namespace_does_not_read_or_remove_old_storage(self):
        old = Path(self.directory.name) / ".storage/homepod_recovery"
        old.mkdir(mode=0o700, parents=True)
        previous = old / "keys.json"
        previous.write_text("legacy data is not ours")
        await self.vault.put(self.key, self.record)
        await self.vault.purge()
        self.assertEqual(previous.read_text(), "legacy data is not ours")

    async def test_each_device_controls_recovery_independently(self):
        from custom_components.homepod_sensor_link.engine import RecoveryEngine

        first, second = {"entry_id": "first", "recovery": False}, {"entry_id": "second", "recovery": True}
        backend = SimpleNamespace(
            read_ha=AsyncMock(side_effect=AuthenticationFailure()), restore_missing=AsyncMock(side_effect=OSError())
        )
        now = [0]
        engine = RecoveryEngine([first, second], backend, AsyncMock(), AsyncMock(), AsyncMock(), lambda: now[0])
        for now[0] in (0, 300, 600):
            await engine.check_all()
        backend.restore_missing.assert_awaited_once_with(second)
        self.assertEqual(engine.states["first"].attempts, [])

    async def test_cancel_interrupts_inflight_provisioning_without_losing_saved_key(self):
        config, runtime = await self.make_runtime()
        runtime.records[self.key]["phase"] = "prepared"
        await self.vault.put(self.key, runtime.records[self.key])
        entered, exited = asyncio.Event(), asyncio.Event()

        async def pending(_key):
            try:
                entered.set()
                await asyncio.Event().wait()
            finally:
                exited.set()

        runtime.provisioner.inspect = pending
        work = asyncio.create_task(runtime.update())
        await entered.wait()
        await runtime.cancel_device(self.key)
        await work
        self.assertTrue(exited.is_set())
        record = self.vault.records()[self.key]
        self.assertTrue(record["cancelled"])
        self.assertEqual(record["target"]["ha_id"], self.target["ha_id"])
        self.assertEqual(runtime.engine.targets, [])
        await runtime.stop()

    async def test_reimport_or_resume_cannot_bypass_existing_controller_recovery_limits(self):
        self.record.update(phase="verified", commissioned=True)
        await self.vault.put(self.key, self.record)
        record, values = await self.provisioner.inspect(self.key, resume=True)
        self.assertEqual(record["error"], "recovery_pending")
        self.assertIsNone(values)
        self.backend.restore_missing.assert_not_called()

    async def test_broken_native_pairing_can_accept_admin_for_bounded_engine_recovery(self):
        target, native = fixture()
        native.title = "Disposable native"
        source = self.source

        async def read(_target, source=False):
            if not source:
                raise AuthenticationFailure()
            return {"temperature": 20, "humidity": 40}

        backend = SimpleNamespace(
            read_ha=AsyncMock(side_effect=read), inspect_recovery=AsyncMock(), restore_missing=AsyncMock()
        )
        found = {target["accessory_id"]: {"name": native.title, "native_entry_id": native.entry_id}}
        with patch.object(self.hass.config_entries, "async_entries", return_value=[native]):
            keys = await prepare(
                self.hass,
                self.vault,
                backend,
                list(found),
                list(found),
                {"controllers": [source], "accessories": {}},
                found,
            )
        record = self.vault.records()[keys[0]]
        self.assertEqual(record["phase"], "verified")
        self.assertNotIn("authorised_boot", record)
        self.assertIn("source", record["target"])
        backend.restore_missing.assert_not_called()

    async def test_manage_removal_removes_only_owned_entities_and_device(self):
        config, runtime = await self.make_runtime()
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
        owned = [Measurement(runtime, self.target, kind) for kind in ("temperature", "humidity")]
        registry = er.async_get(self.hass)
        native = registry.async_get_or_create(
            "sensor", "homekit_controller", "unrelated-native", original_device_class="temperature"
        )
        try:
            await platform.async_add_entities(owned)
            self.assertTrue(all(registry.async_get(s.entity_id) for s in owned))
            await runtime.remove_device(self.key)
            self.assertTrue(all(registry.async_get(s.entity_id) is None for s in owned))
            self.assertIsNotNone(registry.async_get(native.entity_id))
            self.assertEqual(dr.async_entries_for_config_entry(dr.async_get(self.hass), config.entry_id), [])
            await runtime.remove_device(self.key)  # repeated removal is harmless
        finally:
            await platform.async_reset()
            await runtime.stop()

    async def test_manual_resume_consumes_initial_write_authorisation(self):
        await self.vault.put(self.key, self.record)
        self.backend.restore_missing.side_effect = OSError()
        await self.provisioner.inspect(self.key, resume=True)
        self.assertNotIn("authorised_boot", self.vault.records()[self.key])
        self.now[0] = 4000
        await self.provisioner.inspect(self.key)
        self.assertEqual(self.backend.restore_missing.await_count, 1)
        self.assertEqual(self.vault.records()[self.key]["error"], "resume_required")

    async def test_credentials_and_recovery_history_share_one_atomic_store(self):
        config, runtime = await self.make_runtime()
        state = runtime.engine.states[self.key]
        state.attempts = [600]
        state.repairs = 3
        state.blocked = True
        state.native_pending = True
        state.native_attempts = [900]
        await runtime.save_state(runtime.engine.snapshot())
        data = json.loads((Path(self.directory.name) / DATA_FILE).read_text())
        self.assertIn(self.key, data["records"])
        self.assertEqual(data["states"][self.key]["attempts"], [600])
        await runtime.stop()
        restarted = Runtime(self.hass, config, [self.target], SimpleNamespace(async_stop=AsyncMock()), None, self.vault)
        restarted.backend = self.backend
        await restarted.initialize()
        restored = restarted.engine.states[self.key]
        self.assertEqual((restored.repairs, restored.attempts), (3, [600]))
        self.assertTrue(restored.blocked)
        self.assertTrue(restored.native_pending)
        self.assertEqual(restored.native_attempts, [900])
        await restarted.stop()

    async def test_competing_engine_detected_after_startup_pauses_checks(self):
        config, runtime = await self.make_runtime()
        runtime.records[self.key]["phase"] = "active"
        runtime.sync_targets()
        runtime.engine.states[self.key].auth_failures = 3
        runtime.engine.states[self.key].auth_since = 0
        legacy = SimpleNamespace(disabled_by=None, state=SimpleNamespace(value="loaded"))
        with patch.object(self.hass.config_entries, "async_entries", return_value=[legacy]):
            await runtime.update()
            self.assertFalse(runtime.engine.is_active())
        self.backend.read_ha.assert_not_called()
        self.backend.restore_missing.assert_not_called()
        state = runtime.engine.states[self.key]
        self.assertEqual(state.status, "paused")
        self.assertEqual(state.auth_failures, 0)
        await runtime.stop()

    async def test_write_guard_rechecks_after_administrator_table_read(self):
        from custom_components.homepod_sensor_link.engine import RecoveryPaused

        rows = [{"pairingId": self.source["iOSPairingId"], "publicKey": self.source["iOSDeviceLTPK"], "permissions": 1}]
        pairing = SimpleNamespace(list_pairings=AsyncMock(return_value=rows), add_pairing=AsyncMock())

        @asynccontextmanager
        async def scope(*args, **kwargs):
            yield pairing

        backend = HomeKitBackend(None, None, lambda _: None)
        backend.session = scope
        backend.write_allowed = lambda: False
        with self.assertRaises(RecoveryPaused):
            await backend.restore_missing(self.target)
        pairing.add_pairing.assert_not_called()

    async def test_full_setup_excludes_second_owner_unloads_and_purges(self):
        await async_setup(self.hass, {})
        await self.vault.put(self.key, self.record)
        config = entry(
            {
                "device_keys": [self.key],
                "pins": {self.key: public_targets([self.target])[0]},
                "admin_pins": {self.key: self.record["source_pin"]},
            }
        )
        self.hass.config_entries._entries[config.entry_id] = config
        controller = SimpleNamespace(async_start=AsyncMock(), async_stop=AsyncMock())
        with (
            patch("custom_components.homepod_sensor_link.zeroconf.async_get_async_instance", AsyncMock()),
            patch("custom_components.homepod_sensor_link.IpController", return_value=controller),
            patch("custom_components.homepod_sensor_link.coordinator.HomeKitBackend", return_value=self.backend),
            patch("custom_components.homepod_sensor_link.async_at_started", return_value=lambda: None),
            patch.object(self.hass.config_entries, "async_forward_entry_setups", AsyncMock()),
            patch.object(self.hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True)),
        ):
            self.assertTrue(await async_setup_entry(self.hass, config))
            self.assertEqual(self.hass.data[OWNER], config.entry_id)
            self.assertTrue(self.hass.services.has_service(DOMAIN, "check_now"))
            from homeassistant.exceptions import ConfigEntryNotReady

            with self.assertRaises(ConfigEntryNotReady):
                await async_setup_entry(self.hass, config)
            self.assertTrue(await async_unload_entry(self.hass, config))
            self.assertTrue(self.hass.services.has_service(DOMAIN, "check_now"))
            self.assertNotIn(OWNER, self.hass.data)
            self.assertTrue((Path(self.directory.name) / DATA_FILE).exists())
            await async_remove_entry(self.hass, config)
            self.assertFalse((Path(self.directory.name) / DATA_FILE).exists())
            self.assertEqual(config.runtime_data.records, {})
            controller.async_start.assert_awaited_once()
            controller.async_stop.assert_awaited_once()

    async def test_corrupt_onboarding_attempts_cannot_reset_limits(self):
        from custom_components.homepod_sensor_link.vault import Vault

        await self.vault.put(self.key, self.record)
        path = Path(self.directory.name) / DATA_FILE
        corrupted = json.loads(path.read_text())
        corrupted["records"][self.key]["onboarding_attempts"] = [float("nan")]
        path.write_text(json.dumps(corrupted))
        loaded = await Vault(self.hass).load()
        config = entry({"device_keys": [self.key]})
        runtime = Runtime(self.hass, config, [self.target], SimpleNamespace(async_stop=AsyncMock()), None, loaded)
        try:
            with self.assertRaises(SafetyFailure):
                await runtime.initialize()
            self.backend.restore_missing.assert_not_called()
            self.assertIn("NaN", path.read_text())  # fail closed without resetting history
        finally:
            await runtime.stop()

    async def test_unreachable_device_does_not_block_other_authenticated_checks(self):
        from custom_components.homepod_sensor_link.engine import RecoveryEngine

        entered, release = set(), asyncio.Event()

        async def read(target):
            entered.add(target["entry_id"])
            await release.wait()

        backend = SimpleNamespace(read_ha=read, restore_missing=AsyncMock())
        engine = RecoveryEngine(
            [{"entry_id": "first"}, {"entry_id": "second"}], backend, AsyncMock(), AsyncMock(), AsyncMock(), lambda: 0
        )
        checking = asyncio.create_task(engine.check_all())
        try:
            async with asyncio.timeout(1):
                while len(entered) != 2:
                    await asyncio.sleep(0)
            release.set()
            await checking
            self.assertTrue(all(state.status == "healthy" for state in engine.states.values()))
        finally:
            release.set()
            await checking

    async def test_lost_add_reply_requires_persisted_table_preservation_before_success(self):
        _, runtime = await self.make_runtime()
        other_key = keypair()[1]
        other = {"pairingId": "other-original-controller", "publicKey": other_key, "permissions": 1}
        rows = [
            {"pairingId": self.source["iOSPairingId"], "publicKey": self.source["iOSDeviceLTPK"], "permissions": 1},
            other,
        ]
        calls = [0]

        async def listing():
            calls[0] += 1
            if calls[0] == 2:
                raise OSError("lost add response")
            return list(rows)

        async def add(identifier, public, permission):
            checkpoint = self.vault.records()[self.key]["target"]["expected_pairings"]
            self.assertTrue(any(row["pairingId"] == other["pairingId"] for row in checkpoint))
            rows.append({"pairingId": identifier, "publicKey": public, "permissions": 0})

        pairing = SimpleNamespace(
            list_pairings=AsyncMock(side_effect=listing),
            add_pairing=AsyncMock(side_effect=add),
            list_accessories_and_characteristics=AsyncMock(
                return_value=[
                    {
                        "aid": 1,
                        "services": [
                            {
                                "characteristics": [
                                    {"iid": 2, "type": "10", "perms": ["pr"]},
                                    {"iid": 3, "type": "11", "perms": ["pr"]},
                                ]
                            }
                        ],
                    }
                ]
            ),
            get_characteristics=AsyncMock(return_value={(1, 2): {"value": 45}, (1, 3): {"value": 20}}),
        )

        @asynccontextmanager
        async def scope(*args, **kwargs):
            yield pairing

        backend = HomeKitBackend(None, None, lambda _: None)
        backend.session = scope
        backend.before_write = runtime.persist_pairing_checkpoint
        backend.after_write = runtime.persist_pairing_checkpoint
        try:
            with self.assertRaises(OSError):
                await backend.restore_missing(self.target)
            saved = self.vault.records()[self.key]["target"]
            self.assertIn("expected_pairings", saved)
            rows.remove(other)
            with self.assertRaises(SafetyFailure):
                await backend.read_ha(saved)
            self.assertIn("expected_pairings", self.vault.records()[self.key]["target"])
            rows.append(other)
            values = await backend.read_ha(saved)
            self.assertEqual(values, {"humidity": 45, "temperature": 20})
            self.assertNotIn("expected_pairings", self.vault.records()[self.key]["target"])
            pairing.add_pairing.assert_awaited_once()
        finally:
            await runtime.stop()
