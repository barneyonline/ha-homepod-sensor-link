"""Atomic private onboarding journal. No core config storage is edited."""

from __future__ import annotations
from typing import cast
from .models import Data, Record, Snapshot
from homeassistant.core import HomeAssistant


import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import secrets

from .engine import SafetyFailure
from .secure_storage import _directory, _read_at

DOMAIN = "homepod_sensor_link"
DATA_FILE = ".storage/homepod_sensor_link/data.json"


def _write(config_dir: str, data: Data) -> None:
    location = Path(config_dir) / DATA_FILE
    if location.parent.parent.is_symlink():
        raise SafetyFailure("unsafe_storage")
    location.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = _directory(location.parent)
    temporary = ".pending-" + secrets.token_hex(12)
    try:
        try:
            _read_at(directory, location.name, limit=1048576)
        except FileNotFoundError:
            pass
        encoded = json.dumps(data, allow_nan=False)
        if len(encoded.encode()) > 1048576:
            raise SafetyFailure("inventory_capacity")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, location.name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def _load(config_dir: str) -> Data:
    location = Path(config_dir) / DATA_FILE
    if location.parent.parent.is_symlink():
        raise SafetyFailure("unsafe_storage")
    try:
        directory = _directory(location.parent)
    except FileNotFoundError:
        return {"version": 2, "records": {}, "states": {}}
    try:
        try:
            data, _ = _read_at(directory, location.name, limit=1048576)
        except FileNotFoundError:
            return {"version": 2, "records": {}, "states": {}}
        if (
            data.get("version") != 2
            or not isinstance(data.get("records"), dict)
            or not isinstance(data.get("states"), dict)
        ):
            raise SafetyFailure("invalid_private_inventory")
        return data
    finally:
        os.close(directory)


class Vault:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.lock = asyncio.Lock()
        self._data: Data | None = None
        self._accessory_index: tuple[dict[str, Record], dict[str, list[str]]] | None = None

    @property
    def data(self) -> Data:
        if self._data is None:
            raise SafetyFailure("inventory_not_loaded")
        return self._data

    @data.setter
    def data(self, value: Data) -> None:
        if self._accessory_index is not None and self._accessory_index[0] is not value["records"]:
            self._accessory_index = None
        self._data = value

    async def load(self) -> Vault:
        async with self.lock:
            if self._data is None:
                self.data = await self.hass.async_add_executor_job(_load, self.hass.config.config_dir)
        return self

    def records(self) -> dict[str, Record]:
        return deepcopy(self.data["records"])

    def record(self, key: str) -> Record:
        """Return an isolated device without copying unrelated credentials."""
        return deepcopy(self.data["records"][key])

    def record_for_accessory(
        self, accessory_id: str, *, preferred_keys: frozenset[str] = frozenset(), required_key: str | None = None
    ) -> Record | None:
        """Copy a configured match if available, otherwise copy the last matching record."""
        identifier = accessory_id.lower()
        records = cast(dict[str, Record], self.data["records"])
        if required_key is not None:
            record = records.get(required_key)
            if record is not None and record["target"]["accessory_id"].lower() == identifier:
                return deepcopy(record)
            return None
        if self._accessory_index is None or self._accessory_index[0] is not records:
            index: dict[str, list[str]] = {}
            for key, record in records.items():
                index.setdefault(record["target"]["accessory_id"].lower(), []).append(key)
            self._accessory_index = (records, index)
        keys = self._accessory_index[1].get(identifier)
        if keys is None:
            return None
        key = next((key for key in reversed(keys) if key in preferred_keys), keys[-1])
        return deepcopy(records[key])

    async def _finish_io(
        self, task: asyncio.Future[None], data: Data, index: dict[str, list[str]] | None = None
    ) -> None:
        # Repeated cancellations must not release the vault lock while an
        # executor worker can still change the file.
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        self.data = data
        if index is not None:
            self._accessory_index = (data["records"], index)
        if cancelled:
            raise asyncio.CancelledError

    async def _commit(self, data: Data, index: dict[str, list[str]] | None = None) -> None:
        task = self.hass.async_add_executor_job(_write, self.hass.config.config_dir, data)
        await self._finish_io(task, data, index)

    async def put(self, key: str, record: Record, *, require_existing: bool = False) -> bool:
        async with self.lock:
            if require_existing and key not in self.data["records"]:
                return False
            data = deepcopy(self.data)
            data["records"][key] = deepcopy(record)
            if len(data["records"]) > 64:
                raise SafetyFailure("inventory_capacity")
            index = self._index_after_put(key, record)
            await self._commit(data, index)
            return True

    def _index_after_put(self, key: str, record: Record) -> dict[str, list[str]] | None:
        records = self.data["records"]
        if self._accessory_index is None or self._accessory_index[0] is not records:
            return None
        index = self._accessory_index[1]
        identifier = record["target"]["accessory_id"].lower()
        if key in records:
            # A changed ID may need a different position in its new bucket.
            return index if key in index.get(identifier, ()) else None
        return {**index, identifier: [*index.get(identifier, ()), key]}

    async def remove(self, key: str) -> None:
        async with self.lock:
            data = deepcopy(self.data)
            data["records"].pop(key, None)
            data["states"].pop(key, None)
            await self._commit(data)

    async def rename(self, key: str, name: str) -> bool:
        """Merge optional metadata without replacing newer credential/policy state."""
        async with self.lock:
            if key not in self.data["records"]:
                return False
            data = deepcopy(self.data)
            data["records"][key]["name"] = name
            await self._commit(data)
            return True

    async def save_states(self, states: Snapshot) -> None:
        async with self.lock:
            # Inventory mutations replace their own copies under this lock.
            # Keep unchanged credentials shared; isolate the caller's state.
            data = {**self.data, "states": deepcopy(states)}
            await self._commit(data)

    async def pause_flow(self, flow_id: str) -> None:
        async with self.lock:
            data = deepcopy(self.data)
            changed = False
            for record in data["records"].values():
                if record.get("flow_id") == flow_id and record["phase"] != "active":
                    if not record.get("cancelled") or "authorised_boot" in record:
                        changed = True
                    record["cancelled"] = True
                    record.pop("authorised_boot", None)
            if changed:
                await self._commit(data)

    async def purge(self) -> None:
        async with self.lock:

            def delete() -> None:
                location = Path(self.hass.config.config_dir) / DATA_FILE
                if location.parent.parent.is_symlink() or location.parent.is_symlink():
                    raise SafetyFailure("unsafe_storage")
                try:
                    directory = os.open(location.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                except FileNotFoundError:
                    return
                try:
                    if os.fstat(directory).st_uid != os.getuid():
                        raise SafetyFailure("unsafe_storage")
                    # Delete only files owned by this integration; never follow
                    # a symlink or traverse into native HA storage.
                    for name in os.listdir(directory):
                        if name == "data.json" or name == "keys.json" or name.startswith((".pending-", ".import-")):
                            import stat

                            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                            if (
                                not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode))
                                or info.st_uid != os.getuid()
                            ):
                                raise SafetyFailure("unsafe_storage")
                            os.unlink(name, dir_fd=directory)
                    os.fsync(directory)
                finally:
                    os.close(directory)
                try:
                    location.parent.rmdir()
                except OSError:
                    pass

            task = self.hass.async_add_executor_job(delete)
            await self._finish_io(task, {"version": 2, "records": {}, "states": {}})


async def get_vault(hass: HomeAssistant) -> Vault:
    key = DOMAIN + ".vault"
    if key not in hass.data:
        hass.data[key] = Vault(hass)
    return await cast(Vault, hass.data[key]).load()


class VaultStates:
    """Recovery-state adapter: keys, journal and safety counters commit together."""

    def __init__(self, vault: Vault) -> None:
        self.vault = vault

    async def async_load(self) -> Snapshot:
        return deepcopy(self.vault.data["states"])

    async def async_save(self, states: Snapshot) -> None:
        await self.vault.save_states(states)
