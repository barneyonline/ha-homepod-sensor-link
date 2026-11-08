#!/bin/sh
# Build only. No network requests, signing, keychain reads or security changes.
set -eu
umask 077
if [ "$#" -ne 1 ] || [ "$(uname -s)" != Darwin ]; then
    printf '%s\n' 'Usage (macOS): sh scripts/build_macos_homekit.sh /absolute/new/build-folder' >&2
    exit 1
fi
case "$1" in
    /*) ;;
    *) printf '%s\n' 'Choose an absolute path to a new build folder.' >&2; exit 1 ;;
esac
source_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/macos_homekit" && pwd -P)"
destination="$1"
mkdir -m 700 "$destination"
cp "$source_dir/HomeKitExport.swift" "$source_dir/HomeKitExport.entitlements" "$destination/"
/usr/bin/xcrun swiftc -parse-as-library -module-cache-path "$destination/module-cache" \
    "$destination/HomeKitExport.swift" -o "$destination/homekit-export"
printf '%s\n' 'Built the local helper without fetching packages or accessing a keychain.'
