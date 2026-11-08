# Setup and device management

## Before starting

Apple sensor hardware support covers HomePod mini and HomePod (2nd generation), software 16.3+. An advertised HomePod name is a discovery hint, not proof of sensor layout, accessory identity or administrator authority. [Research](RESEARCH.md) distinguishes documented Apple support from real-device evidence.

Obtain controller credentials explicitly using an authorised process. Read [EXTRACTION.md](EXTRACTION.md): the graphical encrypted-backup route is a source-researched candidate, not a proven modern HomeKit export workflow. Do not disable OS protections or export a whole keychain into HA. No setup code/QR is treated as an existing administrator identity.

## Installation

The integration requires **Home Assistant 2026.10.0+**. Built-in Marketplace is available from **2026.11**; use HACS or manual installation on 2026.10. This repository is not listed in the shared catalog and has no published release yet. A `main`/default-branch version is a development build.

### Home Assistant Marketplace — HA 2026.11+

[![Open HomePod Sensor Link in Home Assistant Marketplace](https://my.home-assistant.io/badges/marketplace_repository.svg)](https://my.home-assistant.io/redirect/marketplace_repository/?owner=barneyonline&repository=ha-homepod-sensor-link&category=integration)

1. Select the button above. Alternatively, open **Settings → Marketplace → Add from link**, enter `https://github.com/barneyonline/ha-homepod-sensor-link`, then select **Add**.
2. Connect GitHub if prompted. If Marketplace asks for the repository type, choose **Integration** and select **Add** again.
3. Open **HomePod Sensor Link** and select **Install**. Review the offered version, then confirm. If only the default branch is offered, it installs development code.
4. Follow any restart prompt. Continue the integration setup prompt, or open **Settings → Devices & services → Add integration → HomePod Sensor Link**.
5. Follow **Guided flow** below to select devices and import credentials. Installing files does not authenticate a HomePod or complete sensor setup.

Adding the repository from this button does not submit it to the public catalog. See [HA’s custom-repository instructions](https://www.home-assistant.io/integrations/marketplace/#custom-repositories) and [official repository-link guidance](https://contributors.home-assistant.io/marketplace/guides/catalog/#the-link-to-your-repository).

### HACS — HA 2026.10

1. With HACS installed, [open HomePod Sensor Link in HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=barneyonline&repository=ha-homepod-sensor-link&category=integration). Add the custom repository as **Integration** when prompted.
2. Open the repository, download the offered version and restart Home Assistant.
3. Open **Settings → Devices & services → Add integration → HomePod Sensor Link**, then follow **Guided flow** below.

The older HACS repository link opens Marketplace on HA 2026.11+. Do not install a second copy alongside the Marketplace version.

### Manual development installation

1. Download the repository source and copy only `custom_components/homepod_sensor_link` into `<config>/custom_components/`, producing `<config>/custom_components/homepod_sensor_link/manifest.json`.
2. Restart Home Assistant, add **HomePod Sensor Link** under **Settings → Devices & services**, then follow **Guided flow** below.

## Guided flow

1. Add **HomePod Sensor Link** in HA. Candidate HAP services and existing native entries are discovered and matched by accessory ID. Discovery never supplies or replaces an accessory public-key pin.
2. Select one or more HomePods. Recovery is selected **by default** for each device; clear a device to monitor without recovery. Existing active devices are managed separately through Reconfigure → Manage existing devices.
3. Open the local import link in another tab. The import page requires an authenticated HA administrator and a local source address; Cloud, remote, missing/invalid source and wrong-user sessions are rejected. HA’s trusted-proxy configuration must preserve the real client address. No extra acknowledgement or HTTPS requirement is imposed. HTTP exposes the credentials on the network; certificate verification is never weakened for HTTPS.
4. Choose a selected HAP CSV or JSON export, or enter controller identifier/private key and the accessory identity/public pin in the fallback fields. Return to setup and Continue. The UI never writes native HA credential storage. Raw exports stay in bounded memory, are tied to the user/flow, expire after ten minutes, and are discarded on consumption/abort/shutdown.
5. The progress task parses/validates and tries at most three supplied identities per selected device, using fresh read-only authentication, administrator-table validation and characteristic reads. Network failures do not grant pairing authority or count as recovery failures. If no identity matches, provide a more narrowly selected export; the integration will not spray every key across the home.
6. Each device is journalled separately. Successful devices remain usable while incomplete devices show a pending/needs-credentials result. The summary must not be interpreted as proof that all selected devices are healthy.
7. New devices get a persisted, dedicated regular **User** controller identity. The sole add operation preserves every prior pairing row and verifies the exact expected table afterward. No Apple Home controller is replaced or removed. A failed User-permission path is reported; it is not silently upgraded to Admin.
8. This integration publishes temperature/humidity entities for new devices. Completion requires fresh authenticated characteristic values and fresh reports from both real HA measurement entities. Existing native entries are adopted by reference, preserving their entities, IDs and history.

The first authorised provisioning write can run as part of that same HA process after the entry is created. After interruption/restart, the integration checks the saved dedicated identity read-only. Further onboarding pairing writes require Reconfigure → Manage → Resume. An accepted pairing is never blindly re-added. Explicit Cancel closes pending work and stops setup probes until resumed. The saved controller identity is reused; no new key is generated just to retry.

## Accepted credential formats

Typed JSON:

```json
{
  "controllers": [
    {"iOSPairingId": "exported-controller-id", "iOSDeviceLTSK": "exported-32-byte-seed-as-hex"}
  ],
  "accessories": [
    {"AccessoryPairingID": "exact-exported-accessory-id", "AccessoryLTPK": "exported-32-byte-public-key-as-hex"}
  ]
}
```

The text placeholders are deliberately not usable credentials. An optional controller `iOSDeviceLTPK` is checked against the derived public key. A single JSON record containing these four fields and optional controller public key is also supported.

The selected CSV format requires `Account`, `Password`, `App / Group` columns. Every row must belong to `com.apple.hap.pairing`. Printable 128-hex-character identity payloads contain public key followed by private seed; 64-character accessory payloads are public pins. Conflicting duplicates, wrong public/private pairs, unrelated password rows, binary placeholders and oversized exports are rejected without echoing values. Upload cap: 128 KiB; up to 64 controller and accessory records. Keep exports minimal.

The exact exported accessory identifier spelling is retained for Pair Verify, whose comparison is case-sensitive. Normalised IDs are used only for discovery/deduplication. Native-entry pins are read from HA’s existing public config-entry interface; native private keys are never copied to a new entity or altered.

## Recovery, freshness and credentials

Recovery is on by default but cannot act without a verified, separately trusted administrator credential. It preserves the existing five-minute fresh checks, three confirmed failures spanning ten minutes, ten-minute startup grace, one repair per hour and three per rolling day. Network, discovery and cleanup-only failures never authorise repair. Pending native reloads persist and retry at most three times per rolling hour without re-adding an accepted pairing.

New controllers have User permission; adopted native controllers retain their pinned permission. Recovery restores exactly the pinned HA row and permission. Identity/key/permission mismatches fail closed.

For new entities, values become unavailable when their verified sample is stale or setup is not ready; native adoption also requires recent numeric reports. Recovery success requires actual reports after reload/publication. An unchanged temperature still has a heartbeat; the optional `blueprints/automation/homepod_sensor_link/heartbeat-watchdog.yaml` blueprint lets you select diagnostic entities.

The imported administrator identity is retained while setup is incomplete or recovery is enabled. After completed monitoring-only setup it is removed from the private record; the dedicated HA private key remains for reads. Enabling recovery again requires re-import and verification against the retained public administrator pin.

## Reconfigure and removal

One integration entry manages all devices. Reconfigure offers Add/import and Manage actions: Resume, Cancel, enable/disable recovery, and Remove. Explicitly prepared additions appear without reloading healthy devices. Device removal stops its work, deletes its credentials and recovery state, and leaves other devices intact. A registry row referenced by a foreign entity is retained to preserve that link. Integration removal automatically deletes all owned credentials, journals and recovery history, including when setup failed. Unload/reload preserves data.

Confirmed administrator rejection can start HA’s reauthentication flow. Continue from a local administrator session and re-import the existing pinned identity. Reauthentication cannot rotate pins, reset limits or claim success for an unmatched/blocked device. Repairs explain actionable credential, sensor-access, competing-engine or check failures; ordinary unload preserves those issues.

There is no explicit purge acknowledgement or legacy transition. The original `homepod_recovery` component is unsupported and not packaged. Its active configuration blocks competing recovery, but its entries/files are never migrated or modified by this integration.

Removal intentionally does **not** revoke on-device pairings, delete native HomeKit entries, delete native measurement history, or erase external backups/export copies. Deleting and re-adding the integration starts fresh repair counters and cooldown history; ordinary restarts preserve them. Local file deletion is not a guarantee of forensic erasure.
