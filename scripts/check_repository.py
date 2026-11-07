"""Check local docs, translations, versioning and integration inventory."""

import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "homepod_sensor_link"
NAME = "HomePod Sensor Link"
LINK = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")


def check_links(root):
    failures = []
    for source in root.rglob("*.md"):
        if any(part.startswith(".") for part in source.relative_to(root).parts):
            continue
        for target in LINK.findall(source.read_text()):
            parsed = urlsplit(target)
            if parsed.scheme or parsed.netloc or not parsed.path:
                continue
            path = (source.parent / unquote(parsed.path)).resolve()
            if root.resolve() not in path.parents or not path.exists():
                failures.append(f"Broken or escaping local link in {source.relative_to(root)}")
    return failures


def check(root=ROOT):
    component = root / "custom_components" / DOMAIN
    manifest = json.loads((component / "manifest.json").read_text())
    hacs = json.loads((root / "hacs.json").read_text())
    versions = json.loads((root / "devtools/validation.json").read_text())
    strings = json.loads((component / "strings.json").read_text())
    english = json.loads((component / "translations/en.json").read_text())
    failures = check_links(root)
    if manifest["domain"] != DOMAIN or manifest["name"] != NAME or hacs["name"] != NAME:
        failures.append("Integration naming mismatch")
    if hacs["homeassistant"] != versions["home_assistant"][0]:
        failures.append("Minimum HA version differs from the deliberate matrix")
    if re.search(r'^version = "([^"]+)"', (root / "pyproject.toml").read_text(), re.M)[1] != manifest["version"]:
        failures.append("Project/manifest version mismatch")
    if strings != english:
        failures.append("English translation differs from strings.json")
    services = set(re.findall(r"^([a-z_]+):", (component / "services.yaml").read_text(), re.M))
    if services != set(strings.get("services", {})):
        failures.append("Action translation inventory mismatch")
    for service in services:
        for field in ("name", "description"):
            if not strings.get("services", {}).get(service, {}).get(field):
                failures.append("Missing action translation text")
    components = {p.name for p in (root / "custom_components").iterdir() if p.is_dir() and p.name != "__pycache__"}
    if components != {DOMAIN}:
        failures.append("Unexpected packaged integration domain")
    if failures:
        raise ValueError("\n".join(sorted(set(failures))))
    print("Repository contracts passed: links, names, versions, actions and English translations.")


if __name__ == "__main__":
    check()
