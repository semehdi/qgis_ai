"""API key storage.

Keys go into the QGIS authentication database, encrypted with the QGIS
master password. If that is unavailable the user can opt into plain QGIS
settings. Environment variables (e.g. ANTHROPIC_API_KEY) are used when no
key has been saved.
"""

import os

from qgis.core import QgsApplication, QgsSettings

_AUTH_PREFIX = "qgis_ai_assistant/keys/"
_PLAIN_PREFIX = "qgis_ai_assistant/plain_keys/"


def saved_key(provider_id):
    am = QgsApplication.authManager()
    try:
        if am.existsAuthSetting(_AUTH_PREFIX + provider_id):
            value = am.authSetting(_AUTH_PREFIX + provider_id, "", True)
            if value:
                return str(value)
    except Exception:  # auth DB unavailable or master password refused
        pass
    return str(QgsSettings().value(_PLAIN_PREFIX + provider_id, "") or "")


def get_key(provider):
    key = saved_key(provider.id)
    if key:
        return key
    for env in provider.key_env:
        if os.environ.get(env):
            return os.environ[env]
    return ""


def store_key(provider_id, key, allow_plain=False):
    """Save a key. Returns "encrypted", "plain", "removed" or "failed"."""
    am = QgsApplication.authManager()
    try:
        if am.existsAuthSetting(_AUTH_PREFIX + provider_id):
            am.removeAuthSetting(_AUTH_PREFIX + provider_id)
    except Exception:
        pass
    QgsSettings().remove(_PLAIN_PREFIX + provider_id)
    if not key:
        return "removed"
    try:
        if am.storeAuthSetting(_AUTH_PREFIX + provider_id, key, True):
            return "encrypted"
    except Exception:
        pass
    if allow_plain:
        QgsSettings().setValue(_PLAIN_PREFIX + provider_id, key)
        return "plain"
    return "failed"
