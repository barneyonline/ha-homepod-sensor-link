# Development environment

Python 3.14 runs in the official HA images. Docker is required; no local virtualenv or live installation is needed. `devtools/validation.json` holds the deliberate runtime/tooling matrix: HA 2026.10.0 and 2026.11.0 with aiohomekit 4.0.1, pinned coverage/Ruff/Node/hassfest, and strict mypy 2.3.1 on HA 2026.11.0.

```sh
sh scripts/validate.sh
```

This runs working-content checks, repository/wiki contracts, host tooling/artifact tests, spelling and workflow syntax checks, both HA suites with exact coverage gates, strict typing, the 54-rule quality evidence audit, pinned hassfest, Ruff and Node behavior tests. Containers run without networking and mount source read-only. Reports go outside the source tree. Public tooling-image/package downloads during image construction require network access; test execution does not. `sh scripts/validate_automation.sh` runs the spelling/actionlint/metadata subset independently.

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

Edit canonical source pages and validate them with `python3 scripts/build_wiki.py --check`. This renders and checks the Wiki sources and banner inventory without writing files. Local links, English/action/entity/issue translations and metadata are also checked.

The published GitHub Wiki contains the selected user and developer guides. Their canonical source pages and artwork remain versioned here; generated pages belong only in the separate Wiki repository. Contributing, release notes, research, dependencies and quality/validation evidence stay in the repository and exports link to those files. Prepare a separate export outside the source tree:

```sh
python3 scripts/build_wiki.py --github --output /tmp/homepod-link-wiki-export
python3 scripts/build_wiki.py --github --output /tmp/homepod-link-wiki-export --check
```

Every Wiki page has light/dark artwork in `docs/images/wiki/`, based on the approved README template. The generator adds a theme-aware `<picture>` header with a light fallback and descriptive alternative text. Exports use raw Wiki image URLs and copy all publication JPEGs into `images/`. Checking an export verifies its pages and asset bytes against current sources. Artwork prompts and the page catalogue live beside the assets; manual archives include only the publication JPEGs.

This command has no network access and does not publish. To publish an authorised update, clone `https://github.com/barneyonline/ha-homepod-sensor-link.wiki.git`, compare and copy only the generated pages and `images/` assets into that checkout, audit its working files/index/history with the public-content checker, and commit/push its existing default branch. Preserve unrelated Wiki content and existing history. Never delete canonical source pages or grant CI write permissions to perform this manual step.

Every workflow defaults to `contents: read`; validation, reporting and package jobs retain read-only repository access. `validate.yml` keeps its existing `local`, `platinum` and `release` readiness modes. Standalone workflows add spelling/actionlint, current hassfest, HACS and quality-evidence checks. HACS uses only the job's read-only GitHub token in the pinned validator; it executes no repository integration code. It reads public repository metadata, so its check requires network access, unlike the runtime tests. Dependabot proposes weekly Action updates.

Four isolated issue/PR metadata jobs additionally receive `issues: write`, with `pull-requests: write` only for PR assignment/review. They check out literal trusted `main` with no persisted credentials. The sole `pull_request_target` workflow never checks out or executes PR-head code. Issue text is parsed as bounded data and never inserted into shell/code or logged; attachments are never fetched. Unrelated labels/comments are preserved, and only the bot's own guidance marker can be updated. Label and guidance workflows independently handle form events, because token-generated label events do not launch other workflows. Manual dispatch always previews; it cannot apply changes. Policy tests use disposable fixtures, including injection, forged-marker and API-failure cases.

`release-candidate.yml` builds a scanned development archive, SHA-256 sidecar and provenance tied to an exact clean source commit. `release-assets.yml` calls that builder, downloads its artifact and checks source identity, checksum, version, archive entries and component/licence boundaries. Both are manual, read-only artifact workflows. Evidence explicitly records `release_ready: false` and pending external gates. They do not create tags, publish releases, attach public assets or deploy. Wiki publication remains a separate authorised Git operation. See [repository standards](REPOSITORY_STANDARDS.md) for the complete Enphase workflow comparison.

After validation succeeds, a separate Codecov job downloads this run's coverage artifact and uploads only `coverage.xml` from each HA version. The `CODECOV_TOKEN` secret is scoped to the pinned upload action and is never passed to validation or pull-request runs. Uploads run only on this repository's trusted `main`; forks skip reporting. The job checks out source without persisted Git credentials and executes no repository-authored shell commands. Uploader integrity verification remains enabled. Codecov configuration uses 100% project/patch targets; the exact local per-module and JavaScript gates remain authoritative for every pull request. The public badge reports Python integration coverage, while JavaScript is gated separately.

Hosted CI and Codecov report acceptance have independently passed. The Wiki is published separately through its own Git repository; releases and HACS/brands acceptance remain distinct outcomes.
