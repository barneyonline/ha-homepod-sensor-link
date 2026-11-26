"""Parse explicit user exports. No keychain access, network or secret logging."""

from __future__ import annotations
from typing import Any, Literal
from .models import Data


import base64
import csv
import io
import json
from ipaddress import ip_address
import re

from .credentials import _public
from .engine import SafetyFailure

MAX_IMPORT = 131072
# HA 2026.11.0 http/server.py permits 16 MiB requests; TextSelector has no
# length cap. Leave room for the JSON envelope and worst-case 6x escaping.
# Only irestore dumps get this allowance; normalized formats retain MAX_IMPORT.
MAX_RAW_IMPORT = 2 * 1024 * 1024
CONTROLLER_FIELDS = {"iOSPairingId", "iOSDeviceLTSK", "iOSDeviceLTPK"}
PAIRING_FIELDS = CONTROLLER_FIELDS | {"AccessoryPairingID", "AccessoryLTPK"}
TRANSPORT_FIELDS = {"AccessoryIP", "AccessoryIPs", "AccessoryPort", "Connection"}
_BTLE_LABEL = "BTLE HomeKit Accessory Identifier"
_IRESTORE_BTLE_LABELS = (
    ("string", _BTLE_LABEL),
    ("[]uint8", base64.b64encode(_BTLE_LABEL.encode("ascii")).decode("ascii")),
)
_CSV_BINARY_PLACEHOLDER = re.compile(r"[0-9]+ bytes \(binary\)")
type _ExportErrorCode = Literal[
    "invalid_credential_export",
    "credential_export_too_large",
    "credential_export_binary_placeholder",
    "irestore_no_controller",
]


class CredentialExportError(SafetyFailure):
    """An export rejection with a fixed, user-facing error code."""

    def __init__(self, code: _ExportErrorCode) -> None:
        super().__init__(code)
        self.code = code


def discovery_hosts(record: Data) -> list[str]:
    """Use literal unicast address hints only; advertised HAP ports stay fresh."""
    values = record.get("AccessoryIPs", [])
    values = values if isinstance(values, list) else []
    hosts = []
    for value in [record.get("AccessoryIP"), *values]:
        try:
            if not isinstance(value, str):
                continue
            address = ip_address(value)
            if address.is_unspecified or address.is_multicast or address.is_loopback or address.is_link_local:
                continue
            hosts.append(str(address))
        except ValueError:
            continue
    return hosts


def _hex(value: str, length: int) -> str:
    if not isinstance(value, str) or len(value) != length or len(bytes.fromhex(value)) * 2 != length:
        raise ValueError
    return value.lower()


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError
    return value


def _irestore_fields(record: Data) -> dict[str, str]:
    """Map the parallel metadata emitted by irestore v0.4.0 parseRecord."""
    order, types = record["_fieldOrder"], record["_fieldTypes"]
    if not isinstance(order, str) or not isinstance(types, str):
        raise ValueError
    names, kinds = order.split(","), types.split(",")
    if (
        len(names) != len(kinds)
        or len(set(names)) != len(names)
        or any(not name or name.startswith("_") for name in names)
        or any(not kind for kind in kinds)
        or set(names) != {name for name in record if not name.startswith("_")}
    ):
        raise ValueError
    return dict(zip(names, kinds, strict=True))


def _irestore_bytes(record: Data, fields: dict[str, str], name: str) -> bytes:
    """Decode exactly the declared Go type, never infer an encoding from text."""
    value = record[name]
    if not isinstance(value, str):
        raise ValueError
    if fields[name] == "string":
        return value.encode("utf-8")
    if fields[name] == "[]uint8":
        return base64.b64decode(value, validate=True)
    raise ValueError


def _irestore_records(data: Data) -> tuple[list[Data], list[Data]]:
    """Discard other categories/groups before reading any account or key data."""
    records = data["General"]
    if records is None:
        # irestore marshals an empty Go slice as JSON null.
        records = []
    if not isinstance(records, list):
        raise ValueError
    controllers: list[Data] = []
    accessories: list[Data] = []
    for record in records:
        if not isinstance(record, dict):
            raise ValueError
        if record.get("_fieldOrder") == record.get("_fieldTypes") == "" and all(
            name.startswith("_") for name in record
        ):
            # parseRecord can emit metadata only when ASN.1 decoding fails.
            continue
        fields = _irestore_fields(record)
        if "agrp" not in fields or _irestore_bytes(record, fields, "agrp") != b"com.apple.hap.pairing":
            continue
        if "tomb" in fields:
            if fields["tomb"] != "int64" or type(record["tomb"]) is not int or record["tomb"] not in (0, 1):
                raise ValueError
            if record["tomb"] == 1:
                continue
        if any((fields.get(name), record.get(name)) in _IRESTORE_BTLE_LABELS for name in ("svce", "labl", "desc")):
            # A BLE identifier is metadata, even if its length matches a key.
            continue
        identifier = _irestore_bytes(record, fields, "acct").decode("utf-8")
        value = _irestore_bytes(record, fields, "v_Data")
        if len(value) == 64:
            controllers.append(
                {"iOSPairingId": identifier, "iOSDeviceLTPK": value[:32].hex(), "iOSDeviceLTSK": value[32:].hex()}
            )
        elif len(value) == 32:
            accessories.append({"AccessoryPairingID": identifier, "AccessoryLTPK": value.hex()})
        else:
            raise ValueError
    if not controllers:
        raise CredentialExportError("irestore_no_controller")
    return controllers, accessories


def parse_export(content: bytes | str) -> Data:
    """Return normalized HAP credentials or an error carrying no decoder context."""
    code: _ExportErrorCode = "invalid_credential_export"
    try:
        return _parse_export(content)
    except CredentialExportError as error:
        code = error.code
    except Exception:
        pass
    # Do not attach JSONDecodeError.doc, decoder tracebacks or the dump to the
    # outward exception. The only frame exposed here has no raw input left.
    content = b""
    raise CredentialExportError(code)


def _parse_export(content: bytes | str) -> Data:
    """Parse typed/aiohomekit JSON, selected HAP CSV, or native irestore JSON."""
    if len(content) > MAX_RAW_IMPORT:
        raise CredentialExportError("credential_export_too_large")
    size = len(content) if isinstance(content, bytes) else len(content.encode("utf-8"))
    if size > MAX_RAW_IMPORT:
        raise CredentialExportError("credential_export_too_large")
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
    controllers: list[Data] = []
    accessories: list[Data] = []
    hosts: dict[str, None] = {}
    if text.lstrip().startswith("{"):

        def pairs(items: list[tuple[str, Any]]) -> Data:
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError
                result[key] = value
            return result

        data = json.loads(text, object_pairs_hook=pairs)
        if "General" in data:
            controllers, accessories = _irestore_records(data)
        elif size > MAX_IMPORT:
            raise CredentialExportError("credential_export_too_large")
        elif "iOSDeviceLTSK" in data:
            controllers = [{k: data[k] for k in ("iOSPairingId", "iOSDeviceLTSK", "iOSDeviceLTPK") if k in data}]
            if "AccessoryPairingID" in data:
                accessories = [{k: data[k] for k in ("AccessoryPairingID", "AccessoryLTPK")}]
            if set(data) - {
                "iOSPairingId",
                "iOSDeviceLTSK",
                "iOSDeviceLTPK",
                "AccessoryPairingID",
                "AccessoryLTPK",
            }:
                raise ValueError
        elif set(data) == {"controllers", "accessories"}:
            controllers, accessories = data["controllers"], data["accessories"]
        else:
            # aiohomekit exports are keyed by accessory ID. Endpoints are
            # historical metadata. Addresses only prompt fresh DNS-SD;
            # exported ports and identities never authorise connections.
            if not 1 <= len(data) <= 64:
                raise ValueError
            for identifier, record in data.items():
                if (
                    not isinstance(record, dict)
                    or set(record) - (PAIRING_FIELDS | TRANSPORT_FIELDS)
                    or identifier.lower() != record["AccessoryPairingID"].lower()
                ):
                    raise ValueError
                controllers.append({k: record[k] for k in CONTROLLER_FIELDS if k in record})
                accessories.append({k: record[k] for k in ("AccessoryPairingID", "AccessoryLTPK")})
                hosts.update(dict.fromkeys(discovery_hosts(record)))
    else:
        if size > MAX_IMPORT:
            raise CredentialExportError("credential_export_too_large")
        reader = csv.DictReader(io.StringIO(text))
        if not {"Account", "Password", "App / Group"} <= set(reader.fieldnames or []):
            raise ValueError
        for row in reader:
            if row["App / Group"] != "com.apple.hap.pairing":
                raise ValueError
            value, identifier = row["Password"], row["Account"]
            # Backup Explorer puts a label-only item in Title instead of Label.
            if _BTLE_LABEL in (row.get("Service"), row.get("Label")) or (
                not identifier and row.get("Title") == _BTLE_LABEL
            ):
                continue
            if _CSV_BINARY_PLACEHOLDER.fullmatch(value or ""):
                raise CredentialExportError("credential_export_binary_placeholder")
            if len(value) == 128:
                controllers.append(
                    {"iOSPairingId": identifier, "iOSDeviceLTPK": value[:64], "iOSDeviceLTSK": value[64:]}
                )
            elif len(value) == 64:
                accessories.append({"AccessoryPairingID": identifier, "AccessoryLTPK": value})
            else:
                raise ValueError
    if (
        not isinstance(controllers, list)
        or not isinstance(accessories, list)
        or not 1 <= len(controllers) <= 64
        or len(accessories) > 64
    ):
        raise ValueError
    identities: dict[str, Data] = {}
    pins: dict[str, str] = {}
    raw_ids: dict[str, str] = {}
    for source in controllers:
        if set(source) - {"iOSPairingId", "iOSDeviceLTSK", "iOSDeviceLTPK"}:
            raise ValueError
        identifier = _identifier(source["iOSPairingId"])
        secret = _hex(source["iOSDeviceLTSK"], 64)
        public = _public(secret)
        if "iOSDeviceLTPK" in source and _hex(source["iOSDeviceLTPK"], 64) != public:
            raise ValueError
        value = {"iOSPairingId": identifier, "iOSDeviceLTSK": secret, "iOSDeviceLTPK": public}
        if identifier in identities and identities[identifier] != value:
            raise ValueError
        identities[identifier] = value
    for accessory in accessories:
        if set(accessory) != {"AccessoryPairingID", "AccessoryLTPK"}:
            raise ValueError
        raw = _identifier(accessory["AccessoryPairingID"])
        identifier = raw.lower()
        public = _hex(accessory["AccessoryLTPK"], 64)
        if identifier in pins and (pins[identifier] != public or raw_ids[identifier] != raw):
            raise ValueError
        pins[identifier] = public
        raw_ids[identifier] = raw
    result = {"controllers": list(identities.values()), "accessories": pins, "accessory_identifiers": raw_ids}
    if hosts:
        result["discovery_hosts"] = list(hosts)[:64]
    return result
