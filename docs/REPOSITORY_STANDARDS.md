# Alignment with the other integrations

Reviewed the other local integrations on 7 November 2026, then compared all 14 published [Enphase Energy workflows](https://github.com/barneyonline/ha-enphase-energy/tree/main/.github/workflows) on 8 November 2026. Project-specific vendor probes, live credentials and publishing operations are not copied.

| Reference pattern | HomePod Sensor Link implementation |
|---|---|
| Short feature/setup README with documentation navigation | README links to the published GitHub Wiki |
| Wiki Home, sidebar, setup, operation, troubleshooting and development pages | Published Wiki with versioned sources, portable snapshots and a separate native-link export; no implicit remote sync |
| Pinned Docker validation and explicit compatibility | Existing two HA versions retained; central tooling inventory; offline tests and read-only source mounts |
| Statement and branch coverage evidence | Exact 100% statements/branches per Python module, full credential-panel Node coverage and JSON/XML evidence |
| Consistent formatting and lint | Pinned Ruff correctness rules and formatter; shared editor settings |
| Translation/service and metadata checks | Repository contract tests verify English parity, actions, version/domain and packaging |
| Community contribution/support templates | Sanitised issue forms, pull-request checklist and changelog |
| Dependency and CI maintenance | Weekly Action proposals; scheduled/manual read-only checks, SHA-pinned external actions, actionlint and bounded jobs |
| Issue and PR maintenance | Trusted-main assignment, exact form-label reconciliation and privacy-focused evidence guidance; manual runs preview only |
| Release engineering | Scanned development artifacts, checksums, clean-source provenance and independent downloaded-archive verification; no tag/release/deployment publishing |
| Quality-scale evidence | All 54 rules with source/test/docs evidence, scoped exemptions and strict external readiness modes; no awarded-tier claim |

Complete integration coverage, strict typing and upstream hassfest are enforced locally and have passed on hosted GitHub Actions. Codecov has accepted the two explicit Python reports and shows 100% coverage. Remaining external gaps are HACS/brand acceptance, confirmed maintainership/private reporting, original development provenance and authorised hardware/extraction evidence. The readiness gates refuse missing evidence. The source is published on the existing public repository; private security reporting remains unconfigured.

Workflow policy follows [GitHub’s minimum-permission guidance](https://docs.github.com/en/actions/reference/security/secure-use). Home Assistant’s [lint/hassfest guidance](https://contributors.home-assistant.io/core/development/checks/) describes upstream validation; local repository contracts complement the pinned upstream hassfest run; that offline metadata check does not establish public project URLs or distribution acceptance.

## Workflow comparison

| Enphase workflow | HomePod Sensor Link equivalent |
|---|---|
| `tests.yml` | Existing `validate.yml`: complete two-version offline matrix, exact coverage, typing, Ruff and repository/privacy gates |
| `coverage-reporting.yml` | Existing isolated Codecov job in `validate.yml`; trusted-main upload retained without a privileged cross-workflow artifact consumer |
| `codespell.yml` | `codespell.yml`: pinned spelling checks, actionlint and nine metadata-automation behavior tests |
| `hassfest.yml` | `hassfest.yml`: current upstream metadata rules, independently of the pinned runtime gate |
| `validate.yaml` | `hacs.yml`: immutable upstream HACS validator on HA 2026.10.0, with read-only GitHub access and local brand assets |
| `quality-scale.yml` | `quality-scale.yml`: all quality-rule evidence audited; external todos remain visible |
| `auto_assign.yml` | Same filename: assign PRs and request maintainer review, avoiding self/draft/managed reviews |
| `auto_assign_new_issues.yml` | Same filename: idempotent maintainer assignment |
| `auto_label_issues.yml` | Same filename: reconcile only managed kind, area and diagnostic-status labels |
| `comment_missing_diagnostics.yml` | Same filename: sanitised-evidence guidance; update only the bot's own marked comment, preserve other comments |
| `release-candidate.yml` | Same filename: manual/reusable development package and provenance artifact from exact clean `main` |
| `release-assets.yml` | Same filename: build, download and independently verify development artifact bytes; no attachment to public releases |
| `firmware-catalog.yml` | Enphase-specific catalog scripts and branch publishing are not applicable |
| `service-status.yml` | Enphase cloud credentials, endpoint probes and automatic status/Wiki publishing are not applicable |

All 11 local workflows can be run manually on `main`. The four metadata workflows preview at most 100 open items, or a specified issue/PR; they perform no manual-run mutations. Event runs use the same tested policy and scoped metadata permissions. No dummy issues or public comments are needed to exercise manual workflows. HACS validation is not HACS listing acceptance; package artifacts are not published releases. Independent extraction/hardware/distribution evidence remains required.
