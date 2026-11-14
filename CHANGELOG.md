# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- None

### 🔧 Improvements

- Keep active HomePod sensor reads running while another HomePod is still being set up.
- Avoid rewriting unchanged onboarding failures after a fresh authentication check, and reuse the accessory lookup across device selections.
- Successful HomePod recovery now silently clears any outstanding recovery alert.

### 🔄 Other changes

- None

## v1.1.1 - 2026-11-13

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- Keep credential updates and native-device transitions tied to the selected HomePod record. Ignore duplicate setup records for configured devices and prevent an in-progress credential update from recreating a removed device.

### 🔧 Improvements

- Reduce monitoring overhead by updating heartbeat progress one HomePod at a time and checking for stalled devices across the inventory once per poll, alongside the independent watchdog timer.
- Copy only the matching credential record during provisioning, and avoid rewriting the setup journal when repeated checks find the same HomePod still unreachable.

### 🔄 Other changes

- Set the integration manifest and project version to `1.1.1`.

## v1.1.0 - 2026-11-11

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- None

### 🔧 Improvements

- Start sensor monitoring before refreshing HomePod names in the background. Reuse a matching authenticated startup reading once if it is less than 30 seconds old. Later polls authenticate and read the sensors afresh.
- Try each HomePod's last known address first. If the address is unreachable or belongs to another accessory, try rediscovery and verify the HomePod's pinned identity before using a newly discovered address.
- Reduce polling overhead by avoiding copies of the credential inventory when saving monitoring state. Continue to write state durably.

### 🔄 Other changes

- Clarify the README's setup and recovery guidance for dedicated HomePod connections and devices transitioned from native HomeKit adoption.
- Set the integration manifest and project version to `1.1.0`.

## v1.0.1 - 2026-11-10

### 🚧 Breaking changes

- Remove adoption, credential access, authentication, pairing recovery and reloads for native Home Assistant `homekit_controller` entries. Every sensor connection now uses an integration-owned regular User controller. Upgrade configuration entries to version 3: existing dedicated connections retain their identities, entities and safety counters; previously adopted devices pause until an administrator explicitly confirms separate connections and imports an independently obtained export. Their native entries, pairings and history remain untouched. New temperature/humidity entities are created only after confirmation; automatic recovery starts disabled for these transitions.
- Replace native reload diagnostics with owned sensor publication diagnostics. Declare the aiohomekit dependency directly instead of loading `homekit_controller`.

### ✨ New features

- None

### 🐛 Bug fixes

- None

### 🔧 Improvements

- None

### 🔄 Other changes

- Set the integration manifest and project version to `1.0.1`.

## v1.0.0 - 2026-11-09

### 🚧 Breaking changes

- None

### ✨ New features

- Import irestore v0.4.0 `keychain.json` directly through the existing credential form on macOS or Windows. Decode typed text/Base64 HAP attributes, preserve complete controller/accessory keys and identifiers, and discard deleted records and unrelated credentials. Allow bounded 2 MiB irestore dumps while retaining existing normalized limits, validation and device selection. Verified with synthetic fixtures only; real backups and HomePods remain untested.

### 🐛 Bug fixes

- Preserve task cancellation when aiohomekit suppresses it during connector shutdown, after closing the HAP transport.
- Keep device removal retryable when protected credential deletion fails or is interrupted; remove credentials before discarding live device state.
- Durably save the macOS wizard's original-settings snapshot and phase changes before continuing. Reject FIFO inputs without blocking and files that grow beyond the private-read size limit.

### 🔧 Improvements

- Remove unused setup bookkeeping and its redundant full-inventory copies. Delete credentials and recovery history in one atomic commit when removing a device, avoiding a second unchanged journal write.
- Resolve HAP and AirPlay browser inventories with at most 32 concurrent workers per browser, retaining all results and draining workers on cancellation or failure.

### 🔄 Other changes

- Set the integration manifest and project version to `1.0.0`.
- Mark the project active and add theme-aware sensor and device-management screenshots to the README.
- Add repository agent guidance and simplify the README setup instructions.

## v0.2.0 - 2026-11-08

### 🚧 Breaking changes

- None

### ✨ New features

- Add a guided macOS Terminal wizard and bundled Swift helper for user-authorised HomeKit extraction, with private progress across reboots, saved restoration instructions, and automatic JSON generation after security settings are restored. Select records by menu number without copying keys; remove the temporary key dump after verifying the saved JSON, and advise deleting or securely storing the JSON after HA setup succeeds. The helper compiles against Apple frameworks without the unavailable CodeSignKit/FoundationKit/LoggerKit packages. Signing/export require confirmation; security changes and restarts remain manual. Hardware verification is still outstanding.
- Build heartbeat monitoring into the integration and remove the optional automation blueprint from the repository and installation package. An independent five-minute timer alerts through Repairs and a persistent notification after 15 minutes without a completed device check, clears alerts when checks resume and respects startup grace, pauses and removal. Existing imported blueprint automations can be deleted manually.
- Choose a HomePod before opening its native action menu, with descriptions, a recovery action matching its current setting and a review step before changes. Require explicit confirmation before deleting local credentials and recovery history.
- Use a HomePod's AirPlay name during discovery and refresh existing device names when the integration loads. Require a unique match to freshly discovered sensor addresses; keep the current name when discovery fails or matching is ambiguous, and preserve Home Assistant device and entity name overrides.

### 🐛 Bug fixes

- Allow a corrected pairing export after an empty or failed search without restarting setup; searching again keeps the existing import.
- Confirm proposed names with fresh HAP and AirPlay DNS replies before saving them, preventing stale zeroconf cache addresses from assigning another HomePod's name after an address reassignment.

### 🔧 Improvements

- Clarify the confirmation shown after adding HomePods to an existing setup.
- Replace the empty HomePod selector with a clear message, local discovery guidance and options to search again or import a pairing export. Show a separate message when discovery fails.
- Simplify the credential-import instructions by removing the warning about posting credentials in issues or chat.

### 🔄 Other changes

- Bump the integration manifest and project version to `0.2.0`.
- Apply the five-category changelog layout to all releases.

## v0.1.3 - 2026-11-08

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- Receive directed HAP discovery replies on temporary unicast UDP ports when the shared mDNS return path misses devices. Bound the requests, close sockets after success, failure or cancellation, and parse fresh service records without changing Home Assistant’s shared cache.
- Reuse the existing verification task when credential submissions overlap, preventing duplicate device records and replacement of credentials during setup.

### 🔧 Improvements

- Replace the custom credential-import page with standard Home Assistant setup forms for pasting CSV/JSON exports or entering controller credentials, including a masked private-key field. Raw exports stay in memory; setup, reconfiguration and reauthentication no longer require a separate tab.
- Give the discovery card a readable “HomePod sensors” title and explain temperature, humidity and optional automatic recovery during setup.

### 🔄 Other changes

- None

## v0.1.2 - 2026-11-08

### 🚧 Breaking changes

- None

### ✨ New features

- Add a pairing-import option to Reconfigure and review newly discovered devices before pairing. Keep staged credentials tied to the importing administrator and preserve reauthentication selections.

### 🐛 Bug fixes

- Find HomePods missed by multicast browsing using bounded, directed HAP DNS-SD queries to literal address hints in an imported pairing dictionary. Exported ports remain unused; authentication still verifies pinned identities.

### 🔧 Improvements

- None

### 🔄 Other changes

- None

## v0.1.1 - 2026-11-08

### 🚧 Breaking changes

- None

### ✨ New features

- None

### 🐛 Bug fixes

- Import aiohomekit pairing dictionaries keyed by accessory ID, validating their controller and accessory identities while ignoring historical network endpoints.
- Actively browse for HomePod sensor services before listing setup candidates, allowing delayed mDNS responses and isolating stale or invalid services.

### 🔧 Improvements

- Select all discovered or resumable HomePods by default during setup and reconfiguration. Preserve the available submitted selection when correcting a validation error.

### 🔄 Other changes

- None

## v0.1.0 - 2026-11-08

First experimental release of **HomePod Sensor Link** for **Home Assistant 2026.11.0 or newer**.

### 🚧 Breaking changes

- None

### ✨ New features

- Discover and select HomePods, import authorised HomeKit controller credentials, and provision a dedicated User controller for new temperature/humidity sensors.
- Adopt existing native HomeKit sensors while preserving their entity IDs, history and Apple Home pairings.
- Monitor fresh sensor readings and authentication every five minutes, with diagnostics, administrator-access audits and actionable Repairs issues.
- Enable optional pairing recovery by default per device, with pinned identities, add-only pairing preservation, startup grace, persisted repair limits and pending reload retries.
- Resume partial setup, retry or cancel onboarding, reauthenticate without resetting recovery history, and automatically delete owned credentials and local history on removal.

### 🐛 Bug fixes

- None

### 🔧 Improvements

- Protect imported credentials with owner-bound upload grants, serialized uploads, private atomic storage, cancellation-safe cleanup and sanitised diagnostics.
- Reduce duplicate journal writes, per-sample entity updates and unrelated credential copies; clean up notifications/listeners when devices are removed or re-added.

### 🔄 Other changes

- Include light/dark branding, Marketplace installation links, Wiki guides, an optional heartbeat automation blueprint and deterministic installation packages.
- Validate with 216 offline tests, exact 100% Python statement/branch and credential-panel JavaScript coverage, strict typing, hassfest, security/performance reviews and hosted CI.

Controller-key extraction and real-device setup/recovery still require verification. See the [known limitations](https://github.com/barneyonline/ha-homepod-sensor-link/wiki/Known-Limitations) and [credential prerequisites](https://github.com/barneyonline/ha-homepod-sensor-link/wiki/Credential-Extraction) before installing. This release does not include a legacy migration or claim an official Home Assistant quality tier or HACS listing.
