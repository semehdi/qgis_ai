"""Small helpers that keep the plugin working on both PyQt5 and PyQt6 builds."""

import json

from qgis.core import QgsSettings

SETTINGS_PREFIX = "qgis_ai_assistant/"


def qenum(owner, scope, name):
    """Return an enum member whether Qt exposes it scoped (Qt6) or flat (Qt5)."""
    scoped = getattr(owner, scope, None)
    if scoped is not None and hasattr(scoped, name):
        return getattr(scoped, name)
    return getattr(owner, name)


def setting(key, default=None, kind=None):
    value = QgsSettings().value(SETTINGS_PREFIX + key, default)
    if value is None:
        return default
    if kind is bool:
        if isinstance(value, str):
            return value.lower() in ("1", "true", "yes")
        return bool(value)
    if kind is int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return value


def set_setting(key, value):
    QgsSettings().setValue(SETTINGS_PREFIX + key, value)


def json_setting(key, default):
    raw = setting(key, "")
    if not raw:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def set_json_setting(key, value):
    set_setting(key, json.dumps(value))
