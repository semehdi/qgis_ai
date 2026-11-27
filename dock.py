"""The chat panel."""

import re
import time

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import QEvent, Qt, QTimer, pyqtSignal
from qgis.PyQt.QtGui import (QColor, QDesktopServices, QFontDatabase, QTextBlockFormat,
                             QTextCharFormat, QTextCursor, QTextFormat)
from qgis.PyQt.QtWidgets import (QApplication, QCheckBox, QComboBox, QCompleter,
                                 QDockWidget, QInputDialog, QHBoxLayout, QLabel, QMessageBox,
                                 QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy, QTextBrowser,
                                 QToolButton, QVBoxLayout, QWidget)

from . import chats, i18n, keystore, memory, theme
from .compat import json_setting, qenum, set_json_setting, set_setting, setting
from .context import build_context, map_snapshot, qgis_version
from .executor import CodeRunner
from .claude_path import find_claude
from .cli import ClaudeCliCall, build_cli_request
from .network import JsonCall, StreamingCall
from .i18n import tr
from .providers import (PROVIDERS, PROVIDERS_BY_ID, build_chat_request, build_models_request,
                        display_name, parse_models)
from .settings_dialog import DEFAULT_MAX_TOKENS, SettingsDialog, base_url

MAX_AGENT_STEPS = 10

AGENT_MODE = """How to work:
- You act directly on the user's QGIS session. When a request needs anything done or looked up in QGIS, do not tell the user how to do it and do not ask them to run code: reply with one ```python fenced block. The plugin executes it immediately and sends you its printed output, or the traceback, in an <execution_result> message.
- Work step by step: inspect first when unsure (print layer names, fields, CRS, feature counts), then act, then check the result. Use print() for everything you need to see; only printed output comes back to you.
- If a step fails, read the traceback and send corrected code. You have at most {steps} steps per request.
- When the task is done, reply with a short summary of what you did and the results, with no code block. Pure questions that need nothing from QGIS get a direct answer with no code."""

MANUAL_MODE = """How to help:
- When the task can be done in QGIS, give PyQGIS code the user can run with one click: put it in a single ```python fenced block."""

SYSTEM_PROMPT = """You are an expert GIS analyst and PyQGIS developer, working as an assistant panel inside QGIS {version}. The user is looking at their QGIS project while they talk to you.

{mode}

Rules:
- Be concise: short paragraphs and lists, no long preambles.
- Code runs in the QGIS Python environment, where `iface`, `processing`, and everything from `qgis.core`, `qgis.gui` and `qgis.PyQt` (QtCore, QtGui, QtWidgets) are already imported, and variables persist between runs. It runs on the main thread, so never call exec() on dialogs or block waiting for input.
- Pay close attention to CRS (coordinate reference system) misconfiguration: it is a frequent cause of layers not overlapping, appearing in the wrong place, or "disappearing" when zoomed to. Before combining, comparing or analysing layers, check each layer's CRS against the project CRS and against the others. Watch for signs of a wrongly assigned CRS (as opposed to a different one), such as degree-range coordinates (-180..180, -90..90) on a layer declared in a projected CRS, metre-range coordinates on a layer declared as EPSG:4326, or swapped latitude/longitude. Reproject with the processing algorithm "native:reprojectlayer" when layers are in different correct CRSs; only reassign a CRS (setCrs) when the declared one is wrong, and say so. Measure distances and areas in a suitable projected CRS, not in degrees. When you notice a CRS problem, tell the user, even if they did not ask about it.
- Always mind the difference between the project CRS and each layer's CRS, especially when adding a layer and zooming to it: a layer's extent is in the layer's CRS, so transform it to the project CRS (QgsCoordinateTransform) before setting the map extent, otherwise the map jumps to the wrong place (often the middle of the ocean). After adding a layer, print its CRS, the project CRS and the transformed extent to check it lands where expected.
- Write code for this QGIS version. Prefer `processing.run(...)` with existing algorithms over hand-written geometry loops. Refer to layers by the names or ids given in the project context, and never invent layers or fields that are not there; if you need something that is missing, say so.
- The code acts on the user's real project. Do not delete, overwrite or edit existing data unless the user asked for it; write results to new layers ('TEMPORARY_OUTPUT' or memory layers) and add them to the project. End the code with a print() summarising what it did.
- When code fails, find and fix the cause; do not hide errors with try/except.
- Long work (large layers, downloads, heavy analysis): use Processing algorithms through processing.run(...). The plugin runs them in the background, so QGIS stays usable and the user can stop them; plain Python loops are slower. Download files with "native:filedownloader", not urllib or requests, which freeze QGIS. With remote services (WFS, WMS, ArcGIS REST), request only what you need (a filter or bounding box in the request), and copy a remote layer to a local one with a Processing algorithm (for example "native:savefeatures" to 'TEMPORARY_OUTPUT') before querying or analysing it.
- When present, a <qgis_context> block at the start of a user message is a snapshot of the project taken when that message was sent; a map image, when attached, shows the current map view.{memory}"""

MODE_UPDATE = """Update from the plugin: the user changed the Auto-run setting. This replaces the earlier "How to work" / "How to help" instructions:

{}"""

MEMORY_UPDATE = """Update from the plugin: the user's memory changed. This replaces the earlier memory instructions and <user_preferences> list:

{}"""

NO_MEMORY = "No preferences are saved and learning is off: ignore the earlier memory instructions."


WELCOME_EXAMPLES = 4  # welcome_ex1..4: requests that need none of the user's data


def welcome():
    # Each example is a link: clicking it fills the input box (see ChatDock._use_example).
    examples = "\n".join(
        "- [*{}*](qgisai:example/{})".format(
            tr("welcome_ex{}".format(i)).replace("[", "\\[").replace("]", "\\]"), i)
        for i in range(1, WELCOME_EXAMPLES + 1))
    return "### {}\n\n{}\n\n{}\n\n{}".format(
        tr("app_name"), tr("welcome_intro"), examples, tr("welcome_auto"))

FENCE = re.compile(r"```([\w+.-]*)[^\n]*\n(.*?)```", re.S)
RUNNABLE = {"python", "py", "python3", "pyqgis"}


_FENCE_LINE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+)(.+?)(?<!`)\1(?!`)")
_TAG_START = re.compile(r"<(?=[A-Za-z/!?])")


def _neutralize_html(markdown):
    """Show HTML-looking tags as text. Qt's Markdown importer passes raw HTML to its HTML
    parser, and a stray tag on its own line (models sometimes emit "<br>" or
    "<qgis_context>") breaks the rest of the document: all later text vanishes, only code
    stays. Code blocks and inline code are left as they are."""
    out, fence = [], None
    for line in markdown.split("\n"):
        m = _FENCE_LINE.match(line)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) \
                    and not line.strip().strip(fence[0]):
                fence = None
            out.append(line)
            continue
        if m:
            fence = m.group(1)
            out.append(line)
            continue
        pieces, last = [], 0
        for code in _INLINE_CODE.finditer(line):
            pieces.append(_TAG_START.sub(r"\\<", line[last:code.start()]))
            pieces.append(code.group(0))
            last = code.end()
        pieces.append(_TAG_START.sub(r"\\<", line[last:]))
        out.append("".join(pieces))
    return "\n".join(out)


def _fence(text):
    """A fence longer than any backtick run inside the text."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


HISTORY_SIZE = 200
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _clock(seconds):
    seconds = int(seconds)
    return "{}:{:02d}".format(seconds // 60, seconds % 60)



def _ival(enum_value):
    """Integer value of a Qt enum member (PyQt5 enums are ints, PyQt6 enums are not)."""
    return int(getattr(enum_value, "value", enum_value))


class PromptEdit(QPlainTextEdit):
    """Prompt box: Enter sends, Shift+Enter adds a line, Up/Down recall past prompts like a shell."""

    submitted = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.history = [h for h in json_setting("prompt_history", []) if isinstance(h, str)]
        self._pos = None  # index into history while browsing; None = editing a new prompt
        self._draft = ""

    def add_history(self, text):
        if text and (not self.history or self.history[-1] != text):
            self.history.append(text)
            del self.history[:-HISTORY_SIZE]
            set_json_setting("prompt_history", self.history)
        self._pos = None
        self._draft = ""

    def _cursor_line(self):
        """(is on first visual line, is on last visual line) of the whole text."""
        cursor = self.textCursor()
        block = cursor.block()
        layout = block.layout()
        line = layout.lineForTextPosition(cursor.positionInBlock()).lineNumber() if layout else 0
        first = block == self.document().firstBlock() and line <= 0
        last = block == self.document().lastBlock() and (layout is None or line >= layout.lineCount() - 1)
        return first, last

    def _recall(self, step):
        if not self.history:
            return
        if self._pos is None:
            if step > 0:
                return
            self._draft = self.toPlainText()
            self._pos = len(self.history)
        self._pos = max(0, self._pos + step)
        if self._pos >= len(self.history):
            self._pos = None
            self.setPlainText(self._draft)
        else:
            self.setPlainText(self.history[self._pos])
        self.moveCursor(qenum(QTextCursor, "MoveOperation", "End"))

    def keyPressEvent(self, event):
        # event.key() is an int; compare against int values so this also works on PyQt6,
        # where Qt.Key members are not ints.
        key = event.key()
        key_return, key_enter, key_up, key_down = (
            _ival(qenum(Qt, "Key", name)) for name in ("Key_Return", "Key_Enter", "Key_Up", "Key_Down"))
        shift = qenum(Qt, "KeyboardModifier", "ShiftModifier")
        if key in (key_return, key_enter) and not (event.modifiers() & shift):
            self.submitted.emit()
            return
        # macOS (and numpad keys everywhere) add KeypadModifier to arrows, so only real
        # modifiers turn history off.
        real_mods = (shift | qenum(Qt, "KeyboardModifier", "ControlModifier")
                     | qenum(Qt, "KeyboardModifier", "AltModifier")
                     | qenum(Qt, "KeyboardModifier", "MetaModifier"))
        if key in (key_up, key_down) and not (event.modifiers() & real_mods):
            first, last = self._cursor_line()
            if key == key_up and first:
                self._recall(-1)
                return
            if key == key_down and last and self._pos is not None:
                self._recall(1)
                return
        super().keyPressEvent(event)


class ChatDock(QDockWidget):
    language_changed = pyqtSignal()

    def __init__(self, iface, parent=None):
        super().__init__(tr("app_name"), parent)
        self.setObjectName("QgisAiAssistantDock")
        self.iface = iface
        self.chats = chats.load() or [chats.new_chat()]
        self.chats.sort(key=lambda c: c["created"], reverse=True)
        self.current = self.chats[0]
        self._runners = {}  # chat id -> CodeRunner, so each chat keeps its own variables
        self.code_blocks = []
        self.call = None
        self.models_call = None
        self._agent_steps = 0
        self._active_runner = None  # CodeRunner currently running code, if any
        # What the assistant is doing, shown live in the status line and the busy bar.
        self._task_started = None  # time the current task (all its steps) started
        self._phase_label = ""
        self._phase_started = 0.0
        self._spin = 0
        self._activity_timer = QTimer(self)
        self._activity_timer.setInterval(100)
        self._activity_timer.timeout.connect(self._tick)
        self._render_cost = 0.0  # seconds the last full redraw took
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(60)
        self._render_timer.timeout.connect(self._render)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(800)
        self._save_timer.timeout.connect(self._save_chats)
        self._build_ui()
        self._load_providers()
        self._fill_chat_combo()
        self._render()

    # ------------------------------------------------------------------ chats

    @property
    def messages(self):
        return self.current["messages"]

    @messages.setter
    def messages(self, value):
        self.current["messages"] = value

    @property
    def runner(self):
        cid = self.current["id"]
        if cid not in self._runners:
            self._runners[cid] = CodeRunner(self.iface)
        return self._runners[cid]

    def _fill_chat_combo(self):
        self.chat_combo.blockSignals(True)
        self.chat_combo.clear()
        for c in self.chats:
            self.chat_combo.addItem(c["title"], c["id"])
        self.chat_combo.setCurrentIndex(self.chat_combo.findData(self.current["id"]))
        self.chat_combo.blockSignals(False)

    def _on_chat_selected(self, index):
        cid = self.chat_combo.itemData(index)
        chat = next((c for c in self.chats if c["id"] == cid), None)
        if chat is None or chat is self.current:
            return
        self._drop_if_empty(self.current)
        self.current = chat
        self._agent_steps = 0
        self._fill_chat_combo()
        self._render(force_bottom=True)

    def _drop_if_empty(self, chat):
        if not chat["messages"] and len(self.chats) > 1:
            self.chats = [c for c in self.chats if c is not chat]
            self._runners.pop(chat["id"], None)

    def new_chat(self):
        if self.call is not None:
            return
        if self.messages:
            self.current = chats.new_chat()
            self.chats.insert(0, self.current)
        self._agent_steps = 0
        self._fill_chat_combo()
        self._render()
        self.input.setFocus()

    def rename_chat(self):
        title, ok = QInputDialog.getText(self, tr("rename_chat"), tr("chat_name"),
                                         text=self.current["title"])
        if ok and title.strip():
            self.current["title"] = title.strip()
            self.current["auto_title"] = False
            self._fill_chat_combo()
            self._save_chats()

    def delete_chat(self):
        if self.call is not None:
            return
        yes = qenum(QMessageBox, "StandardButton", "Yes")
        no = qenum(QMessageBox, "StandardButton", "No")
        if self.messages and QMessageBox.question(
                self, tr("delete_chat"), tr("delete_chat_confirm").format(self.current["title"]),
                yes | no, no) != yes:
            return
        self._runners.pop(self.current["id"], None)
        self.chats = [c for c in self.chats if c is not self.current]
        if not self.chats:
            self.chats = [chats.new_chat()]
        self.current = self.chats[0]
        self._fill_chat_combo()
        self._render(force_bottom=True)
        self._save_chats()

    def _save_chats(self):
        try:
            chats.save(self.chats)
        except OSError as e:
            self._set_status(tr("save_chats_failed").format(e))

    def _set_busy(self, busy):
        """Chats can't be switched while an answer or agent step is in progress."""
        for w in (self.chat_combo, self.new_btn, self.delete_btn, self.settings_btn):
            w.setEnabled(not busy)

    @property
    def running_code(self):
        return self._active_runner is not None

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(4, 4, 4, 4)

        top = QHBoxLayout()
        self.provider_combo = QComboBox()
        self.provider_combo.setToolTip(tr("provider_tip"))
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setInsertPolicy(qenum(QComboBox, "InsertPolicy", "NoInsert"))
        self.model_combo.setToolTip(tr("model_tip"))
        self.model_combo.setSizePolicy(qenum(QSizePolicy, "Policy", "Expanding"),
                                       qenum(QSizePolicy, "Policy", "Fixed"))
        self.model_combo.setMinimumContentsLength(12)
        completer = self.model_combo.completer()
        if completer is not None:
            completer.setFilterMode(qenum(Qt, "MatchFlag", "MatchContains"))
            completer.setCompletionMode(qenum(QCompleter, "CompletionMode", "PopupCompletion"))
        refresh_btn = self._tool_button("/mActionRefresh.svg", tr("refresh_tip"),
                                        self.refresh_models)
        self.settings_btn = settings_btn = self._tool_button(
            "/mActionOptions.svg", tr("settings_tip"), self.open_settings)
        top.addWidget(self.provider_combo)
        top.addWidget(self.model_combo, 1)
        for b in (refresh_btn, settings_btn):
            top.addWidget(b)
        layout.addLayout(top)

        chat_row = QHBoxLayout()
        self.chat_combo = QComboBox()
        self.chat_combo.setToolTip(tr("chats_tip"))
        self.chat_combo.setSizePolicy(qenum(QSizePolicy, "Policy", "Expanding"),
                                      qenum(QSizePolicy, "Policy", "Fixed"))
        self.chat_combo.setMinimumContentsLength(12)
        self.chat_combo.currentIndexChanged.connect(self._on_chat_selected)
        self.new_btn = self._tool_button("/mActionAdd.svg", tr("new_chat"), self.new_chat)
        rename_btn = self._tool_button("/mActionToggleEditing.svg", tr("rename_chat"), self.rename_chat)
        self.delete_btn = self._tool_button("/mActionDeleteSelected.svg", tr("delete_chat"),
                                            self.delete_chat)
        chat_row.addWidget(self.chat_combo, 1)
        for b in (self.new_btn, rename_btn, self.delete_btn):
            chat_row.addWidget(b)
        layout.addLayout(chat_row)

        self.browser = QTextBrowser()
        self.browser.setOpenLinks(False)
        self.browser.anchorClicked.connect(self._on_link)
        layout.addWidget(self.browser, 1)
        self.busy_bar = QProgressBar()
        self.busy_bar.setRange(0, 0)  # indeterminate: an endless moving animation
        self.busy_bar.setTextVisible(False)
        self.busy_bar.setFixedHeight(4)
        self.busy_bar.hide()
        layout.addWidget(self.busy_bar)

        opts = QHBoxLayout()
        self.ctx_check = QCheckBox(tr("project_context"))
        self.ctx_check.setToolTip(tr("project_context_tip"))
        self.ctx_check.setChecked(setting("include_context", True, bool))
        self.ctx_check.toggled.connect(lambda v: set_setting("include_context", v))
        self.map_check = QCheckBox(tr("map_snapshot"))
        self.map_check.setToolTip(tr("map_snapshot_tip"))
        self.auto_check = QCheckBox(tr("auto_run"))
        self.auto_check.setToolTip(tr("auto_run_tip"))
        self.auto_check.setChecked(setting("auto_run", True, bool))
        self.auto_check.toggled.connect(lambda v: set_setting("auto_run", v))
        self.status = QLabel("")
        opts.addWidget(self.auto_check)
        opts.addWidget(self.ctx_check)
        opts.addWidget(self.map_check)
        opts.addStretch(1)
        # What the assistant is doing, on its own full-width line under the busy bar.
        layout.addWidget(self.status)
        layout.addLayout(opts)

        bottom = QHBoxLayout()
        self.input = PromptEdit()
        self.input.setPlaceholderText(tr("input_placeholder"))
        self.input.setMaximumHeight(90)
        self.input.submitted.connect(self.send)
        self.send_btn = QPushButton(tr("send"))
        self.send_btn.clicked.connect(self._send_or_stop)
        bottom.addWidget(self.input, 1)
        bottom.addWidget(self.send_btn)
        layout.addLayout(bottom)

        self.setWidget(root)
        self._apply_colors()

    def _apply_colors(self):
        """Give the panel colours that contrast, from the current theme. Runs again
        whenever the theme or the OS appearance changes."""
        app_palette = QApplication.palette()
        self._palette_sig = theme.signature(app_palette)
        self.widget().setPalette(theme.fixed_palette(app_palette))
        self._colors = bg, text, link, muted = theme.chat_colors(app_palette)
        # Stylesheets, not palettes: QGIS themes paint these backgrounds with their own
        # stylesheet, which a palette can't override.
        self.browser.setStyleSheet("QTextBrowser {{ background-color: {}; color: {}; }}".format(
            bg.name(), text.name()))
        self.input.setStyleSheet(
            "QPlainTextEdit {{ background-color: {}; color: {}; border: 1px solid {};"
            " border-radius: 3px; }}".format(bg.name(), text.name(), muted.name()))
        # The status line keeps the theme's own label colour (like the check boxes next
        # to it), in italics to stay discreet.
        self.status.setStyleSheet("font-style: italic;")
        self.busy_bar.setStyleSheet(
            "QProgressBar {{ border: none; background-color: {}; }}"
            " QProgressBar::chunk {{ background-color: {}; }}".format(muted.name(), link.name()))

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() in (qenum(QEvent, "Type", "PaletteChange"),
                            qenum(QEvent, "Type", "ApplicationPaletteChange"),
                            qenum(QEvent, "Type", "StyleChange")):
            if not getattr(self, "_recolor_pending", False):
                self._recolor_pending = True
                QTimer.singleShot(0, self._recolor)

    def _recolor(self):
        self._recolor_pending = False
        if theme.signature(QApplication.palette()) != self._palette_sig:
            self._apply_colors()
            self._render()

    def _tool_button(self, icon, tip, slot):
        b = QToolButton()
        b.setIcon(QgsApplication.getThemeIcon(icon))
        b.setToolTip(tip)
        b.setAutoRaise(True)
        b.clicked.connect(slot)
        return b

    # ------------------------------------------------------------------ providers & models

    @property
    def provider(self):
        return PROVIDERS_BY_ID[self.provider_combo.currentData()]

    def _load_providers(self):
        for p in PROVIDERS:
            self.provider_combo.addItem(display_name(p), p.id)
        idx = self.provider_combo.findData(setting("provider", "anthropic"))
        self.provider_combo.setCurrentIndex(max(idx, 0))
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        self.model_combo.currentTextChanged.connect(self._on_model_changed)
        self._on_provider_changed()

    def _on_provider_changed(self):
        p = self.provider
        set_setting("provider", p.id)
        cached = json_setting("models/" + p.id, None)
        self._fill_models([m["id"] for m in cached] if cached else p.models)
        if not cached and (keystore.get_key(p) or not p.needs_key) and p.id not in ("custom",):
            self.refresh_models(quiet=True)

    def _fill_models(self, ids):
        p = self.provider
        wanted = setting("model/" + p.id, "") or (ids[0] if ids else "")
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        self.model_combo.addItems(ids)
        if wanted and self.model_combo.findText(wanted) < 0:
            self.model_combo.insertItem(0, wanted)
        self.model_combo.setCurrentText(wanted)
        self.model_combo.blockSignals(False)

    def _on_model_changed(self, text):
        if text.strip():
            set_setting("model/" + self.provider.id, text.strip())

    def refresh_models(self, quiet=False):
        p = self.provider
        if p.kind == "claude_cli":  # no model list to fetch: the CLI takes aliases or full ids
            self._fill_models(p.models)
            return
        key = keystore.get_key(p)
        if p.needs_key and not key and p.id != "openrouter":
            if not quiet:
                self._set_status(tr("add_key_first").format(display_name(p)))
            return
        url, headers = build_models_request(p, base_url(p), key)
        call = JsonCall(url, headers, self)
        self.models_call = call

        def done(data, pid=p.id):
            provider = PROVIDERS_BY_ID[pid]
            models = parse_models(provider, data)
            if not models:
                self._set_status(tr("no_models"))
                return
            set_json_setting("models/" + pid, models)
            if self.provider.id == pid:
                self._fill_models([m["id"] for m in models])
            self._set_status(tr("n_models").format(len(models)))

        def failed(err):
            if not quiet:
                self._set_status(tr("models_failed"))
                self._add_note("⚠ " + tr("models_failed_note").format(display_name(p), err))

        call.done.connect(done)
        call.failed.connect(failed)
        self._set_status(tr("fetching_models"))
        call.start()

    def _max_tokens(self, model):
        limit = setting("max_tokens", DEFAULT_MAX_TOKENS, int)
        for m in json_setting("models/" + self.provider.id, []) or []:
            if m.get("id") == model and m.get("max_tokens"):
                return min(limit, int(m["max_tokens"]))
        return limit

    # ------------------------------------------------------------------ chat

    def _send_or_stop(self):
        if self._active_runner is not None:
            self._active_runner.stop()
        elif self.call is not None:
            self.call.cancel()
        else:
            self.send()

    def send(self, text=None):
        if self.call is not None or self.running_code:
            return
        typed = text is None
        text = (self.input.toPlainText() if typed else text).strip()
        if not text:
            return
        p = self.provider
        key = keystore.get_key(p)
        if p.needs_key and not key:
            self._add_note("🔑 " + tr("no_key_note").format("**{}**".format(display_name(p))))
            self.open_settings()
            return
        model = self.model_combo.currentText().strip()
        if not model:
            self._add_note(tr("choose_model"))
            return
        if p.kind == "claude_cli" and not find_claude(setting("base_url/" + p.id, "")):
            self._add_note("⚠ " + tr("cli_not_found"))
            return

        user = {"role": "user", "text": text}
        try:
            if self.ctx_check.isChecked():
                user["context"] = build_context(self.iface)
            if self.map_check.isChecked():
                user["image_b64"], user["image_mime"] = map_snapshot(self.iface)
        except Exception as e:  # never lose the user's message over context capture
            self._add_note("⚠ " + tr("context_failed").format(e))
        if typed:
            self.input.add_history(text)
            self.input.clear()
        self.messages.append(user)
        self.current["updated"] = time.time()
        if self.current.get("auto_title"):
            chats.auto_title(self.current)
            self._fill_chat_combo()
        self._agent_steps = 0
        self._start_request()

    def _start_request(self):
        """Ask the current model to continue the conversation in self.messages."""
        p = self.provider
        key = keystore.get_key(p)
        model = self.model_combo.currentText().strip()
        reply = {"role": "assistant", "text": "", "model": model, "provider": p.id,
                 "provider_kind": p.kind, "pending": True}
        frozen, current = self._system_prompts()
        system = frozen if p.kind == "anthropic" else current
        if p.kind == "claude_cli":
            exe = find_claude(setting("base_url/" + p.id, ""))
            if not exe:
                self._agent_steps = 0
                self._end_task(tr("stopped"))
                self._add_note("⚠ " + tr("cli_not_found"))
                return
            args, stdin, system_file, parser = build_cli_request(model, system, self.messages)
            call = ClaudeCliCall(exe, args, stdin, system_file, parser, self)
        else:
            url, headers, body, parser = build_chat_request(
                p, base_url(p), key, model, system, self.messages, self._max_tokens(model))
            call = StreamingCall(url, headers, body, parser, self)
        self.messages.append(reply)

        call.delta.connect(lambda t, r=reply: self._on_delta(r, t))
        call.done.connect(lambda res, r=reply: self._on_done(r, res))
        call.failed.connect(lambda err, r=reply: self._on_failed(r, err))
        self.call = call
        call.phase.connect(lambda ph, r=reply: self._on_phase(r, ph))
        self._set_busy(True)
        self.send_btn.setText(tr("stop"))
        self._begin_task()
        self._set_phase(tr("phase_connecting").format(model), reply)
        self._render(force_bottom=True)
        call.start()

    # ------------------------------------------------------------------ activity display

    def _begin_task(self):
        if self._task_started is None:
            self._task_started = time.monotonic()
        self.busy_bar.show()
        if not self._activity_timer.isActive():
            self._activity_timer.start()

    def _end_task(self, message=""):
        """Nothing more is coming: stop the animation and say how it ended."""
        if self._task_started is not None and not message:
            message = "✓ " + tr("task_done").format(_clock(time.monotonic() - self._task_started))
        self._task_started = None
        self._activity_timer.stop()
        self.busy_bar.hide()
        self._phase_label = ""
        self._set_status(message)

    def _set_phase(self, label, reply=None):
        """Show what the assistant is doing now (also inside the pending reply)."""
        self._phase_label = label
        self._phase_started = time.monotonic()
        if reply is not None:
            reply["phase"] = label
        self._tick()

    def _on_phase(self, reply, phase):
        model = reply["model"]
        label = tr("thinking").format(model) if phase == "thinking" else tr("phase_writing").format(model)
        self._set_phase(label, reply)
        if reply.get("pending") and not reply["text"]:
            self._render()

    def _tick(self):
        if self._task_started is None or not self._phase_label:
            return
        self._spin = (self._spin + 1) % len(SPINNER)
        text = "{} {} {}".format(SPINNER[self._spin], self._phase_label,
                                 _clock(time.monotonic() - self._phase_started))
        progress = self._active_runner.progress() if self._active_runner is not None else None
        if progress:
            text += " · {:.0f} %".format(progress)
        self._set_status(text)

    def _system_prompts(self):
        """(system prompt frozen for this chat, up-to-date system prompt).

        Claude gets the frozen one: changing the system prompt mid-chat would invalidate
        its earlier reasoning and the prompt cache. Changes made since (Auto-run toggled,
        a preference remembered or deleted) are appended to the chat as "system" updates
        instead. Other providers simply get the up-to-date prompt."""
        mode = AGENT_MODE.format(steps=MAX_AGENT_STEPS) if self.auto_check.isChecked() else MANUAL_MODE
        mem = memory.prompt_section(learn=setting("memory_learn", True, bool))
        current = SYSTEM_PROMPT.format(version=qgis_version(), mode=mode,
                                       memory="\n\n" + mem if mem else "")
        chat = self.current
        told = chat.get("told")
        if not chat.get("system") or not isinstance(told, dict):
            chat["system"], chat["told"] = current, {"mode": mode, "memory": mem}
        else:
            updates = []
            if told.get("mode") != mode:
                updates.append(MODE_UPDATE.format(mode))
            if told.get("memory") != mem:
                updates.append(MEMORY_UPDATE.format(mem or NO_MEMORY))
            if updates:
                told.update(mode=mode, memory=mem)
                # Right after the user turn being answered, so the history stays append-only.
                self.messages.append({"role": "system", "text": "\n\n".join(updates)})
        return chat["system"], current

    def _on_delta(self, reply, text):
        reply["text"] += text
        if not self._render_timer.isActive():
            # Redraw at most every 60 ms, and leave QGIS at least twice the redraw time
            # in between, so long chats never saturate the interface while streaming.
            self._render_timer.start(max(60, int(3000 * self._render_cost)))

    def _finish_call(self):
        self.call = None
        self.send_btn.setText(tr("send"))
        self._set_busy(False)

    def _on_done(self, reply, result):
        self._finish_call()
        reply["pending"] = False
        reply["text"] = result["text"] or reply["text"]
        reply["provider_blocks"] = result.get("blocks")
        served = result.get("served_model")
        if served and served != reply["model"] and not served.startswith(reply["model"]):
            reply["served_model"] = served
        if result.get("refused"):
            reply["exclude"] = True
        if result.get("notice"):
            reply["notice"] = result["notice"]
        if result.get("cancelled"):
            reply["notice"] = tr("stopped")
            if not reply["text"]:
                reply["exclude"] = True
        elif not reply["text"] and not reply.get("notice"):
            reply["notice"] = tr("no_text")
        if not reply.get("exclude") and not result.get("cancelled"):
            self._apply_memory(reply)
        reply.pop("phase", None)
        self._render()
        if (self.auto_check.isChecked() and not result.get("cancelled")
                and not reply.get("exclude") and self._runnable_blocks(reply["text"])):
            # The task goes on: say so right away, so it never looks finished.
            self._set_phase(tr("running_step").format(self._agent_steps + 1))
            QTimer.singleShot(0, lambda r=reply: self._agent_step(r))
        elif result.get("cancelled"):
            self._end_task(tr("stopped"))
        else:
            self._end_task()

    def _apply_memory(self, reply):
        """Save the preferences the model chose to remember or forget in this answer."""
        if not setting("memory_learn", True, bool):
            return
        try:
            saved, forgot = memory.apply_tags(reply["text"])
        except OSError as e:
            self._set_status(tr("memory_save_failed").format(e))
            return
        if saved:
            reply["memory_saved"] = saved
        if forgot:
            reply["memory_forgot"] = forgot

    def _undo_memory(self, arg):
        """Undo link under an answer: forget a preference it had just saved."""
        msg, _, n = arg.partition("-")
        try:
            m, n = self.messages[int(msg)], int(n)
            text = m["memory_saved"][n]
        except (ValueError, IndexError, KeyError, TypeError):
            return
        try:
            memory.remove(text)
        except OSError as e:
            self._set_status(tr("memory_save_failed").format(e))
            return
        m.setdefault("memory_undone", []).append(n)
        self._render()

    # ------------------------------------------------------------------ agent loop

    @staticmethod
    def _runnable_blocks(text):
        return [m.group(2) for m in FENCE.finditer(text or "") if m.group(1).lower() in RUNNABLE]

    def _agent_step(self, reply):
        """Run the code the model just wrote and send it the result."""
        if (self.call is not None or self.running_code or not self.messages
                or self.messages[-1] is not reply):
            return  # the user started something else meanwhile
        if self._agent_steps >= MAX_AGENT_STEPS:
            self._end_task(" ")
            self._add_note("⏸ " + tr("max_steps").format(MAX_AGENT_STEPS))
            return
        self._agent_steps += 1
        step = self._agent_steps
        outputs, ok_all = [], True
        for code in self._runnable_blocks(reply["text"]):
            ok, out, err = self._execute(code, tr("running_step").format(step))
            if self.runner.stopped:
                # Stop ends the whole task: nothing is sent back to the model.
                self._agent_steps = 0
                self._end_task(tr("stopped"))
                self._add_note("⏹ " + tr("code_stopped"))
                return
            outputs.append((ok, out, err))
            if not ok:
                ok_all = False
                break

        result_text = "\n\n".join(
            "status: {}\n{}".format("success" if ok else "error",
                                    (out.rstrip() or "(no output)") + ("\n" + err.rstrip() if err else ""))
            for ok, out, err in outputs)
        self.messages.append({
            "role": "user", "auto": True, "step": step, "ok": ok_all,
            "output": "\n".join((o.rstrip() + "\n" + e.rstrip()).strip() for _, o, e in outputs),
            "text": "<execution_result step=\"{}\">\n{}\n</execution_result>\n"
                    "If the task is done, reply to the user with a short summary and no code. "
                    "Otherwise send the next code block.".format(step, result_text),
        })
        self._start_request()

    def _execute(self, code, label=None):
        """Run code in the current chat's namespace. QGIS stays usable meanwhile and the
        Send button becomes Stop."""
        runner = self.runner
        self._active_runner = runner
        self._set_busy(True)
        self.send_btn.setText(tr("stop"))
        self._begin_task()
        self._set_phase(label or tr("running_code"))
        try:
            result = runner.run(code)
        finally:
            self._active_runner = None
            self._set_busy(False)
            self.send_btn.setText(tr("send"))
        self.iface.mapCanvas().refresh()
        return result

    def _on_failed(self, reply, error):
        self._finish_call()
        if not reply["text"]:
            self.messages = [m for m in self.messages if m is not reply]
        else:
            reply["pending"] = False
            reply["exclude"] = True
        self._end_task(" ")
        self._add_note("⚠ **{}** - {}\n\n[↻ {}](qgisai:retry)".format(
            display_name(self.provider), error, tr("retry")))

    def _add_note(self, text, **extra):
        note = {"role": "note", "text": text}
        note.update(extra)
        self.messages.append(note)
        self._render(force_bottom=True)
        return note

    # ------------------------------------------------------------------ rendering

    def _render(self, force_bottom=False):
        bar = self.browser.verticalScrollBar()
        old = bar.value()
        at_bottom = force_bottom or old >= bar.maximum() - 40
        self.code_blocks = []
        parts = [] if self.messages else [welcome()]
        for msg_idx, m in enumerate(self.messages):
            if m["role"] == "system":
                continue  # instruction updates for the model, not shown
            if m["role"] == "user" and m.get("auto"):
                head = "🛠 **{}** {}".format(
                    tr("ran_step").format(m["step"]), "✅" if m["ok"] else "❌ - " + tr("will_fix"))
                out = m["output"].strip()
                if out:
                    f = _fence(out)
                    head += "\n\n{f}\n{}\n{f}".format(out, f=f)
                parts.append(head)
            elif m["role"] == "user":
                extras = [tr(label) for flag, label in (("context", "with_context"),
                                                        ("image_b64", "with_snapshot"),
                                                        ("had_image", "with_snapshot")) if m.get(flag)]
                head = "**{}**".format(tr("you")) + ("  *(+ {})*".format(", ".join(extras)) if extras else "")
                parts.append("{}\n\n{}".format(head, m["text"]))
            elif m["role"] == "assistant":
                name = m["model"]
                if m.get("served_model"):
                    name += " → " + m["served_model"]
                head = "**🤖 {}**{}".format(name, " ⏳" if m.get("pending") else "")
                text = memory.strip_tags(m["text"])
                pending = "*{}*".format(m.get("phase") or "...") if m.get("pending") else ""
                body = self._linkify(text) if text else pending
                for n, saved in enumerate(m.get("memory_saved") or []):
                    if n in (m.get("memory_undone") or []):
                        body += "\n\n> 🧠 ~~{}~~".format(tr("memory_saved").format(saved))
                    else:
                        body += "\n\n> 🧠 {} · [{}](qgisai:forget/{}-{})".format(
                            tr("memory_saved").format(saved), tr("undo"), msg_idx, n)
                for forgot in m.get("memory_forgot") or []:
                    body += "\n\n> 🧠 " + tr("memory_forgot").format(forgot)
                if m.get("notice"):
                    body += "\n\n> ⚠ " + m["notice"]
                parts.append("{}\n\n{}".format(head, body))
            else:
                parts.append(m["text"])
        # U+FE0F (emoji presentation selector, as in ⚠️ or ✔️) makes Qt 5 on macOS draw the
        # whole paragraph as one unreadable glyph; the symbols read fine without it.
        markdown = _neutralize_html("\n\n---\n\n".join(parts).replace("\ufe0f", ""))
        started = time.monotonic()
        if hasattr(self.browser, "setMarkdown"):
            self.browser.setMarkdown(markdown)
            self.browser.document().setUndoRedoEnabled(False)  # read-only view: no undo history
            self._style_code()
        else:  # Qt < 5.14 (e.g. QGIS 3.22 on Ubuntu 20.04): no Markdown renderer
            self.browser.setPlainText(markdown)
        if not any(m.get("pending") for m in self.messages):
            self._save_timer.start()  # debounced save after every finished change
        bar.setValue(bar.maximum() if at_bottom else old)
        self._render_cost = time.monotonic() - started

    def _style_code(self):
        """Monospace font and a tinted background for fenced code blocks, and readable
        link colours. All changes go in one edit block: Qt then lays the document out once
        instead of after every change, which made long chats slow to redraw."""
        doc = self.browser.document()
        mono = QFontDatabase.systemFont(qenum(QFontDatabase, "SystemFont", "FixedFont"))
        try:
            fence = qenum(QTextFormat, "Property", "BlockCodeFence")
        except AttributeError:
            fence = None
        _, text, link, _ = self._colors
        bg = QColor(text)
        bg.setAlpha(26)
        code_blocks, anchors = [], []
        block = doc.begin()
        while block.isValid():
            fmt = block.blockFormat()
            if (fence is not None and fmt.hasProperty(fence)) or fmt.nonBreakableLines():
                code_blocks.append((block.position(), block.length() - 1, fmt))
            else:
                it = block.begin()
                while not it.atEnd():
                    frag = it.fragment()
                    if frag.isValid() and frag.charFormat().isAnchor():
                        anchors.append((frag.position(), frag.length()))
                    it += 1
            block = block.next()

        keep = qenum(QTextCursor, "MoveMode", "KeepAnchor")
        char_fmt = QTextCharFormat()
        char_fmt.setFontFamily(mono.family())
        char_fmt.setFontFixedPitch(True)
        # Qt colours links with the application palette, which may not suit the
        # background; use the checked link colour instead.
        link_fmt = QTextCharFormat()
        link_fmt.setForeground(link)
        cursor = QTextCursor(doc)
        cursor.beginEditBlock()
        for pos, length, fmt in code_blocks:
            cursor.setPosition(pos)
            new_fmt = QTextBlockFormat(fmt)
            new_fmt.setBackground(bg)
            new_fmt.setLeftMargin(6)
            cursor.setBlockFormat(new_fmt)
            cursor.setPosition(pos + length, keep)
            cursor.mergeCharFormat(char_fmt)
        for pos, length in anchors:
            cursor.setPosition(pos)
            cursor.setPosition(pos + length, keep)
            cursor.mergeCharFormat(link_fmt)
        cursor.endEditBlock()

    def _linkify(self, text):
        def add_links(match):
            lang = match.group(1).lower()
            idx = len(self.code_blocks)
            self.code_blocks.append(match.group(2))
            links = "[📋 {}](qgisai:copy/{})".format(tr("copy"), idx)
            if lang in RUNNABLE and not self.auto_check.isChecked():
                links = "[▶ {}](qgisai:run/{}) · ".format(tr("run_in_qgis"), idx) + links
            return match.group(0) + "\n\n" + links + "\n"
        return FENCE.sub(add_links, text)

    # ------------------------------------------------------------------ links & code

    def _on_link(self, url):
        if url.scheme() != "qgisai":
            QDesktopServices.openUrl(url)
            return
        action, _, arg = url.path().partition("/")
        if self.running_code and action != "copy":
            self._set_status(tr("wait_answer"))
            return
        if action == "retry":
            self._retry()
            return
        if action == "forget":
            self._undo_memory(arg)
            return
        if action == "example":
            self._use_example(arg)
            return
        try:
            idx = int(arg)
        except ValueError:
            return
        if action == "copy" and idx < len(self.code_blocks):
            QApplication.clipboard().setText(self.code_blocks[idx])
            self._set_status(tr("copied"))
        elif action == "run" and idx < len(self.code_blocks):
            self._run_code(self.code_blocks[idx])
        elif action == "fix" and idx < len(self.messages):
            note = self.messages[idx]
            self.send("Running your code in QGIS failed.\n\nCode:\n```python\n{}\n```\n\n"
                      "Error:\n```\n{}\n```\n\nPlease fix it.".format(
                          note["code"].rstrip(), note["error"].strip()))

    def _use_example(self, arg):
        """A welcome example was clicked: put it in the input box, ready to edit or send."""
        try:
            n = int(arg)
        except ValueError:
            return
        if not 1 <= n <= WELCOME_EXAMPLES:
            return
        self.input.setPlainText(tr("welcome_ex{}".format(n)))
        self.input.moveCursor(qenum(QTextCursor, "MoveOperation", "End"))
        self.input.setFocus()

    def _run_code(self, code):
        if self.call is not None or self.running_code:
            self._set_status(tr("wait_answer"))
            return
        ok, out, err = self._execute(code)
        self._end_task(tr("stopped") if self.runner.stopped else "")
        if self.runner.stopped:
            self._add_note("⏹ " + tr("code_stopped"))
        elif ok:
            text = "✅ **{}**".format(tr("code_ok"))
            if out.strip():
                f = _fence(out)
                text += "\n\n{f}\n{}\n{f}".format(out.rstrip(), f=f)
            self._add_note(text)
        else:
            idx = len(self.messages)
            detail = (out.rstrip() + "\n" if out.strip() else "") + err.rstrip()
            f = _fence(detail)
            self._add_note("❌ **{}**\n\n{f}\n{}\n{f}\n\n[🛠 {}](qgisai:fix/{})".format(
                               tr("code_failed"), detail, tr("ask_fix"), idx, f=f),
                           code=code, error=detail)

    def _retry(self):
        if self.call is not None or self.running_code:
            return
        while self.messages and self.messages[-1]["role"] == "note":
            self.messages.pop()
        if self.messages and self.messages[-1]["role"] == "assistant" and self.messages[-1].get("exclude"):
            self.messages.pop()
        last = next((m for m in reversed(self.messages) if m["role"] != "system"), None)
        if last is not None and last["role"] == "user":
            # Ask again for the same turn; the history before it stays exactly as sent.
            if not last.get("auto"):
                self._agent_steps = 0
            self._start_request()

    # ------------------------------------------------------------------ misc

    def open_settings(self):
        before = i18n.current()
        if SettingsDialog(self).exec():
            if i18n.current() != before:
                self.language_changed.emit()  # the plugin rebuilds the panel in the new language
                return
            self._on_provider_changed()

    def _set_status(self, text):
        self.status.setText(text)

    def shutdown(self):
        if self._active_runner is not None:
            self._active_runner.stop()
        if self.call is not None:
            self.call.cancel()
        self._save_timer.stop()
        self._save_chats()
