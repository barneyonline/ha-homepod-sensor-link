"""Published Wiki navigation and retained artwork; page content lives in the Wiki."""

from pathlib import Path

WIKI_URL = "https://github.com/barneyonline/ha-homepod-sensor-link/wiki"
PAGES = (
    "Home",
    "Getting-Started",
    "Credential-Extraction",
    "Using-the-Integration",
    "Automation-Examples",
    "Troubleshooting-and-Support",
    "Known-Limitations",
    "Development-Environment",
    "Architecture",
    "Security",
)
BANNER_DIR = Path("docs/images/wiki")


def is_wiki_reference(reference):
    """Accept only exact documentation pages in this project's public Wiki."""
    return reference in {f"{WIKI_URL}/{page}" for page in PAGES}


def check_assets(root):
    """Require regular, complete banner pairs without generating any pages."""
    for page in PAGES:
        for theme in ("dark", "light"):
            path = root / BANNER_DIR / f"{page}-{theme}.jpg"
            if any(p.is_symlink() for p in (path, *path.parents) if p == root or root in p.parents):
                raise ValueError("Wiki banner symlink refused")
            if not path.is_file():
                raise ValueError("Missing Wiki banner")
