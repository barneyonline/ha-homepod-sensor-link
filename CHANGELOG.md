# Changelog

## Unreleased

- Replace the custom credential-import page with standard Home Assistant setup forms for pasting CSV/JSON exports or entering controller credentials, including a masked private-key field. Raw exports stay in memory; setup, reconfiguration and reauthentication no longer require a separate tab.
- Reuse the existing verification task when credential submissions overlap, preventing duplicate device records and replacement of credentials during setup.

## 0.1.1 — 2026-11-08

- Import aiohomekit pairing dictionaries keyed by accessory ID, validating their controller and accessory identities while ignoring historical network endpoints.
- Actively browse for HomePod sensor services before listing setup candidates, allowing delayed mDNS responses and isolating stale or invalid services.
- Select all discovered or resumable HomePods by default during setup and reconfiguration. Preserve the available submitted selection when correcting a validation error.

## 0.1.0 — 2026-11-08

First experimental release of **HomePod Sensor Link** for **Home Assistant 2026.11.0 or newer**.

- Discover and select HomePods, import authorised HomeKit controller credentials, and provision a dedicated User controller for new temperature/humidity sensors.
- Adopt existing native HomeKit sensors while preserving their entity IDs, history and Apple Home pairings.
- Monitor fresh sensor readings and authentication every five minutes, with diagnostics, administrator-access audits and actionable Repairs issues.
- Enable optional pairing recovery by default per device, with pinned identities, add-only pairing preservation, startup grace, persisted repair limits and pending reload retries.
- Resume partial setup, retry or cancel onboarding, reauthenticate without resetting recovery history, and automatically delete owned credentials and local history on removal.
- Protect imported credentials with owner-bound upload grants, serialized uploads, private atomic storage, cancellation-safe cleanup and sanitised diagnostics.
- Reduce duplicate journal writes, per-sample entity updates and unrelated credential copies; clean up notifications/listeners when devices are removed or re-added.
- Include light/dark branding, Marketplace installation links, Wiki guides, an optional heartbeat automation blueprint and deterministic installation packages.
- Validate with 216 offline tests, exact 100% Python statement/branch and credential-panel JavaScript coverage, strict typing, hassfest, security/performance reviews and hosted CI.

Controller-key extraction and real-device setup/recovery still require verification. See the [known limitations](https://github.com/barneyonline/ha-homepod-sensor-link/wiki/Known-Limitations) and [credential prerequisites](https://github.com/barneyonline/ha-homepod-sensor-link/wiki/Credential-Extraction) before installing. This release does not include a legacy migration or claim an official Home Assistant quality tier or HACS listing.
