"""Require exactly zero missed statements/branches in every integration module."""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "custom_components/homepod_sensor_link/"


def check(report, modules):
    if report.get("meta", {}).get("branch_coverage") is not True:
        raise ValueError("Branch coverage is required")
    files = report.get("files", {})
    if not modules or set(files) != set(modules):
        raise ValueError("Coverage inventory mismatch")
    failures = []
    for path in sorted(modules):
        summary = files[path]["summary"]
        if summary.get("excluded_lines", 0) != 0:
            raise ValueError("Coverage exclusions are not allowed")
        for key in ("missing_lines", "missing_branches"):
            actual = summary[key]
            if type(actual) is not int or actual < 0:
                raise ValueError("Invalid coverage counts")
            if actual:
                failures.append(f"{path}: {key} must be zero, got {actual}")
    if failures:
        raise ValueError("\n".join(failures))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    modules = {str(path.relative_to(ROOT)) for path in (ROOT / PREFIX).rglob("*.py")}
    check(report, modules)
    print("Exact 100% statement and branch coverage passed for every integration module.")


if __name__ == "__main__":
    main()
