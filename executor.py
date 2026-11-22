"""Run AI-written PyQGIS code inside the QGIS session."""

import io
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout

MAX_OUTPUT = 6000

_PRELUDE = """
from qgis.core import *
from qgis.gui import *
from qgis.PyQt.QtCore import *
from qgis.PyQt.QtGui import *
from qgis.PyQt.QtWidgets import *
"""


class CodeRunner:
    """Executes code in one namespace per chat, so variables persist between runs."""

    def __init__(self, iface):
        self.iface = iface
        self.ns = None

    def reset(self):
        self.ns = None

    def _namespace(self):
        if self.ns is None:
            ns = {"__name__": "__qgis_ai__", "iface": self.iface}
            exec(_PRELUDE, ns)
            try:
                import processing
                ns["processing"] = processing
            except ImportError:
                pass
            self.ns = ns
        return self.ns

    def run(self, code):
        """Return (ok, output, error_traceback)."""
        buf = io.StringIO()
        ok, err = True, ""
        with redirect_stdout(buf), redirect_stderr(buf):
            try:
                exec(compile(code, "<ai-code>", "exec"), self._namespace())
            except (Exception, SystemExit):
                ok = False
                etype, value, tb = sys.exc_info()
                # Skip this file's own frame; show only the user's code.
                err = "".join(traceback.format_exception(etype, value, tb.tb_next))
        out = buf.getvalue()
        if len(out) > MAX_OUTPUT:
            out = out[:MAX_OUTPUT] + "\n... (output truncated)"
        return ok, out, err
