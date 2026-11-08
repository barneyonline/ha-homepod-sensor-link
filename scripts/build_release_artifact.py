"""Build or verify a development package bound to one clean source commit."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import subprocess
from zipfile import ZipFile

try:
    from . import package
except ImportError:
    import package

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "homepod_sensor_link"


def verify(directory, expected_sha):
    directory = Path(directory)
    evidence = json.loads((directory / "package-evidence.json").read_text())
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha) or evidence["source_sha"] != expected_sha:
        raise ValueError("Package source identity mismatch")
    version = evidence["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[A-Za-z0-9.+-]*)", version):
        raise ValueError("Unsupported package version")
    name = f"{DOMAIN}-{version}.zip"
    if (
        evidence["archive"] != name
        or evidence["scope"] != "development-artifact"
        or evidence["release_ready"] is not False
    ):
        raise ValueError("Package scope or filename mismatch")
    archive = directory / name
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if evidence["sha256"] != digest or (directory / (name + ".sha256")).read_text() != f"{digest}  {name}\n":
        raise ValueError("Package checksum mismatch")
    with ZipFile(archive) as zipped:
        names = zipped.namelist()
        if len(names) != len(set(names)) or sum(info.file_size for info in zipped.infolist()) > 64 * 1024 * 1024:
            raise ValueError("Package inventory budget exceeded")
        for info in zipped.infolist():
            path = Path(info.filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in info.filename
                or not stat.S_ISREG(info.external_attr >> 16)
            ):
                raise ValueError("Unsafe archive entry")
            if info.filename.startswith("custom_components/") and (len(path.parts) < 3 or path.parts[1] != DOMAIN):
                raise ValueError("Unexpected integration in package")
        manifest = json.loads(zipped.read(f"custom_components/{DOMAIN}/manifest.json"))
        if manifest["domain"] != DOMAIN or manifest["version"] != version:
            raise ValueError("Packaged manifest mismatch")
        if not zipped.read("LICENSE").startswith(b"                    GNU GENERAL PUBLIC LICENSE"):
            raise ValueError("Project licence missing")
        if zipped.testzip() is not None:
            raise ValueError("Package integrity failure")
    return evidence


def build(directory, expected_sha, root=ROOT):
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise ValueError("Use an exact source commit")
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=root, text=True)
    if actual != expected_sha or dirty:
        raise ValueError("Artifact source must be the exact clean reviewed commit")
    directory = Path(directory).resolve()
    if directory == root.resolve() or root.resolve() in directory.parents:
        raise ValueError("Keep artifacts outside the source tree")
    manifest = json.loads((root / "custom_components" / DOMAIN / "manifest.json").read_text())
    version = manifest["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[A-Za-z0-9.+-]*)", version):
        raise ValueError("Unsupported package version")
    directory.mkdir(parents=True, exist_ok=True)
    name = f"{DOMAIN}-{version}.zip"
    archive = directory / name
    package.build(archive)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    with (directory / (name + ".sha256")).open("x") as handle:
        handle.write(f"{digest}  {name}\n")
    pending = json.loads((root / "devtools/quality-evidence.json").read_text())["release_gates"]
    evidence = {
        "source_sha": expected_sha,
        "version": version,
        "archive": name,
        "sha256": digest,
        "scope": "development-artifact",
        "release_ready": False,
        "pending_release_gates": sorted(name for name, gate in pending.items() if gate["status"] != "done"),
    }
    with (directory / "package-evidence.json").open("x") as handle:
        json.dump(evidence, handle, indent=2)
        handle.write("\n")
    verify(directory, expected_sha)
    print("Scanned development package, checksum and source provenance verified; no tag or release created.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--output", type=Path)
    mode.add_argument("--verify-dir", type=Path)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    if args.verify_dir:
        verify(args.verify_dir, args.expected_sha)
        print("Downloaded development artifact matches its source commit, checksum and archive contract.")
    else:
        build(args.output, args.expected_sha)
