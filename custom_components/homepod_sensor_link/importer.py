"""Parse explicit user exports. No keychain access, network or secret logging."""

from __future__ import annotations
from typing import Any
from .models import Data


import csv
import io
import json

from .credentials import _public
from .engine import SafetyFailure

MAX_IMPORT = 131072


def _hex(value: str, length: int) -> str:
    if not isinstance(value, str) or len(value) != length or len(bytes.fromhex(value)) * 2 != length:
        raise ValueError
    return value.lower()


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise ValueError
    return value


def parse_export(content: bytes | str) -> Data:
    """Supported typed JSON or selected HAP rows from iPhone Backup Explorer CSV."""
    try:
        if len(content) > MAX_IMPORT:
            raise ValueError
        text = content.decode("utf-8-sig") if isinstance(content, bytes) else content
        controllers, accessories = [], []
        if text.lstrip().startswith("{"):

            def pairs(items: list[tuple[str, Any]]) -> Data:
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError
                    result[key] = value
                return result

            data = json.loads(text, object_pairs_hook=pairs)
            if "iOSDeviceLTSK" in data:
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
            else:
                if set(data) != {"controllers", "accessories"}:
                    raise ValueError
                controllers, accessories = data["controllers"], data["accessories"]
        else:
            reader = csv.DictReader(io.StringIO(text))
            if not {"Account", "Password", "App / Group"} <= set(reader.fieldnames or []):
                raise ValueError
            for row in reader:
                if row["App / Group"] != "com.apple.hap.pairing":
                    raise ValueError
                value, identifier = row["Password"], row["Account"]
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
        return {"controllers": list(identities.values()), "accessories": pins, "accessory_identifiers": raw_ids}
    except Exception:
        raise SafetyFailure("invalid_credential_export") from None
