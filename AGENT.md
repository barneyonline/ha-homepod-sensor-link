# Agent instructions

## Scope and documentation

- Do not update `README.md` unless the user explicitly directs you to do so. Keep it unchanged during routine code, documentation, dependency and release work, including when other guidance calls for keeping documentation consistent.
- Read `CONTRIBUTING.md` before making changes; it defines development, validation, dependency and release requirements.
- User and developer guides live in the separate GitHub Wiki. Follow the documentation workflow in `CONTRIBUTING.md` when a task requires those changes; keep duplicate guides and generated Wiki snapshots out of this repository.
- Preserve unrelated local changes and keep each change focused on the requested task.

## Implementation and validation

- Read `SECURITY.md` before changing credential handling, discovery, pairing, recovery, storage or diagnostics. Preserve its identity, permission, persistence and privacy guarantees.
- Add meaningful behavioural tests for runtime changes and retain the coverage requirements in `CONTRIBUTING.md` without exclusions or suppressions.
- Run `sh scripts/validate.sh` before submitting changes. Use the focused checks documented in `CONTRIBUTING.md` while developing, including the synthetic macOS checks when changing the export helper.
- Report the checks actually run and any failures or unavailable prerequisites. Keep local validation, hosted CI, publication, installation and real-device verification distinct.

## Private data and live systems

- Use generated disposable identities and documentation-only endpoints in tests and examples. Keep real credentials, identifiers, addresses, exports, logs and private diagnostics out of source, artifacts and tool output.
- Ordinary development uses offline tests and synthetic fixtures. Live Home Assistant/HomePod access, real keychains or backups, signing and host security changes require separate explicit user direction and the applicable workflows in `CONTRIBUTING.md` and `SECURITY.md`.
