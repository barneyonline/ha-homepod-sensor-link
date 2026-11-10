"""Fresh authenticated HAP sessions; no remove-pairing or reset operations."""

from __future__ import annotations

from typing import cast
from .models import Data, Target, Readings, PairingRow
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from aiohomekit.controller.ip import IpController
from zeroconf.asyncio import AsyncZeroconf


import asyncio
from contextlib import AsyncExitStack, asynccontextmanager, suppress
import ipaddress
import math
import sys

from aiohomekit.controller.ip.pairing import IpPairing
from aiohomekit.controller.abstract import AbstractPairingData
from aiohomekit.exceptions import (
    AccessoryDisconnectedError,
    AccessoryNotFoundError,
    AuthenticationError,
    IncorrectPairingIdError,
    InvalidSignatureError,
)
from aiohomekit.zeroconf import HAP_TYPE_TCP, HomeKitService
from zeroconf.asyncio import AsyncServiceInfo
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .engine import AuthenticationFailure, SafetyFailure, RecoveryPaused, canonical


class AccessoryIdMismatch(SafetyFailure):
    """The contacted endpoint belongs to a different accessory ID."""


def check_current(target: Target) -> Data:
    """Accept only this integration's dedicated, pinned regular User identity."""
    if target.get("owned") is not True or target.get("ha_permissions") != 0:
        raise SafetyFailure("owned_controller_required")
    data = dict(target.get("pairing", {}))
    if (
        data.get("Connection") != "IP"
        or str(data.get("AccessoryPairingID", "")).lower() != target["accessory_id"].lower()
        or str(data.get("AccessoryLTPK", "")).lower() != target["accessory_key"].lower()
        or data.get("iOSPairingId") != target["ha_id"]
        or str(data.get("iOSDeviceLTPK", "")).lower() != target["ha_key"].lower()
    ):
        raise SafetyFailure("owned_identity_changed")
    try:
        public = (
            Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["iOSDeviceLTSK"]))
            .public_key()
            .public_bytes_raw()
            .hex()
        )
        if public != target["ha_key"].lower():
            raise ValueError
    except Exception:
        raise SafetyFailure("owned_private_key_changed") from None
    return data


class HomeKitBackend:
    def __init__(self, controller: IpController, zeroconf: AsyncZeroconf) -> None:
        self.controller, self.zeroconf = controller, zeroconf
        self.write_allowed: Callable[[], bool] = lambda: True
        self.before_write: Callable[[Target], Awaitable[None]] | None = None
        self.after_write: Callable[[Target], Awaitable[None]] | None = None

    @staticmethod
    def preserved(target: Target, rows: list[PairingRow]) -> None:
        expected = target.get("expected_pairings")
        if not expected:
            return
        current = canonical(rows)
        present = any(row["pairingId"] == target["ha_id"] for row in rows)
        for fingerprint, count in canonical(expected).items():
            if fingerprint[0] == target["ha_id"] and not present:
                continue  # a lost reply may mean the add was never accepted
            if current[fingerprint] < count:
                raise SafetyFailure("existing_pairings_changed")

    async def clear_expected(self, target: Target) -> None:
        target.pop("expected_pairings", None)
        if self.after_write:
            await self.after_write(target)

    async def endpoint(self, target: Target, *, require_discovery: bool = False) -> Data:
        try:
            discovery = await self.controller.async_find(target["accessory_id"], timeout=2)
        except AccessoryNotFoundError:
            discovery = None
        if discovery is not None:
            info = AsyncServiceInfo(HAP_TYPE_TCP, f"{discovery.description.name}.{HAP_TYPE_TCP}")
            if await info.async_request(self.zeroconf.zeroconf, 3000):
                description = HomeKitService.from_service_info(info)
                if description.id.lower() != target["accessory_id"].lower():
                    raise SafetyFailure("advertised_identity_changed")
                return self._endpoint(description.addresses, description.port)
        if require_discovery:
            raise OSError("endpoint_unavailable")
        # Only the integration's own saved endpoint is a fallback. Authentication
        # still verifies the accessory's pinned identity before any pairing write.
        data = check_current(target)
        addresses = data.get("AccessoryIPs") or [data.get("AccessoryIP")]
        return self._endpoint(addresses, data.get("AccessoryPort"))

    @staticmethod
    def _endpoint(addresses: Sequence[str | None] | None, port: int | None) -> Data:
        if not addresses or not isinstance(port, int) or not 0 < port < 65536:
            raise OSError("endpoint_unavailable")
        try:
            parsed = [ipaddress.ip_address(a) for a in addresses if a]
        except ValueError:
            raise OSError("endpoint_unavailable") from None
        parsed = [a for a in parsed if not a.is_unspecified and not a.is_multicast]
        if not parsed:
            raise OSError("endpoint_unavailable")
        parsed.sort(key=lambda a: a.version)
        hosts = [str(a) for a in parsed]
        return {"AccessoryIP": hosts[0], "AccessoryIPs": hosts, "AccessoryPort": port}

    @asynccontextmanager
    async def session(self, target: Target, source: bool = False) -> AsyncIterator[IpPairing]:
        data = check_current(target)
        require_discovery = False
        try:
            saved = self._endpoint(data.get("AccessoryIPs") or [data.get("AccessoryIP")], data.get("AccessoryPort"))
        except OSError:
            pass
        else:
            async with AsyncExitStack() as stack:
                try:
                    pairing = await stack.enter_async_context(self._session(target, source, saved, 2))
                except AccessoryIdMismatch:
                    # DHCP may have reassigned this hint to another accessory.
                    # Require a matched discovery before trying the pinned ID.
                    require_discovery = True
                except (OSError, AccessoryDisconnectedError, AccessoryNotFoundError):
                    pass
                else:
                    # Retry only connection establishment, never the yielded
                    # operation (which may already have added a pairing).
                    yield pairing
                    return
        endpoint = await self.endpoint(target, require_discovery=require_discovery)
        async with self._session(target, source, endpoint, 10) as pairing:
            # Remember an authenticated move for subsequent fresh sessions.
            target["pairing"].update(endpoint)
            yield pairing

    @asynccontextmanager
    async def _session(
        self, target: Target, source: bool, endpoint: Data, connect_timeout: float
    ) -> AsyncIterator[IpPairing]:
        data = check_current(target)
        data.update(endpoint)
        if source:
            data.update(target["source"])
        pairing = IpPairing(self.controller, cast(AbstractPairingData, data))
        try:
            async with asyncio.timeout(35):
                async with asyncio.timeout(connect_timeout):
                    await cast(Callable[[], Awaitable[None]], pairing._ensure_connected)()
                yield pairing
        except Exception as error:
            last = pairing.connection.last_connector_error
            if isinstance(error, (IncorrectPairingIdError, InvalidSignatureError)) or isinstance(
                last, (IncorrectPairingIdError, InvalidSignatureError)
            ):
                if isinstance(error, (AuthenticationError, InvalidSignatureError)) or isinstance(
                    last, (AuthenticationError, InvalidSignatureError)
                ):
                    raise SafetyFailure("accessory_identity_changed") from None
                raise AccessoryIdMismatch("accessory_identity_changed") from None
            if isinstance(error, AuthenticationError) or isinstance(last, AuthenticationError):
                if source:
                    raise SafetyFailure("recovery_identity_rejected") from None
                raise AuthenticationFailure() from None
            raise
        finally:
            # aiohomekit awaits its connector task again during close. If that
            # task already failed, shutdown re-raises its original exception and
            # would overwrite AuthenticationFailure/SafetyFailure above. Keep
            # the primary result (including cancellation), and finish closing
            # the transport even when connector cleanup raises.
            primary_error = sys.exception()
            try:
                await pairing.shutdown()
                # aiohomekit suppresses CancelledError while draining its
                # connector, including cancellation of this enclosing task.
                # Finish closing first, then preserve the caller's cancellation.
                if cast(asyncio.Task[None], asyncio.current_task()).cancelling():
                    raise asyncio.CancelledError
            except Exception:
                transport = pairing.connection.transport
                if transport is not None:
                    with suppress(Exception):
                        transport.close()
                if primary_error is not None:
                    raise primary_error from None
                # Cleanup alone is not evidence permitting a pairing write.
                raise OSError("session_cleanup_failed") from None

    async def read_ha(self, target: Target, source: bool = False) -> Readings:
        async with self.session(target, source=source) as pairing:
            accessories = await pairing.list_accessories_and_characteristics()
            characteristics = {}
            for accessory in accessories:
                for service in accessory["services"]:
                    for item in service["characteristics"]:
                        kind = item["type"].split("-")[0].lstrip("0").upper()
                        if kind in ("10", "11") and "pr" in item.get("perms", []):
                            characteristics[(accessory["aid"], item["iid"])] = kind
            if set(characteristics.values()) != {"10", "11"}:
                raise SafetyFailure("sensor_layout_changed")
            values = await pairing.get_characteristics(characteristics)
            if any(
                k not in values
                or values[k].get("status", 0) != 0
                or type(values[k].get("value")) not in (int, float)
                or not math.isfinite(values[k]["value"])
                for k in characteristics
            ):
                raise OSError("sensor_read_failed")
            if len(characteristics) != 2:
                raise SafetyFailure("ambiguous_sensor_layout")
            readings = {
                "humidity" if kind == "10" else "temperature": values[key]["value"]
                for key, kind in characteristics.items()
            }
        if not source and target.get("expected_pairings"):
            result = await self.inspect_recovery(target)
            if not result["ha_identity_present"]:
                raise OSError("pairing_preservation_pending")
            await self.clear_expected(target)
        return readings

    async def restore_missing(self, target: Target) -> bool:
        async with self.session(target, source=True) as pairing:
            before = await cast(Callable[[], Awaitable[list[PairingRow]]], pairing.list_pairings)()
            canonical(before)  # Validate before permitting any write.
            self.preserved(target, before)
            own = [r for r in before if r["pairingId"] == target["source"]["iOSPairingId"]]
            if (
                len(own) != 1
                or own[0]["permissions"] != 1
                or own[0]["publicKey"].lower() != target["source"]["iOSDeviceLTPK"].lower()
            ):
                raise SafetyFailure("recovery_admin_changed")
            existing = [r for r in before if r["pairingId"] == target["ha_id"]]
            if existing:
                if (
                    len(existing) != 1
                    or existing[0]["publicKey"].lower() != target["ha_key"].lower()
                    or existing[0]["permissions"] != target["ha_permissions"]
                ):
                    raise SafetyFailure("ha_public_key_changed")
                if target.get("expected_pairings"):
                    await self.clear_expected(target)
                return False
            target["expected_pairings"] = [
                {
                    "pairingId": row["pairingId"],
                    "publicKey": row["publicKey"].lower(),
                    "permissions": row["permissions"],
                }
                for row in before
            ] + [
                {
                    "pairingId": target["ha_id"],
                    "publicKey": target["ha_key"].lower(),
                    "permissions": target["ha_permissions"],
                }
            ]
            if self.before_write:
                await self.before_write(target)
            # The ONLY pairing mutation implemented by this component.
            if not self.write_allowed():
                raise RecoveryPaused()
            await cast(Callable[[str, str, str], Awaitable[None]], pairing.add_pairing)(
                target["ha_id"], target["ha_key"], "User"
            )
            after = await cast(Callable[[], Awaitable[list[PairingRow]]], pairing.list_pairings)()
            expected = canonical(before)
            expected[(target["ha_id"], target["ha_key"].lower(), target["ha_permissions"])] += 1
            if canonical(after) != expected:
                raise SafetyFailure("existing_pairings_changed")
            await self.clear_expected(target)
            return True

    async def inspect_recovery(self, target: Target) -> Data:
        """Read-only commissioning check; return counts/booleans, never keys."""
        async with self.session(target, source=True) as pairing:
            rows = await cast(Callable[[], Awaitable[list[PairingRow]]], pairing.list_pairings)()
            canonical(rows)
            self.preserved(target, rows)
            own = [r for r in rows if r["pairingId"] == target["source"]["iOSPairingId"]]
            if (
                len(own) != 1
                or own[0]["permissions"] != 1
                or own[0]["publicKey"].lower() != target["source"]["iOSDeviceLTPK"].lower()
            ):
                raise SafetyFailure("recovery_admin_changed")
            ha = [r for r in rows if r["pairingId"] == target["ha_id"]]
            if ha and (
                len(ha) != 1
                or ha[0]["publicKey"].lower() != target["ha_key"].lower()
                or ha[0]["permissions"] != target["ha_permissions"]
            ):
                raise SafetyFailure("ha_public_key_changed")
            return {
                "entry_id": target["entry_id"],
                "recovery_admin_verified": True,
                "ha_identity_present": bool(ha),
                "pairing_count": len(rows),
            }

    async def pairing_present(self, target: Target) -> bool:
        result = await self.inspect_recovery(target)
        return cast(bool, result["ha_identity_present"])
