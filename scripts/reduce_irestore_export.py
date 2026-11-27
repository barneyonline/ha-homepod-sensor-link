"""Create a local HomeKit-only copy of an irestore v0.4.0 keychain export."""

import argparse
import base64
import json
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile

HAP_GROUP = "com.apple.hap.pairing"
GROUP_VALUES = {"string": HAP_GROUP, "[]uint8": base64.b64encode(HAP_GROUP.encode("ascii")).decode("ascii")}
BTLE_LABEL = "BTLE HomeKit Accessory Identifier"
BTLE_LABELS = (
    ("string", BTLE_LABEL),
    ("[]uint8", base64.b64encode(BTLE_LABEL.encode("ascii")).decode("ascii")),
)
MAX_IMPORT_BYTES = 6 * 1024 * 1024  # Native irestore JSON limit in the Home Assistant form.


def unique_fields(pairs):
    """Prevent duplicate JSON fields from changing meaning when re-serialized."""
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError
    return result


def is_homekit_record(record):
    """Select live pairing records using irestore's declared field types."""
    group = record.get("agrp")
    if group not in GROUP_VALUES.values():
        return False
    order, types = record.get("_fieldOrder"), record.get("_fieldTypes")
    if not isinstance(order, str) or not isinstance(types, str):
        raise ValueError
    names, kinds = order.split(","), types.split(",")
    if len(names) != len(kinds) or names.count("agrp") != 1:
        raise ValueError
    fields = dict(zip(names, kinds))
    if GROUP_VALUES.get(fields["agrp"]) != group:
        return False
    tomb = record.get("tomb")
    if names.count("tomb") == 1 and fields["tomb"] == "int64" and type(tomb) is int and tomb == 1:
        return False
    return not any((fields.get(name), record.get(name)) in BTLE_LABELS for name in ("svce", "labl", "desc"))


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
        selected = [record for record in records if is_homekit_record(record)]
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
