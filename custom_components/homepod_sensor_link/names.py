"""Optional AirPlay display names, matched only to fresh HAP addresses."""

from __future__ import annotations

import asyncio
import sys

from aiohomekit.zeroconf import HomeKitService
from homeassistant.components import zeroconf
from homeassistant.core import HomeAssistant
from zeroconf import IPVersion, ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

from .models import Data
from .directed_discovery import discover_hosts

AIRPLAY_TYPE = "_airplay._tcp.local."
DISCOVERY_WINDOW = 5
RESOLVE_TIMEOUT = 3000


def airplay_service(info: AsyncServiceInfo) -> Data | None:
    model = info.decoded_properties.get("model", "") or ""
    label = info.name.removesuffix("." + AIRPLAY_TYPE).strip()
    if not model.lower().startswith(("audioaccessory", "homepod")) or not label:
        return None
    return {
        "name": label,
        "addresses": {str(address) for address in info.ip_addresses_by_version(IPVersion.All)},
    }


async def discover_airplay(hass: HomeAssistant) -> list[Data]:
    """Browse public metadata without pairing or connecting to an AirPlay port."""

    async def browse() -> list[Data]:
        zc = await zeroconf.async_get_async_instance(hass)
        names: set[str] = set()

        def changed(zeroconf: Zeroconf, service_type: str, name: str, state_change: ServiceStateChange) -> None:
            if state_change is ServiceStateChange.Removed:
                names.discard(name)
            else:
                names.add(name)

        browser = AsyncServiceBrowser(zc.zeroconf, AIRPLAY_TYPE, handlers=[changed])
        try:
            await asyncio.sleep(DISCOVERY_WINDOW)

            async def resolve(name: str) -> Data | None:
                try:
                    info = AsyncServiceInfo(AIRPLAY_TYPE, name)
                    if not await info.async_request(zc.zeroconf, RESOLVE_TIMEOUT):
                        return None
                    return airplay_service(info)
                except (OSError, ValueError):
                    return None

            return [result for result in await asyncio.gather(*(resolve(name) for name in names)) if result is not None]
        finally:
            primary = sys.exception()
            try:
                await browser.async_cancel()
            except Exception:
                if primary is not None:
                    raise primary from None
                raise

    try:
        return await browse()
    except Exception:
        # Optional metadata must never prevent sensor setup or authorise recovery.
        return []


def friendly_names(candidates: dict[str, Data], services: list[Data]) -> dict[str, str]:
    """Require a one-to-one address match; identities remain pinned separately."""
    matches = {
        key: [
            index
            for index, service in enumerate(services)
            if set(candidate.get("endpoint", {}).get("AccessoryIPs", [])) & service["addresses"]
        ]
        for key, candidate in candidates.items()
    }
    return {
        key: services[indices[0]]["name"]
        for key, indices in matches.items()
        if len(indices) == 1 and sum(indices[0] in other for other in matches.values()) == 1
    }


async def confirmed_names(candidates: dict[str, Data], services: list[Data]) -> dict[str, str]:
    """Confirm cached matches using fresh replies on temporary unicast sockets."""
    proposed = friendly_names(candidates, services)
    if not proposed:
        return {}
    hosts = sorted({address for key in proposed for address in candidates[key]["endpoint"]["AccessoryIPs"]})[:64]
    hap_infos, airplay_infos = await asyncio.gather(discover_hosts(hosts), discover_hosts(hosts, AIRPLAY_TYPE))
    fresh: dict[str, Data] = {}
    for info in hap_infos:
        try:
            description = HomeKitService.from_service_info(info)
            if str(description.model).lower().startswith("homepod"):
                fresh.setdefault(description.id.lower(), {"endpoint": {"AccessoryIPs": set()}})["endpoint"][
                    "AccessoryIPs"
                ].update(description.addresses)
        except ValueError:
            continue
    fresh_services: dict[str, Data] = {}
    for info in airplay_infos:
        service = airplay_service(info)
        if service is not None:
            fresh_services.setdefault(info.name, service)["addresses"].update(service["addresses"])
    current = friendly_names(fresh, list(fresh_services.values()))
    return {key: name for key, name in proposed.items() if current.get(key) == name}
