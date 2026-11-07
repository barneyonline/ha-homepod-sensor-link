# HomePod Sensor Link documentation

Discover HomePod temperature and humidity sensors, adopt existing native sensors, and manage dedicated sensor access with optional pairing recovery.

This is experimental development documentation for the public source repository. A separate GitHub Wiki publication, HACS listing and supported hardware verification are not claimed. Requires Home Assistant 2026.10.0 or newer; the tested matrix is 2026.10.0 and 2026.11.0.

## Common tasks

| Task | Guide |
|---|---|
| Install, select devices and complete setup | [Getting started](SETUP.md) |
| Prepare explicitly authorised controller credentials | [Credential extraction](EXTRACTION.md) |
| Understand sensors, diagnostics and actions | [Using the integration](USAGE.md) |
| Create alerts | [Automation examples](AUTOMATIONS.md) |
| Resolve pending setup or missing readings | [Troubleshooting](TROUBLESHOOTING.md) |
| Check supported boundaries | [Known limitations](LIMITATIONS.md) |
| Understand credential lifetime and removal | [Security](../SECURITY.md) |

## Development and evidence

- [Development environment](DEVELOPMENT.md) and [contribution guide](../CONTRIBUTING.md)
- [Architecture](ARCHITECTURE.md), [protocol research](RESEARCH.md) and [HA UI research](UI_RESEARCH.md)
- [Validation evidence](VALIDATION.md), [completion boundaries](COMPLETION.md) and [roadmap](ROADMAP.md)
- [Quality target and readiness](QUALITY.md), [quality research](QUALITY_RESEARCH.md) and [dependency ownership](DEPENDENCIES.md)
- [Repository alignment](REPOSITORY_STANDARDS.md) and [release notes](../CHANGELOG.md)

These pages also generate the local `wiki/` tree, including its navigation sidebar. `python3 scripts/build_wiki.py --check` detects drift. Publishing the wiki is a separate, authorised step; there is no automatic remote sync.
