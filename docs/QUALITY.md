# Quality target and readiness gates

HomePod Sensor Link has a **local Platinum engineering target**. Home Assistant reports custom integrations as `custom`; this project does not claim an officially awarded tier. The [primary-source research](QUALITY_RESEARCH.md) explains the cumulative rules and transport-specific exceptions.

All **54 rules** are inventoried in `quality_scale.yaml` and backed by `devtools/quality-evidence.json`: **49 done locally, 3 scoped exemptions, 2 external todos**. The validator checks the full reviewed rule inventory, source/test/documentation references, allowed exemptions and independent evidence requirements. It does not turn file existence into official certification.

## Enforced local gates

- Exact **100% statement and branch coverage** in every integration Python module on HA 2026.10.0 and 2026.11.0, with no exclusions or tolerated misses.
- Exact **100% JavaScript line, branch and function coverage** for the credential panel under the offline Node harness.
- Strict mypy over all packaged modules, pinned Ruff lint/format, upstream hassfest, version/action/translation contracts and wiki/link checks.
- Read-only startup probes before platform setup, globally registered translated actions, dynamic selected-device entities, safe same-pin reauthentication and actionable Repairs.
- Preserved recovery/cancellation/transport safety tests, atomic credential/state transactions, foreign/native registry preservation and deterministic scanned packaging.

## Exemptions

| Rule | Scope |
|---|---|
| `docs-conditions` | No custom condition platform |
| `docs-triggers` | No custom trigger platform; examples use HA's ordinary triggers |
| `inject-websession` | aiohomekit uses custom encrypted asyncio HAP sockets and provides no aiohttp/HTTPX session interface; HA AsyncZeroconf is injected |

Revisit these exemptions if the corresponding platform or outbound HTTP client is introduced. Reauthentication, typing, branding, dynamic devices and Repairs are not exempted.

## Remaining external quality requirements

| Rule | Missing evidence |
|---|---|
| `brands` | Separately authorised upstream branding submission and acceptance |
| `integration-owner` | Confirmed support commitment and real private security reporting route; proposed metadata names the existing repository owner |

The manifest points to the public source repository's README and issue tracker. The repository preserves its existing initial commit. These working endpoints do not establish private reporting, supported hardware, HACS acceptance or an officially awarded tier.

`--require-complete` refuses the two todos. `--require-release-ready` also refuses the five pending evidence gates: actual Git provenance/index/history; authorised controller-extraction proof; authorised hardware/firmware/lifecycle proof; hosted CI; and public distribution/reporting/HACS/branding/Codecov readiness. No values or private artifacts belong in evidence files.

See [development commands](DEVELOPMENT.md), [observed validation](VALIDATION.md) and [remaining delivery boundaries](COMPLETION.md). Source publication is authorised; live verification, releases and additional distribution steps remain separate.
