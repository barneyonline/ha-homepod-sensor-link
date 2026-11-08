"""Read-only candidate discovery and verification of adopted native sensors."""

from __future__ import annotations
from typing import cast
from .models import Data
from homeassistant.core import HomeAssistant, State
from collections.abc import AsyncIterator, Callable, Awaitable
from datetime import datetime
from zeroconf import ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncZeroconf, AsyncServiceBrowser, AsyncServiceInfo


from contextlib import asynccontextmanager
import asyncio
import math
import sys

from aiohomekit.characteristic_cache import CharacteristicCacheMemory
from aiohomekit.controller.ip import IpController
from aiohomekit.zeroconf import HomeKitService
from homeassistant.components import zeroconf
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from .backend import HomeKitBackend
from .engine import INTERVAL, SafetyFailure

DISCOVERY_WINDOW = 5
DISCOVERY_RESOLVE_TIMEOUT = 3000
HAP_TYPE = "_hap._tcp.local."


def sensor_ids(hass: HomeAssistant, entry_id: str) -> list[str]:
    registry = er.async_get(hass)
    by_class = {}
    for entity in er.async_entries_for_config_entry(registry, entry_id):
        if entity.domain == "sensor" and not entity.disabled_by:
            kind = entity.device_class or entity.original_device_class
            if kind in ("temperature", "humidity"):
                if kind in by_class:
                    raise SafetyFailure("ambiguous_native_sensors")
                by_class[kind] = entity.entity_id
    if set(by_class) != {"temperature", "humidity"}:
        raise SafetyFailure("native_sensor_inventory_changed")
    return list(by_class.values())


def verify_readings(hass: HomeAssistant, entry_id: str, ids: list[str], since: datetime | None = None) -> None:
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.disabled_by is not None or entry.state.value != "loaded":
        raise OSError("native_entry_not_loaded")
    now = dt_util.utcnow()
    states = [hass.states.get(entity_id) for entity_id in ids]
    try:
        valid = all(
            s is not None
            and math.isfinite(float(s.state))
            and 0 <= (now - s.last_reported).total_seconds() <= 2 * INTERVAL
            and (since is None or s.last_reported >= since)
            for s in states
        )
    except (ValueError, TypeError):
        valid = False
    if not valid or {cast(State, s).attributes.get("device_class") for s in states} != {"temperature", "humidity"}:
        raise OSError("native_readings_not_fresh")


@asynccontextmanager
async def controller_session(hass: HomeAssistant) -> AsyncIterator[tuple[IpController, AsyncZeroconf]]:
    zc = await zeroconf.async_get_async_instance(hass)
    controller = IpController(CharacteristicCacheMemory(), zc)
    try:
        await cast(Callable[[], Awaitable[None]], controller.async_start)()
        yield controller, zc
    finally:
        # No pairing is stored on this read-only discovery controller.
        primary = sys.exception()
        try:
            await cast(Callable[[], Awaitable[None]], controller.async_stop)()
        except Exception:
            if primary is not None:
                raise primary from None
            raise OSError("discovery_cleanup_failed") from None


async def _discover_candidates(hass: HomeAssistant) -> dict[str, Data]:
    """Actively browse before resolving, rather than snapshot a partial cache."""
    zc = await zeroconf.async_get_async_instance(hass)
    names: set[str] = set()

    def changed(zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange) -> None:
        if state_change is ServiceStateChange.Removed:
            names.discard(name)
        else:
            names.add(name)

    browser = AsyncServiceBrowser(zc.zeroconf, HAP_TYPE, handlers=[changed])
    try:
        await asyncio.sleep(DISCOVERY_WINDOW)
    finally:
        await browser.async_cancel()

    async def resolve(name: str) -> tuple[str, Data] | None:
        try:
            info = AsyncServiceInfo(HAP_TYPE, name)
            if not await info.async_request(zc.zeroconf, DISCOVERY_RESOLVE_TIMEOUT):
                return None
            description = HomeKitService.from_service_info(info)
            if not str(description.model).lower().startswith("homepod"):
                return None
            return description.id.lower(), {
                "name": description.name,
                "endpoint": HomeKitBackend._endpoint(description.addresses, description.port),
            }
        except (ValueError, OSError):
            # One stale/unresolvable service must not hide healthy candidates.
            return None

    return {
        result[0]: result[1]
        for result in await asyncio.gather(*(resolve(name) for name in names))
        if result is not None
    }


async def discover_devices(hass: HomeAssistant) -> dict[str, Data]:
    """Only public candidate metadata; pins always come from trusted records."""
    found = await _discover_candidates(hass)
    registry = dr.async_get(hass)
    for entry in hass.config_entries.async_entries("homekit_controller"):
        devices = dr.async_entries_for_config_entry(registry, entry.entry_id)
        if entry.disabled_by is None and (
            any(str(d.model).lower().startswith("homepod") for d in devices)
            or str(entry.data.get("AccessoryPairingID", "")).lower() in found
        ):
            identifier = str(entry.data["AccessoryPairingID"]).lower()
            found[identifier] = {"name": entry.title, "native_entry_id": entry.entry_id}
    return found
