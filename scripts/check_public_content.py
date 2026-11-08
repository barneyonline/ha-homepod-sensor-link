"""Reject recognisable private material without printing matched values.

Checks files, staged/index content, and optionally all reachable Git blobs.
This is a targeted publication guard, not an exhaustive secret detector.
"""

import argparse
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parent.parent
RULES = {
    "private-key-header": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----"),
    "ssh-public-identity": re.compile(r"ssh-(?:ed25519|rsa)\s+[A-Za-z0-9+/]{30,}"),
    "jwt": re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
    "github-token": re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    "literal-key-material": re.compile(r"(?<![A-Za-z0-9])[0-9a-fA-F]{64}(?![A-Za-z0-9])"),
    "literal-device-mac": re.compile(r"(?<![A-Za-z0-9])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![A-Za-z0-9])"),
    "literal-ha-entry-id": re.compile(r"\b[0-9]{2}[A-HJKMNP-TV-Z0-9]{24}\b"),
    "household-ip": re.compile(r"\b(?:192\.168|10\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
    "personal-local-path": re.compile(r"/Us" + r"ers/[^/\s]+/"),
}
FORBIDDEN_PARTS = {".storage", "exports", "backups", "diagnostics", "outputs", "work", "logs"}
FORBIDDEN_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".zip", ".log"}

# Exact public registry digests verified for workflow tooling on 8 November 2026.
# Only complete references are recognised; unknown digests and bare hashes still fail.
# Historical pins remain necessary for auditing reachable Git history.
PUBLIC_IMAGE_PINS = {
    "python:3.14-slim@sha256:" + "af8a5aa14780025dbfbcfbc81bd8a91e" + "bd87711dd5b0f48ed71090949dd7cb1d",
    "ghcr.io/hacs/action@sha256:" + "a0336b447da4500204122cabea01cac1" + "e0992840d8b927535782ddd5b601f698",
    "ghcr.io/home-assistant/home-assistant@sha256:"
    + "c6c1ca9931036bf3a2c4cc088d8db25dfea"
    + "3b33935a5ecc83c40349462e2f69e",
    "ghcr.io/home-assistant/home-assistant@sha256:"
    + "1b64d38f38d922bf9d59336451fd6453"
    + "e1d614f934456af4ee3d2a51061be3a4",
    "rhysd/actionlint@sha256:" + "b1934ee5f1c509618f2508e6eb47ee0d" + "3520686341fec936f3b79331f9315667",
}


def scan_text(text):
    def without_public_pins(line):
        for reference in PUBLIC_IMAGE_PINS:
            line = line.replace(reference, "[verified-public-image]")
        return line

    return [
        (number, label)
        for number, line in enumerate(text.splitlines(), 1)
        for label, pattern in RULES.items()
        if pattern.search(without_public_pins(line) if label == "literal-key-material" else line)
    ]


def scan_path(name):
    p = Path(name)
    return bool(
        set(p.parts) & FORBIDDEN_PARTS
        or p.suffix.lower() in FORBIDDEN_SUFFIXES
        or p.name == "keys.json"
        or p.name == ".env"
        or p.name.startswith(".env.")
    )


def audit(root=ROOT, *, history=False):
    """Return findings without exposing matched content, for CLI and packaging tests."""
    root = Path(root).resolve()
    findings, checked = [], 0

    def git(*args):
        return subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True).stdout

    def inspect(name, content, label="file"):
        nonlocal checked
        checked += 1
        if scan_path(name):
            findings.append((label, name, 0, "runtime-or-credential-file"))
        for number, rule in scan_text(content.decode("utf-8", errors="replace")):
            findings.append((label, name, number, rule))

    repo = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    is_repo = repo.returncode == 0 and Path(repo.stdout.strip()) == root
    excluded = {".git", "__pycache__", ".ruff_cache", ".venv"}
    for path in root.rglob("*"):
        name = str(path.relative_to(root))
        if set(Path(name).parts) & excluded:
            continue
        if path.is_symlink():
            findings.append(("file", name, 0, "symlink"))
        elif path.is_file():
            inspect(name, path.read_bytes())
    if is_repo:
        for item in git("ls-files", "--stage", "-z").split(b"\0"):
            if not item:
                continue
            metadata, name_bytes = item.split(b"\t", 1)
            mode, oid, stage = metadata.decode().split()
            name = name_bytes.decode("utf-8", errors="replace")
            if stage != "0":
                findings.append(("index", name, 0, "unmerged-index"))
            if mode == "120000":
                findings.append(("index", name, 0, "symlink"))
            if mode == "160000":
                findings.append(("index", name, 0, "unscanned-submodule"))
            else:
                inspect(name, git("cat-file", "blob", oid), "index")
        if history:
            seen = set()
            for commit in git("rev-list", "--all").decode().splitlines():
                for item in git("ls-tree", "-r", "-z", commit).split(b"\0"):
                    if not item:
                        continue
                    metadata, name_bytes = item.split(b"\t", 1)
                    mode, kind, oid = metadata.decode().split()
                    name = name_bytes.decode("utf-8", errors="replace")
                    # The same blob under a forbidden historical name still
                    # needs a path check. rev-list --objects elides such aliases.
                    if (oid, name) in seen:
                        continue
                    seen.add((oid, name))
                    if mode == "120000":
                        findings.append(("history", name, 0, "symlink"))
                    if kind != "blob":
                        findings.append(("history", name, 0, "unscanned-submodule"))
                    else:
                        inspect(name, git("cat-file", "blob", oid), "history")
    return findings, checked, is_repo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--require-git", action="store_true")
    args = parser.parse_args()
    findings, checked, is_repo = audit(ROOT, history=args.history)
    if not is_repo:
        print("Git metadata unavailable: staged content and reachable history were NOT checked.")
    if findings:
        for label, name, number, rule in findings:
            for pattern in RULES.values():
                name = pattern.sub("[redacted]", name)
            name = name.replace("\n", "\\n").replace("\r", "\\r")
            print(f"{label}:{name}:{number}: {rule}")
        raise SystemExit(1)
    if not is_repo and args.require_git:
        raise SystemExit(2)
    print(f"Public-content check passed ({checked} available-content inspections).")


if __name__ == "__main__":
    main()
