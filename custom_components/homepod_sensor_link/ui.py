"""Local administrator-only secret import, never through generic file upload."""

from __future__ import annotations
from typing import cast
from .models import Data
from homeassistant.core import HomeAssistant
from aiohttp import web
from homeassistant.auth.models import User
from homeassistant.core import Event


import asyncio
from ipaddress import ip_address
from pathlib import Path
import secrets
import time

from homeassistant.components import panel_custom
from homeassistant.helpers.http import HomeAssistantView, current_request
from homeassistant.components.http.server import StaticPathConfig
from homeassistant.helpers.network import is_cloud_connection
from homeassistant.util.network import is_local

from .importer import MAX_IMPORT, parse_export

KEY = "homepod_sensor_link.imports"
TTL = 600


def local_admin(hass: HomeAssistant, request: web.Request | None = None) -> User | None:
    request = cast(web.Request, request if request is not None else current_request.get())
    try:
        user = request.get("hass_user")
        return (
            user
            if user
            and user.is_admin
            and request.get("ha_authenticated", False)
            and not is_cloud_connection(hass)
            and is_local(ip_address(cast(str, request.remote)))
            else None
        )
    except (AttributeError, ValueError, TypeError):
        return None


class Imports:
    def __init__(self) -> None:
        self.pending: dict[str, Data] = {}

    def expire(self) -> None:
        for flow, grant in list(self.pending.items()):
            if grant["expires"] < time.monotonic():
                self.pending.pop(flow)

    def grant(self, flow: str, owner: str) -> str:
        self.expire()
        existing = self.pending.get(flow)
        if existing and existing["owner"] == owner:
            return cast(str, existing["nonce"])
        if len(self.pending) >= 8:
            raise ValueError("import_capacity")
        nonce = secrets.token_urlsafe(32)
        self.pending[flow] = {"owner": owner, "nonce": nonce, "expires": time.monotonic() + TTL}
        return nonce

    def consume(self, flow: str, owner: str) -> Data | None:
        self.expire()
        grant = self.pending.get(flow)
        if not grant or grant["owner"] != owner or "data" not in grant:
            return None
        self.pending.pop(flow)
        return cast(Data, grant["data"])


class ImportView(HomeAssistantView):
    url = "/api/homepod_sensor_link/import/{flow_id}"
    name = "api:homepod_sensor_link:import"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, imports: Imports) -> None:
        self.hass, self.imports = hass, imports

    async def post(self, request: web.Request, flow_id: str) -> web.Response:
        user = local_admin(self.hass, request)
        self.imports.expire()
        grant = self.imports.pending.get(flow_id)
        nonce = request.headers.get("X-HomePod-Import", "")
        if not user or not grant or grant["owner"] != user.id or not secrets.compare_digest(nonce, grant["nonce"]):
            return self.json({"error": "local_admin_import_required"}, status_code=403)
        if "data" in grant:
            return self.json({"error": "already_imported"}, status_code=409)
        try:
            content = bytearray()
            async with asyncio.timeout(15):
                async for block in request.content.iter_chunked(4096):
                    content.extend(block)
                    if len(content) > MAX_IMPORT:
                        raise ValueError
            data = parse_export(bytes(content))
            # An expired/revoked/replaced grant cannot be resurrected by an
            # upload that was already waiting on body bytes.
            self.imports.expire()
            if self.imports.pending.get(flow_id) is not grant:
                raise ValueError
            grant["data"] = data
            return self.json(
                {
                    "imported": True,
                    "controller_count": len(data["controllers"]),
                    "accessory_count": len(data["accessories"]),
                }
            )
        except Exception:
            return self.json({"error": "invalid_or_expired_export"}, status_code=400)


async def setup_ui(hass: HomeAssistant) -> None:
    if KEY in hass.data:
        return
    imports = hass.data[KEY] = Imports()
    hass.http.register_view(ImportView(hass, imports))
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                "/homepod_sensor_link/import.js", str(Path(__file__).parent / "frontend" / "import.js"), False
            )
        ]
    )
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path="homepod-sensor-link-import",
        webcomponent_name="homepod-sensor-link-import",
        module_url="/homepod_sensor_link/import.js",
        require_admin=True,
        config_panel_domain="homepod_sensor_link",
    )
    from homeassistant.helpers.event import async_track_time_interval
    from datetime import timedelta
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    cancel = async_track_time_interval(hass, lambda _: imports.expire(), timedelta(minutes=1))

    def stop(_: Event) -> None:
        cancel()
        imports.pending.clear()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)
