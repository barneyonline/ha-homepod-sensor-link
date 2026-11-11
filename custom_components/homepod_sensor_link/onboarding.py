"""Read-only public candidate discovery for dedicated sensor connections."""

from __future__ import annotations
from typing import cast
from .models import Data
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from collections.abc import AsyncIterator, Callable, Awaitable
from zeroconf import ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncZeroconf, AsyncServiceBrowser, AsyncServiceInfo


from contextlib import asynccontextmanager
import asyncio
import sys

from aiohomekit.characteristic_cache import CharacteristicCacheMemory
from aiohomekit.controller.ip import IpController
from aiohomekit.zeroconf import HomeKitService
from homeassistant.components import zeroconf

from .directed_discovery import discover_hosts
from .credentials import DOMAIN
from .backend import HomeKitBackend
from .names import discover_airplay, confirmed_names
from .vault import Vault
from .resolution import resolve_all

DISCOVERY_WINDOW = 5
DISCOVERY_RESOLVE_TIMEOUT = 3000
HAP_TYPE = "_hap._tcp.local."


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


async def _discover_candidates(hass: HomeAssistant, hosts: list[str] | None = None) -> dict[str, Data]:
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
        if hosts:
            _, directed = await asyncio.gather(asyncio.sleep(DISCOVERY_WINDOW), discover_hosts(hosts))
        else:
            directed = []
            await asyncio.sleep(DISCOVERY_WINDOW)
    finally:
        await browser.async_cancel()

    async def resolve(name: str, info: AsyncServiceInfo | None = None) -> tuple[str, Data] | None:
        try:
            if info is None:
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

    # Directed replies already contain fresh records: parse them before waiting
    # for network resolution, retaining their precedence over browser results.
    direct_results = [await resolve(info.name, info) for info in directed]
    results = [*await resolve_all(names, resolve), *direct_results]
    return {result[0]: result[1] for result in results if result is not None}


async def discover_devices(hass: HomeAssistant, hosts: list[str] | None = None) -> dict[str, Data]:
    """Only public candidate metadata; pins always come from trusted records."""
    # Browser results may come from HA's cache. Confirm proposed associations
    # from fresh wire replies before using addresses to change a device name.
    found, services = await asyncio.gather(_discover_candidates(hass, hosts), discover_airplay(hass))
    names = await confirmed_names(found, services)
    for identifier, name in names.items():
        found[identifier].update(name=name, friendly_name=name)
    return found


async def refresh_names(
    hass: HomeAssistant,
    records: dict[str, Data],
    vault: Vault,
    *,
    entry_id: str | None = None,
    discovered: dict[str, Data] | None = None,
) -> None:
    """Refresh existing devices on setup, retaining their last name on failure."""
    if discovered is None:
        try:
            discovered = await discover_devices(hass)
        except Exception:
            return
    for key, record in records.items():
        name = discovered.get(record["target"]["accessory_id"].lower(), {}).get("friendly_name")
        if name and name != record.get("name") and await vault.rename(key, name):
            record["name"] = name
            if entry_id is not None:
                registry = dr.async_get(hass)
                device = registry.async_get_device_by_identifier((DOMAIN, key), entry_id)
                if device is not None:
                    registry.async_update_device(device.id, name=name)
