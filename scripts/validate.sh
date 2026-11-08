#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
python3 scripts/check_public_content.py --history
python3 scripts/check_repository.py
python3 -m unittest discover -s tests -p test_public_content.py -q
python3 -m unittest discover -s tests -p test_tooling.py -q
python3 -m unittest discover -s tests -p test_automation.py -q
sh scripts/validate_automation.sh
report_dir="${HSL_REPORT_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/homepod-link-validation.XXXXXX")}"
versions="$(python3 -c 'import json; print(" ".join(json.load(open("devtools/validation.json"))["home_assistant"]))')"
ruff_version="$(python3 -c 'import json; print(json.load(open("devtools/validation.json"))["ruff"])')"
node_image="$(python3 -c 'import json; print(json.load(open("devtools/validation.json"))["node_image"])')"
typing_ha="$(python3 -c 'import json; print(json.load(open("devtools/validation.json"))["typing_ha"])')"
hassfest_image="$(python3 -c 'import json; print(json.load(open("devtools/validation.json"))["hassfest_image"])')"
for version in $versions; do
    python3 scripts/run_tests.py --version "$version" --output "$report_dir/$version"
    python3 scripts/check_coverage.py "$report_dir/$version/coverage.json"
done
docker build --file devtools/Dockerfile --build-arg "HA_VERSION=$typing_ha" --tag "homepod-link-quality:$typing_ha" .
docker run --rm --network none -v "$PWD:/project:ro" -w /project --entrypoint mypy \
    "homepod-link-quality:$typing_ha" --cache-dir /tmp/homepod-link-mypy custom_components/homepod_sensor_link
docker run --rm --network none -v "$PWD:/project:ro" -w /project --entrypoint python \
    "homepod-link-quality:$typing_ha" scripts/validate_quality_scale.py
docker run --rm --network none -v "$PWD:/github/workspace:ro" "$hassfest_image"
docker run --rm --network none -v "$PWD:/project:ro" -w /project \
    "ghcr.io/astral-sh/ruff:$ruff_version" check --no-cache \
    custom_components tests scripts
docker run --rm --network none -v "$PWD:/project:ro" -w /project \
    "ghcr.io/astral-sh/ruff:$ruff_version" format --check --no-cache \
    custom_components tests scripts
docker run --rm --network none -v "$PWD:/project:ro" -w /project \
    "$node_image" node --check \
    custom_components/homepod_sensor_link/frontend/import.js
docker run --rm --network none -v "$PWD:/project:ro" -w /project "$node_image" \
    node --test --experimental-test-coverage \
    --test-coverage-include=custom_components/homepod_sensor_link/frontend/import.js \
    --test-coverage-lines=100 --test-coverage-branches=100 --test-coverage-functions=100 \
    tests/frontend/import.test.mjs
printf 'Synthetic coverage reports: %s\n' "$report_dir"
