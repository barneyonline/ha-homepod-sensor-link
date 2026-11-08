# Dependency ownership and provenance

The deliberate runtime matrix is HA 2026.10.0 and 2026.11.0, both supplying aiohomekit 4.0.1 through the manifest dependency on `homekit_controller`. Tests assert the installed HA/aiohomekit versions; this integration does not independently replace HA-owned dependency pins.

| Direct import | Owner and role | Primary source |
|---|---|---|
| aiohomekit | HA HomeKit Device requirement; asynchronous HAP authentication, readings and add-pairing | [Tagged 4.0.1 source](https://github.com/Jc2k/aiohomekit/tree/4.0.1), [PyPI artifact/provenance metadata](https://pypi.org/project/aiohomekit/4.0.1/) |
| cryptography | HA-provided cryptographic dependency; Ed25519 key generation and public-key derivation | [Project documentation](https://cryptography.io/en/latest/), [source](https://github.com/pyca/cryptography) |
| zeroconf | HA zeroconf dependency; injected HA AsyncZeroconf instance | [Source](https://github.com/python-zeroconf/python-zeroconf) |
| aiohttp | HA HTTP server request/response types; no independently created outbound client session | [Source](https://github.com/aio-libs/aiohttp) |
| probatio | HA-owned configuration-schema implementation in both tested images | [HA config-entry source](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/config_entries.py) |

aiohomekit includes `py.typed`, but some public methods lack annotations. Narrow callable casts document these library boundaries; internal callbacks, runtime/config-entry types, coordinator snapshots and readings are checked by strict mypy. No local error suppression, excluded source modules or disabled strict checks are used.

HAP uses a custom encrypted asyncio socket protocol with HTTP-like framing and events. aiohomekit exposes no conventional aiohttp/HTTPX session to inject. The web-session quality-rule exemption is specific to that transport; HA’s zeroconf instance is injected. See [quality research](QUALITY_RESEARCH.md) for constructor/source evidence. Protected filesystem operations run in executor jobs and are drained across cancellation.

Development tooling uses explicit versions in `devtools/validation.json`, `devtools/requirements-quality.txt` and `devtools/requirements-automation.txt`. The typing image derives from the tested HA runtime, and its build context excludes repository content other than its Dockerfile/requirements. Installation of public development tools is distinct from test execution: test, typing, lint, Node and hassfest containers run without networking and with read-only source mounts.

Dependabot follows the Enphase policy: weekly GitHub Actions version proposals, plus the Python requirements in `/devtools` with routine version PRs disabled (`open-pull-requests-limit: 0`). [GitHub documents a separate security-update PR limit](https://docs.github.com/en/code-security/reference/supply-chain-security/dependabot-options-reference#open-pull-requests-limit); this setting does not enable security updates or alerts by itself. HA-owned runtime dependencies are not duplicated here. Changes to the HA/image/tooling matrix in `devtools/validation.json` still require a coordinated manual update and full validation.
