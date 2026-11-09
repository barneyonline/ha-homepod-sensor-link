# Contributing

HomePod Sensor Link uses domain `homepod_sensor_link` and repository `ha-homepod-sensor-link`. The project uses GPL-3.0; see [LICENSE](LICENSE).

Read the [setup](https://github.com/barneyonline/ha-homepod-sensor-link/wiki/Getting-Started), [credential extraction](https://github.com/barneyonline/ha-homepod-sensor-link/wiki/Credential-Extraction) and [architecture](https://github.com/barneyonline/ha-homepod-sensor-link/wiki/Architecture) guides in the GitHub Wiki. User and developer documentation lives there; `docs/` retains artwork and its provenance.

## Development and validation

Run the complete local gate before submitting changes:

```sh
sh scripts/validate.sh
```

The minimum and tested runtime is **HA 2026.11.0**, with **aiohomekit 4.0.1** and **Python 3.14**. Docker is required. Runtime tests mount source read-only and run with networking disabled. Reports are written outside the repository. Public tooling downloads during image construction are separate from test execution. Pinned versions live in `devtools/validation.json` and the two `devtools/requirements-*.txt` files.

Focused commands:

```sh
python3 scripts/check_repository.py
python3 scripts/run_tests.py --version 2026.11.0 --output /tmp/homepod-link-tests
python3 scripts/check_coverage.py /tmp/homepod-link-tests/coverage.json
python3 scripts/check_public_content.py --history --require-git
sh scripts/validate_automation.sh
```

Add meaningful behavioural tests for changes. Every integration module must retain exactly **100% statements and branches**, with zero missing or excluded lines. The credential panel separately requires **100% JavaScript lines, branches and functions**. Strict mypy, pinned Ruff, upstream hassfest, translation/action metadata parity and repository contracts must pass. Do not introduce tolerated misses, suppressions or excluded modules to satisfy a gate. Background task failures also fail the test runner.

On macOS, validate the standalone export helper with `sh scripts/test_macos_homekit.sh`. This compiles a synthetic test entry point with real keychain access excluded. `sh scripts/build_macos_homekit.sh /tmp/homepod-helper-build` compiles the normal executable into a new folder; `--help` and `--describe-query` are harmless checks. Both builds use Apple frameworks without package downloads. The separate macOS CI job performs these checks without signing or extraction. Do not run `--export-hap`, sign against a real certificate, or change security settings during ordinary development. Actual extraction and HomePod authentication remain external release gates.

Preserve fresh five-minute authentication checks, the ten-minute startup grace, three confirmed authentication failures spanning ten minutes, one repair attempt per hour and three per rolling day. Pending owned-sensor publication retries persist and retry at most three times per rolling hour. Network/discovery and cleanup-only failures cannot authorise repair. Keep the real aiohomekit connector cleanup classification/cancellation regressions.

Preserve identity and permission pins, add-only pairing-table checks, persist-before-write transactions, interrupted setup/Resume, foreign/native entity history and safe removal. New devices use dedicated User controllers. Recovery defaults on per device and requires verified administrator credentials. Removal automatically deletes owned credentials and recovery history; it does not revoke on-device pairings or remove native sensors. The config-entry v2-to-v3 transition reads only our private inventory: dedicated identities remain unchanged, while previously adopted native devices pause pending explicit consent and a fresh administrator export. No native entries are read, reloaded, adopted or recovered. No `homepod_recovery` compatibility package is included.

Performance regressions use deterministic operation counts in `tests/test_performance_storage.py` and `tests/test_performance_publication.py`: seven simulated days of administrator audits, protected-file writes/fsyncs, isolated 64-record reads, and actual HA entity reports at 1, 16 and 64 devices. Measurements publish synchronously to the selected device before fresh-report verification; the final coordinator broadcast still updates health and inventory. Skipped administrator audits need no journal commit. Attempt reservations and pairing checkpoints must always validate and durably write protected storage, even when memory appears unchanged. Keep fresh authentication and verification work; do not replace it with cached success.

## Dependencies

The integration directly declares `aiohomekit>=4.0.1` in its manifest and owns its `IpController` and dedicated regular User identities. It does not depend on or inspect `homekit_controller`. HA supplies cryptography, zeroconf, aiohttp and probatio; do not independently override those core runtime pins. [aiohomekit 4.0.1](https://github.com/Jc2k/aiohomekit/tree/4.0.1) uses encrypted asyncio HAP sockets rather than an injectable aiohttp/HTTPX session. The integration uses HA's AsyncZeroconf instance for normal discovery and HAP sessions. Export address hints and friendly-name confirmation additionally use bounded, temporary unicast UDP sockets for DNS-SD replies; these close after each request and do not modify HA's shared cache. Friendly names require fresh HAP identity and AirPlay name replies confirming a unique address association. Blocking protected-file work runs in drained executor jobs.

Development requirements are pinned in `devtools/requirements-quality.txt` and `devtools/requirements-automation.txt`. Dependabot follows the Enphase policy: weekly Action updates and development Python requirements with routine version PRs disabled. Security-update settings are separate. Update the HA/image/tooling matrix deliberately and run the full gate after changing it.

## Quality checks

`quality_scale.yaml` and `devtools/quality-evidence.json` track all **54** engineering targets: **49 done, 3 scoped exemptions and 2 todos**. This is a local checklist; HA identifies this as a custom integration.

Evidence requires existing local code/test files and local documentation or an exact page in this project's Wiki inventory. Wiki references are checked against the declared navigation without fetching their content. Unknown, retired, insecure or foreign Wiki references are rejected. The validator does not treat a reference check as independent certification or release evidence.

The exemptions are `docs-conditions` and `docs-triggers` because no custom platforms are implemented, and `inject-websession` because aiohomekit exposes no conventional HTTP session to inject. Revisit them if those interfaces change. Branding and integration-owner rules remain todos requiring independent evidence. The four release gates remain in `devtools/quality-evidence.json`: original development provenance, controller extraction, hardware and distribution.

After building the quality image through full validation, run stricter checks with:

```sh
docker run --rm --network none -v "$PWD:/project:ro" -w /project --entrypoint python homepod-link-quality:2026.11.0 scripts/validate_quality_scale.py --require-complete
docker run --rm --network none -v "$PWD:/project:ro" -w /project --entrypoint python homepod-link-quality:2026.11.0 scripts/validate_quality_scale.py --require-release-ready
```

Keep these gates closed until their recorded requirements are met. Do not mark external requirements complete merely to pass a check.

## Documentation

Edit pages directly in the [GitHub Wiki](https://github.com/barneyonline/ha-homepod-sensor-link/wiki), using GitHub's editor or its separate Git repository. Do not create duplicate pages or generated snapshots in this repository. Review Wiki changes independently and preserve unrelated work and history.

The Wiki checkout is `https://github.com/barneyonline/ha-homepod-sensor-link.wiki.git`. Its page Markdown and `images/` assets are maintained there. Banner sources remain under `docs/images/wiki/`; keep light/dark pairs for pages declared in `scripts/wiki.py`. `scripts/check_repository.py` checks retained artwork, repository links and Wiki navigation. It performs no publication or live HA access.

Keep [SECURITY.md](SECURITY.md), [CHANGELOG.md](CHANGELOG.md), README links and the Wiki consistent. Maintain action metadata in `services.yaml`, `strings.json` and the English translation together. Use the prepared issue/PR forms with sanitised evidence only.

## Automation

CI uses read-only repository access for validation, package and coverage jobs. The isolated Codecov job receives `CODECOV_TOKEN` only after successful validation on trusted `main` and uploads the HA runtime’s Python coverage report. Pull-request validation does not receive that secret. Four metadata jobs use scoped issue/PR permissions and trusted-main code; manual dispatch only previews changes. The `pull_request_target` workflow never executes PR-head code.

`release-candidate.yml` builds a scanned development artifact with checksum and exact clean source provenance. `release-assets.yml` downloads and independently verifies it. Neither creates tags, publishes releases or deploys. Wiki publication is a separate authorised operation. Workflow definitions, metadata policy and tests document the implementation.

## Privacy and security

Do not access live systems, 1Password, real backups/keychains or credentials during ordinary development. Separately authorised live work follows the existing 1Password workflow. The credential guide's manual macOS extraction steps are for users; they do not authorise development access or security changes to this Mac. Do not weaken network verification, permissions or approval settings.

Use generated disposable identities and documentation-only endpoints. Never commit real keys, pairing exports, device/controller IDs, addresses, household mappings, logs, HA storage or private diagnostics. Curated diagnostics must omit raw exception messages, payloads and sensitive locals. See [SECURITY.md](SECURITY.md).

`python3 scripts/check_public_content.py --history --require-git` audits working files, staged content and reachable history. Missing Git metadata is fatal; synthetic fixtures do not establish the missing initial development provenance. Review staged file lists and diffs as well as scanner output.

`python3 scripts/package.py --output /tmp/homepod-sensor-link-local.zip` creates a deterministic single-component archive and SHA-256 sidecar after scanning its exact bytes. Keep real credentials and evidence out of artifacts. Automated tests, hosted checks, publication, installation and real-device verification are separate outcomes.
