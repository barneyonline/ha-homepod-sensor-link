"""Spell-check maintained text and English UI strings, excluding derived artwork/wiki."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def files(root=ROOT):
    for path in sorted(root.rglob("*")):
        parts = path.relative_to(root).parts
        if any(part.startswith(".") and part != ".github" for part in parts):
            continue
        if parts[0] == "wiki" or parts[:2] == ("docs", "images") or not path.is_file():
            continue
        english = path.name == "strings.json" or (path.parent.name == "translations" and path.name == "en.json")
        if path.suffix in {".py", ".md", ".yaml", ".yml", ".cjs", ".mjs", ".sh"} or english:
            yield str(path)


if __name__ == "__main__":
    subprocess.run(["codespell", "--ignore-words", str(ROOT / ".codespellignore"), *files()], check=True)
