"""Create entry-linked issues with Home Assistant's public Repairs API."""

from __future__ import annotations
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from .credentials import DOMAIN


def create_issue(
    hass: HomeAssistant, issue_id: str, translation_key: str, entry_id: str, severity: ir.IssueSeverity
) -> None:
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        config_entry_id=entry_id,
        is_fixable=False,
        severity=severity,
        translation_key=translation_key,
    )
