#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
docker build --file devtools/Dockerfile.automation --tag homepod-link-automation .
docker run --rm --network none -v "$PWD:/project:ro" -w /project homepod-link-automation python scripts/check_spelling.py
docker run --rm --network none -v "$PWD:/repo:ro" -w /repo \
    rhysd/actionlint@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667
node_image="$(python3 -c 'import json; print(json.load(open("devtools/validation.json"))["node_image"])')"
issue_forms_json="$(docker run --rm --network none -v "$PWD:/project:ro" -w /project homepod-link-automation python -c '
import json
from pathlib import Path

import yaml

templates = Path(".github/ISSUE_TEMPLATE").glob("*.yml")
print(json.dumps({path.stem: yaml.safe_load(path.read_text(encoding="utf-8")) for path in templates}))
')"
docker run --rm --network none -v "$PWD:/project:ro" -w /project \
    -e "HOMEPOD_ISSUE_FORMS_JSON=$issue_forms_json" "$node_image" \
    node --test tests/automation/issue_automation.test.cjs tests/automation/issue_forms.test.cjs
