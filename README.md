<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/readme-banner-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/readme-banner-light.png">
  <img src="docs/images/readme-banner-light.png" alt="HomePod Sensor Link — Temperature and humidity for Home Assistant, illustrated with a midnight HomePod linked to external sensor tiles" width="1200">
</picture>

<p align="center">
  <a href="https://github.com/barneyonline/ha-homepod-sensor-link/wiki/Known-Limitations"><img src="https://img.shields.io/badge/development-experimental-orange" alt="Experimental development"></a>
  <a href="https://github.com/barneyonline/ha-homepod-sensor-link/wiki/Getting-Started"><img src="https://img.shields.io/badge/Home_Assistant-2026.11%2B-41BDF5?logo=homeassistant&amp;logoColor=white" alt="Home Assistant 2026.11 or newer"></a>
  <a href="#get-started"><img src="https://img.shields.io/badge/install-HA_Marketplace-41BDF5" alt="Install through Home Assistant Marketplace"></a>
</p>

<p align="center">Monitor temperature and humidity. Keep your HomePod sensor connection healthy.</p>

<p align="center">
  <a href="#get-started">Install</a> ·
  <a href="#key-features">Features</a> ·
  <a href="https://github.com/barneyonline/ha-homepod-sensor-link/wiki">Documentation</a> ·
  <a href="https://github.com/barneyonline/ha-homepod-sensor-link/blob/main/CONTRIBUTING.md">Contribute</a>
</p>

> [!IMPORTANT]
> HomePod Sensor Link is an independent community project with no affiliation to Apple. **Experimental:** controller-key extraction, device setup and pairing recovery still require real-device verification. Review the [known limitations](https://github.com/barneyonline/ha-homepod-sensor-link/wiki/Known-Limitations) before installing. No supported release or HACS listing is claimed.

## Key features

- **Climate sensors:** temperature and humidity from one or more selected HomePods.
- **Guided setup:** discover devices, import authorised controller credentials, or adopt existing native HomeKit sensors without changing their entities or history.
- **Connection monitoring:** fresh authenticated checks, sensor freshness, diagnostics and administrator-access audits.
- **Optional recovery:** on by default per device, with pinned identities, pairing-preservation checks and persisted repair limits.
- **Device management and privacy:** resume partial setup, retry or cancel work, and automatically remove owned credentials and recovery history when a device is removed.

## Get started

<!-- ha-marketplace:exclude-start -->
[![Open HomePod Sensor Link in Home Assistant Marketplace](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=barneyonline&repository=ha-homepod-sensor-link&category=integration)

1. Read the [credential prerequisite and extraction guide](https://github.com/barneyonline/ha-homepod-sensor-link/wiki/Credential-Extraction). A controller key needs its controller ID and accessory public-key pin; a HomeKit setup code is insufficient.
2. On **HA 2026.11+**, use the button above, or open **Settings → Marketplace → Add from link** and enter `https://github.com/barneyonline/ha-homepod-sensor-link`. Connect GitHub when prompted and add it as an **Integration**.
3. Open **HomePod Sensor Link**, select **Install**, review the version, and confirm. Follow any restart prompt. There is no published release yet: a `main`/default-branch version is a development build.
4. Continue the setup prompt, or open **Settings → Devices & services → Add integration → HomePod Sensor Link**. Select your devices and recovery policy, then follow the local credential-import link.

See the [setup guide](https://github.com/barneyonline/ha-homepod-sensor-link/wiki/Getting-Started#installation) for detailed installation steps and [HA’s Marketplace instructions](https://www.home-assistant.io/integrations/marketplace/#custom-repositories) for adding custom repositories.
<!-- ha-marketplace:exclude-end -->

Requires **Home Assistant 2026.11.0+** (Python **3.14+**).

See the [Wiki](https://github.com/barneyonline/ha-homepod-sensor-link/wiki) for [setup](https://github.com/barneyonline/ha-homepod-sensor-link/wiki/Getting-Started), [using the integration](https://github.com/barneyonline/ha-homepod-sensor-link/wiki/Using-the-Integration), [automation examples](https://github.com/barneyonline/ha-homepod-sensor-link/wiki/Automation-Examples) and [troubleshooting](https://github.com/barneyonline/ha-homepod-sensor-link/wiki/Troubleshooting-and-Support).

Credential import requires a local HA administrator. HTTP import is accepted but does not encrypt keys in transit. Never share controller keys, exports or HA storage in issues. [Security guidance](https://github.com/barneyonline/ha-homepod-sensor-link/wiki/Security).

<details>
<summary>Setup, recovery and removal details</summary>

- New devices use a dedicated User controller; existing native HomeKit sensor entities and history remain unchanged.
- Recovery is optional per device and requires verified administrator credentials. Network/discovery failures never authorise pairing changes.
- Removing a device deletes its owned credentials and recovery history. It does not revoke on-device pairings or delete native HomeKit entries/history.
- No legacy `homepod_recovery` transition or storage migration is provided.

Read the [setup guide](https://github.com/barneyonline/ha-homepod-sensor-link/wiki/Getting-Started) and [known limitations](https://github.com/barneyonline/ha-homepod-sensor-link/wiki/Known-Limitations) for the full lifecycle and human prerequisites.

</details>

<details>
<summary>Development and validation</summary>

```sh
sh scripts/validate.sh
python3 scripts/package.py --output /tmp/homepod-sensor-link-local.zip
```

Automated tests run on HA **2026.11.0** with aiohomekit **4.0.1**. Validation requires **100% Python statements/branches per integration module**, **100% JavaScript lines/branches/functions**, strict typing, hassfest, Ruff and repository/wiki checks. Codecov reports the Python integration coverage; JavaScript is gated separately.

See [contributing](https://github.com/barneyonline/ha-homepod-sensor-link/blob/main/CONTRIBUTING.md), [development](https://github.com/barneyonline/ha-homepod-sensor-link/wiki/Development-Environment), [validation evidence](https://github.com/barneyonline/ha-homepod-sensor-link/actions/workflows/validate.yml) and the [Platinum engineering target](https://github.com/barneyonline/ha-homepod-sensor-link/blob/main/CONTRIBUTING.md#quality-checks). A target is not an awarded Home Assistant quality tier; extraction and real-device verification remain outstanding.

</details>

[![Checks](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-homepod-sensor-link/validate.yml?branch=main&label=checks)](https://github.com/barneyonline/ha-homepod-sensor-link/actions/workflows/validate.yml)
[![HACS validation](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-homepod-sensor-link/hacs.yml?branch=main&label=HACS)](https://github.com/barneyonline/ha-homepod-sensor-link/actions/workflows/hacs.yml)
[![Hassfest](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-homepod-sensor-link/hassfest.yml?branch=main&label=hassfest)](https://github.com/barneyonline/ha-homepod-sensor-link/actions/workflows/hassfest.yml)
[![Spelling and workflows](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-homepod-sensor-link/codespell.yml?branch=main&label=spelling%20%26%20workflows)](https://github.com/barneyonline/ha-homepod-sensor-link/actions/workflows/codespell.yml)
[![Codecov — Python integration](https://codecov.io/gh/barneyonline/ha-homepod-sensor-link/branch/main/graph/badge.svg)](https://app.codecov.io/gh/barneyonline/ha-homepod-sensor-link)
[![Quality target](https://img.shields.io/badge/quality_target-Platinum-blue)](https://github.com/barneyonline/ha-homepod-sensor-link/blob/main/CONTRIBUTING.md#quality-checks)
[![Stars](https://img.shields.io/github/stars/barneyonline/ha-homepod-sensor-link)](https://github.com/barneyonline/ha-homepod-sensor-link/stargazers)
[![License](https://img.shields.io/github/license/barneyonline/ha-homepod-sensor-link)](LICENSE)
[![Open Issues](https://img.shields.io/github/issues/barneyonline/ha-homepod-sensor-link)](https://github.com/barneyonline/ha-homepod-sensor-link/issues)

GPL-3.0 licensed. See [LICENSE](LICENSE).
