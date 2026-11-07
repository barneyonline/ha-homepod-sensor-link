# Changelog

## Unreleased — 2.0.0 development

- Introduce **HomePod Sensor Link**, domain `homepod_sensor_link`, intended repository `ha-homepod-sensor-link`.
- Add discovery, local administrator credential import, bounded identity matching and dedicated User-controller setup for new integration-owned measurements.
- Adopt existing native HomeKit sensors without changing their identity/history; preserve add-only pairing safety and the v1.1.1 aiohomekit cleanup classification fix.
- Default optional recovery on per device, support partial setup and restart/Resume, and automatically delete owned credentials/recovery history on removal.
- Use fresh configuration without legacy compatibility, credential/state migration or access to old helper storage.
- Enforce exact 100% Python statement/branch and credential-panel JavaScript coverage, strict typing, upstream hassfest, repository contracts and the full 54-rule quality evidence/readiness audit.
- Register translated actions independently of entry load, add same-pin reauthentication, dynamic selected-device entities, read-only startup preflight and persistent actionable Repairs.
- Preserve foreign entity/device links during removal and prevent incomplete storage or failed unloaded reauth from erasing pins or reporting success.
- Add formatting, community templates, deterministic packaging/checksums and generated local wiki documentation.
- Prepare public source publication on the existing GitHub history, retain GPL-3.0 and the prior MIT attribution notice, and include the selected theme-aware README banners and local integration branding.
- Align the README with the other integrations, add live repository/coverage badges, and isolate token-authenticated Codecov uploads to successful trusted-main runs with explicit Python reports.
- Publish the reader-facing documentation to GitHub Wiki, retain versioned source/snapshots, and export Wiki-native navigation without changing CI write permissions.

This entry records development, not a release. Extraction, hardware and supported-distribution gates remain open. Hosted results are available through GitHub Actions. The original development baseline `2d5e069` was not supplied; the existing GitHub initial commit is preserved without inventing the missing ancestry.
