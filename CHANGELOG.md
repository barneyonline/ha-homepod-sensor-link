# Changelog

## Unreleased

- Add a guided macOS Terminal wizard and bundled Swift helper for user-authorised HomeKit extraction, with private progress across reboots, saved restoration instructions, and automatic JSON generation after security settings are restored. Select records by menu number without copying keys; remove the temporary key dump after verifying the saved JSON, and advise deleting or securely storing the JSON after HA setup succeeds. The helper compiles against Apple frameworks without the unavailable CodeSignKit/FoundationKit/LoggerKit packages. Signing/export require confirmation; security changes and restarts remain manual. Hardware verification is still outstanding.
- Build heartbeat monitoring into the integration and remove the optional automation blueprint from the repository and installation package. An independent five-minute timer alerts through Repairs and a persistent notification after 15 minutes without a completed device check, clears alerts when checks resume and respects startup grace, pauses and removal. Existing imported blueprint automations can be deleted manually.

- Choose a HomePod before opening its native action menu, with descriptions, a recovery action matching its current setting and a review step before changes. Require explicit confirmation before deleting local credentials and recovery history.

- Clarify the confirmation shown after adding HomePods to an existing setup.
- Replace the empty HomePod selector with a clear message, local discovery guidance and options to search again or import a pairing export. Show a separate message when discovery fails.
- Allow a corrected pairing export after an empty or failed search without restarting setup; searching again keeps the existing import.
- Use a HomePod's AirPlay name during discovery and refresh existing device names when the integration loads. Require a unique match to freshly discovered sensor addresses; keep the current name when discovery fails or matching is ambiguous, and preserve Home Assistant device and entity name overrides.
- Confirm proposed names with fresh HAP and AirPlay DNS replies before saving them, preventing stale zeroconf cache addresses from assigning another HomePod's name after an address reassignment.

## 0.1.3 — 2026-11-08

- Receive directed HAP discovery replies on temporary unicast UDP ports when the shared mDNS return path misses devices. Bound the requests, close sockets after success, failure or cancellation, and parse fresh service records without changing Home Assistant’s shared cache.
- Replace the custom credential-import page with standard Home Assistant setup forms for pasting CSV/JSON exports or entering controller credentials, including a masked private-key field. Raw exports stay in memory; setup, reconfiguration and reauthentication no longer require a separate tab.
- Reuse the existing verification task when credential submissions overlap, preventing duplicate device records and replacement of credentials during setup.
- Give the discovery card a readable “HomePod sensors” title and explain temperature, humidity and optional automatic recovery during setup.

## 0.1.2 — 2026-11-08

- Find HomePods missed by multicast browsing using bounded, directed HAP DNS-SD queries to literal address hints in an imported pairing dictionary. Exported ports remain unused; authentication still verifies pinned identities.
- Add a pairing-import option to Reconfigure and review newly discovered devices before pairing. Keep staged credentials tied to the importing administrator and preserve reauthentication selections.

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
