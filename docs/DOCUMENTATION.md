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

## Development

- [Development environment](DEVELOPMENT.md)
- [Architecture](ARCHITECTURE.md)
- [Repository standards](REPOSITORY_STANDARDS.md)

The [source repository](https://github.com/barneyonline/ha-homepod-sensor-link) retains the canonical documentation, technical evidence and changelog. For documentation updates, follow the [contribution guide](../CONTRIBUTING.md) and development guide. Wiki publication is explicit; CI does not write to the Wiki.
