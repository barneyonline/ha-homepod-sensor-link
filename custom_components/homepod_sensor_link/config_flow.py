"""Discover, import and commission dedicated controllers through one administrator-owned flow."""

from __future__ import annotations
from typing import cast
from .models import Data, ProvisioningBackend
from homeassistant.auth.models import User
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo


import asyncio
import json

import probatio as vol
from homeassistant import config_entries
from homeassistant.helpers import selector

from .backend import HomeKitBackend
from .credentials import DOMAIN, public_targets
from .onboarding import discover_devices, controller_session
from .provisioning import prepare
import uuid
from .engine import SafetyFailure
from .importer import parse_export
from .ui import local_admin
from .vault import get_vault


def choices(options: dict[str, str], multiple: bool = False, *, dropdown: bool = False) -> selector.SelectSelector:
    config = selector.SelectSelectorConfig(
        options=[selector.SelectOptionDict(value=k, label=v) for k, v in options.items()], multiple=multiple
    )
    if dropdown:
        config["mode"] = selector.SelectSelectorMode.DROPDOWN
    return selector.SelectSelector(config)


class HomePodSensorLinkFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 3

    def __init__(self) -> None:
        self._task: asyncio.Task[list[str]]
        self._progress = False
        self._committed = False
        self._force_validate = False
        self._owner: str | None = None
        self._imported: Data | None = None
        self._discovery_hosts: list[str] = []
        self._selected: list[str] = []
        self._selected_keys: dict[str, str] = {}
        self._managed_device: str | None = None
        self._transition_confirmed = False

    async def _access(self) -> User | None:
        user = local_admin(self.hass)
        if not user:
            return None
        if self._owner is not None and self._owner != user.id:
            return None
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
        selected = {
            records[key]["target"]["accessory_id"].lower(): key
            for key in current.data["device_keys"]
            if records[key]["target"].get("recovery", True) or records[key]["phase"] == "needs_credentials"
        }
        if not selected:
            return self.async_abort(reason="reauth_not_required")
        self._selected_keys = selected
        self._selected = list(selected)
        self._recovery_ids = [
            identifier for identifier, key in selected.items() if records[key]["target"].get("recovery", True)
        ]
        self._discovered = {identifier: {"name": records[key]["name"]} for identifier, key in selected.items()}
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
        options = ["devices", "credentials", "manage"]
        if any(record["phase"] == "transition_required" for record in self._vault.records().values()):
            options.insert(0, "transition")
        return self.async_show_menu(step_id="reconfigure", menu_options=options)

    async def async_step_transition(self, user_input: Data | None = None) -> ConfigFlowResult:
        if not await self._access():
            return self.async_abort(reason="local_admin_required")
        current = self._current()
        if current is None:
            return self.async_abort(reason="invalid_selection")
        records = self._vault.records()
        configured = {records[key]["target"]["accessory_id"].lower(): key for key in current.data["device_keys"]}
        pending = {
            key: record
            for key, record in records.items()
            if record["phase"] == "transition_required"
            and configured.get(record["target"]["accessory_id"].lower(), key) == key
        }
        errors = {}
        if user_input is not None:
            keys = user_input.get("devices", [])
            if not keys or len(set(keys)) != len(keys) or not set(keys) <= pending.keys():
                errors["base"] = "select_devices"
            elif user_input.get("confirm") is not True:
                errors["base"] = "confirm_transition"
            else:
                selected = {pending[key]["target"]["accessory_id"].lower(): key for key in keys}
                if len(selected) == len(keys):
                    self._transition_confirmed = True
                    self._selected_keys = selected
                    self._selected = list(selected)
                    self._recovery_ids = []
                    self._discovered = {
                        identifier: {"name": pending[key]["name"]} for identifier, key in selected.items()
                    }
                    return await self.async_step_credentials()
                errors["base"] = "select_devices"
        return self.async_show_form(
            step_id="transition",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("devices", default=[]): choices(
                        {key: record["name"] for key, record in pending.items()}, True
                    ),
                    vol.Required("confirm", default=False): selector.BooleanSelector(),
                }
            ),
        )

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
        # Hide active and transition-only devices even if an interrupted flow
        # left another journal record for the same accessory.
        excluded = set()
        for record in self._vault.records().values():
            identifier = record["target"]["accessory_id"].lower()
            if record["phase"] in ("active", "transition_required"):
                excluded.add(identifier)
                continue
            self._discovered.setdefault(identifier, {"name": record.get("name", "HomePod")})
        for identifier in excluded:
            self._discovered.pop(identifier, None)
        if not self._discovered:
            return await self.async_step_discovery_failed() if errors else await self.async_step_no_devices()
        if user_input is not None:
            selected = user_input.get("devices", [])
            if selected and len(set(selected)) == len(selected) and set(selected) <= self._discovered.keys():
                self._selected_keys.clear()
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

    async def async_step_no_devices(self, user_input: Data | None = None) -> ConfigFlowResult:
        if not await self._access():
            return self.async_abort(reason="local_admin_required")
        return self.async_show_menu(step_id="no_devices", menu_options=["devices", "import_missing"])

    async def async_step_discovery_failed(self, user_input: Data | None = None) -> ConfigFlowResult:
        if not await self._access():
            return self.async_abort(reason="local_admin_required")
        return self.async_show_menu(step_id="discovery_failed", menu_options=["devices", "import_missing"])

    async def async_step_policy(self, user_input: Data | None = None) -> ConfigFlowResult:
        if not await self._access():
            return self.async_abort(reason="local_admin_required")
        if user_input is not None:
            self._recovery_ids = user_input.get("recovery_devices", [])
            if not set(self._recovery_ids) <= set(self._selected):
                return self.async_abort(reason="invalid_selection")
            if self._imported is None:
                return await self.async_step_credentials()
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
        return self.async_show_menu(step_id="credentials", menu_options=["credential_export", "credential_manual"])

    async def async_step_import_missing(self, user_input: Data | None = None) -> ConfigFlowResult:
        if not await self._access():
            return self.async_abort(reason="local_admin_required")
        if hasattr(self, "_task"):
            return await self.async_step_verify()
        # The empty-state action explicitly requests a fresh export. Search
        # again keeps the staged import; verification must keep it immutable.
        self._imported = None
        self._discovery_hosts = []
        self._selected = []
        self._selected_keys.clear()
        return await self.async_step_credential_export()

    async def async_step_credential_export(self, user_input: Data | None = None) -> ConfigFlowResult:
        if not await self._access():
            return self.async_abort(reason="local_admin_required")
        # Access can yield on the vault lock. Reuse any task started meanwhile
        # before parsing or replacing the credentials it is still using.
        if hasattr(self, "_task"):
            return await self.async_step_verify()
        if self._imported is not None:
            return await self._credentials_ready()
        errors = {}
        if user_input is not None:
            try:
                self._imported = parse_export(user_input.pop("credential_export", "").encode("utf-8"))
            except (SafetyFailure, UnicodeError):
                errors["base"] = "invalid_credential_export"
            else:
                return await self._credentials_ready()
        return self.async_show_form(
            step_id="credential_export",
            data_schema=vol.Schema(
                {
                    vol.Required("credential_export"): selector.TextSelector(
                        selector.TextSelectorConfig(multiline=True, autocomplete="off")
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_credential_manual(self, user_input: Data | None = None) -> ConfigFlowResult:
        if not await self._access():
            return self.async_abort(reason="local_admin_required")
        if hasattr(self, "_task"):
            return await self.async_step_verify()
        if self._imported is not None:
            return await self._credentials_ready()
        errors = {}
        if user_input is not None:
            source = {
                "iOSPairingId": user_input.get("controller_id", ""),
                "iOSDeviceLTSK": user_input.get("controller_private_key", ""),
            }
            accessory = user_input.get("accessory_id", "")
            pin = user_input.get("accessory_public_key", "")
            try:
                if bool(accessory) != bool(pin):
                    raise SafetyFailure("invalid_credential_export")
                self._imported = parse_export(
                    json.dumps(
                        {
                            "controllers": [source],
                            "accessories": [{"AccessoryPairingID": accessory, "AccessoryLTPK": pin}]
                            if accessory
                            else [],
                        }
                    ).encode("utf-8")
                )
            except SafetyFailure:
                errors["base"] = "invalid_credential_export"
            else:
                return await self._credentials_ready()
        return self.async_show_form(
            step_id="credential_manual",
            data_schema=vol.Schema(
                {
                    vol.Required("controller_id"): selector.TextSelector(),
                    vol.Required("controller_private_key"): selector.TextSelector(
                        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD, autocomplete="off")
                    ),
                    vol.Optional("accessory_id"): selector.TextSelector(),
                    vol.Optional("accessory_public_key"): selector.TextSelector(),
                }
            ),
            errors=errors,
        )

    async def _credentials_ready(self) -> ConfigFlowResult:
        self._discovery_hosts = cast(Data, self._imported).get("discovery_hosts", [])
        if self._transition_confirmed or (self._selected_keys and self._discovery_hosts and not self._force_validate):
            try:
                found = await discover_devices(self.hass, hosts=self._discovery_hosts)
            except Exception:
                found = {}
            for identifier in self._selected:
                self._discovered[identifier].update(found.get(identifier, {}))
            return await self._begin()
        if not self._force_validate and (self._discovery_hosts or not self._selected):
            return await self.async_step_devices()
        return await self._begin()

    async def _begin(self) -> ConfigFlowResult:
        if not hasattr(self, "_task"):
            self._progress = False
            self._task = self.hass.async_create_background_task(self._prepare(), "HomePod onboarding", eager_start=True)
        return await self.async_step_verify()

    async def _prepare(self) -> list[str]:
        results = []
        try:
            current = self._current()
            preferred_keys = frozenset(current.data["device_keys"]) if current else frozenset()
            async with controller_session(self.hass) as (controller, zc):
                backend = HomeKitBackend(controller, zc)
                operations = ProvisioningBackend(
                    backend.read_ha, backend.restore_missing, backend.inspect_recovery, backend.pairing_present
                )
                for identifier in self._selected:
                    required_key = self._selected_keys.get(identifier)
                    try:
                        async with asyncio.timeout(120):
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
                                transition_confirmed=self._transition_confirmed,
                                preferred_keys=preferred_keys,
                                required_key=required_key,
                            )
                            results.extend(keys)
                    except Exception:
                        # Keep a visible, resumable per-device failure while
                        # preserving every successful device and existing pin.
                        previous = self._vault.record_for_accessory(
                            identifier, preferred_keys=preferred_keys, required_key=required_key
                        )
                        if required_key is not None and previous is None:
                            continue
                        target = {"entry_id": uuid.uuid4().hex, "accessory_id": identifier, "owned": True}
                        target["recovery"] = identifier in self._recovery_ids
                        phase = "needs_credentials"
                        record = previous or {
                            "target": target,
                            "name": self._discovered[identifier]["name"],
                            "phase": phase,
                            "flow_id": self.flow_id,
                        }
                        record["error"] = "verification_failed"
                        key = record["target"]["entry_id"]
                        if await self._vault.put(key, record, require_existing=required_key is not None):
                            results.append(key)
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
        if any(key not in records for key in all_keys):
            return self.async_abort(reason="verification_failed")
        if len({records[key]["target"]["accessory_id"].lower() for key in all_keys}) != len(all_keys):
            return self.async_abort(reason="verification_failed")
        pins = {k: public_targets([records[k]["target"]])[0] for k in all_keys if "ha_key" in records[k]["target"]}
        if current and any(pins.get(k) != value for k, value in current.data.get("pins", {}).items()):
            return self.async_abort(reason="verification_failed")
        admin_pins = {k: records[k]["source_pin"] for k in all_keys if records[k].get("source_pin")}
        if current and any(admin_pins.get(k) != value for k, value in current.data.get("admin_pins", {}).items()):
            return self.async_abort(reason="verification_failed")
        self._committed = True
        if current:
            runtime = getattr(current, "runtime_data", None)
            failed = (
                self._transition_confirmed
                and any(
                    records[key]["phase"] in ("transition_required", "needs_credentials", "blocked")
                    or records[key].get("error") == "verification_failed"
                    for key in keys
                )
            ) or (
                self.context.get("source") == "reauth"
                and any(
                    records[key]["phase"] in ("needs_credentials", "blocked")
                    or records[key].get("error") == "verification_failed"
                    for key in keys
                )
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
        """Choose a device before offering any actions."""
        if not await self._access():
            return self.async_abort(reason="local_admin_required")
        current = self._current()
        runtime = getattr(current, "runtime_data", None)
        if not runtime or runtime.stopped:
            return self.async_abort(reason="runtime_unavailable")
        self._managed_device = None
        keys = cast(ConfigEntry, current).data["device_keys"]
        if user_input is not None:
            key = user_input.get("device")
            if key not in keys or key not in runtime.records:
                return self.async_abort(reason="invalid_selection")
            self._managed_device = key
            return await self.async_step_manage_actions()
        return self.async_show_form(
            step_id="manage",
            last_step=False,
            data_schema=vol.Schema(
                {
                    vol.Required("device"): choices(
                        {k: r["name"] for k, r in runtime.records.items() if k in keys}, dropdown=True
                    ),
                }
            ),
        )

    async def async_step_manage_actions(self, user_input: Data | None = None) -> ConfigFlowResult:
        """Offer actions for the selected device's current recovery policy."""
        if not await self._access():
            return self.async_abort(reason="local_admin_required")
        current = self._current()
        runtime = getattr(current, "runtime_data", None)
        if not runtime or runtime.stopped:
            return self.async_abort(reason="runtime_unavailable")
        key = self._managed_device
        if key not in cast(ConfigEntry, current).data["device_keys"] or key not in runtime.records:
            return self.async_abort(reason="invalid_selection")
        record = runtime.records[key]
        return self.async_show_menu(
            step_id="manage_actions",
            description_placeholders={"name": record["name"]},
            menu_options=["manage_remove", "manage"]
            if record["phase"] == "transition_required"
            else [
                "manage_resume",
                "manage_cancel",
                "manage_recovery_off" if record["target"].get("recovery", True) else "manage_recovery_on",
                "manage_remove",
                "manage",
            ],
        )

    async def async_step_manage_resume(self, user_input: Data | None = None) -> ConfigFlowResult:
        return await self._confirm_manage_action("resume", user_input)

    async def async_step_manage_cancel(self, user_input: Data | None = None) -> ConfigFlowResult:
        return await self._confirm_manage_action("cancel", user_input)

    async def async_step_manage_recovery_on(self, user_input: Data | None = None) -> ConfigFlowResult:
        return await self._confirm_manage_action("recovery_on", user_input)

    async def async_step_manage_recovery_off(self, user_input: Data | None = None) -> ConfigFlowResult:
        return await self._confirm_manage_action("recovery_off", user_input)

    async def async_step_manage_remove(self, user_input: Data | None = None) -> ConfigFlowResult:
        return await self._confirm_manage_action("remove", user_input)

    async def _confirm_manage_action(self, action: str, user_input: Data | None) -> ConfigFlowResult:
        """Review an action before dispatching through the existing runtime safeguards."""
        if not await self._access():
            return self.async_abort(reason="local_admin_required")
        current = self._current()
        runtime = getattr(current, "runtime_data", None)
        if not runtime or runtime.stopped:
            return self.async_abort(reason="runtime_unavailable")
        key = self._managed_device
        if key not in cast(ConfigEntry, current).data["device_keys"] or key not in runtime.records:
            return self.async_abort(reason="invalid_selection")
        record = runtime.records[key]
        if record["phase"] == "transition_required" and action != "remove":
            return self.async_abort(reason="explicit_transition_required")
        errors = {}
        if user_input is not None and action == "remove" and not user_input.get("confirm"):
            errors["base"] = "confirm_removal"
        elif user_input is not None:
            try:
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
                else:
                    needs_import = not await runtime.set_recovery(key, True)
                if needs_import:
                    identifier = record["target"]["accessory_id"].lower()
                    self._selected = [identifier]
                    self._selected_keys = {identifier: cast(str, key)}
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
            step_id=f"manage_{action}",
            errors=errors,
            description_placeholders={"name": record["name"]},
            data_schema=vol.Schema(
                {vol.Required("confirm", default=False): selector.BooleanSelector()} if action == "remove" else {}
            ),
        )

    def async_remove(self) -> None:
        self._imported = None
        if not self._committed and hasattr(self, "_vault"):
            self.hass.async_create_background_task(
                self._vault.pause_flow(self.flow_id), "Pause cancelled HomePod setup"
            )
