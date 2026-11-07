"""Public Repairs API compatibility across the supported HA matrix."""

from __future__ import annotations
import inspect
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from .credentials import DOMAIN
from .models import Data

SUPPORTS_ENTRY_LINK = "config_entry_id" in inspect.signature(ir.async_create_issue).parameters


def create_issue(
    hass: HomeAssistant, issue_id: str, translation_key: str, entry_id: str, severity: ir.IssueSeverity
) -> None:
    parameters: Data = {"is_fixable": False, "severity": severity, "translation_key": translation_key}
    if SUPPORTS_ENTRY_LINK:
        parameters["config_entry_id"] = entry_id
    # HA 2026.10 lacks entry linkage; explicit resolution/removal cleanup applies
    # on both versions. No core storage or issue registry internals are edited.
    ir.async_create_issue(hass, DOMAIN, issue_id, **parameters)
