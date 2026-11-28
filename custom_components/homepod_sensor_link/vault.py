"""Atomic private onboarding journal. No core config storage is edited."""

from __future__ import annotations
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast
from .models import Data, Record, Snapshot, Target
from homeassistant.core import HomeAssistant


import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import secrets

from .engine import RecoveryPaused, SafetyFailure
from .secure_storage import _directory, _read_at

DOMAIN = "homepod_sensor_link"
DATA_FILE = ".storage/homepod_sensor_link/data.json"
MAX_RECORDS = 64
_ENDPOINT_FIELDS = frozenset(("AccessoryIP", "AccessoryIPs", "AccessoryPort"))


def _without_endpoint(target: Target) -> Target:
    pairing = target.get("pairing")
    if pairing is None:
        return target
    return {**target, "pairing": {key: value for key, value in pairing.items() if key not in _ENDPOINT_FIELDS}}


def _pairing_policy(target: Target) -> Target:
    return _without_endpoint({key: value for key, value in target.items() if key != "expected_pairings"})


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


async def async_drain_future[T](future: asyncio.Future[T]) -> asyncio.CancelledError | None:
    """Wait for the future to finish while deferring caller cancellation."""
    cancellation = None
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError as error:
            cancellation = error
    return cancellation


@dataclass(repr=False)
class _Checkpoint:
    record: Record
    revision: object | None
    committed: bool = False


@dataclass(repr=False)
class _CheckpointBatch:
    requests: dict[str, _Checkpoint]
    task: asyncio.Task[None] = field(init=False)
    sealed: bool = False


class Vault:
    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.lock = asyncio.Lock()
        self._data: Data | None = None
        self._accessory_index: tuple[dict[str, Record], dict[str, list[str]]] | None = None
        self._checkpoint_batch: _CheckpointBatch | None = None

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

    def record_revision(self, key: str) -> object:
        """Capture the value a transition depends on without copying it again."""
        # Writers replace vault-owned records rather than modifying them.
        return self.data["records"][key]

    def activation_revision(self, key: str, verified: Record) -> object | None:
        """Bind activation to the record used for the fresh report."""
        current = cast(dict[str, Record], self.data["records"]).get(key)
        if current is None or current == verified:
            return current
        # Authentication may have learned a new endpoint. All other fields
        # must still match the verified record, including reauth metadata.
        expected = {**verified, "target": _without_endpoint(verified["target"])}
        stored = {**current, "target": _without_endpoint(current["target"])}
        return current if stored == expected else None

    def inspection_revision(self, key: str, original: object, target: Target) -> object:
        """Recognize pairing proof persisted by this inspection."""
        current = cast(dict[str, Record], self.data["records"]).get(key)
        previous = cast(Record, original)
        if current == {**previous, "target": target} and _pairing_policy(previous["target"]) == _pairing_policy(target):
            return current
        return original

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
        cancellation = await async_drain_future(task)
        task.result()
        self.data = data
        if index is not None:
            self._accessory_index = (data["records"], index)
        if cancellation is not None:
            raise asyncio.CancelledError

    async def _commit(self, data: Data, index: dict[str, list[str]] | None = None) -> None:
        # Own the completion future. HA's shutdown cancels tracked executor
        # futures directly, which can leave the underlying thread running.
        task = self.hass.loop.run_in_executor(None, _write, self.hass.config.config_dir, data)
        await self._finish_io(task, data, index)

    @asynccontextmanager
    async def _exclusive_write(self) -> AsyncIterator[None]:
        # A new checkpoint must queue behind an intervening mutation.
        self._checkpoint_batch = None
        async with self.lock:
            yield

    async def put(
        self, key: str, record: Record, *, require_existing: bool = False, revision: object | None = None
    ) -> bool:
        async with self._exclusive_write():
            records = self.data["records"]
            if revision is not None and records.get(key) != revision:
                return False
            if require_existing and key not in records:
                return False
            # Vault-owned entries are replaced under the lock. Shared records
            # and states stay unchanged until the executor finishes serializing.
            updated = {**records, key: deepcopy(record)}
            if len(updated) > MAX_RECORDS:
                raise SafetyFailure("inventory_capacity")
            index = self._index_after_put(key, record)
            await self._commit({**self.data, "records": updated}, index)
            return True

    async def checkpoint_pairing_target(self, target: Target, *, before_write: bool = False) -> Record:
        """Persist pairing proof immediately while preserving current device controls."""
        key = target["entry_id"]
        async with self._exclusive_write():
            records = cast(dict[str, Record], self.data["records"])
            if len(records) > MAX_RECORDS:
                raise SafetyFailure("inventory_capacity")
            current = records.get(key)
            if (
                current is None
                or (
                    before_write
                    and (current.get("cancelled") or current["phase"] not in ("active", "verified", "write_uncertain"))
                )
                or _pairing_policy(current["target"]) != _pairing_policy(target)
            ):
                raise RecoveryPaused()
            updated = deepcopy(current)
            updated["target"] = deepcopy(target)
            await self._commit({**self.data, "records": {**records, key: deepcopy(updated)}})
            return updated

    async def checkpoint(self, key: str, record: Record, *, revision: object | None = None) -> bool:
        """Share a durable commit among ready onboarding transitions."""
        request = _Checkpoint(deepcopy(record), revision)
        batch = self._checkpoint_batch
        if batch is not None and key not in batch.requests:
            batch.requests[key] = request
        else:
            # A later transition of the same device needs its own durable commit.
            batch = self._checkpoint_batch = _CheckpointBatch({key: request})
            # Enter the lock queue now, and collect distinct devices until
            # this writer starts.
            batch.task = asyncio.create_task(self._commit_checkpoints(batch), eager_start=True)
        try:
            await asyncio.shield(batch.task)
        except asyncio.CancelledError:
            if batch.sealed:
                await async_drain_future(batch.task)
                batch.task.result()
            else:
                del batch.requests[key]
            raise
        return request.committed

    async def _commit_checkpoints(self, batch: _CheckpointBatch) -> None:
        async with self.lock:
            await asyncio.sleep(0)  # Let other ready devices join even when the lock was free.
            if self._checkpoint_batch is batch:
                self._checkpoint_batch = None
            if not batch.requests:
                return
            batch.sealed = True
            records = cast(dict[str, Record], self.data["records"])
            if len(records) > MAX_RECORDS:
                raise SafetyFailure("inventory_capacity")
            changes = {}
            for key, request in batch.requests.items():
                current = records.get(key)
                if request.revision is not None and current != request.revision:
                    continue
                if current is None:
                    raise SafetyFailure("configured_inventory_changed")
                if request.record["phase"] == "active" and (current["phase"] != "verified" or current.get("cancelled")):
                    continue
                changes[key] = request.record
            if changes:
                await self._commit({**self.data, "records": {**records, **changes}})
                for key in changes:
                    batch.requests[key].committed = True

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
        async with self._exclusive_write():
            data = deepcopy(self.data)
            data["records"].pop(key, None)
            data["states"].pop(key, None)
            await self._commit(data)

    async def rename(self, key: str, name: str) -> bool:
        """Merge optional metadata without replacing newer credential/policy state."""
        async with self._exclusive_write():
            if key not in self.data["records"]:
                return False
            data = deepcopy(self.data)
            data["records"][key]["name"] = name
            await self._commit(data)
            return True

    async def save_states(self, states: Snapshot) -> None:
        async with self._exclusive_write():
            # Inventory mutations replace their own copies under this lock.
            # Keep unchanged credentials shared; isolate the caller's state.
            data = {**self.data, "states": deepcopy(states)}
            await self._commit(data)

    async def pause_flow(self, flow_id: str) -> None:
        async with self._exclusive_write():
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
        async with self._exclusive_write():

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

            task = self.hass.loop.run_in_executor(None, delete)
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
