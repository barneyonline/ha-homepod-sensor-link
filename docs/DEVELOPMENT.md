# Development environment

Python 3.14 runs in the official HA images. Docker is required; no local virtualenv or live installation is needed. `devtools/validation.json` holds the deliberate runtime/tooling matrix: HA 2026.10.0 and 2026.11.0 with aiohomekit 4.0.1, pinned coverage/Ruff/Node/hassfest, and strict mypy 2.3.1 on HA 2026.11.0.

```sh
sh scripts/validate.sh
```

This runs working-content checks, repository/wiki contracts, host tooling tests, both HA suites with exact coverage gates, strict typing, the 54-rule quality evidence audit, upstream hassfest, Ruff lint/format and Node behavior/coverage tests. Containers run without networking and mount source read-only. Reports go outside the source tree. Public tooling-image/package downloads during image construction require network access; test execution does not.

## Focused commands

```sh
python3 scripts/build_wiki.py --check
python3 scripts/check_repository.py
python3 scripts/run_tests.py --version 2026.11.0 --output /tmp/homepod-link-tests
python3 scripts/check_coverage.py /tmp/homepod-link-tests/coverage.json
python3 scripts/check_public_content.py --history --require-git
python3 scripts/package.py --output /tmp/homepod-sensor-link-local.zip
```

Strict typing uses the image built by full validation:

```sh
docker run --rm --network none -v "$PWD:/project:ro" -w /project --entrypoint mypy homepod-link-quality:2026.11.0 --cache-dir /tmp/homepod-link-mypy custom_components/homepod_sensor_link
```

The image exposes the actual HA source through `MYPYPATH`. It checks every packaged module with strict options and no blanket missing-import suppression, disabled errors or excluded modules. Dynamic HAP/storage dictionaries are validated at boundaries; lifecycle objects, callbacks, collections, readings, config-entry runtime data and coordinator snapshots are typed. Narrow callable casts describe aiohomekit's incompletely annotated public methods. See [dependencies](DEPENDENCIES.md).

## Exact coverage policy

Every integration Python module must have **zero missing statements, zero missing branches and zero excluded lines** on both HA versions. The checker compares the report against the complete on-disk module inventory and uses integer miss counts rather than rounded percentages. No tolerated-miss baseline remains. New modules enter the same gate automatically. Background task exceptions fail the runner even if unittest reports no failures.

The credential-panel Node suite separately requires **100% lines, branches and functions** for the production JavaScript. It simulates HA authentication, selected files, field input, size limits, session errors, server/network refusal, clearing secrets and removing the nonce. This is not an installed-HA browser/device test.

## Quality and release gates

The [quality guide](QUALITY.md) records all 54 official rule targets. The ordinary audit checks evidence consistency while retaining external todos. These stricter commands intentionally fail until the missing evidence is supplied:

```sh
docker run --rm --network none -v "$PWD:/project:ro" -w /project --entrypoint python homepod-link-quality:2026.11.0 scripts/validate_quality_scale.py --require-complete
docker run --rm --network none -v "$PWD:/project:ro" -w /project --entrypoint python homepod-link-quality:2026.11.0 scripts/validate_quality_scale.py --require-release-ready
```

The Git audit fails when actual metadata is missing. Publication preserves the existing GitHub initial commit but cannot authenticate the missing original development baseline `2d5e069`; do not fabricate that ancestry. Keep real-device/backup evidence separately authorised and sanitised. Local `quality_scale.yaml` is an engineering checklist: HA reports a custom integration as `custom`, not an awarded Platinum tier.

## Documentation and automation

Edit canonical source pages, regenerate `wiki/` with `python3 scripts/build_wiki.py`, then validate. Local links, English/action/entity/issue translations, metadata and generated-page drift are checked.

The published GitHub Wiki is the reader-facing documentation; the source pages and portable `wiki/` snapshots remain versioned here. Prepare a separate export with GitHub Wiki page URLs rather than local `.md` links:

```sh
python3 scripts/build_wiki.py --github --output /tmp/homepod-link-wiki-export
python3 scripts/build_wiki.py --github --output /tmp/homepod-link-wiki-export --check
```

Every documentation page has light/dark artwork in `docs/images/wiki/`, based on the approved README template. The generator adds a theme-aware `<picture>` header with a light fallback and descriptive alternative text. Local snapshots use portable relative image paths; GitHub exports use raw Wiki image URLs and copy all publication JPEGs into `images/`. The complete theme set and exact exported bytes are checked for drift. Artwork prompts and the page catalogue live beside the assets; manual archives include only the publication JPEGs.

This command has no network access and does not publish. To publish an authorised update, clone `https://github.com/barneyonline/ha-homepod-sensor-link.wiki.git`, compare and copy only the generated pages and `images/` assets into that checkout, audit its working files/index/history with the public-content checker, and commit/push its existing default branch. Preserve unrelated Wiki content and existing history. Never delete canonical source pages or grant CI write permissions to perform this manual step.

The workflow keeps GitHub repository permissions at `contents: read` and supports scheduled/manual checks with `local`, `platinum` and `release` readiness modes. Validation remains offline and secret-free. There are no device credentials, release/deployment steps or wiki push. Dependabot proposes weekly Action updates.

After validation succeeds, a separate Codecov job downloads this run's coverage artifact and uploads only `coverage.xml` from each HA version. The `CODECOV_TOKEN` secret is scoped to the pinned upload action and is never passed to validation or pull-request runs. Uploads run only on this repository's trusted `main`; forks skip reporting. The job checks out source without persisted Git credentials and executes no repository-authored shell commands. Uploader integrity verification remains enabled. Codecov configuration uses 100% project/patch targets; the exact local per-module and JavaScript gates remain authoritative for every pull request. The public badge reports Python integration coverage, while JavaScript is gated separately.

Hosted CI and Codecov report acceptance have independently passed. The Wiki is published separately through its own Git repository; releases and HACS/brands acceptance remain distinct outcomes.
