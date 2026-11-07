# Primary-source boundaries and extraction research

Reviewed 7 November 2026. Implemented target: **HomePod Sensor Link**, domain `homepod_sensor_link`. No live device, backup or keychain was accessed.

## Apple support and trust

Apple documents temperature/humidity sensing on HomePod mini and HomePod (2nd generation), introduced with software 16.3. Measurement optimisation and calibration caveats do not establish HAP interoperability for every firmware. [HomePod guide](https://support.apple.com/en-au/guide/homepod/apd8085c1032/homepod), [software notes](https://support.apple.com/en-ae/108045), [specifications](https://support.apple.com/en-au/111843).

HomeKit uses controller Ed25519 identities and mutually authenticated sessions. A setup code does not supply an already trusted administrator identity. Apple documents identity keys in encrypted keychain backups; its separate statement about local backup of the Home database addresses a different artifact. [Communication security](https://support.apple.com/en-ge/guide/security/sec3a881ccb1/web), [home data](https://support.apple.com/en-ca/guide/security/sec49613249e/web).

Normal HomeKit consent does not grant arbitrary access to Apple’s protected keychain access groups, whose membership depends on signing entitlements. Keychain Access’s generic export UI does not establish export of these Generic Password records. [Access groups](https://developer.apple.com/documentation/security/sharing-access-to-keychain-items-among-a-collection-of-apps), [Keychain Access export](https://support.apple.com/en-ie/guide/keychain-access/kyca35961/mac).

## aiohomekit and native HA boundaries

HA 2026.10.0 and 2026.11.0 use aiohomekit 4.0.1 in the tested images. Their native HomeKit Device flows have no prepared-credential import step and skip already paired discoveries. This integration therefore owns newly created measurement entities through HA’s entity API, while existing native entries are adopted unchanged. It never calls private `_entry_from_accessory` or edits `.storage/core.config_entries`. [2026.10 flow](https://github.com/home-assistant/core/blob/2026.10.0/homeassistant/components/homekit_controller/config_flow.py), [2026.11 flow](https://github.com/home-assistant/core/blob/2026.11.0/homeassistant/components/homekit_controller/config_flow.py).

Pair Verify requires the controller identifier/private seed and accessory identifier/public key. It compares the accessory identifier exactly before validating its signature; normalising that protocol field would break legitimate exports. Controller public keys are derived locally and compared when supplied. [Pair Verify](https://github.com/Jc2k/aiohomekit/blob/4.0.1/aiohomekit/protocol/__init__.py#L469-L500).

`IpPairing.add_pairing` operates through an existing authenticated connection and accepts User/Admin permissions. `list_pairings` supplies the table for administrator validation and preservation checks. These primitives do not acquire initial Apple trust. New controllers here use User permission; recovery restores the pinned permission and never removes another row. [Pairing implementation](https://github.com/Jc2k/aiohomekit/blob/4.0.1/aiohomekit/controller/ip/pairing.py).

## Candidate protection-preserving extraction

[iPhone Backup Explorer V1.4.4](https://github.com/dripster82/IphoneBackupExplorer/releases/tag/V1.4.4) is a graphical macOS 14+ candidate. Source reads encrypted keychain records, exposes system items and exports selected rows. Printable textual payloads can survive; binary payloads become unusable placeholders. The author reports limited testing of additional viewers/exporters. [README](https://github.com/dripster82/IphoneBackupExplorer), [reader](https://github.com/dripster82/IphoneBackupExplorer/blob/V1.4.4/Sources/Services/KeychainStore.swift), [selection UI](https://github.com/dripster82/IphoneBackupExplorer/blob/V1.4.4/Sources/Views/KeychainListView.swift), [CSV export](https://github.com/dripster82/IphoneBackupExplorer/blob/V1.4.4/Sources/ViewModels/AppModel.swift#L810).

KeychainKit’s author documents HAP identity/accessory field mappings, but its known extraction procedure requires disabling SIP/AMFI. Those steps and its manual HA-storage recipe are excluded. The field documentation is evidence about an export shape, not permission to weaken protections. [Field mapping](https://github.com/pseudorandomuser/KeychainKit#extract-information-from-dump), [excluded procedure](https://github.com/pseudorandomuser/KeychainKit#disable-system-protections).

The candidate route has not been proved against a current real backup. Exact record presence, binary/text format and per-device identity matching remain empirical gates. `EXTRACTION.md` marks the guide accordingly. No claim is made that encrypted-backup extraction always works or is impossible.

## UI, lifecycle and final policy

`UI_RESEARCH.md` records the supported HTTP request context, panel, administrator gate, source-network helpers and awaited device-removal hook. Generic HA file upload lacks the required pre-body local/admin gate, so the implementation uses its own view and bounded memory grants. HA 2026.10 lacks the newer view body-limit convenience property; an independent streaming cap is used in both versions.

The final user-approved policy supersedes earlier migration/retention proposals: one integration entry, per-device recovery **on by default**, HTTP permitted without acknowledgement, automatic deletion of owned credentials/history, and no legacy compatibility or transitions. Native measurement references remain unchanged; a competing recovery engine is a conflict only. Source API facts do not constitute device verification or authorise live work.
