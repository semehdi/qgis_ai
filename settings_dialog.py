"""Settings: API keys, endpoints, language and behaviour."""

from qgis.PyQt.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
                                 QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                                 QListWidget, QMessageBox, QPushButton, QScrollArea, QSpinBox,
                                 QVBoxLayout)

from . import i18n, keystore, memory
from .compat import qenum, set_setting, setting
from .i18n import tr
from .providers import PROVIDERS, display_name

DEFAULT_MAX_TOKENS = 64000


def base_url(provider):
    return setting("base_url/" + provider.id, "") or provider.base_url


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("settings_title"))
        self.resize(760, 820)
        layout = QVBoxLayout(self)

        keys_box = QGroupBox(tr("providers"))
        grid = QGridLayout(keys_box)
        grid.addWidget(QLabel("<b>{}</b>".format(tr("provider"))), 0, 0)
        grid.addWidget(QLabel("<b>{}</b>".format(tr("api_key"))), 0, 1)
        grid.addWidget(QLabel("<b>{}</b>".format(tr("base_url_header"))), 0, 2)
        self._key_edits, self._url_edits, self._orig_keys = {}, {}, {}
        password = qenum(QLineEdit, "EchoMode", "Password")
        for row, p in enumerate(PROVIDERS, start=1):
            name = display_name(p)
            label = QLabel('<a href="{}">{}</a>'.format(p.key_url, name) if p.key_url else name)
            label.setOpenExternalLinks(True)
            if p.key_url:
                label.setToolTip(tr("get_key_tip").format(p.key_url))
            grid.addWidget(label, row, 0)

            key_edit = QLineEdit()
            key_edit.setEchoMode(password)
            saved = keystore.saved_key(p.id)
            key_edit.setText(saved)
            self._orig_keys[p.id] = saved
            hint = tr("key_not_needed") if not p.needs_key else tr("paste_key")
            if p.key_env:
                hint += " " + tr("or_env").format(p.key_env[0])
            key_edit.setPlaceholderText(hint)
            grid.addWidget(key_edit, row, 1)
            self._key_edits[p.id] = key_edit

            url_edit = QLineEdit(setting("base_url/" + p.id, ""))
            url_edit.setPlaceholderText(p.base_url)
            grid.addWidget(url_edit, row, 2)
            self._url_edits[p.id] = url_edit
        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(2, 2)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(keys_box)
        layout.addWidget(scroll, 1)

        note = QLabel(tr("keys_note"))
        note.setWordWrap(True)
        layout.addWidget(note)

        opts_box = QGroupBox(tr("behaviour"))
        form = QFormLayout(opts_box)
        self.language = QComboBox()
        self.language.addItem(tr("language_auto"), "auto")
        for code, native in i18n.LANGUAGES:
            self.language.addItem(native, code)
        self.language.setCurrentIndex(max(0, self.language.findData(setting("language", "auto"))))
        form.addRow(tr("language"), self.language)
        self.max_tokens = QSpinBox()
        self.max_tokens.setRange(256, 128000)
        self.max_tokens.setSingleStep(1000)
        self.max_tokens.setValue(setting("max_tokens", DEFAULT_MAX_TOKENS, int))
        self.max_tokens.setToolTip(tr("max_tokens_tip"))
        form.addRow(tr("max_tokens"), self.max_tokens)
        layout.addWidget(opts_box)

        mem_box = QGroupBox(tr("memory"))
        mem_layout = QVBoxLayout(mem_box)
        self.memory_learn = QCheckBox(tr("memory_learn"))
        self.memory_learn.setToolTip(tr("memory_learn_tip"))
        self.memory_learn.setChecked(setting("memory_learn", True, bool))
        mem_layout.addWidget(self.memory_learn)
        self.memory_list = QListWidget()
        self.memory_list.setWordWrap(True)
        self.memory_list.setMaximumHeight(130)
        self.memory_list.setSelectionMode(qenum(QListWidget, "SelectionMode", "ExtendedSelection"))
        self._memory_items = memory.load()
        for item in self._memory_items:
            self.memory_list.addItem(item["text"])
        self.memory_empty = QLabel("<i>{}</i>".format(tr("memory_empty")))
        mem_layout.addWidget(self.memory_list)
        mem_layout.addWidget(self.memory_empty)
        row = QHBoxLayout()
        self.memory_delete = QPushButton(tr("memory_delete"))
        self.memory_delete.clicked.connect(self._delete_memory)
        self.memory_clear = QPushButton(tr("memory_clear"))
        self.memory_clear.clicked.connect(self.memory_list.clear)
        self.memory_clear.clicked.connect(self._update_memory_ui)
        row.addStretch(1)
        row.addWidget(self.memory_delete)
        row.addWidget(self.memory_clear)
        mem_layout.addLayout(row)
        self.memory_list.itemSelectionChanged.connect(self._update_memory_ui)
        self._update_memory_ui()
        layout.addWidget(mem_box)

        buttons = QDialogButtonBox(qenum(QDialogButtonBox, "StandardButton", "Save")
                                   | qenum(QDialogButtonBox, "StandardButton", "Cancel"))
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _delete_memory(self):
        for item in self.memory_list.selectedItems():
            self.memory_list.takeItem(self.memory_list.row(item))
        self._update_memory_ui()

    def _update_memory_ui(self):
        empty = self.memory_list.count() == 0
        self.memory_list.setVisible(not empty)
        self.memory_empty.setVisible(empty)
        self.memory_delete.setEnabled(bool(self.memory_list.selectedItems()))
        self.memory_clear.setEnabled(not empty)

    def _save_memory(self):
        """Apply the deletions made in the list (applied on Save, like everything else)."""
        kept = {self.memory_list.item(i).text() for i in range(self.memory_list.count())}
        removed = [i["text"] for i in self._memory_items if i["text"] not in kept]
        try:
            for text in removed:
                memory.remove(text)
        except OSError as e:
            QMessageBox.warning(self, tr("app_name"), tr("memory_save_failed").format(e))

    def _save(self):
        for pid, edit in self._url_edits.items():
            set_setting("base_url/" + pid, edit.text().strip())
        set_setting("max_tokens", self.max_tokens.value())

        changed = {pid: e.text().strip() for pid, e in self._key_edits.items()
                   if e.text().strip() != self._orig_keys[pid]}
        failed = [pid for pid, key in changed.items() if keystore.store_key(pid, key) == "failed"]
        if failed:
            yes = qenum(QMessageBox, "StandardButton", "Yes")
            no = qenum(QMessageBox, "StandardButton", "No")
            answer = QMessageBox.question(self, tr("app_name"), tr("plain_keys_question"), yes | no, no)
            if answer == yes:
                for pid in failed:
                    keystore.store_key(pid, changed[pid], allow_plain=True)
            else:
                return

        set_setting("memory_learn", self.memory_learn.isChecked())
        self._save_memory()
        set_setting("language", self.language.currentData())
        i18n.load()
        self.accept()
