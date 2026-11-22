"""Interface translations.

Each language is a JSON file in i18n/ mapping message ids to text; en.json is the
reference and the fallback for anything missing. The default language follows the
operating system ("auto"); users can pick another one in Settings.
"""

import json
import os

from qgis.PyQt.QtCore import QLocale

from .compat import setting

I18N_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "i18n")

# The languages QGIS itself ships, with their names written in that language.
LANGUAGES = [
    ("en", "English"), ("ar", "العربية"), ("az", "Azərbaycanca"), ("bg", "Български"),
    ("bs", "Bosanski"), ("ca", "Català"), ("cs", "Čeština"), ("da", "Dansk"),
    ("de", "Deutsch"), ("es", "Español"), ("et", "Eesti"), ("eu", "Euskara"),
    ("fi", "Suomi"), ("fr", "Français"), ("gl", "Galego"), ("hu", "Magyar"),
    ("is", "Íslenska"), ("it", "Italiano"), ("ja", "日本語"), ("ko", "한국어"),
    ("lt", "Lietuvių"), ("lv", "Latviešu"), ("nb", "Norsk bokmål"), ("nl", "Nederlands"),
    ("pl", "Polski"), ("pt_BR", "Português (Brasil)"), ("pt_PT", "Português (Portugal)"),
    ("ro", "Română"), ("ru", "Русский"), ("sc", "Sardu"), ("sk", "Slovenčina"),
    ("sv", "Svenska"), ("ta", "தமிழ்"), ("tr", "Türkçe"), ("uk", "Українська"),
    ("vi", "Tiếng Việt"), ("zh-Hans", "简体中文"), ("zh-Hant", "繁體中文"),
]
CODES = [code for code, _ in LANGUAGES]

_english = {}
_catalog = {}
_current = "en"


def _read(code):
    try:
        with open(os.path.join(I18N_DIR, code + ".json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def match_locale(name):
    """Map a locale name such as "fr_FR", "pt-BR", "zh-Hant-TW" or "nn_NO" to a language code."""
    name = (name or "").replace("-", "_")
    parts = name.split("_")
    lang = parts[0].lower()
    rest = [p.lower() for p in parts[1:]]
    if lang == "zh":
        traditional = "hant" in rest or any(r in ("tw", "hk", "mo") for r in rest)
        return "zh-Hant" if traditional else "zh-Hans"
    if lang == "pt":
        return "pt_PT" if "pt" in rest else "pt_BR"
    if lang in ("no", "nn", "nb"):
        return "nb"
    return lang if lang in CODES else "en"


def system_language():
    """The language the user's operating system interface is set to."""
    locale = QLocale.system()
    for name in list(locale.uiLanguages()) + [locale.name()]:
        code = match_locale(name)
        if code != "en" or name.lower().startswith("en"):
            return code
    return "en"


def load(code=None):
    """Load the chosen language ("auto" = the system language). Returns the code in use."""
    global _english, _catalog, _current
    choice = code or setting("language", "auto")
    _current = system_language() if choice == "auto" else (choice if choice in CODES else "en")
    _english = _read("en")
    _catalog = _read(_current) if _current != "en" else {}
    return _current


def current():
    return _current


def tr(msg_id):
    """Text for a message id in the current language, falling back to English."""
    if not _english:
        load()
    return _catalog.get(msg_id) or _english.get(msg_id) or msg_id
