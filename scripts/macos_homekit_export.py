"""Private state and selected credential export for the user-run macOS wizard.

Only the CLI's explicit snapshot/check/export operations read system settings.
This helper never changes NVRAM, SIP, pairings or Home Assistant storage.
"""

import argparse
import base64
import getpass
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import stat
import subprocess
import sys
import tempfile

TEMPORARY_ARGUMENT = "amfi_get_out_of_my_way=0x1"
STATE_FILE = "wizard-state.json"


class WizardError(Exception):
    """A controlled error safe to display without credential values."""


def private_directory(directory):
    directory = Path(directory).absolute()
    if directory.resolve() != directory:
        raise WizardError("The export folder and its parents must not be symbolic links.")
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise WizardError("The export folder must be owned by you with permissions 0700.")
    return directory


def private_read(path, limit=131072):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
            or info.st_size > limit
        ):
            raise WizardError("A wizard file has unsafe permissions, type or size.")
        return stream.read(limit + 1)


def private_write(directory, name, content):
    descriptor, temporary = tempfile.mkstemp(prefix=".wizard-", dir=directory)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / name)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def tool_source(directory):
    source = Path(private_read(directory / "tool-source.txt", 4096).decode().strip())
    binary = source / "homekit-export"
    if (
        not source.is_absolute()
        or source.resolve() != source
        or source.name != "HomeKitExport"
        or source.parent.parent != directory
        or not source.parent.name.startswith("build.")
        or source not in binary.resolve().parents
        or not binary.is_file()
        or binary.is_symlink()
        or not os.access(binary, os.X_OK)
    ):
        raise WizardError("Prepared tool path is invalid; restore protections and use the manual guide.")
    try:
        entitlements = plistlib.loads(private_read(source / "HomeKitExport.entitlements"))
    except (ValueError, plistlib.InvalidFileException):
        raise WizardError(
            "Prepared helper entitlements are invalid; restore protections before troubleshooting."
        ) from None
    if entitlements != {"keychain-access-groups": ["com.apple.hap.pairing"]}:
        raise WizardError("Helper entitlements differ from the restricted HomeKit access group.")
    return source


def system_command(*command):
    return subprocess.run(command, capture_output=True, env={**os.environ, "LC_ALL": "C"}, check=False, timeout=10)


def boot_arguments():
    result = system_command("/usr/sbin/nvram", "-x", "boot-args")
    if result.returncode:
        if b"data was not found" in result.stderr:
            return None, b""
        raise WizardError("Cannot read boot arguments; no security changes should be made.")
    try:
        data = plistlib.loads(result.stdout)
        if set(data) != {"boot-args"}:
            raise ValueError
        value = data["boot-args"]
        text = value.decode("utf-8") if isinstance(value, bytes) else value
        if not isinstance(text, str) or any(ord(c) < 32 or ord(c) > 126 for c in text):
            raise ValueError
        return text, result.stdout
    except (ValueError, TypeError, AttributeError, plistlib.InvalidFileException):
        raise WizardError("Unsupported boot-args format; keep the original settings unchanged.") from None


def security_status():
    sip = system_command("/usr/bin/csrutil", "status")
    kernel = system_command("/usr/sbin/sysctl", "-n", "kern.bootargs")
    if sip.returncode or kernel.returncode:
        raise WizardError("Cannot verify SIP and the running kernel's boot arguments.")
    text = sip.stdout.decode("utf-8").strip()
    modes = {
        "System Integrity Protection status: enabled.": "enabled",
        "System Integrity Protection status: disabled.": "disabled",
    }
    if text not in modes:
        raise WizardError("Custom or unrecognised SIP configuration; use the manual guide.")
    return modes[text], kernel.stdout.decode("utf-8").split()


def check_initial():
    arguments, _ = boot_arguments()
    sip, kernel = security_status()
    if sip != "enabled" or "amfi" in (arguments or "").lower() or any("amfi" in arg.lower() for arg in kernel):
        raise WizardError("Start with SIP enabled and without AMFI overrides; use the manual guide otherwise.")


def load_state(directory):
    try:
        state = json.loads(private_read(directory / STATE_FILE))
        if set(state) != {"phase", "original", "plist"} or state["phase"] not in {"extract", "restore", "complete"}:
            raise ValueError
        original = state["original"]
        if original is not None and (not isinstance(original, str) or any(ord(c) < 32 for c in original)):
            raise ValueError
        raw = base64.b64decode(state["plist"], validate=True)
        if original is None:
            if raw:
                raise ValueError
        else:
            parsed = plistlib.loads(raw)
            value = parsed["boot-args"]
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            if set(parsed) != {"boot-args"} or value != original:
                raise ValueError
        return state
    except (ValueError, TypeError, KeyError, plistlib.InvalidFileException):
        raise WizardError("Wizard state is invalid; retain it and use the manual restoration guide.") from None


def save_state(directory, state):
    private_write(directory, STATE_FILE, (json.dumps(state) + "\n").encode())


def snapshot(directory):
    if (directory / STATE_FILE).exists() or (directory / STATE_FILE).is_symlink():
        raise WizardError("An original-settings snapshot already exists; it will not be overwritten.")
    check_initial()
    original, raw = boot_arguments()
    state = {"phase": "extract", "original": original, "plist": base64.b64encode(raw).decode("ascii")}
    save_state(directory, state)


def check_extraction(state):
    original = state["original"]
    expected = (original + " " if original is not None else "") + TEMPORARY_ARGUMENT
    current, _ = boot_arguments()
    sip, kernel = security_status()
    if state["phase"] != "extract" or sip != "disabled" or current != expected or TEMPORARY_ARGUMENT not in kernel:
        raise WizardError("Extraction is locked: verify the manual security steps and restart before resuming.")


def check_restored(state):
    current, raw = boot_arguments()
    sip, kernel = security_status()
    original_raw = base64.b64decode(state["plist"])
    same_type_and_value = (
        not raw
        and not original_raw
        or (bool(raw) and bool(original_raw) and plistlib.loads(raw) == plistlib.loads(original_raw))
    )
    if (
        current != state["original"]
        or not same_type_and_value
        or sip != "enabled"
        or any("amfi" in argument.lower() for argument in kernel)
    ):
        raise WizardError("Import export is locked until original boot arguments and SIP are restored, then restarted.")


def restoration_instructions(directory, state):
    print("In normal macOS Terminal, restore only the original boot-args setting:")
    if state["original"] is None:
        print("  sudo /usr/sbin/nvram -d boot-args")
    else:
        backup = "boot-args-before.plist"
        private_write(directory, backup, base64.b64decode(state["plist"]))
        print("  sudo /usr/sbin/nvram -x -f " + shlex.quote(str(directory / backup)))
    print("Then shut down, hold the power button for Options → Continue, and open Utilities → Terminal in Recovery:")
    print("  csrutil enable")
    print("  reboot")
    print("Rerun the wizard in normal macOS. Keep the private export folder until restoration is verified.")


def identifier(value):
    if not value or len(value) > 256 or any(ord(c) < 32 for c in value):
        raise WizardError("Identifier missing or invalid; copy the exact Account field without changing its case.")
    return value


def hexadecimal(value, length):
    value = value.strip().strip('"“”').removeprefix("0x")
    if not re.fullmatch(r"[0-9a-fA-F]{" + str(length) + r"}", value):
        raise WizardError("Key format invalid; use hexadecimal key values, not binary placeholders.")
    return value.lower()


def collect_credentials(secret=getpass.getpass, prompt=input):
    controllers, accessories = [], []
    for records, label, maximum in ((controllers, "controller identities", 3), (accessories, "selected HomePods", 64)):
        try:
            count = int(prompt("Number of " + label + " to include: "))
        except ValueError:
            raise WizardError("Enter a whole-number record count.") from None
        if not 1 <= count <= maximum:
            raise WizardError("Record count is outside the supported range.")
        for number in range(1, count + 1):
            account = identifier(secret("Record " + str(number) + " — exact Account (hidden): "))
            if records is controllers:
                value = secret("Identity Key (public+private), or just its 64-character private seed (hidden): ")
                value = value.strip().strip('"“”').removeprefix("0x").replace("+", "")
                if len(value) == 128:
                    combined = hexadecimal(value, 128)
                    record = {"iOSPairingId": account, "iOSDeviceLTPK": combined[:64], "iOSDeviceLTSK": combined[64:]}
                else:
                    record = {"iOSPairingId": account, "iOSDeviceLTSK": hexadecimal(value, 64)}
            else:
                record = {
                    "AccessoryPairingID": account,
                    "AccessoryLTPK": hexadecimal(secret("Accessory public Key (hidden): "), 64),
                }
            records.append(record)
    return {"controllers": controllers, "accessories": accessories}


def export_credentials(directory, state):
    check_restored(state)
    print("Open homekit-keys.txt in a local text editor. Copy only the records for your selected HomePods.")
    print("Inputs are hidden. HA will verify key pairs, accessory pins and administrator permission on import.")
    bundle = collect_credentials()
    content = (json.dumps(bundle, indent=2) + "\n").encode()
    if len(content) > 131072:
        raise WizardError("Selected export exceeds HA's import limit.")
    descriptor, name = tempfile.mkstemp(prefix="homepod-import-", suffix=".json", dir=directory)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    save_state(directory, {**state, "phase": "complete"})
    print("Selected credential bundle saved privately to: " + name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "status",
            "initial",
            "source",
            "snapshot",
            "enable",
            "check-extract",
            "extracted",
            "restore",
            "check-restored",
            "export",
        ),
    )
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    try:
        directory = private_directory(args.directory)
        if args.action == "status":
            print(load_state(directory)["phase"] if (directory / STATE_FILE).exists() else "new")
        elif args.action == "initial":
            check_initial()
        elif args.action == "snapshot":
            snapshot(directory)
        elif args.action == "source":
            print(tool_source(directory))
        else:
            state = load_state(directory)
            if args.action == "enable":
                original, _ = boot_arguments()
                if original != state["original"]:
                    raise WizardError("Boot arguments changed since the snapshot; use restoration instructions.")
                setting = "boot-args=" if original is None else "boot-args+= "
                print("  sudo /usr/sbin/nvram " + shlex.quote(setting + TEMPORARY_ARGUMENT))
            elif args.action == "check-extract":
                check_extraction(state)
            elif args.action == "extracted":
                check_extraction(state)
                save_state(directory, {**state, "phase": "restore"})
            elif args.action == "restore":
                restoration_instructions(directory, state)
            elif args.action == "check-restored":
                check_restored(state)
            elif args.action == "export":
                if not sys.stdin.isatty() or not sys.stdout.isatty():
                    raise WizardError("Enter credentials interactively in Terminal; do not pipe or record input.")
                export_credentials(directory, state)
    except WizardError as error:
        print(str(error))
        return 1
    except (OSError, EOFError, KeyboardInterrupt, subprocess.SubprocessError):
        print("Stopped. No settings were changed by this helper. Keep private files and follow the restoration guide.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
