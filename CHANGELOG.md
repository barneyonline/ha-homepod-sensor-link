# Changelog

## Unreleased — 2.0.0 development

- Serialize credential uploads per grant with cancellation-safe retries; clarify restart grace and preserve unconditional durable pairing checkpoints.
- Skip journal saves for administrator audits that are not due, copy only selected credential records, and publish fresh measurements to their device before the final health update.
- Clear removed-device notifications and listener bookkeeping, and support re-adding the same device without reloading healthy peers. Add deterministic 1/16/64-device publication and protected-storage benchmarks.
- Require Home Assistant 2026.11.0 or newer, use entry-linked Repairs directly, and align validation, coverage reporting and Marketplace documentation with that minimum.
- Introduce **HomePod Sensor Link**, domain `homepod_sensor_link`, intended repository `ha-homepod-sensor-link`.
- Add discovery, local administrator credential import, bounded identity matching and dedicated User-controller setup for new integration-owned measurements.
- Adopt existing native HomeKit sensors without changing their identity/history; preserve add-only pairing safety and the v1.1.1 aiohomekit cleanup classification fix.
- Default optional recovery on per device, support partial setup and restart/Resume, and automatically delete owned credentials/recovery history on removal.
- Use fresh configuration without legacy compatibility, credential/state migration or access to old helper storage.
- Enforce exact 100% Python statement/branch and credential-panel JavaScript coverage, strict typing, upstream hassfest, repository contracts and the full 54-rule quality evidence/readiness audit.
- Register translated actions independently of entry load, add same-pin reauthentication, dynamic selected-device entities, read-only startup preflight and persistent actionable Repairs.
- Preserve foreign entity/device links during removal and prevent incomplete storage or failed unloaded reauth from erasing pins or reporting success.
- Add formatting, community templates, deterministic packaging/checksums and Wiki documentation navigation.
- Prepare public source publication on the existing GitHub history, use GPL-3.0, and include the selected theme-aware README banners and local integration branding.
- Align the README with the other integrations, add live repository/coverage badges, and isolate token-authenticated Codecov uploads to successful trusted-main runs with explicit Python reports.
- Maintain reader-facing documentation in GitHub Wiki, with repository contribution/security guidance and read-only CI.
- Add page-specific light/dark Wiki banner pairs based on the approved README artwork, with automatic theme selection and retained artwork sources.
- Focus the Wiki on user and development guides, with contribution instructions, changelog and technical evidence maintained in the repository.
- Add the applicable Enphase-style HACS, current hassfest, spelling/actionlint, quality, metadata-maintenance and read-only development-artifact workflows; retain isolated coverage reporting and prohibit release/deployment publishing.
- Move the optional heartbeat watchdog to `blueprints/automation/homepod_sensor_link/`, add import metadata and a direct import link, and document updating existing local copies without changing automation references.
- Remove duplicate `wiki/` and top-level `docs/` pages and the Wiki generator; maintain pages in the separate Wiki, check declared documentation links and artwork, and preserve local code/test evidence and release gates.

This entry records development, not a release. Extraction, hardware and supported-distribution gates remain open. Hosted results are available through GitHub Actions. The original development baseline `2d5e069` was not supplied; the existing GitHub initial commit is preserved without inventing the missing ancestry.
