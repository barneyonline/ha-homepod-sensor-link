# Automation examples

Choose your entities with Home Assistant’s UI rather than copying household-specific IDs. No credentials or controller identifiers belong in an automation.

## Alert when checks stop

The optional [heartbeat blueprint](https://github.com/barneyonline/ha-homepod-sensor-link/blob/main/blueprints/automation/homepod_sensor_link/heartbeat-watchdog.yaml) lives in `blueprints/automation/homepod_sensor_link/heartbeat-watchdog.yaml`. Import it separately from the integration:

1. Open **Settings → Automations & scenes → Blueprints → Import blueprint**.
2. Paste the linked GitHub file URL, select **Preview**, then **Import blueprint**.
3. Select **HomePod Sensor Link heartbeat → Create automation**, choose this integration’s sensor-access diagnostic entities, and save.

[![Import the heartbeat blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Fbarneyonline%2Fha-homepod-sensor-link%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Fhomepod_sensor_link%2Fheartbeat-watchdog.yaml)

For manual installation, copy the file to `<config>/blueprints/automation/homepod_sensor_link/heartbeat-watchdog.yaml`, then reload automations. HA requires automation blueprints beneath `<config>/blueprints/automation/`; GitHub source folders are flexible. See [HA’s blueprint directory guide](https://www.home-assistant.io/docs/blueprint/tutorial/#creating-the-blueprint-file).

If you imported the earlier GitHub location, your installed copy and automation references remain local. Import the new URL and keep the existing local blueprint path when replacing it; do not move or delete the installed copy just to match this repository. Re-importing the earlier URL will no longer fetch updates. [HA’s re-import guidance](https://www.home-assistant.io/docs/automation/using_blueprints/#re-importing-a-blueprint).

The automation creates a persistent notification after no completed check has been reported for fifteen minutes, and dismisses it once checks resume.

The watchdog has a fifteen-minute automation startup grace. It does not perform pairing repair, restart HA or require a device/household mapping. A failed check still updates the heartbeat; use the diagnostic status to investigate access failures separately. See [using the integration](USAGE.md).

## Alert when a measurement is unavailable

In the automation editor, add a state trigger, select a temperature or humidity entity, set the destination state to `unavailable`, and set a duration of fifteen minutes. Add your preferred notification action. Use a separate recovery notification if you want to be told when a numeric state returns.

This catches unavailable measurements; it does not prove that a constant numeric value is fresh. Avoid automatic recovery loops built from entity availability. The integration already applies authenticated checks, startup grace and rolling repair limits.
