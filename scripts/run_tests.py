"""Run the pinned offline HA suite and export synthetic coverage evidence."""

import argparse
import json
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]

RUNNER = r"""
import json
from pathlib import Path
import sys
import unittest
from contextlib import redirect_stdout
import asyncio
from importlib.metadata import version
import coverage
expected = json.loads(sys.argv[1])
assert version("homeassistant") == sys.argv[2]
assert version("aiohomekit") == expected["aiohomekit"]
assert coverage.__version__ == expected["coverage"][sys.argv[2]]
cov = coverage.Coverage(data_file="/tmp/homepod-link.coverage")
background_errors = []
def test_loop():
    loop = asyncio.new_event_loop()
    loop.set_exception_handler(lambda _loop, _context: background_errors.append(True))
    return loop
unittest.IsolatedAsyncioTestCase.loop_factory = staticmethod(test_loop)
cov.start()
with redirect_stdout(sys.stderr):
    suite = unittest.defaultTestLoader.discover("tests")
    result = unittest.TextTestRunner(verbosity=1).run(suite)
cov.stop()
cov.json_report(outfile="/tmp/coverage.json")
cov.xml_report(outfile="/tmp/coverage.xml")
print(json.dumps({"coverage": json.loads(Path("/tmp/coverage.json").read_text()),
                  "xml": Path("/tmp/coverage.xml").read_text(),
                  "tests": result.testsRun, "success": result.wasSuccessful() and not background_errors,
                  "background_errors": len(background_errors)}))
"""


def run(version, output):
    config = json.loads((ROOT / "devtools/validation.json").read_text())
    if version not in config["home_assistant"]:
        raise ValueError("Version is outside the deliberate compatibility matrix")
    output = Path(output).resolve()
    if output == ROOT or ROOT in output.parents:
        raise ValueError("Keep generated reports outside the source tree")
    # Reports return on stdout, so remote/VM Docker daemons need only the source
    # mount. No credentials, writable source mount or host temporary bind mount.
    process = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "python",
            "-e",
            "PYTHONDONTWRITEBYTECODE=1",
            "-v",
            f"{ROOT}:/project:ro",
            "-w",
            "/project",
            f"ghcr.io/home-assistant/home-assistant:{version}",
            "-c",
            RUNNER,
            json.dumps(config),
            version,
        ],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    result = json.loads(process.stdout)
    output.mkdir(parents=True, exist_ok=True)
    (output / "coverage.json").write_text(json.dumps(result["coverage"], indent=2) + "\n")
    (output / "coverage.xml").write_text(result["xml"])
    totals = result["coverage"]["totals"]
    print(f"HA {version}: {result['tests']} tests; statement/branch coverage {totals['percent_covered']:.2f}%")
    if result.get("background_errors", 0):
        print(f"Unhandled background task failures: {result['background_errors']}")
    if not result["success"] or result.get("background_errors", 0):
        raise SystemExit(1)
    return result["coverage"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    run(args.version, args.output or tempfile.mkdtemp(prefix="homepod-link-tests-"))
