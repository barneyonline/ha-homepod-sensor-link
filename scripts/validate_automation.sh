#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
docker build --file devtools/Dockerfile.automation --tag homepod-link-automation .
docker run --rm --network none -v "$PWD:/project:ro" -w /project homepod-link-automation python scripts/check_spelling.py
docker run --rm --network none -v "$PWD:/repo:ro" -w /repo \
    rhysd/actionlint@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667
node_image="$(python3 -c 'import json; print(json.load(open("devtools/validation.json"))["node_image"])')"
docker run --rm --network none -v "$PWD:/project:ro" -w /project "$node_image" \
    node --test tests/automation/issue_automation.test.cjs
