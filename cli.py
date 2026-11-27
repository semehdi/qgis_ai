"""Claude through the Claude Code command-line tool, signed in with the user's Claude
account: no API key needed.

Each request starts `claude -p` with every built-in tool and MCP server turned off, so
the model only writes text (the plugin runs the code itself, as with the other
providers). The conversation goes in on stdin as one stream-json user message, and the
answer comes back as stream-json lines that wrap the usual Messages API events, read by
the same parser as the Anthropic provider.
"""

import json
import os
import tempfile

from qgis.PyQt.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, pyqtSignal

from .chats import data_dir
from .claude_path import WINDOWS, child_env, launch
from .compat import qenum
from .i18n import tr
from .providers import AnthropicParser, ProviderError, _history, user_text

_FAILED_TO_START = qenum(QProcess, "ProcessError", "FailedToStart")
_NOT_RUNNING = qenum(QProcess, "ProcessState", "NotRunning")
_LOGIN_WORDS = ("log in", "login", "logged in", "authenticat", "/login", "oauth")


def _kill_if_running(proc):
    try:
        if proc.state() != _NOT_RUNNING:
            proc.kill()
    except RuntimeError:  # already deleted: it had ended
        pass


def _stop(proc, wait=True):
    """End the CLI. SIGTERM first where it exists, so flatpak-spawn passes it on to the
    claude it started on the host; Windows has no such signal for console programs."""
    if WINDOWS:
        proc.kill()
    else:
        proc.terminate()
        if not wait:
            QTimer.singleShot(1500, lambda: _kill_if_running(proc))
            return
        if not proc.waitForFinished(1000):
            proc.kill()
    if wait:
        proc.waitForFinished(1000)


def work_dir():
    """An empty folder to run in, so no project's CLAUDE.md or settings get loaded."""
    path = os.path.join(data_dir(), "claude_cli")
    os.makedirs(path, exist_ok=True)
    return path


def _transcript(history):
    """The whole conversation as the content of one user message: earlier turns as a
    transcript, then the latest user message (and its map snapshot)."""
    turns = [m for m in history if m["role"] in ("user", "assistant") and (m.get("text") or m["role"] == "user")]
    last = turns.pop() if turns and turns[-1]["role"] == "user" else None
    parts = []
    for m in turns:
        if m["role"] == "user":
            note = "\n[a map snapshot was attached]" if m.get("image_b64") else ""
            parts.append("<user>\n{}{}\n</user>".format(user_text(m), note))
        else:
            parts.append("<assistant>\n{}\n</assistant>".format(m["text"]))
    text = user_text(last) if last else ""
    if parts:
        text = ("<conversation_so_far>\n{}\n</conversation_so_far>\n\n"
                "Continue this conversation: answer the user's latest message below, as the "
                "assistant.\n\n{}".format("\n\n".join(parts), text))
    content = []
    if last is not None and last.get("image_b64"):
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": last.get("image_mime", "image/jpeg"),
            "data": last["image_b64"]}})
    content.append({"type": "text", "text": text})
    return content


def build_cli_request(model, system, history):
    """Return (arguments, stdin_bytes, system_prompt_file, parser)."""
    fd, system_file = tempfile.mkstemp(prefix="system_", suffix=".txt", dir=work_dir())
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(system)
    args = ["-p", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--include-partial-messages",
            "--tools", "", "--strict-mcp-config", "--disable-slash-commands",
            "--no-session-persistence", "--system-prompt-file", system_file]
    if model:
        args += ["--model", model]
    turns = [m for m in _history(history) if m["role"] != "system"]  # system: sent whole each time
    line = {"type": "user", "message": {"role": "user", "content": _transcript(turns)}}
    stdin = (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8")
    return args, stdin, system_file, CliParser()


class CliParser:
    """Reads the CLI's stream-json lines; the API events inside go to AnthropicParser."""

    def __init__(self):
        self.inner = AnthropicParser()
        self.complete = False
        self.served_model = None

    @property
    def phase(self):
        return self.inner.phase

    def feed(self, obj):
        t = obj.get("type")
        if t == "stream_event":
            return self.inner.feed(obj.get("event") or {})
        if t == "system" and obj.get("subtype") == "init":
            self.served_model = obj.get("model")
        elif t == "result":
            self.complete = True
            if obj.get("is_error") or (obj.get("subtype") not in (None, "success")):
                msg = obj.get("result") or "; ".join(str(e) for e in obj.get("errors") or []) \
                    or obj.get("subtype") or tr("err_failed")
                raise ProviderError(describe_cli_error(msg))
        return ""

    def result(self, cancelled=False):
        res = self.inner.result(cancelled)
        res["blocks"] = None  # Claude Code's thinking blocks can't be replayed to the API
        res["served_model"] = res.get("served_model") or self.served_model
        return res


def describe_cli_error(msg):
    msg = (msg or "").strip()
    if any(w in msg.lower() for w in _LOGIN_WORDS):
        msg += " - " + tr("cli_login_hint")
    return "Claude Code: " + msg


class ClaudeCliCall(QObject):
    """Runs one request through the claude executable; same signals as StreamingCall."""

    delta = pyqtSignal(str)
    phase = pyqtSignal(str)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, executable, args, stdin, system_file, parser, parent=None):
        super().__init__(parent)
        self._exe, self._args, self._stdin = executable, args, stdin
        self._system_file, self._parser = system_file, parser
        self._proc = None
        self._buf = b""
        self._err = b""
        self._phase = None
        self._error = None
        self._finished = False
        self._cancelled = False

    def start(self):
        proc = self._proc = QProcess(self)
        env = QProcessEnvironment()
        for name, value in child_env(dict(os.environ), self._exe).items():
            env.insert(name, value)
        proc.setProcessEnvironment(env)
        proc.setWorkingDirectory(work_dir())
        program, args, native = launch(self._exe, self._args)
        proc.setProgram(program)
        if native is not None and hasattr(proc, "setNativeArguments"):  # Windows only
            proc.setNativeArguments(native)
        else:
            proc.setArguments(args)
        proc.readyReadStandardOutput.connect(self._on_stdout)
        proc.readyReadStandardError.connect(self._on_stderr)
        proc.finished.connect(self._on_exit)
        proc.errorOccurred.connect(self._on_error)
        proc.start()
        proc.write(self._stdin)
        proc.closeWriteChannel()

    def cancel(self):
        if self._finished:
            return
        self._cancelled = True
        if self._proc is not None:
            _stop(self._proc, wait=False)

    # ------------------------------------------------------------------ output

    def _on_stdout(self):
        self._buf += bytes(self._proc.readAllStandardOutput())
        while b"\n" in self._buf and not self._finished:
            line, self._buf = self._buf.split(b"\n", 1)
            self._handle(line)

    def _on_stderr(self):
        self._err = (self._err + bytes(self._proc.readAllStandardError()))[-4000:]

    def _handle(self, line):
        line = line.strip()
        if not line or self._error:
            return
        try:
            obj = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            return
        try:
            text = self._parser.feed(obj)
        except ProviderError as e:
            self._error = str(e)
            text = ""
        phase = self._parser.phase
        if phase and phase != self._phase:
            self._phase = phase
            self.phase.emit(phase)
        if text:
            self.delta.emit(text)
        if (self._parser.complete or self._error) and not self._cancelled:
            self._finish()

    def _on_error(self, error):
        if error == _FAILED_TO_START:
            self._error = self._error or tr("cli_not_found")
            self._finish()

    def _on_exit(self, *args):
        if self._finished:
            return
        self._on_stdout()
        if self._buf.strip():
            self._handle(self._buf)
            self._buf = b""
        if not self._finished:
            self._finish()

    def _finish(self):
        if self._finished:
            return
        self._finished = True
        try:
            os.remove(self._system_file)
        except OSError:
            pass
        proc = self._proc
        if proc is not None:
            for sig, slot in ((proc.readyReadStandardOutput, self._on_stdout),
                              (proc.readyReadStandardError, self._on_stderr),
                              (proc.finished, self._on_exit), (proc.errorOccurred, self._on_error)):
                try:
                    sig.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
            if proc.state() != _NOT_RUNNING:
                # The answer is complete (or stopped): the CLI has nothing more to say.
                _stop(proc)
        if self._cancelled:
            result = self._parser.result(cancelled=True)
            result["cancelled"] = True
            self.done.emit(result)
        elif self._error:
            self.failed.emit(self._error)
        elif self._parser.complete or self._parser.inner.complete:
            self.done.emit(self._parser.result())
        else:
            err = self._err.decode("utf-8", "replace").strip()[-1500:]
            self.failed.emit(describe_cli_error(err or tr("err_failed")))
        self.deleteLater()
