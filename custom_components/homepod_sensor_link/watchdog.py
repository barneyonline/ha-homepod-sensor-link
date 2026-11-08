"""Independent, notification-only supervision of completed device checks."""

from collections.abc import Callable
from datetime import datetime, timedelta

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval

from .credentials import DOMAIN
from .repairs import create_issue

INTERVAL = timedelta(minutes=5)
STALE_AFTER = 15 * 60
ISSUE_ID = "monitoring_stalled"


class HeartbeatWatchdog:
    """Keep supervision outside the coordinator's update task and locks."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry_id: str,
        devices: Callable[[], set[str]],
        active: Callable[[], bool],
    ) -> None:
        self.hass, self.entry_id = hass, entry_id
        self.devices, self.active = devices, active
        # Wall-clock adjustments and persisted timestamps cannot defeat grace
        # or freshness checks. Every load/resume gets a fresh monotonic baseline.
        self.clock = hass.loop.time
        self.progress: dict[str, float] = {}
        self.reported: set[str] = set()
        self._cancel: Callable[[], None] | None = None
        self.notification_id = DOMAIN + "_watchdog_" + entry_id

    @callback
    def start(self) -> None:
        if self._cancel is None:
            self.clear_alert()
            self._cancel = async_track_time_interval(self.hass, self.check, INTERVAL)
            self.check()

    @callback
    def completed(self, key: str) -> None:
        if key in self.progress:
            self.progress[key] = self.clock()
        self.check()

    @callback
    def check(self, _now: datetime | None = None) -> None:
        if self._cancel is None:
            return
        now = self.clock()
        devices = self.devices() if self.active() else set()
        self.progress = {key: self.progress.get(key, now) for key in devices}
        stalled = {key for key, completed in self.progress.items() if now - completed >= STALE_AFTER}
        if stalled == self.reported:
            return
        if stalled:
            create_issue(self.hass, ISSUE_ID, ISSUE_ID, self.entry_id, ir.IssueSeverity.WARNING)
            device_label = "HomePod" if len(stalled) == 1 else "HomePods"
            persistent_notification.async_create(
                self.hass,
                f"Sensor checks have not completed for at least 15 minutes for {len(stalled)} {device_label}. "
                "Review the Sensor access diagnostics and Repairs. No pairing changes were made by this alert.",
                title="HomePod Sensor Link monitoring has stalled",
                notification_id=self.notification_id,
            )
            self.reported = stalled
        else:
            self.clear_alert()

    @callback
    def clear_alert(self) -> None:
        ir.async_delete_issue(self.hass, DOMAIN, ISSUE_ID)
        persistent_notification.async_dismiss(self.hass, self.notification_id)
        self.reported = set()

    @callback
    def stop(self) -> None:
        if self._cancel is not None:
            self._cancel()
            self._cancel = None
        self.progress.clear()
        self.clear_alert()
