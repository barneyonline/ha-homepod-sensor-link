"""No-follow, owner-checked private file reads for the integration journal."""

from __future__ import annotations
from typing import Any
from .models import Data


import json
import os
from pathlib import Path
import stat

MAX_BYTES = 65536


def _pairs(items: list[tuple[str, Any]]) -> Data:
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate_field")
        result[key] = value
    return result


def _directory(location: Path | str) -> int:
    directory = os.open(location, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(directory)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(directory)
        raise ValueError("private_directory_required")
    return directory


def _read_at(directory: int, name: str, *, links: int = 1, limit: int = MAX_BYTES) -> tuple[Data, os.stat_result]:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > limit
            or info.st_nlink != links
        ):
            raise ValueError("private_file_required")
        value = json.loads(stream.read(limit + 1), object_pairs_hook=_pairs)
        return value, info


def read_private(location: Path | str) -> Data:
    """Use directory descriptors so symlink swaps cannot redirect file opens."""
    location = Path(location)
    directory = _directory(location.parent)
    try:
        return _read_at(directory, location.name)[0]
    finally:
        os.close(directory)
