"""Plugin entry point: toolbar button, menu entry and the chat dock."""

import os

from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QIcon
try:  # Qt6 (QGIS 4) moved QAction to QtGui
    from qgis.PyQt.QtGui import QAction
except ImportError:
    from qgis.PyQt.QtWidgets import QAction

from . import i18n
from .compat import qenum
from .i18n import tr


class QgisAiAssistantPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.dock = None
        self.menu = None
        i18n.load()

    def initGui(self):  # noqa: N802
        icon = QIcon(os.path.join(os.path.dirname(__file__), "icon.svg"))
        self.action = QAction(icon, tr("app_name"), self.iface.mainWindow())
        self.action.setCheckable(True)
        self.action.setToolTip(tr("open_panel_tip"))
        self.action.triggered.connect(self.toggle)
        self.iface.addToolBarIcon(self.action)
        self.menu = "&" + tr("app_name")
        self.iface.addPluginToMenu(self.menu, self.action)

    def toggle(self, checked):
        if self.dock is None:
            from .dock import ChatDock

            self.dock = ChatDock(self.iface, self.iface.mainWindow())
            self.iface.addDockWidget(qenum(Qt, "DockWidgetArea", "RightDockWidgetArea"), self.dock)
            self.dock.visibilityChanged.connect(self.action.setChecked)
            self.dock.language_changed.connect(lambda: QTimer.singleShot(0, self.retranslate))
        self.dock.setVisible(checked)
        if checked:
            self.dock.raise_()
            self.dock.input.setFocus()

    def _remove_dock(self):
        if self.dock is not None:
            self.dock.shutdown()  # saves the chats
            self.iface.removeDockWidget(self.dock)
            self.dock.deleteLater()
            self.dock = None

    def retranslate(self):
        """Apply a new interface language: rename the menu and action, rebuild the panel."""
        # isHidden(), not isVisible(): a panel tabbed behind another dock is open but not visible.
        was_open = self.dock is not None and not self.dock.isHidden()
        self._remove_dock()
        self.iface.removePluginMenu(self.menu, self.action)
        self.action.setText(tr("app_name"))
        self.action.setToolTip(tr("open_panel_tip"))
        self.menu = "&" + tr("app_name")
        self.iface.addPluginToMenu(self.menu, self.action)
        if was_open:
            self.toggle(True)

    def unload(self):
        self.iface.removePluginMenu(self.menu, self.action)
        self.iface.removeToolBarIcon(self.action)
        self._remove_dock()
