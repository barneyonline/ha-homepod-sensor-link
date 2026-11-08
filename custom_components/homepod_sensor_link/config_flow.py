"""Discover, import, adopt and commission through one administrator-owned flow."""

from __future__ import annotations
from typing import cast
from .models import Data, ProvisioningBackend
from homeassistant.auth.models import User
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo


import asyncio
from urllib.parse import urlencode

import probatio as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from .backend import HomeKitBackend
from .credentials import DOMAIN, public_targets, pinned_target
from .onboarding import discover_devices, controller_session
from .provisioning import prepare
import uuid
from .ui import KEY, local_admin, setup_ui
from .vault import get_vault


def choices(options: dict[str, str], multiple: bool = False) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[selector.SelectOptionDict(value=k, label=v) for k, v in options.items()], multiple=multiple
        )
    )


class HomePodSensorLinkFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    def __init__(self) -> None:
        self._task: asyncio.Task[list[str]]
        self._progress = False
        self._committed = False
        self._force_validate = False
        self._imported: Data | None = None
        self._import_owner: str | None = None
        self._discovery_hosts: list[str] = []
        self._selected: list[str] = []
        self._new_keys: list[str] = []

    async def _access(self) -> User | None:
        user = local_admin(self.hass)
        if not user:
            return None
        await setup_ui(self.hass)
        if self._import_owner is not None and self._import_owner != user.id:
            self._imported = None
            self._import_owner = None
            self._discovery_hosts = []
        self._owner = user.id
        self._vault = await get_vault(self.hass)
        return user

    def _current(self) -> ConfigEntry | None:
        if self.context.get("source") == "reauth":
            return self._get_reauth_entry()
        return self._get_reconfigure_entry() if self.context.get("source") == "reconfigure" else None

    async def async_step_reauth(self, entry_data: Data) -> ConfigFlowResult:
        return self.async_show_form(step_id="reauth_confirm", data_schema=vol.Schema({}))

    async def async_step_reauth_confirm(self, user_input: Data | None = None) -> ConfigFlowResult:
        if not await self._access():
            return self.async_abort(reason="local_admin_required")
        current = self._get_reauth_entry()
        records = self._vault.records()
        selected = [
            records[key]
            for key in current.data["device_keys"]
            if records[key]["target"].get("recovery", True) or records[key]["phase"] == "needs_credentials"
        ]
        if not selected:
            return self.async_abort(reason="reauth_not_required")
        self._selected = [record["target"]["accessory_id"] for record in selected]
        self._recovery_ids = [
            record["target"]["accessory_id"] for record in selected if record["target"].get("recovery", True)
        ]
        self._discovered = {record["target"]["accessory_id"]: {"name": record["name"]} for record in selected}
        self._force_validate = True
        return await self.async_step_credentials()

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        self.context["title_placeholders"] = {"name": "HomePod sensors"}
        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

    async def async_step_user(self, user_input: Data | None = None) -> ConfigFlowResult:
        if not await self._access():
            return self.async_abort(reason="local_admin_required")
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return await self.async_step_devices()

    async def async_step_reconfigure(self, user_input: Data | None = None) -> ConfigFlowResult:
        if not await self._access():
            return self.async_abort(reason="local_admin_required")
        return self.async_show_menu(step_id="reconfigure", menu_options=["devices", "credentials", "manage"])

    async def async_step_devices(self, user_input: Data | None = None) -> ConfigFlowResult:
        if not await self._access():
            return self.async_abort(reason="local_admin_required")
        errors = {}
        try:
            self._discovered = (
                await discover_devices(self.hass, hosts=self._discovery_hosts)
                if self._discovery_hosts
                else await discover_devices(self.hass)
            )
        except Exception:
            self._discovered = {}
            errors["base"] = "discovery_failed"
        # Retained interrupted journals can always be selected for continuation.
        for record in self._vault.records().values():
            if record["phase"] == "active":
                self._discovered.pop(record["target"]["accessory_id"], None)
                continue
            self._discovered.setdefault(record["target"]["accessory_id"], {"name": record.get("name", "HomePod")})
        if user_input is not None:
            selected = user_input.get("devices", [])
            if selected and len(set(selected)) == len(selected) and set(selected) <= self._discovered.keys():
                self._selected = selected
                return await self.async_step_policy()
            errors["base"] = "select_devices"
        default = (
            list(self._discovered)
            if user_input is None
            else list(dict.fromkeys(key for key in user_input.get("devices", []) if key in self._discovered))
        )
        return self.async_show_form(
            step_id="devices",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("devices", default=default): choices(
                        {k: v["name"] for k, v in self._discovered.items()}, True
                    )
                }
            ),
        )

    async def async_step_policy(self, user_input: Data | None = None) -> ConfigFlowResult:
        if not await self._access():
            return self.async_abort(reason="local_admin_required")
        if user_input is not None:
            self._recovery_ids = user_input.get("recovery_devices", [])
            if not set(self._recovery_ids) <= set(self._selected):
                return self.async_abort(reason="invalid_selection")
            needs_import = any(
                k in self._recovery_ids or "native_entry_id" not in self._discovered[k] for k in self._selected
            )
            if needs_import and self._imported is None:
                return await self.async_step_credentials()
            if self._imported is None:
                self._imported = {"controllers": [], "accessories": {}}
            return await self._begin()
        return self.async_show_form(
            step_id="policy",
            data_schema=vol.Schema(
                {
                    vol.Required("recovery_devices", default=self._selected): choices(
                        {k: self._discovered[k]["name"] for k in self._selected}, True
                    )
                }
            ),
        )

    async def async_step_credentials(self, user_input: Data | None = None) -> ConfigFlowResult:
        if not await self._access():
            return self.async_abort(reason="local_admin_required")
        imports = self.hass.data[KEY]
        if user_input is not None:
            self._imported = imports.consume(self.flow_id, self._owner)
            if self._imported is not None:
                self._import_owner = self._owner
                self._discovery_hosts = self._imported.get("discovery_hosts", [])
                if not self._force_validate and (self._discovery_hosts or not self._selected):
                    return await self.async_step_devices()
                return await self._begin()
        nonce = imports.grant(self.flow_id, self._owner)
        url = "/homepod-sensor-link-import#" + urlencode({"flow": self.flow_id, "nonce": nonce})
        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema({}),
            errors={"base": "import_required"} if user_input is not None else {},
            description_placeholders={"import_url": url},
        )

    async def _begin(self) -> ConfigFlowResult:
        self._progress = False
        self._task = self.hass.async_create_background_task(self._prepare(), "HomePod onboarding", eager_start=True)
        return await self.async_step_verify()

    async def _prepare(self) -> list[str]:
        results = []
        try:
            async with controller_session(self.hass) as (controller, zc):
                backend = HomeKitBackend(controller, zc, self.hass.config_entries.async_get_entry)
                operations = ProvisioningBackend(
                    backend.read_ha, backend.restore_missing, backend.inspect_recovery, backend.pairing_present
                )
                for identifier in self._selected:
                    try:
                        async with asyncio.timeout(120):
                            before = set(self._vault.records())
                            keys = await prepare(
                                self.hass,
                                self._vault,
                                operations,
                                [identifier],
                                self._recovery_ids,
                                cast(Data, self._imported),
                                self._discovered,
                                flow_id=self.flow_id,
                                force_validate=self._force_validate,
                            )
                            results.extend(keys)
                            self._new_keys.extend(k for k in keys if k not in before)
                    except Exception:
                        # Keep a visible, resumable per-device failure while
                        # preserving every successful device and existing pin.
                        previous = next(
                            (r for r in self._vault.records().values() if r["target"]["accessory_id"] == identifier),
                            None,
                        )
                        native = next(
                            (
                                e
                                for e in self.hass.config_entries.async_entries("homekit_controller")
                                if str(e.data.get("AccessoryPairingID", "")).lower() == identifier
                            ),
                            None,
                        )
                        target = (
                            pinned_target(native)
                            if native is not None
                            else {"entry_id": uuid.uuid4().hex, "accessory_id": identifier, "owned": True}
                        )
                        target["recovery"] = identifier in self._recovery_ids
                        phase = "verified" if native is not None and not target["recovery"] else "needs_credentials"
                        record = previous or {
                            "target": target,
                            "name": self._discovered[identifier]["name"],
                            "phase": phase,
                            "flow_id": self.flow_id,
                        }
                        record["error"] = "verification_failed"
                        key = record["target"]["entry_id"]
                        await self._vault.put(key, record)
                        results.append(key)
                        if previous is None:
                            self._new_keys.append(key)
            return results
        except Exception:
            return results
        finally:
            self._imported = None

    async def async_step_verify(self, user_input: Data | None = None) -> ConfigFlowResult:
        if not self._task.done():
            self._progress = True
            return self.async_show_progress(step_id="verify", progress_action="verify", progress_task=self._task)
        if self._progress:
            return self.async_show_progress_done(next_step_id="finish")
        return await self.async_step_finish()

    async def async_step_finish(self, user_input: Data | None = None) -> ConfigFlowResult:
        if not await self._access():
            return self.async_abort(reason="local_admin_required")
        keys = self._task.result()
        if not keys:
            return self.async_abort(reason="verification_failed")
        current = self._current()
        all_keys = list(dict.fromkeys((current.data.get("device_keys", []) if current else []) + keys))
        records = self._vault.records()
        pins = {k: public_targets([records[k]["target"]])[0] for k in all_keys if "ha_key" in records[k]["target"]}
        if current and any(pins.get(k) != value for k, value in current.data.get("pins", {}).items()):
            return self.async_abort(reason="verification_failed")
        admin_pins = {k: records[k]["source_pin"] for k in all_keys if records[k].get("source_pin")}
        if current and any(admin_pins.get(k) != value for k, value in current.data.get("admin_pins", {}).items()):
            return self.async_abort(reason="verification_failed")
        self._committed = True
        if current:
            runtime = getattr(current, "runtime_data", None)
            failed = self.context.get("source") == "reauth" and any(
                records[key]["phase"] in ("needs_credentials", "blocked")
                or records[key].get("error") == "verification_failed"
                for key in keys
            )
            if runtime and not runtime.stopped:
                await runtime.accept_inventory({"device_keys": all_keys, "pins": pins, "admin_pins": admin_pins})
                return self.async_abort(
                    reason="verification_failed"
                    if failed
                    else "reauth_successful"
                    if self.context.get("source") == "reauth"
                    else "reconfigure_successful"
                )
            if failed:
                return self.async_abort(reason="verification_failed")
            return self.async_update_reload_and_abort(
                current, data={"device_keys": all_keys, "pins": pins, "admin_pins": admin_pins}
            )
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="HomePod Sensor Link", data={"device_keys": all_keys, "pins": pins, "admin_pins": admin_pins}
        )

    async def async_step_manage(self, user_input: Data | None = None) -> ConfigFlowResult:
        if not await self._access():
            return self.async_abort(reason="local_admin_required")
        current = self._current()
        runtime = getattr(current, "runtime_data", None)
        if not runtime or runtime.stopped:
            return self.async_abort(reason="runtime_unavailable")
        errors = {}
        if user_input:
            key, action = user_input["device"], user_input["action"]
            if key not in cast(ConfigEntry, current).data["device_keys"]:
                return self.async_abort(reason="invalid_selection")
            try:
                record = runtime.records[key]
                needs_import = action == "resume" and record["phase"] == "needs_credentials"
                if action == "resume" and record["phase"] == "blocked":
                    errors["base"] = "safety_blocked"
                elif action == "remove":
                    await runtime.remove_device(key)
                elif needs_import:
                    pass
                elif action == "resume":
                    await runtime.resume_device(key)
                elif action == "cancel":
                    await runtime.cancel_device(key)
                elif action == "recovery_off":
                    await runtime.set_recovery(key, False)
                elif action == "recovery_on":
                    needs_import = not await runtime.set_recovery(key, True)
                else:
                    raise ValueError("invalid_device_action")
                if needs_import:
                    identifier = record["target"]["accessory_id"]
                    self._selected = [identifier]
                    self._recovery_ids = (
                        [identifier] if action == "recovery_on" or record["target"].get("recovery", True) else []
                    )
                    self._discovered = await discover_devices(self.hass)
                    self._discovered.setdefault(identifier, {"name": record["name"]})
                    return await self.async_step_credentials()
                if not errors:
                    return self.async_abort(reason="device_updated")
            except Exception:
                errors["base"] = "device_action_failed"
        return self.async_show_form(
            step_id="manage",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("device"): choices({k: r["name"] for k, r in runtime.records.items()}),
                    vol.Required("action"): choices(
                        {
                            "resume": "Resume setup",
                            "cancel": "Cancel setup",
                            "recovery_on": "Enable recovery",
                            "recovery_off": "Disable recovery",
                            "remove": "Remove device and local data",
                        }
                    ),
                }
            ),
        )

    def async_remove(self) -> None:
        imports = self.hass.data.get(KEY)
        if imports:
            imports.pending.pop(self.flow_id, None)
        self._imported = None
        if not self._committed and hasattr(self, "_vault"):
            self.hass.async_create_background_task(
                self._vault.pause_flow(self.flow_id), "Pause cancelled HomePod setup"
            )
