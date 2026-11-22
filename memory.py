"""Long-term memory of the user's preferences, kept on this computer and shared by all chats.

The model saves a preference by writing <remember>...</remember> in its answer (when the
user tells it to stop doing something, or keeps asking for the same thing), and drops one
with <forget>...</forget>. The plugin stores them in memory.json in the QGIS profile and
adds them to the system prompt of every chat. The tags are hidden from the chat view.
"""

import json
import os
import re
import time

from .chats import data_dir

MAX_ITEMS = 50
MAX_LEN = 300

REMEMBER = re.compile(r"<remember>(.*?)</remember>", re.S | re.I)
FORGET = re.compile(r"<forget>(.*?)</forget>", re.S | re.I)
_ANY_TAG = re.compile(r"[ \t]*<(remember|forget)>.*?</\1>[ \t]*\n?", re.S | re.I)
_OPEN_TAG = re.compile(r"<(remember|forget)>(?![\s\S]*</\1>)[\s\S]*$", re.I)  # still streaming

RULES = """Memory of this user's preferences:
- You have a long-term memory kept on the user's computer and shared by all their chats. Saved preferences are listed in <user_preferences>; follow them in every answer, without mentioning them unless asked.
- Save a lasting preference when the user tells you to stop doing something (for example something you keep doing), to always or never do something, or when you notice them asking for the same thing again and again (for example after each of your answers). Write it on its own line as <remember>one short, self-contained instruction to yourself, in the user's language</remember>. Save only how the user wants you to work, never data, file contents, keys or one-off task details, and never something already saved.
- When the user withdraws a saved preference, write <forget>the saved text, exactly as listed</forget>; to change one, forget the old text and remember the new one.
- These tags are hidden from the user; the plugin shows them what was saved and lets them undo it."""


def _path():
    return os.path.join(data_dir(), "memory.json")


def _clean(text):
    return " ".join((text or "").split())[:MAX_LEN]


def load():
    """Saved preferences, oldest first: a list of {"text", "created"}."""
    try:
        with open(_path(), encoding="utf-8") as fh:
            items = json.load(fh).get("items") or []
    except (OSError, ValueError, AttributeError):
        return []
    return [i for i in items if isinstance(i, dict) and _clean(i.get("text"))]


def save(items):
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "items": items[-MAX_ITEMS:]}, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _same(a, b):
    return _clean(a).casefold() == _clean(b).casefold()


def add(text):
    """Remember text; returns the stored text, or None if it was empty or already saved."""
    text = _clean(text)
    items = load()
    if not text or any(_same(i["text"], text) for i in items):
        return None
    items.append({"text": text, "created": time.time()})
    save(items)
    return text


def remove(text):
    """Forget text; returns True if something was removed."""
    items = load()
    kept = [i for i in items if not _same(i["text"], text)]
    if len(kept) == len(items):
        return False
    save(kept)
    return True


def apply_tags(answer):
    """Store the <remember>/<forget> tags of a model answer: returns (remembered, forgotten)."""
    forgotten = [_clean(t) for t in FORGET.findall(answer or "") if remove(t)]
    remembered = [t for t in (add(t) for t in REMEMBER.findall(answer or "")) if t]
    return remembered, forgotten


def strip_tags(text):
    """The answer as the user sees it: memory tags removed, even a half-streamed one."""
    return _OPEN_TAG.sub("", _ANY_TAG.sub("", text or "")).strip()


def prompt_section(learn=True):
    """The part of the system prompt about memory ('' when there is nothing to say)."""
    items = load()
    parts = [RULES] if learn else []
    if items:
        if not learn:
            parts.append("Follow these preferences of the user in every answer:")
        parts.append("<user_preferences>\n{}\n</user_preferences>".format(
            "\n".join("- " + _clean(i["text"]) for i in items)))
    return "\n\n".join(parts)
