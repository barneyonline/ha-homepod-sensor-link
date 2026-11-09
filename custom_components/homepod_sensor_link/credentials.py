"""Validate selected pinned identities from a protected local administrator bundle."""

from __future__ import annotations
from .models import Data, Target


from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .backend import check_current
from .engine import SafetyFailure

DOMAIN = "homepod_sensor_link"


def public_targets(targets: list[Target]) -> list[Target]:
    """Canonical public pins; hexadecimal letter case is not an identity change."""
    return [
        {
            key: (target[key].lower() if key in ("accessory_id", "accessory_key", "ha_key") else target[key])
            for key in ("entry_id", "accessory_id", "accessory_key", "ha_id", "ha_key", "ha_permissions", "owned")
            if key in target
        }
        for target in targets
    ]


def _public(secret: str) -> str:
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(secret)).public_key().public_bytes_raw().hex()


def validate_target(target: Target, source_pin: Data | None = None) -> Data:
    """Pin owned credentials and reject changed private/public identities."""
    try:
        data = check_current(target)
        if (
            _public(data["iOSDeviceLTSK"]) != target["ha_key"].lower()
            or len(bytes.fromhex(target["accessory_key"])) != 32
        ):
            raise ValueError
        if "source" in target:
            source = target["source"]
            if (
                set(source) != {"iOSPairingId", "iOSDeviceLTSK", "iOSDeviceLTPK"}
                or source["iOSPairingId"] == target["ha_id"]
                or _public(source["iOSDeviceLTSK"]) != source["iOSDeviceLTPK"].lower()
            ):
                raise ValueError
            if source_pin and source_pin != {k: v for k, v in source.items() if k != "iOSDeviceLTSK"}:
                raise ValueError
        return data
    except Exception:
        raise SafetyFailure("invalid_pinned_identity") from None
