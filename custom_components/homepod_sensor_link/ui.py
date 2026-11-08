"""Require the same local administrator for native credential forms."""

from __future__ import annotations

from ipaddress import ip_address
from typing import cast

from aiohttp import web
from homeassistant.auth.models import User
from homeassistant.core import HomeAssistant
from homeassistant.helpers.http import current_request
from homeassistant.helpers.network import is_cloud_connection
from homeassistant.util.network import is_local


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
