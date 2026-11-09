"""Native irestore v0.4.0 imports using generated, disposable credentials only."""

import base64
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from homeassistant.components.http.server import MAX_CLIENT_SIZE
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig

from custom_components.homepod_sensor_link import config_flow
from custom_components.homepod_sensor_link.diagnostics import async_get_config_entry_diagnostics
from custom_components.homepod_sensor_link.engine import SafetyFailure
from custom_components.homepod_sensor_link.importer import MAX_IMPORT, MAX_RAW_IMPORT, parse_export
import test_flow_lifecycle as flow_fixtures
import test_onboarding_v2 as fixtures


def b64(value):
    return base64.b64encode(value).decode("ascii")


def record(identifier, value, *, byte_account=False, byte_group=False, group="com.apple.hap.pairing", tomb=0):
    # Go reflect.TypeOf([]byte{}).String() is []uint8, not []byte.
    return {
        "_fieldOrder": "v_Data,tomb,acct,agrp",
        "_fieldTypes": f"[]uint8,int64,{'[]uint8' if byte_account else 'string'},{'[]uint8' if byte_group else 'string'}",
        "v_Data": b64(value),
        "tomb": tomb,
        "acct": b64(identifier.encode()) if byte_account else identifier,
        "agrp": b64(group.encode()) if byte_group else group,
        "_class": 6,
        "_version": 3,
        "_wkey": "discarded-bookkeeping",
    }


def dump(source=None, accessory=None, **encoding):
    if source is None:
        seed = bytes(range(32))
        public = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes_raw()
        source = {"iOSPairingId": " Disposable-é-Owner ", "iOSDeviceLTPK": public.hex(), "iOSDeviceLTSK": seed.hex()}
    accessory = accessory or {
        "accessory_id": ":".join(f"{n:02X}" for n in range(170, 176)),
        "accessory_key": bytes(range(224, 256)).hex(),
    }
    return {
        "General": [
            record(
                source["iOSPairingId"], bytes.fromhex(source["iOSDeviceLTPK"] + source["iOSDeviceLTSK"]), **encoding
            ),
            record(accessory["accessory_id"], bytes.fromhex(accessory["accessory_key"]), **encoding),
        ],
        "Internet": None,
        "Certs": [],
        "Keys": [],
    }


class IrestoreParserTests(unittest.TestCase):
    def reject(self, value):
        content = json.dumps(value) if not isinstance(value, (str, bytes)) else value
        try:
            parse_export(content)
        except SafetyFailure as error:
            self.assertEqual(str(error), "invalid_credential_export")
            self.assertIsNone(error.__context__)
            self.assertIsNone(error.__cause__)
            # No inner JSONDecodeError.doc or decoder frame containing the dump.
            self.assertIsNone(error.__traceback__.tb_next.tb_next)
            self.assertEqual(error.__traceback__.tb_next.tb_frame.f_locals["content"], b"")
        else:
            self.fail("Invalid export was accepted")

    def test_both_attribute_encodings_preserve_every_key_byte_and_exact_accounts(self):
        for byte_account in (False, True):
            for byte_group in (False, True):
                data = dump(byte_account=byte_account, byte_group=byte_group)
                raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
                result = parse_export(b"\xef\xbb\xbf" + raw)
                controller = result["controllers"][0]
                self.assertEqual(controller["iOSPairingId"], " Disposable-é-Owner ")
                self.assertEqual(bytes.fromhex(controller["iOSDeviceLTSK"]), bytes(range(32)))
                self.assertEqual(
                    bytes.fromhex(controller["iOSDeviceLTPK"] + controller["iOSDeviceLTSK"]),
                    base64.b64decode(data["General"][0]["v_Data"]),
                )
                self.assertEqual(
                    result["accessories"], {":".join(f"{n:02x}" for n in range(170, 176)): bytes(range(224, 256)).hex()}
                )
                self.assertEqual(
                    result["accessory_identifiers"],
                    {":".join(f"{n:02x}" for n in range(170, 176)): ":".join(f"{n:02X}" for n in range(170, 176))},
                )
                self.assertEqual(set(result), {"controllers", "accessories", "accessory_identifiers"})

    def test_declared_strings_are_never_guessed_as_base64_or_hex(self):
        data = dump()
        data["General"][0]["acct"] = "YWJj"
        data["General"][1]["_fieldTypes"] = "string,int64,string,string"
        data["General"][1]["v_Data"] = "a" * 32
        result = parse_export(json.dumps(data))
        self.assertEqual(result["controllers"][0]["iOSPairingId"], "YWJj")
        self.assertEqual(next(iter(result["accessories"].values())), b"a".hex() * 32)
        data["General"][1]["v_Data"] = "ab" * 32
        # A 64-character string is 64 bytes, not a 32-byte hexadecimal key.
        self.reject(data)

    def test_unrelated_categories_groups_and_deleted_records_are_discarded(self):
        data = dump()
        expected = parse_export(json.dumps(data))
        for byte_group in (False, True):
            unrelated = record(
                "unrelated-password-account", b"unrelated-password", group="other.group", byte_group=byte_group
            )
            unrelated["v_Data"] = "not even base64"
            unrelated["acct"] = {"unrelated": "secret"}
            data["General"].append(unrelated)
        deleted = record("deleted-account", b"deleted-secret", tomb=1)
        deleted["v_Data"] = "malformed deleted key"
        data["General"].append(deleted)
        deleted_conflict = deepcopy(data["General"][0])
        deleted_conflict.update(tomb=1, v_Data=b64(bytes(64)))
        data["General"].append(deleted_conflict)
        no_group = {"acct": "unrelated-account", "_fieldOrder": "acct", "_fieldTypes": "string"}
        data["General"].append(no_group)
        for category in ("Internet", "Keys", "Certs", "unrelated-category"):
            data[category] = [{"unrelated": "unrelated-password"}, *deepcopy(dump()["General"])]
        result = parse_export(json.dumps(data))
        self.assertEqual(result, expected)
        self.assertNotIn("unrelated", json.dumps(result))
        self.assertNotIn("deleted", json.dumps(result))

    def test_live_records_without_optional_tombstone_attribute_are_imported(self):
        data = dump()
        expected = parse_export(json.dumps(data))
        for row in data["General"]:
            del row["tomb"]
            row["_fieldOrder"] = "agrp,acct,v_Data"
            row["_fieldTypes"] = "string,string,[]uint8"
        self.assertEqual(parse_export(json.dumps(data)), expected)

    def test_malformed_metadata_and_wrong_field_types_fail_closed(self):
        modifications = [
            {"_fieldOrder": None},
            {"_fieldTypes": []},
            {"_fieldTypes": "[]uint8,int64,string"},
            {"_fieldOrder": "v_Data,tomb,acct,acct"},
            {"_fieldOrder": "v_Data,tomb,acct,"},
            {"_fieldOrder": "v_Data,tomb,acct,_agrp"},
            {"_fieldTypes": "[]uint8,int64,string,"},
            {"_fieldOrder": "v_Data,tomb,acct,missing"},
            {"_fieldTypes": "[]byte,int64,string,string"},
            {"_fieldTypes": "[]uint8,int64,int64,string"},
            {"_fieldTypes": "[]uint8,int64,string,time.Time"},
            {"_fieldTypes": "[]uint8,string,string,string"},
            {"acct": 123},
            {"tomb": True},
            {"tomb": "1"},
            {"tomb": 2},
            {"tomb": -1},
            {"unexpected_attribute": "secret"},
        ]
        for update in modifications:
            with self.subTest(update=update):
                data = dump()
                data["General"][0].update(update)
                self.reject(data)
        for value in (None, {}, "invalid", [None], [False]):
            self.reject({"General": value})
        for field in ("_fieldOrder", "_fieldTypes", "acct", "v_Data"):
            data = dump()
            del data["General"][0][field]
            self.reject(data)

    def test_malformed_base64_utf8_identifiers_lengths_and_key_pairs_fail_closed(self):
        for field in ("acct", "agrp", "v_Data"):
            for value in (None, "!invalid!", "YQ", "YQ==\n", "é"):
                data = dump(byte_account=True, byte_group=True)
                data["General"][0][field] = value
                self.reject(data)
        for value in (b"\xff", b"", b"account\x00", b"x" * 257):
            data = dump(byte_account=True)
            data["General"][0]["acct"] = b64(value)
            self.reject(data)
        for length in (0, 1, 31, 33, 63, 65, 96):
            data = dump()
            data["General"][1]["v_Data"] = b64(bytes(length))
            self.reject(data)
        data = dump()
        data["General"][0]["v_Data"] = b64(bytes(64))
        self.reject(data)
        data = dump()
        data["General"][0]["v_Data"] = b64(base64.b64decode(data["General"][0]["v_Data"])[::-1])
        self.reject(data)
        self.reject('{"General": [], "General": []}')
        self.reject('{"General": ["unrelated-private-secret"')
        self.reject('{"General":' + "[" * 2000)

    def test_identical_duplicates_deduplicate_but_conflicting_pins_and_identities_fail(self):
        data = dump()
        expected = parse_export(json.dumps(data))
        data["General"] *= 2
        self.assertEqual(parse_export(json.dumps(data)), expected)
        original = dump()
        data = deepcopy(original)
        different_source, _, _ = fixtures.export_fixture()
        data["General"].append(dump(different_source)["General"][0])
        data["General"][-1]["acct"] = original["General"][0]["acct"]
        self.reject(data)
        for update in ({"v_Data": b64(b"a" * 32)}, {"acct": original["General"][1]["acct"].lower()}):
            data = deepcopy(original)
            extra = deepcopy(data["General"][1])
            extra.update(update)
            data["General"].append(extra)
            self.reject(data)

    def test_raw_utf8_boundaries_and_existing_normalized_limits(self):
        data = dump()
        data["Internet"] = "private-unrelated-secret-" * 10000
        text = json.dumps(data)
        self.assertGreater(len(text), MAX_IMPORT)
        expected = parse_export(text)
        boundary = text + " " * (MAX_RAW_IMPORT - len(text))
        self.assertEqual(parse_export(boundary), expected)
        self.assertEqual(parse_export(boundary.encode()), expected)
        self.reject(boundary + " ")
        self.reject((boundary + " ").encode())
        unicode_dump = dump()
        unicode_dump["Keys"] = "é" * (MAX_RAW_IMPORT // 2)
        unicode_text = json.dumps(unicode_dump, ensure_ascii=False)
        self.assertLess(len(unicode_text), MAX_RAW_IMPORT)
        self.assertGreater(len(unicode_text.encode()), MAX_RAW_IMPORT)
        self.reject(unicode_text)
        # Normalized and selected CSV imports keep their original byte cap.
        _, _, normalized = fixtures.export_fixture()
        value = json.dumps(normalized)
        self.assertTrue(parse_export(value + " " * (MAX_IMPORT - len(value))))
        self.reject(value + " " * (MAX_IMPORT + 1 - len(value)))
        self.reject("Account,Password,App / Group\n" + " " * MAX_IMPORT)
        self.reject({"General": []})
        for index in (0, 1):
            data = dump()
            data["General"] = [data["General"][0]] * (64 if index == 0 else 1) + [data["General"][1]] * (
                64 if index == 1 else 1
            )
            self.assertTrue(parse_export(json.dumps(data)))
            data["General"].append(data["General"][index])
            self.reject(data)

    def test_actual_ha_text_and_request_limits_leave_room_for_json_escaping(self):
        selector = TextSelector(TextSelectorConfig(multiline=True, autocomplete="off"))
        raw = "\x00" * MAX_RAW_IMPORT
        self.assertEqual(selector(raw), raw)
        request = json.dumps({"credential_export": raw}).encode()
        self.assertLess(len(request), MAX_CLIENT_SIZE)
        self.assertEqual(MAX_CLIENT_SIZE, 16 * 1024 * 1024)

    def test_all_existing_json_and_csv_formats_normalize_to_the_same_credentials(self):
        source, accessory, typed = fixtures.export_fixture()
        expected = parse_export(json.dumps(dump(source, accessory)))
        flat = {
            **source,
            "AccessoryPairingID": accessory["accessory_id"],
            "AccessoryLTPK": accessory["accessory_key"],
        }
        csv = (
            "Account,Password,App / Group\n"
            f"{source['iOSPairingId']},{source['iOSDeviceLTPK']}{source['iOSDeviceLTSK']},com.apple.hap.pairing\n"
            f"{accessory['accessory_id']},{accessory['accessory_key']},com.apple.hap.pairing\n"
        )
        for content in (json.dumps(typed), json.dumps(flat), json.dumps({accessory["accessory_id"]: flat}), csv):
            self.assertEqual(parse_export(content), expected)


class IrestoreFlowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.OnboardingTests.asyncSetUp
    asyncTearDown = fixtures.OnboardingTests.asyncTearDown
    flow_environment = fixtures.OnboardingTests.flow_environment
    finish_flow = fixtures.OnboardingTests.finish_flow
    flow = flow_fixtures.FlowLifecycleTests.flow

    async def test_large_dump_uses_existing_form_and_retains_only_normalized_hap_state(self):
        manager = self.hass.config_entries.flow
        data = dump(self.source, self.accessory, byte_account=True, byte_group=True)
        data["Internet"] = "UNRELATED-PRIVATE-PASSWORD" * 10000
        async with self.flow_environment():
            with patch(
                "homeassistant.config_entries._async_get_flow_handler",
                AsyncMock(return_value=config_flow.HomePodSensorLinkFlow),
            ):
                result = await manager.async_init(config_flow.DOMAIN, context={"source": "user"})
            flow_id = result["flow_id"]
            flow = manager._progress[flow_id]
            await manager.async_configure(flow_id, {"devices": [self.target["accessory_id"]]})
            await manager.async_configure(flow_id, {"recovery_devices": [self.target["accessory_id"]]})
            await manager.async_configure(flow_id, {"next_step_id": "credential_export"})
            # Exercise an invalid retry through HA's actual schema and manager.
            result = await manager.async_configure(flow_id, {"credential_export": '{"General":"PRIVATE-INVALID"'})
            self.assertEqual(result["errors"], {"base": "invalid_credential_export"})
            self.assertNotIn("PRIVATE-INVALID", str(vars(flow)))
            self.assertIsNone(flow._imported)
            with patch.object(
                flow, "_begin", AsyncMock(return_value=flow.async_show_menu(step_id="credentials", menu_options=[]))
            ):
                result = await manager.async_configure(flow_id, {"credential_export": json.dumps(data)})
            self.assertEqual(flow._imported, parse_export(json.dumps(data)))
            self.assertNotIn("UNRELATED-PRIVATE-PASSWORD", str(vars(flow)))
            self.assertNotIn("_fieldOrder", str(vars(flow)))
            self.assertNotIn(self.source["iOSDeviceLTSK"], str(result))
            self.assertEqual(self.vault.records(), {})
            manager.async_abort(flow_id)
            self.assertIsNone(flow._imported)

    async def test_selected_device_only_persists_validated_source_and_user_identity(self):
        flow = self.flow()
        data = dump(self.source, self.accessory)
        data["General"].append(record("unselected-accessory", b"a" * 32))
        data["General"].append(record("unrelated-account", b"UNRELATED-PRIVATE-PASSWORD", group="other.group"))
        async with self.flow_environment() as backend:
            await flow.async_step_user()
            await flow.async_step_devices({"devices": [self.target["accessory_id"]]})
            await flow.async_step_policy({"recovery_devices": [self.target["accessory_id"]]})
            payload = {"credential_export": json.dumps(data)}
            result = await self.finish_flow(flow, await flow.async_step_credential_export(payload))
            self.assertEqual(payload, {})
            self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY)
            saved = list(self.vault.records().values())
            self.assertEqual(len(saved), 1)
            target = saved[0]["target"]
            self.assertEqual(target["accessory_id"], self.target["accessory_id"])
            self.assertEqual(target["source"], self.source)
            self.assertEqual(target["ha_permissions"], 0)
            backend.inspect_recovery.assert_awaited_once()
            backend.read_ha.assert_awaited_once()
            backend.restore_missing.assert_not_called()
            self.assertIsNone(flow._imported)
            diagnostics = await async_get_config_entry_diagnostics(self.hass, SimpleNamespace())
            stored = "".join(p.read_text() for p in Path(self.directory.name).rglob("*.json"))
            for text in (stored, json.dumps(result["data"]), json.dumps(diagnostics), str(vars(flow))):
                self.assertNotIn("unselected-accessory", text)
                self.assertNotIn("UNRELATED-PRIVATE-PASSWORD", text)
                self.assertNotIn("_fieldOrder", text)

    async def test_import_cannot_bypass_existing_administrator_permission_validation(self):
        flow = self.flow()
        backend = SimpleNamespace(inspect_recovery=AsyncMock(side_effect=SafetyFailure("source_not_admin")))
        async with self.flow_environment(backend):
            await flow.async_step_user()
            flow._selected = flow._recovery_ids = [self.target["accessory_id"]]
            await self.finish_flow(
                flow,
                await flow.async_step_credential_export(
                    {"credential_export": json.dumps(dump(self.source, self.accessory))}
                ),
            )
        saved = list(self.vault.records().values())[0]
        self.assertEqual(saved["phase"], "needs_credentials")
        self.assertNotIn("source", saved["target"])
        backend.read_ha.assert_not_called()
        backend.restore_missing.assert_not_called()
        self.assertIsNone(flow._imported)
