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
"$test_directory/tests"
