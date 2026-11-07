"""Exercise rejected input, native adoption, UI and protected-store boundaries."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers import entity_registry as er, device_registry as dr

from custom_components.homepod_sensor_link import onboarding, ui, sensor
from custom_components.homepod_sensor_link.credentials import pinned_target, validate_target
from custom_components.homepod_sensor_link.engine import SafetyFailure
from custom_components.homepod_sensor_link.importer import parse_export
from custom_components.homepod_sensor_link.vault import DATA_FILE, _load, _write
from custom_components.homepod_sensor_link.secure_storage import read_private
import test_onboarding_v2 as fixtures
from test_recovery import fixture, keypair


def native_config(data, title="Native HomePod"):
    return ConfigEntry(
        domain="homekit_controller",
        title=title,
        data=data,
        options={},
        source="user",
        unique_id=None,
        version=1,
        minor_version=1,
        discovery_keys={},
        subentries_data=None,
    )


class ExportBoundaryTests(unittest.TestCase):
    def test_single_pairing_and_controller_only_exports_and_duplicate_identical_rows(self):
        source, accessory, export = fixtures.export_fixture()
        record = {
            **source,
            "AccessoryPairingID": accessory["accessory_id"],
            "AccessoryLTPK": accessory["accessory_key"],
        }
        self.assertEqual(parse_export(json.dumps(record))["controllers"], [source])
        self.assertEqual(parse_export(json.dumps(source))["accessories"], {})
        export["controllers"] *= 2
        export["accessories"] *= 2
        self.assertEqual(len(parse_export(json.dumps(export))["controllers"]), 1)

    def test_malformed_or_conflicting_exports_never_echo_private_material(self):
        source, accessory, export = fixtures.export_fixture()
        invalid = []
        for identifier in (None, "", "x" * 257, "control\ncharacter"):
            changed = deepcopy(export)
            changed["controllers"][0]["iOSPairingId"] = identifier
            invalid.append(changed)
        for seed in (False, "00", " " * 64, "zz" * 32):
            invalid.append({**source, "iOSDeviceLTSK": seed})
        invalid += [
            {**source, "unexpected": True},
            {"controllers": []},
            {"controllers": {}, "accessories": []},
            {"controllers": [source], "accessories": {}},
            {"controllers": [source] * 65, "accessories": []},
            {"controllers": [source], "accessories": export["accessories"] * 65},
            {"controllers": [{**source, "extra": "field"}], "accessories": []},
            {"controllers": [source], "accessories": [{"extra": "field"}]},
            {"controllers": [source, {**source, "iOSDeviceLTSK": keypair()[0]}], "accessories": []},
            {
                "controllers": [source],
                "accessories": [
                    export["accessories"][0],
                    {"AccessoryPairingID": accessory["accessory_id"], "AccessoryLTPK": keypair()[1]},
                ],
            },
        ]
        for value in invalid:
            with self.subTest(shape=type(value).__name__), self.assertRaises(SafetyFailure) as error:
                parse_export(json.dumps(value))
            self.assertEqual(str(error.exception), "invalid_credential_export")
        for value in ("unrelated,headers\nx,y\n", "Account,Password,App / Group\nx,short,com.apple.hap.pairing\n"):
            with self.assertRaises(SafetyFailure):
                parse_export(value)

    def test_native_and_source_identity_pins_reject_invalid_input(self):
        target, entry = fixture()
        self.assertEqual(pinned_target(entry)["entry_id"], entry.entry_id)
        for changed in (
            None,
            SimpleNamespace(data={}),
            SimpleNamespace(**{**vars(entry), "entry_id": 42}),
            SimpleNamespace(**{**vars(entry), "data": {**entry.data, "AccessoryLTPK": "ab"}}),
        ):
            with self.assertRaises(SafetyFailure):
                pinned_target(changed)
        with self.assertRaises(SafetyFailure):
            validate_target(target, entry, {"iOSPairingId": "changed"})
        for source in (
            {**target["source"], "extra": True},
            {**target["source"], "iOSPairingId": target["ha_id"]},
            {**target["source"], "iOSDeviceLTPK": keypair()[1]},
        ):
            with self.assertRaises(SafetyFailure):
                validate_target({**target, "source": source}, entry)


class BoundaryLifecycleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.OnboardingTests.asyncSetUp
    asyncTearDown = fixtures.OnboardingTests.asyncTearDown

    async def test_native_inventory_filters_disabled_and_non_measurement_entities(self):
        registry = er.async_get(self.hass)
        config = fixtures.entry()
        self.hass.config_entries._entries[config.entry_id] = config
        for kind in ("temperature", "humidity"):
            item = registry.async_get_or_create(
                "sensor", "homekit_controller", kind, config_entry=config, original_device_class=kind
            )
            self.assertIsNotNone(item)
        registry.async_get_or_create("switch", "homekit_controller", "power", config_entry=config)
        registry.async_get_or_create(
            "sensor", "homekit_controller", "battery", config_entry=config, original_device_class="battery"
        )
        disabled = registry.async_get_or_create(
            "sensor",
            "homekit_controller",
            "ignored",
            config_entry=config,
            original_device_class="temperature",
            disabled_by=er.RegistryEntryDisabler.USER,
        )
        self.assertEqual(len(onboarding.sensor_ids(self.hass, config.entry_id)), 2)
        registry.async_update_entity(disabled.entity_id, disabled_by=None)
        with self.assertRaises(SafetyFailure):
            onboarding.sensor_ids(self.hass, config.entry_id)
        registry.async_remove(disabled.entity_id)
        registry.async_remove(next(e for e in onboarding.sensor_ids(self.hass, config.entry_id) if "humidity" in e))
        with self.assertRaises(SafetyFailure):
            onboarding.sensor_ids(self.hass, config.entry_id)

    async def test_discovery_merges_native_entries_and_ignores_other_models(self):
        native = native_config({"AccessoryPairingID": "native-only"})
        self.hass.config_entries._entries[native.entry_id] = native
        unrelated = native_config({"AccessoryPairingID": "unrelated"})
        self.hass.config_entries._entries[unrelated.entry_id] = unrelated
        dr.async_get(self.hass).async_get_or_create(
            config_entry_id=native.entry_id, identifiers={("homekit_controller", "synthetic")}, model="HomePod mini"
        )

        async def discover():
            for model, identifier in (("Other speaker", "ignored"), ("HomePod mini", "DISCOVERED")):
                yield SimpleNamespace(
                    description=SimpleNamespace(
                        model=model, id=identifier, name="Disposable candidate", addresses=["192.0.2.1"], port=12345
                    )
                )

        controller = SimpleNamespace(async_discover=discover)

        @asynccontextmanager
        async def session(hass):
            yield controller, None

        with patch.object(onboarding, "controller_session", session):
            result = await onboarding.discover_devices(self.hass)
        self.assertEqual(set(result), {"discovered", "native-only"})
        self.assertEqual(result["native-only"]["native_entry_id"], native.entry_id)

    async def test_discovery_controller_closes_on_success_failure_and_cancellation(self):
        controller = SimpleNamespace(async_start=AsyncMock(), async_stop=AsyncMock())
        with (
            patch.object(onboarding.zeroconf, "async_get_async_instance", AsyncMock(return_value=Mock())),
            patch.object(onboarding, "IpController", return_value=controller),
        ):
            async with onboarding.controller_session(self.hass) as pair:
                self.assertIs(pair[0], controller)
            controller.async_stop.side_effect = OSError("untrusted cleanup text")
            with self.assertRaises(OSError) as error:
                async with onboarding.controller_session(self.hass):
                    pass
            self.assertEqual(str(error.exception), "discovery_cleanup_failed")
            with self.assertRaises(asyncio.CancelledError):
                async with onboarding.controller_session(self.hass):
                    raise asyncio.CancelledError
            with self.assertRaises(ValueError):
                async with onboarding.controller_session(self.hass):
                    raise ValueError("primary")

    async def test_import_capacity_bad_peer_expired_inflight_upload_and_ui_stop(self):
        for request in (None, {}, SimpleNamespace(get=lambda *a: Mock(is_admin=True), remote="invalid")):
            self.assertIsNone(ui.local_admin(self.hass, request))
        imports = ui.Imports()
        for index in range(8):
            imports.grant(str(index), "owner")
        self.assertEqual(imports.grant("0", "owner"), imports.pending["0"]["nonce"])
        with self.assertRaises(ValueError):
            imports.grant("overflow", "owner")
        imports.pending.clear()
        nonce = imports.grant("flow", "owner")
        request = fixtures.Request(peer="127.0.0.1", nonce=nonce)

        async def revoked(_size):
            imports.pending.clear()
            yield json.dumps(self.export).encode()

        request.content.iter_chunked = revoked
        self.assertEqual((await ui.ImportView(self.hass, imports).post(request, "flow")).status, 400)
        self.hass.data.pop(ui.KEY)
        http = SimpleNamespace(register_view=Mock(), async_register_static_paths=AsyncMock())
        with (
            patch.object(self.hass, "http", http, create=True),
            patch.object(ui.panel_custom, "async_register_panel", AsyncMock()) as panel,
        ):
            await ui.setup_ui(self.hass)
            await ui.setup_ui(self.hass)
            panel.assert_awaited_once()
            http.register_view.assert_called_once()
            self.hass.data[ui.KEY].grant("flow", "owner")
            self.hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
            await self.hass.async_block_till_done()
            self.assertEqual(self.hass.data[ui.KEY].pending, {})

    async def test_owned_platform_creates_only_owned_measurements_and_exposes_pending_status(self):
        from custom_components.homepod_sensor_link.engine import DeviceState

        native = {"entry_id": "native"}
        runtime = SimpleNamespace(
            targets=[self.target, native],
            records={self.key: self.record, "native": {"name": "Native", "phase": "active"}},
            coordinator=SimpleNamespace(data={}, async_add_listener=Mock()),
            engine=SimpleNamespace(states={self.key: DeviceState()}),
        )
        config = SimpleNamespace(runtime_data=runtime, async_on_unload=Mock())
        add = Mock()
        await sensor.async_setup_entry(self.hass, config, add)
        self.assertEqual(len(add.call_args.args[0]), 4)
        diagnostic = sensor.RecoveryStatus(runtime, self.target)
        self.assertEqual(diagnostic.native_value, "prepared")
        self.record["cancelled"] = True
        self.assertEqual(diagnostic.native_value, "setup_cancelled")
        runtime.records.pop(self.key)
        self.assertEqual(diagnostic.native_value, "removed")

    async def test_private_json_rejects_duplicates_and_inventory_capacity(self):
        location = Path(self.directory.name) / DATA_FILE
        location.parent.mkdir(parents=True, mode=0o700)
        location.write_text('{"version":2,"version":2}')
        location.chmod(0o600)
        with self.assertRaises(ValueError):
            read_private(location)
        location.unlink()
        self.assertEqual(_load(self.directory.name)["records"], {})
        for data in (
            {"version": 1, "records": {}, "states": {}},
            {"version": 2, "records": [], "states": {}},
            {"version": 2, "records": {}, "states": []},
        ):
            _write(self.directory.name, data)
            with self.assertRaises(SafetyFailure):
                _load(self.directory.name)
        with self.assertRaises(SafetyFailure):
            _write(self.directory.name, {"oversize": "x" * 1048576})
        self.vault.data["records"] = {str(i): {} for i in range(64)}
        with self.assertRaises(SafetyFailure):
            await self.vault.put("65", {})

    async def test_private_storage_symlinks_and_unexpected_owned_directory_fail_closed(self):
        storage = Path(self.directory.name) / ".storage"
        # The registry has created storage; use a separate disposable root.
        import tempfile

        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other:
            (Path(directory) / ".storage").symlink_to(other)
            for operation in (_load, lambda path: _write(path, {})):
                with self.assertRaises(SafetyFailure):
                    operation(directory)
        owned = storage / "homepod_sensor_link"
        owned.mkdir(mode=0o700, parents=True)
        (owned / "data.json").mkdir()
        with self.assertRaises(SafetyFailure):
            await self.vault.purge()
        (owned / "data.json").rmdir()
        (owned / "unrelated.txt").write_text("preserve")
        await self.vault.purge()
        self.assertEqual((owned / "unrelated.txt").read_text(), "preserve")
