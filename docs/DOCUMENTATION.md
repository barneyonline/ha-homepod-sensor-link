# HomePod Sensor Link documentation

Discover HomePod temperature and humidity sensors, adopt existing native sensors, and manage dedicated sensor access with optional pairing recovery.

This Wiki is the reader-facing documentation for the experimental community integration. A HACS listing and supported hardware verification are not claimed. Requires Home Assistant 2026.10.0 or newer; the tested matrix is 2026.10.0 and 2026.11.0.

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

The [source repository](https://github.com/barneyonline/ha-homepod-sensor-link) retains the canonical documentation and generated snapshots so changes can be reviewed and checked. For documentation updates, follow the contribution and development guides. Wiki publication is explicit; CI does not write to the Wiki.
