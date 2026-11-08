"""Build a scanned manual-install archive with the component directory."""

import argparse
import hashlib
from pathlib import Path
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

try:
    from .check_public_content import scan_path, scan_text
except ImportError:
    from check_public_content import scan_path, scan_text

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ("homepod_sensor_link",)
BRAND_FILES = {f"{kind}{scale}.png" for kind in ("icon", "dark_icon", "logo", "dark_logo") for scale in ("", "@2x")}


def build(output):
    output = Path(output).resolve()
    if output == ROOT or ROOT in output.parents:
        raise ValueError("Choose an output outside the source tree")
    files = []
    for component in COMPONENTS:
        directory = ROOT / "custom_components" / component
        if directory.is_symlink():
            raise ValueError("Symlink refused")
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ValueError("Symlink refused")
            brand_image = path.parent == directory / "brand" and path.name in BRAND_FILES
            if path.is_file() and (path.suffix in (".py", ".json", ".yaml", ".js") or brand_image):
                files.append(path)
    files.extend(
        ROOT / name
        for name in (
            "README.md",
            "SECURITY.md",
            "CONTRIBUTING.md",
            "CHANGELOG.md",
            "LICENSE",
            "hacs.json",
            "quality_scale.yaml",
            "blueprints/automation/homepod_sensor_link/heartbeat-watchdog.yaml",
        )
    )
    files.extend(sorted((ROOT / "docs/images").glob("*.svg")))
    for filename in ("readme-banner-light.png", "readme-banner-dark.png"):
        files.extend((ROOT / "docs/images").glob(filename))
    files.extend(sorted((ROOT / "docs/images/wiki").glob("*.jpg")))
    files.extend(sorted((ROOT / "devtools").glob("*.json")))
    contents = {}
    for path in files:
        if any(p.is_symlink() for p in (path, *path.parents) if p == ROOT or ROOT in p.parents):
            raise ValueError("Symlink refused")
        name = str(path.relative_to(ROOT))
        content = path.read_bytes()
        if scan_path(name) or scan_text(content.decode("utf-8", errors="replace")):
            raise ValueError("Public-content check refused package")
        contents[name] = content
    # Write the exact bytes scanned, and only create an archive after preflight.
    with ZipFile(output, "x", compression=ZIP_DEFLATED) as archive:
        for name, content in sorted(contents.items()):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, content)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    output = Path(parser.parse_args().output).resolve()
    checksum = output.with_name(output.name + ".sha256")
    if checksum.exists() or checksum.is_symlink():
        raise ValueError("Checksum output already exists")
    build(output)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    with checksum.open("x") as handle:
        handle.write(f"{digest}  {output.name}\n")
    print("Scanned deterministic archive and SHA-256 sidecar created outside the source tree.")
