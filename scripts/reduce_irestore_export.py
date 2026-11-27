"""Create a local HomeKit-only copy of an irestore v0.4.0 keychain export."""

import argparse
import base64
import json
from pathlib import Path
import re
import sys
from tempfile import NamedTemporaryFile

HAP_GROUP = "com.apple.hap.pairing"
GROUP_VALUES = {"string": HAP_GROUP, "[]uint8": base64.b64encode(HAP_GROUP.encode("ascii")).decode("ascii")}
BTLE_LABEL = b"BTLE HomeKit Accessory Identifier"
ACCESSORY_LABEL = b"Paired HomeKit Accessory"
TEXT_CONTROLLER = re.compile(rb"[0-9a-fA-F]{64}\+[0-9a-fA-F]{64}")
TEXT_ACCESSORY = re.compile(rb"[0-9a-fA-F]{64}")
MAX_IMPORT_BYTES = 6 * 1024 * 1024  # Native irestore JSON limit in the Home Assistant form.


def unique_fields(pairs):
    """Prevent duplicate JSON fields from changing meaning when re-serialized."""
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError
    return result


def pairing_fields(record):
    """Return declared field types for a live pairing record."""
    group = record.get("agrp")
    if group not in GROUP_VALUES.values():
        return None
    order, types = record.get("_fieldOrder"), record.get("_fieldTypes")
    if not isinstance(order, str) or not isinstance(types, str):
        raise ValueError
    names, kinds = order.split(","), types.split(",")
    if (
        len(names) != len(kinds)
        or len(set(names)) != len(names)
        or any(not name or name.startswith("_") for name in names)
        or any(not kind for kind in kinds)
        or set(names) != {name for name in record if not name.startswith("_")}
    ):
        raise ValueError
    fields = dict(zip(names, kinds))
    group_kind = fields["agrp"]
    if group_kind == "[]uint8":
        group = base64.b64decode(group, validate=True).decode("ascii")
    elif group_kind != "string":
        raise ValueError
    if group != HAP_GROUP:
        return None
    if "tomb" in fields:
        tomb = record["tomb"]
        if fields["tomb"] != "int64" or type(tomb) is not int or tomb not in (0, 1):
            raise ValueError
        if tomb == 1:
            return None
    return fields


def declared_bytes(record, fields, name):
    value = record.get(name)
    if not isinstance(value, str):
        return b""
    kind = fields.get(name)
    try:
        if kind == "string":
            return value.encode("utf-8")
        if kind == "[]uint8":
            return base64.b64decode(value, validate=True)
    except ValueError:
        pass
    return b""


def has_controller_value(record, fields, label_names):
    """Recognise controller candidates before Home Assistant validates their keys."""
    value = declared_bytes(record, fields, "v_Data")
    if TEXT_CONTROLLER.fullmatch(value):
        return True
    return len(value) == 64 and not (ACCESSORY_LABEL in label_names and TEXT_ACCESSORY.fullmatch(value))


def main():
    parser = argparse.ArgumentParser(
        description="Make a smaller irestore export in the same private local folder as the original. "
        "Paste the new JSON file into HomePod Sensor Link."
    )
    parser.add_argument("source", type=Path, help="path to the keychain.json file exported by irestore v0.4.0")
    source = parser.parse_args().source

    try:
        with source.open("rb") as file:
            data = json.load(file, object_pairs_hook=unique_fields)
        records = data["General"]
        if records is None:
            records = []
        if not isinstance(records, list) or any(not isinstance(record, dict) for record in records):
            raise ValueError
        selected = []
        has_controller = False
        for record in records:
            fields = pairing_fields(record)
            if fields is None:
                continue
            labels = {declared_bytes(record, fields, name) for name in ("svce", "labl", "desc")}
            label_names = {label.partition(b":")[0] for label in labels}
            if BTLE_LABEL in label_names:
                continue
            selected.append(record)
            has_controller = has_controller or has_controller_value(record, fields, label_names)
        content = json.dumps({"General": selected}, separators=(",", ":")).encode("utf-8")
    except (OSError, KeyError, TypeError, ValueError, RecursionError):
        print(
            "The tool could not read the irestore export. Check the file path or export the backup again.",
            file=sys.stderr,
        )
        return 1

    if not selected:
        print("No HomeKit pairing records were found in the irestore export.", file=sys.stderr)
        return 1
    if not has_controller:
        print("The export contains no HomeKit controller identity. Try a fresh encrypted backup.", file=sys.stderr)
        return 1
    if len(content) > MAX_IMPORT_BYTES:
        print("The HomeKit records still exceed 6 MiB. Select the records for the intended HomePods.", file=sys.stderr)
        return 1

    try:
        output = NamedTemporaryFile(dir=source.parent, prefix="homepod-irestore-", suffix=".json", delete=False)
        try:
            with output:
                output.write(content)
        except OSError:
            Path(output.name).unlink(missing_ok=True)
            raise
    except OSError:
        print("The tool could not save the reduced export. Check write access to the private folder.", file=sys.stderr)
        return 1

    print(f"Paste the contents of {output.name} into HomePod Sensor Link.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
