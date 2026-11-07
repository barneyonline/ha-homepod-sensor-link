<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/readme-banner-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/readme-banner-light.png">
  <img src="docs/images/readme-banner-light.png" alt="HomePod Sensor Link — Temperature and humidity for Home Assistant, illustrated with a midnight HomePod linked to external sensor tiles" width="1200">
</picture>

<p align="center">Discover HomePod sensors. Keep their connection healthy. Preserve Apple Home.</p>

<p align="center">
  <a href="#get-started">Get started</a> ·
  <a href="#key-features">Features</a> ·
  <a href="wiki/Home.md">Documentation &amp; local wiki</a> ·
  <a href="CONTRIBUTING.md">Contribute</a>
</p>

> [!IMPORTANT]
> Unofficial community project, not affiliated with Apple or Home Assistant. **Experimental development:** the controller-extraction route and real-device setup/recovery still need verification. Publishing the source does not establish a supported release. No release, HACS listing or live installation is claimed.

## Key features

- **Discovery and setup:** select one or more HomePods, import explicitly authorised controller credentials, and complete each device independently.
- **Temperature and humidity:** create dedicated User-controller access for new integration-owned measurements; adopt existing native HomeKit sensors without changing their entities, IDs or history.
- **Guarded recovery:** on by default per device, independently configurable, with verified administrator access, identity pins, startup grace and persisted repair limits.
- **Lifecycle:** keep ready devices working through partial setup, retry with the same identity, resume after restart, and cancel pending work.
- **Privacy:** local administrator-only input, anonymous diagnostics, and automatic deletion of owned credentials/recovery history on removal.

## Get started

Requires **Home Assistant 2026.10.0+** / Python **3.14+**. Automated compatibility tests cover HA **2026.10.0** and **2026.11.0** with aiohomekit **4.0.1**.

Apple documents sensor hardware for HomePod mini and HomePod (2nd generation), software 16.3+. That does not establish this integration’s tested hardware/firmware compatibility. See [known limitations](docs/LIMITATIONS.md).

1. Read the [credential prerequisite and candidate extraction guide](docs/EXTRACTION.md). A controller key needs its controller ID and the accessory ID/public pin; a HomeKit setup code is insufficient. The encrypted-backup/Mac route remains unproven on real modern HomeKit records.
2. Once installation is separately authorised, copy `custom_components/homepod_sensor_link` into the HA configuration’s `custom_components` directory and restart HA. No HACS listing or one-click install is claimed.
3. Add **HomePod Sensor Link** in Settings → Devices & services, choose devices and recovery policy, then follow the local import link. [Full setup guide](docs/SETUP.md).

HTTP import is allowed without an acknowledgement checkbox and carries credentials without transport encryption. The server requires a local authenticated HA administrator; normal HTTPS verification stays enabled. Never post controller keys or exports in issues, chat or logs.

## Documentation

| Task | Guide |
|---|---|
| Set up or manage devices | [Getting started](wiki/Getting-Started.md) |
| Understand measurements and actions | [Using the integration](wiki/Using-the-Integration.md) |
| Create heartbeat/availability alerts | [Automation examples](wiki/Automation-Examples.md) |
| Resolve missing readings or pending setup | [Troubleshooting](wiki/Troubleshooting-and-Support.md) |
| Review boundaries and credential lifetime | [Limitations](wiki/Known-Limitations.md) · [Security](SECURITY.md) |
| Develop and verify changes | [Development](docs/DEVELOPMENT.md) · [Validation](docs/VALIDATION.md) |

The [local wiki](wiki/Home.md) is generated from canonical documentation, checked for drift, and ready for a separately authorised publication step. [Architecture](docs/ARCHITECTURE.md), [research](docs/RESEARCH.md), [repository alignment](docs/REPOSITORY_STANDARDS.md) and [changelog](CHANGELOG.md) provide developer detail.

## Removal and fresh configuration

Removal deletes this integration’s owned credentials and recovery history; it does not revoke on-device pairings or delete native HomeKit entries/history. No `homepod_recovery` compatibility package, transition or storage migration is included. Old storage is untouched; a conflict guard prevents competing recovery engines. Removing/re-adding intentionally resets recovery history; normal restarts preserve it.

## Development and status

```sh
sh scripts/validate.sh
python3 scripts/package.py --output /tmp/homepod-sensor-link-local.zip
```

Validation enforces **100% statements and branches in every Python integration module**, **100% JavaScript lines/branches/functions**, strict mypy, upstream hassfest, Ruff lint/format, translations, documentation/wiki contracts and the public-content guard. The [54-rule Platinum target](docs/QUALITY.md) records 49 local passes, 3 scoped exemptions and 2 external todos; HA still reports this as a custom integration. GitHub automation is read-only and uses no live credentials. Check [GitHub Actions](https://github.com/barneyonline/ha-homepod-sensor-link/actions) for hosted results; local validation alone is not hosted evidence.

Repository: [**`barneyonline/ha-homepod-sensor-link`**](https://github.com/barneyonline/ha-homepod-sensor-link). Domain/component: **`homepod_sensor_link`** / **`custom_components/homepod_sensor_link`**. The local folder is now `ha-homepod-sensor-link`. This upload preserves the existing GitHub initial commit; the earlier development baseline `2d5e069` was not present in the supplied files and remains unverified. [Completion boundaries](docs/COMPLETION.md).

The selected **HomePod Midnight** artwork ships in the component's local `brand/` directory, with transparent standard/high-resolution light and dark variants. See [branding assets and reproduction](docs/BRANDING.md).

GPL-3.0 licensed; the prior MIT notice is retained in [LICENSES/MIT.txt](LICENSES/MIT.txt). Public development issues are available on GitHub. A private security-reporting route, HACS acceptance and a supported release remain outstanding.
