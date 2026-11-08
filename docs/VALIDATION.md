# Local validation evidence

Date: 8 November 2026 (Australia/Melbourne). HomePod Sensor Link, domain `homepod_sensor_link`, repository `barneyonline/ha-homepod-sensor-link`. Version 2.0.0 is development metadata, not a published release.

| Check | Observed result |
|---|---|
| Supplied baseline before development | 62 tests passed on HA 2026.11.0 |
| Current HA 2026.10.0 matrix | 196 tests passed |
| Current HA 2026.11.0 matrix | 196 tests passed |
| Host content/Git scanner/package tests | 12 tests passed |
| Host repository tooling tests | 16 tests passed |
| Integration Python coverage | All 18 modules: 100% statements and branches, zero missing/excluded lines or branches |
| Credential-panel JavaScript | 5 Node behavior tests; 100% lines, branches and functions; syntax check passed |
| Strict typing | mypy 2.3.1, all 18 integration modules, HA 2026.11.0/Python 3.14; no local suppressions or blanket missing-import ignore |
| Python lint and formatting | Ruff 0.14.1 `E4,E7,E9,F` and formatter checks passed |
| Upstream hassfest | Pinned 2026.10.4 image: manifest, discovery, icons, translations, actions and quality-scale metadata validation |
| Quality evidence | All 54 targets audited: 49 done locally, 3 scoped exemptions, 2 external todos; four release evidence gates pending |
| Hosted GitHub Actions | First published implementation passed [run 42424449553](https://github.com/barneyonline/ha-homepod-sensor-link/actions/runs/42424449553) on commit `3b4c407`: complete two-version validation and coverage artifacts |
| Codecov reporting | [Run 42425887513](https://github.com/barneyonline/ha-homepod-sensor-link/actions/runs/42425887513) passed validation and the isolated upload job on `a76f130`; public badge reports 100%, with successful project/patch checks |
| Strict Platinum/release readiness | Intentionally refused while external requirements remain unverified |
| Repository contracts | Naming/version/matrix/action/translation parity, enum labels/icons, source suppression checks, local links, read-only repository permissions and isolated trusted-main Codecov reporting |
| Local wiki | 12 generated pages including sidebar match canonical sources; all 11 Wiki pages have complete light/dark banner pairs and export drift checks |
| Published GitHub Wiki | 11 documentation pages and custom sidebar; native page links/anchors, preserved source snapshots and existing Wiki history |
| Working-file public-content audit | Passed, including hidden source files |
| Strict actual Git audit | Existing GitHub history preserved; current files/index/reachable commits scanned before upload; earlier `2d5e069` remains unverified |
| Synthetic browser preview | Earlier local mock-receiver check rejected empty input, accepted disposable fields, cleared inputs and removed nonce; no installed-HA/device proof |
| Manual package | One component, frontend/banner assets, docs/wiki/licence/blueprint and quality evidence; exact bytes scanned, deterministic ZIP and SHA-256 sidecar |

`sh scripts/validate.sh` enforces the entire local gate. Runtime containers use `--network none` and read-only source mounts. Public development dependencies can be downloaded while constructing the locked typing image; that build context contains only its Dockerfile/requirements. No tests use live credentials or a live HA installation. Coverage tools are pinned to the supplied images: 7.16.2 on HA 2026.10.0 and 7.17.1 on HA 2026.11.0.

## Coverage and regression evidence

The gate checks exact integer miss counts for every on-disk integration module, not rounded display percentages. No tolerated-miss baseline, omitted module or coverage exclusion remains. Node independently gates the production import script. Unhandled async background task failures fail the Python runner even when individual tests appear green.

The suite preserves the original recovery timing, rolling repair/reload limits, pairing-table preservation, private identity/permission checks and real aiohomekit shutdown classification/cancellation regressions. It adds import rejection, exact protocol spelling, discovery changes, local/admin/Cloud/owner/expiry/body-limit security, partial setup, startup probes, real HA measurement publication, administrator disposal, same-pin reauth, dynamic entities, active/unloaded removal, Repairs lifecycle, foreign registry preservation, atomic cancellation/deletion races and fail-closed pin retention. A lost add reply retains its private pairing-table checkpoint and cannot lead to blind re-add or success without preserved rows and fresh readings.

The Node harness tests selected CSV/JSON upload, explicit controller fields with/without accessory pins, pasted records, authenticated request construction, missing sessions, byte/file limits, server/network errors, field clearing and nonce removal. It simulates DOM/HA boundaries and does not establish full installed-HA browser behavior.

Strict mypy resolves the supported HA source via `MYPYPATH`, checks typed config-entry/runtime/coordinator/callback seams and keeps dynamic wire/storage maps behind validation. Narrow callable casts describe incomplete aiohomekit annotations. Upstream hassfest runs offline; metadata syntax success does not prove future public URLs exist or a HACS/branding submission was accepted.

HA 2026.10 lacks the Repairs entry-link parameter available in 2026.11. The adapter detects that public capability, omits only the unsupported argument, and explicitly cleans issues on resolution/removal on both versions. Tests cover both signatures independently.

## Remaining independent evidence

The original source folder arrived without `.git`. Publication attaches the existing GitHub repository history and preserves its initial commit. Current files, staged content and all reachable commits are scanned before upload. The earlier development baseline `2d5e069` is still unavailable and cannot be authenticated. No replacement ancestry is claimed; the pre-publication source snapshot was preserved outside the repository.

Source publication is complete on the existing public GitHub repository, and the generated documentation is published in its separate GitHub Wiki. Hosted validation, the isolated trusted-main Codecov upload and service-side processing have passed independently of local validation. Review GitHub Actions and Codecov for results on later commits. A release, HACS acceptance and upstream branding acceptance remain unperformed. Private vulnerability reporting is not configured. The workflow's manual Platinum/release modes refuse incomplete evidence.

The controller-extraction guide is a primary-source-researched candidate. Actual modern backup record availability, intact key export, hardware/firmware interoperability, User access and full setup/recovery/removal still require separately authorised evidence through the existing 1Password workflow. No real backup, keychain, HomePod, credential or live HA system was accessed. [Quality target](QUALITY.md) records the distinction between local gates and official/external acceptance.
