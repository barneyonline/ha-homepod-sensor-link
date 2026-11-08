#!/bin/sh
# Compile a separate synthetic test entry point. Never sign or query a keychain.
set -eu
umask 077
if [ "$(uname -s)" != Darwin ]; then
    printf '%s\n' 'These Apple-framework tests require macOS.' >&2
    exit 1
fi
project_root="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd -P)"
test_directory="$(mktemp -d "${TMPDIR:-/tmp}/homepod-helper-tests.XXXXXX")"
trap 'rm -rf "$test_directory"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
/usr/bin/xcrun swiftc -D HSL_TESTING -parse-as-library -module-cache-path "$test_directory/module-cache" \
    "$project_root/scripts/macos_homekit/HomeKitExport.swift" \
    "$project_root/tests/macos_homekit/ExportTests.swift" -o "$test_directory/tests"
"$test_directory/tests" "$test_directory/homekit-keys.txt"
python3 - "$project_root" "$test_directory" <<'PY'
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, sys.argv[1])
from scripts import macos_homekit_export as wizard

directory = Path(sys.argv[2]).resolve()
with patch.object(wizard, "check_restored"), redirect_stdout(io.StringIO()):
    path = wizard.export_credentials(directory, {"phase": "restore", "original": None, "plist": ""})
bundle = json.loads(wizard.private_read(path))
assert len(bundle["controllers"]) == len(bundle["accessories"]) == 1
assert bundle["controllers"][0]["iOSPairingId"] == "synthetic-controller"
assert bundle["controllers"][0]["iOSDeviceLTSK"] == bytes(range(32)).hex()
assert bundle["accessories"][0]["AccessoryPairingID"] == "synthetic-accessory"
assert bundle["accessories"][0]["AccessoryLTPK"] == bytes(range(32, 64)).hex()
assert not (directory / "homekit-keys.txt").exists()
assert wizard.load_state(directory)["export_file"] == path.name
print("Native synthetic dump to private JSON conversion and cleanup passed without keychain access.")
PY
