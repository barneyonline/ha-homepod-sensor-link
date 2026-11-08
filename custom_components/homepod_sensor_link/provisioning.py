"""Persist-before-write commissioning using dedicated regular User controllers."""

from __future__ import annotations
from typing import cast
from .models import Data, Target, Record, Readings, Clock, ProvisioningBackend
from homeassistant.core import HomeAssistant
from .vault import Vault


from copy import deepcopy
import uuid

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .credentials import pinned_target
from .engine import AuthenticationFailure, SafetyFailure, RecoveryPaused

BOOT = "homepod_sensor_link.boot"


def boot_id(hass: HomeAssistant) -> str:
    return cast(str, hass.data.setdefault(BOOT, uuid.uuid4().hex))


def make_owned(identifier: str, public: str, endpoint: Data) -> Target:
    private = Ed25519PrivateKey.generate()
    seed, key = private.private_bytes_raw().hex(), private.public_key().public_bytes_raw().hex()
    controller = str(uuid.uuid4())
    target = {
        "entry_id": uuid.uuid4().hex,
        "accessory_id": identifier.lower(),
        "accessory_key": public,
        "ha_id": controller,
        "ha_key": key,
        "ha_permissions": 0,
        "owned": True,
        "pairing": {
            "Connection": "IP",
            "AccessoryPairingID": identifier,
            "AccessoryLTPK": public,
            "iOSPairingId": controller,
            "iOSDeviceLTPK": key,
            "iOSDeviceLTSK": seed,
            **endpoint,
        },
    }
    return target


async def prepare(
    hass: HomeAssistant,
    vault: Vault,
    backend: ProvisioningBackend,
    selected: list[str],
    recovery_ids: list[str],
    imported: Data,
    discovered: dict[str, Data],
    *,
    flow_id: str | None = None,
    force_validate: bool = False,
) -> list[str]:
    """Only read-only probes here. Commit each device independently before writes."""
    results = []
    existing = {r["target"]["accessory_id"].lower(): r for r in vault.records().values()}
    native = {
        str(e.data.get("AccessoryPairingID", "")).lower(): e
        for e in hass.config_entries.async_entries("homekit_controller")
        if e.disabled_by is None
    }
    for identifier in selected:
        previous = existing.get(identifier)
        if (
            previous
            and not force_validate
            and previous["phase"] != "needs_credentials"
            and not (identifier in recovery_ids and "source" not in previous["target"])
        ):
            results.append(previous["target"]["entry_id"])
            continue
        entry = native.get(identifier)
        if previous and "ha_key" in previous["target"]:
            target = previous["target"]
        elif entry is not None:
            target = pinned_target(entry)
            target["ha_permissions"] = 1
        else:
            public = imported.get("accessories", {}).get(identifier)
            if not public:
                raise SafetyFailure("accessory_pin_required")
            raw_id = imported.get("accessory_identifiers", {}).get(identifier, identifier)
            target = make_owned(raw_id, public, discovered[identifier]["endpoint"])
        if previous and "ha_key" not in previous["target"]:
            target["entry_id"] = previous["target"]["entry_id"]
        target["recovery"] = identifier in recovery_ids
        record = {
            **(previous or {}),
            "target": target,
            "name": discovered.get(identifier, {}).get("name", "HomePod"),
            "phase": "prepared",
            "cancelled": False,
            "error": None,
            "flow_id": flow_id,
        }
        # A native entry can be monitored without an administrator credential.
        if not target.get("owned"):
            try:
                await backend.read_ha(target)
            except AuthenticationFailure:
                # A missing native pairing may be recovered only by the normal
                # failure/grace/rate-limited engine after admin validation.
                record["error"] = "native_authentication_pending"
        needs_source = target.get("owned") or target["recovery"]
        if needs_source:
            target.pop("source", None)
            source = None
            for candidate in imported.get("controllers", [])[:3]:
                if candidate["iOSPairingId"] == target["ha_id"]:
                    continue
                trial = {**target, "source": candidate}
                try:
                    await backend.inspect_recovery(trial)
                    await backend.read_ha(trial, source=True)
                except SafetyFailure:
                    continue
                # Network/read failures deliberately stop matching; they do not
                # justify trying every identity or permitting a pairing write.
                if (
                    previous
                    and previous.get("source_pin")
                    and previous["source_pin"] != {k: v for k, v in candidate.items() if k != "iOSDeviceLTSK"}
                ):
                    raise SafetyFailure("recovery_identity_changed")
                source = candidate
                break
            if source is None:
                record["error"] = "administrator_not_matched"
                record["phase"] = "needs_credentials"
            else:
                target["source"] = deepcopy(source)
                record["source_pin"] = {k: v for k, v in source.items() if k != "iOSDeviceLTSK"}
        if not target.get("owned") and (not needs_source or "source" in target):
            record["phase"] = "verified"
        if previous and (previous.get("commissioned") or previous["phase"] == "active") and "source" in target:
            record["commissioned"] = True
            record["phase"] = "verified"
            record.pop("authorised_boot", None)
        if record["phase"] == "prepared":
            record["authorised_boot"] = boot_id(hass)
        await vault.put(target["entry_id"], record)
        results.append(target["entry_id"])
    return results


class Provisioner:
    def __init__(self, vault: Vault, backend: ProvisioningBackend, clock: Clock, boot: str) -> None:
        self.vault, self.backend, self.clock, self.boot = vault, backend, clock, boot

    async def inspect(self, key: str, *, resume: bool = False) -> tuple[Record, Readings | None]:
        record = self.vault.record(key)
        if record.get("cancelled") and not resume:
            return record, None
        if resume:
            record["cancelled"] = False
        target = record["target"]
        if not target.get("owned") or record["phase"] in ("needs_credentials", "blocked"):
            return record, None
        # Following restart, only a fresh read with the existing dedicated key
        # is automatic. Any further pairing write needs explicit Resume.
        try:
            values = await self.backend.read_ha(target)
        except AuthenticationFailure:
            values = None
        except SafetyFailure:
            record.update(phase="blocked", error="identity_validation_failed")
            await self.vault.put(key, record)
            return record, None
        except Exception:
            record["error"] = "device_unreachable"
            await self.vault.put(key, record)
            return record, None
        else:
            record.update(phase="verified", error=None)
            record.pop("authorised_boot", None)
            await self.vault.put(key, record)
            return record, values
        initial_authorisation = record.pop("authorised_boot", None) == self.boot
        allowed = (resume or initial_authorisation) and not record.get("commissioned")
        if record["phase"] == "blocked" or "source" not in target:
            allowed = False
        if not allowed:
            record["error"] = "recovery_pending" if record.get("commissioned") else "resume_required"
            await self.vault.put(key, record)
            return record, None
        # Persist before the first pairing operation, even if cancellation or an
        # ambiguous network result follows. Reuse this target on every retry.
        now = self.clock()
        attempts = [t for t in record.get("onboarding_attempts", []) if now - t < 86400]
        if len(attempts) >= 3 or (attempts and now - attempts[-1] < 3600):
            record["error"] = "pairing_rate_limit"
            await self.vault.put(key, record)
            return record, None
        record["onboarding_attempts"] = attempts + [now]
        record.update(phase="write_uncertain", error=None)
        record["last_onboarding_attempt"] = now
        await self.vault.put(key, record)
        try:
            if await self.backend.pairing_present(target):
                # A present pairing which rejected its own pinned private key
                # requires investigation. Never overwrite or re-add it.
                raise SafetyFailure("accepted_pairing_rejected")
            await self.backend.restore_missing(target)
            record["phase"] = "accepted"
            await self.vault.put(key, record)
            values = await self.backend.read_ha(target)
            record.update(phase="verified", error=None)
        except RecoveryPaused:
            record["error"] = "competing_recovery"
            values = None
        except SafetyFailure:
            record.update(phase="blocked", error="pairing_safety_failed")
            values = None
        except Exception:
            record["error"] = "onboarding_incomplete"
            values = None
        await self.vault.put(key, record)
        return record, values

    async def cancel(self, key: str) -> None:
        record = self.vault.record(key)
        record["cancelled"] = True
        record.pop("authorised_boot", None)
        await self.vault.put(key, record)
