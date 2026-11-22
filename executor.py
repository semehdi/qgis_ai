"""Run AI-written PyQGIS code inside the QGIS session without freezing QGIS.

The code itself runs on the main thread, where it may safely touch the project, layers
and map. Two things keep QGIS usable while it works, and let the user stop it:

- Processing algorithms started with processing.run() / runAndLoadResults() are sent to
  a background thread, exactly like the Processing toolbox does
  (QgsProcessingAlgRunnerTask), while the code waits in a local event loop. Algorithms
  flagged as not thread-safe still run on the main thread.
- Long Python loops let the interface process events about 20 times a second (through a
  profile hook limited to the AI code), and Stop interrupts them.
"""

import ctypes
import io
import os
import sys
import threading
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout

from qgis.core import (Qgis, QgsApplication, QgsProcessingAlgorithm, QgsProcessingAlgRunnerTask,
                       QgsProcessingException)
from qgis.PyQt.QtCore import QCoreApplication, QEventLoop, QThread

MAX_OUTPUT = 6000
CODE_NAME = "<ai-code>"  # file name of the compiled AI code, used to recognise its frames
PUMP_INTERVAL = 0.05  # seconds between two interface refreshes during long Python code
STOPPED_TEXT = "Stopped by the user before the code finished."

_PRELUDE = """
from qgis.core import *
from qgis.gui import *
from qgis.PyQt.QtCore import *
from qgis.PyQt.QtGui import *
from qgis.PyQt.QtWidgets import *
"""


class StoppedByUser(BaseException):
    """Raised in the AI code when the user presses Stop. Not an Exception subclass, so
    `except Exception` in the generated code does not swallow it."""


def _set_async_exc(thread_id, exc_type):
    """Raise exc_type in a thread at its next Python instruction (None clears it)."""
    ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(thread_id), ctypes.py_object(exc_type) if exc_type else None)


def _no_threading(alg):
    """True if the algorithm must run on the main thread."""
    flag = getattr(getattr(Qgis, "ProcessingAlgorithmFlag", None), "NoThreading", None)
    if flag is None:  # QGIS < 3.36
        flag = getattr(QgsProcessingAlgorithm, "FlagNoThreading", None)
    try:
        return flag is None or bool(alg.flags() & flag)
    except TypeError:
        return True  # can't tell: stay on the safe side


def _on_main_thread():
    app = QCoreApplication.instance()
    return app is not None and QThread.currentThread() == app.thread()


class CodeRunner:
    """Executes code in one namespace per chat, so variables persist between runs."""

    def __init__(self, iface):
        self.iface = iface
        self.ns = None
        self.running = False
        self.stopped = False  # the last run was stopped by the user
        self._stop_requested = False
        self._stop_sent = False
        self._feedback = None  # feedback of the background algorithm being waited for
        self._started = 0.0
        self._next_pump = 0.0
        self._processing_dirs = ()

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

    # ------------------------------------------------------------------ status & stop

    def elapsed(self):
        return time.monotonic() - self._started if self.running else 0.0

    def progress(self):
        """Progress (0-100) of the background algorithm being waited for, or None."""
        fb = self._feedback
        try:
            return fb.progress() if fb is not None else None
        except RuntimeError:  # deleted
            return None

    def stop(self):
        """Ask the running code to stop as soon as possible."""
        if not self.running:
            return
        self._stop_requested = True
        if self._feedback is not None:
            self._feedback.cancel()

    # ------------------------------------------------------------------ running

    def run(self, code):
        """Return (ok, output, error_traceback)."""
        buf = io.StringIO()
        ok, err = True, ""
        self.running, self.stopped = True, False
        self._stop_requested = self._stop_sent = False
        self._started = time.monotonic()
        self._next_pump = self._started + PUMP_INTERVAL
        thread_id = threading.get_ident()
        prev_profile = sys.getprofile()
        restore_processing = self._patch_processing()
        try:
            with redirect_stdout(buf), redirect_stderr(buf):
                try:
                    compiled = compile(code, CODE_NAME, "exec")
                    sys.setprofile(self._profile)
                    exec(compiled, self._namespace())
                    sys.setprofile(prev_profile)
                except StoppedByUser:
                    ok, self.stopped = False, True
                    err = STOPPED_TEXT
                except (Exception, SystemExit):
                    ok = False
                    etype, value, tb = sys.exc_info()
                    # Skip this file's own frame; show only the user's code.
                    err = "".join(traceback.format_exception(etype, value, tb.tb_next))
        except StoppedByUser:  # Stop landed just after the code finished
            ok, self.stopped = False, True
            err = STOPPED_TEXT
        finally:
            sys.setprofile(prev_profile)
            _set_async_exc(thread_id, None)  # drop a Stop that did not land
            restore_processing()
            self.running = False
            self._feedback = None
        out = buf.getvalue()
        if len(out) > MAX_OUTPUT:
            out = out[:MAX_OUTPUT] + "\n... (output truncated)"
        return ok, out, err

    def _profile(self, frame, event, arg):
        """Called on every function call while the AI code runs; cheap unless it is the
        AI code's own frame. Keeps the interface alive and delivers Stop."""
        if frame.f_code.co_filename != CODE_NAME:
            # Also the moment the AI code calls a Python function from a library.
            caller = frame.f_back if event == "call" else None
            if caller is None or caller.f_code.co_filename != CODE_NAME:
                return
        if self._stop_requested:
            if not self._stop_sent:
                self._stop_sent = True
                _set_async_exc(threading.get_ident(), StoppedByUser)
            return
        now = time.monotonic()
        if now >= self._next_pump:
            # Profiling is paused inside this function, so code run by these events
            # (Stop button, repaints, other plugins) is not slowed down or re-entered.
            QCoreApplication.processEvents()
            self._next_pump = time.monotonic() + PUMP_INTERVAL

    # ------------------------------------------------------------------ processing in background

    def _patch_processing(self):
        """While the AI code runs, make processing run algorithms in a background task.
        Returns a function that undoes the patch."""
        try:
            import processing
            from processing.core import Processing as processing_core
        except ImportError:
            return lambda: None
        original = getattr(processing_core, "execute", None)
        if original is None:
            return lambda: None
        self._processing_dirs = tuple(
            os.path.dirname(os.path.abspath(m.__file__)) + os.sep
            for m in (processing, sys.modules.get("qgis.processing")) if getattr(m, "__file__", None))

        def execute(alg, parameters, context=None, feedback=None, catch_exceptions=True):
            if (self._feedback is not None or context is None or feedback is None
                    or not _on_main_thread() or not self._called_from_ai_code()
                    or _no_threading(alg)):
                return original(alg, parameters, context, feedback, catch_exceptions)
            return self._run_in_background(alg, parameters, context, feedback, catch_exceptions)

        processing_core.execute = execute

        def restore():
            if processing_core.execute is execute:
                processing_core.execute = original
        return restore

    def _called_from_ai_code(self):
        """True if the first caller outside the processing package is the AI code (not
        another plugin reacting to an event while the AI code waits)."""
        frame = sys._getframe(2)
        while frame is not None and frame.f_code.co_filename.startswith(self._processing_dirs):
            frame = frame.f_back
        return frame is not None and frame.f_code.co_filename == CODE_NAME

    def _run_in_background(self, alg, parameters, context, feedback, catch_exceptions):
        if self._stop_requested:
            raise StoppedByUser()
        task = QgsProcessingAlgRunnerTask(alg, parameters, context, feedback)
        outcome = {}
        if not task.isCanceled():  # it cancels itself when the algorithm can't be prepared
            loop = QEventLoop()

            def executed(ok, results):
                outcome["ok"], outcome["results"] = ok, results
                loop.quit()

            task.executed.connect(executed)
            self._feedback = feedback
            try:
                QgsApplication.taskManager().addTask(task)  # the task manager owns it now
                loop.exec()  # QGIS stays responsive while the algorithm runs
            finally:
                self._feedback = None
        if self._stop_requested or feedback.isCanceled():
            raise StoppedByUser()
        if outcome.get("ok"):
            return True, outcome.get("results") or {}
        if catch_exceptions:
            return False, {}
        log = (feedback.textLog() if hasattr(feedback, "textLog") else "").strip()
        raise QgsProcessingException(log[-1500:] or "There were errors executing the algorithm.")
