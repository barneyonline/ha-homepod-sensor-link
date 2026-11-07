# Explicit controller-key extraction guide

## Status and boundary

This is a **candidate workflow awaiting separately authorised real-backup and device verification**. The integration can parse the documented CSV/textual record shape and validate generated fixtures; that does not prove current HomeKit records are present/exportable from every iOS backup. Do not advertise a supported combination until it is tested.

A Mac prerequisite is accepted for the initial guide. HA itself may run on Home Assistant OS elsewhere; the HA import needs no SSH, shell commands or manual storage-file editing. This integration never extracts a keychain implicitly.

Apple documents HomeKit identity keys in encrypted keychain backups. HomeKit home-database backup behaviour is a separate subject. [Apple communication security](https://support.apple.com/en-ge/guide/security/sec3a881ccb1/web), [Apple home data](https://support.apple.com/en-ca/guide/security/sec49613249e/web).

## Candidate graphical route

The source-reviewed candidate is [iPhone Backup Explorer V1.4.4](https://github.com/dripster82/IphoneBackupExplorer/releases/tag/V1.4.4), requiring macOS 14+. The author documents local reading of known-password encrypted backups and selected-record export. Its additional viewers/exporters have limited device/iOS testing; this project has not installed or executed it against a real backup. [Author README](https://github.com/dripster82/IphoneBackupExplorer).

1. Use your own authorised, encrypted local iPhone backup and its known password. Keep the backup on your own protected machine. Do not upload the full backup or disclose its password to this integration.
2. Open the backup in the graphical application. Open **Passwords**, then **Options → Show system items**.
3. Search for `com.apple.hap.pairing`. Select only the identity and accessory records required for the intended HomePods.
4. Choose **Export Selected** and save the CSV locally. Do not export unrelated password groups or all keychain records.
5. In HA’s guided setup, open the local import page and choose that CSV. The integration derives controller public keys, validates key pairs and trusted pins, then performs bounded read-only matching to the selected HomePods before allowing provisioning.
6. After successful setup, remove unnecessary local export copies through your approved local process. Backups still contain secrets. Never attach exports to issues, diagnostics, screenshots or chat.

### Required record shape

- Identity `Account` → controller `iOSPairingId`.
- Identity `Password` → printable hexadecimal `iOSDeviceLTPK` followed by `iOSDeviceLTSK` (128 characters total).
- Accessory `Account` → exact `AccessoryPairingID`, including original spelling.
- Accessory `Password` → hexadecimal `AccessoryLTPK` (64 characters).
- `App / Group` must be `com.apple.hap.pairing`.

These mappings come from the [KeychainKit author’s record documentation](https://github.com/pseudorandomuser/KeychainKit#extract-information-from-dump). Different accessories can use different controller identities. The HA flow must authenticate and verify administrator permission; a structurally valid export alone proves neither.

The GUI’s [keychain reader](https://github.com/dripster82/IphoneBackupExplorer/blob/V1.4.4/Sources/Services/KeychainStore.swift) handles textual secrets but replaces non-printable binary payloads with an “N bytes (binary)” placeholder. Such a placeholder has lost the key and is rejected by this integration. Missing records, unsupported backup formats and non-textual current HomeKit payloads are unresolved compatibility boundaries—not reasons to guess keys or claim extraction succeeded.

## Excluded procedures

The known KeychainKit extraction procedure requires disabling SIP/AMFI and includes manual HA storage editing. Those steps conflict with this project’s security constraints and are not part of this guide. Apple’s generic Keychain Access export and ordinary HomeKit consent do not establish access to protected HAP Generic Password records. Do not disable OS protections, bypass certificate checks, jailbreak a device or modify native HA credential storage to complete onboarding.

If this candidate cannot export the required records intact, stop and report only application/OS versions, record counts and a controlled error code. Finding a different authorised, protection-preserving extraction route is a real prerequisite; the integration cannot manufacture an existing trusted administrator identity from a setup code or discovery advertisement.
