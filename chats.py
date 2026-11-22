"""Chat storage: several conversations, saved as JSON in the QGIS profile folder."""

import json
import os
import time
import uuid

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import QStandardPaths

from .compat import qenum
from .i18n import tr



def _path():
    base = os.environ.get("QGIS_AI_ASSISTANT_DATA")  # override, used by tests
    if not base:
        profile = QgsApplication.qgisSettingsDirPath()  # the active QGIS user profile
        if not profile:
            profile = QStandardPaths.writableLocation(
                qenum(QStandardPaths, "StandardLocation", "AppDataLocation"))
        base = os.path.join(profile, "qgis_ai_assistant")
    return os.path.join(base, "chats.json")


def new_chat():
    now = time.time()
    return {"id": uuid.uuid4().hex, "title": tr("new_chat"), "auto_title": True,
            "created": now, "updated": now, "messages": []}


def auto_title(chat):
    """Name an untitled chat after its first message."""
    if not chat.get("auto_title"):
        return
    for m in chat["messages"]:
        if m["role"] == "user" and not m.get("auto"):
            text = " ".join(m["text"].split())
            chat["title"] = text[:45] + ("..." if len(text) > 45 else "")
            chat["auto_title"] = False
            return


def load():
    try:
        with open(_path(), encoding="utf-8") as fh:
            chats = json.load(fh).get("chats") or []
    except (OSError, ValueError):
        chats = []
    for chat in chats:
        # A reply cut off by closing QGIS mid-stream must not look in progress.
        for m in chat.get("messages", []):
            if m.pop("pending", False):
                m["notice"] = m.get("notice") or tr("interrupted")
    return chats


def save(chats):
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    stored = []
    for chat in chats:
        if not chat["messages"]:
            continue  # don't keep empty chats around
        c = dict(chat)
        # Map snapshots are large; keep only the marker that one was sent.
        c["messages"] = []
        for m in chat["messages"]:
            m2 = {k: v for k, v in m.items() if k != "image_b64"}
            if m.get("image_b64"):
                m2["had_image"] = True
            c["messages"].append(m2)
        stored.append(c)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "chats": stored}, fh, ensure_ascii=False)
    os.replace(tmp, path)
