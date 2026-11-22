"""Integration tests for the QGIS Assistant plugin. They run the real plugin in a headless
QGIS against a local mock server (no real AI provider is called).

Run with QGIS's own Python:
  Linux (QGIS installed):  QT_QPA_PLATFORM=offscreen python3 tests/run_tests.py
  Docker:                  see .github/workflows/ci.yml
  macOS:                   see tests/README.md
Exit code 0 means every check passed. Nothing outside a temporary folder is touched.
"""

import json
import os
import platform
import shutil
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = tempfile.mkdtemp(prefix="qgis_ai_tests_")

# Isolate everything QGIS and the plugin write (settings, auth DB, chats) in WORK.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["QGIS_CUSTOM_CONFIG_PATH"] = os.path.join(WORK, "config")
os.environ["QGIS_AUTH_DB_DIR_PATH"] = os.path.join(WORK, "auth")
os.environ["QGIS_AI_ASSISTANT_DATA"] = os.path.join(WORK, "chats")

# Import the plugin as the package QGIS would: a folder named after the plugin id.
PKG_PARENT = os.path.join(WORK, "pkg")
shutil.copytree(ROOT, os.path.join(PKG_PARENT, "qgis_assistant"),
                ignore=shutil.ignore_patterns("tests", "tools", ".git*", ".github", "dist",
                                              "build", "__pycache__", "*.zip"))
sys.path.insert(0, PKG_PARENT)

from qgis.PyQt.QtCore import QCoreApplication  # noqa: E402

QCoreApplication.setOrganizationName("QGISAITest")
QCoreApplication.setApplicationName("QGISAITest")

from qgis.core import QgsApplication  # noqa: E402

app = QgsApplication([], True)
default_prefix = "/Applications/QGIS.app/Contents/MacOS" if platform.system() == "Darwin" else "/usr"
QgsApplication.setPrefixPath(os.environ.get("QGIS_PREFIX_PATH", default_prefix), True)
app.initQgis()

# Processing, as QGIS loads it at startup.
sys.path += [os.path.join(QgsApplication.pkgDataPath(), "python", "plugins"),
             "/Applications/QGIS.app/Contents/Resources/qgis/python/plugins"]
from processing.core.Processing import Processing  # noqa: E402

Processing.initialize()
import warnings  # noqa: E402

warnings.simplefilter("ignore", ResourceWarning)  # Processing turns these on

from qgis.gui import QgsMapCanvas  # noqa: E402
from qgis.PyQt.QtWidgets import QApplication, QMainWindow  # noqa: E402

results = []
captured = []


def check(name, cond, detail=""):
    results.append(("PASS" if cond else "FAIL") + " " + name
                   + ("" if cond else " :: " + str(detail)[:600]))


class StubIface:
    """The parts of QgisInterface the plugin uses."""

    def __init__(self):
        self.win = QMainWindow()
        self.canvas = QgsMapCanvas(self.win)
        self.win.setCentralWidget(self.canvas)
        self.win.resize(900, 700)
        self._active = None

    def mainWindow(self):
        return self.win

    def mapCanvas(self):
        return self.canvas

    def activeLayer(self):
        return self._active

    def setActiveLayer(self, layer):
        self._active = layer
        self.canvas.setLayers([layer])
        self.canvas.setExtent(layer.extent())
        self.canvas.refresh()

    def addDockWidget(self, area, dock):
        self.win.addDockWidget(area, dock)

    def removeDockWidget(self, dock):
        self.win.removeDockWidget(dock)

    def addToolBarIcon(self, action):
        pass

    def removeToolBarIcon(self, action):
        pass

    def addPluginToMenu(self, menu, action):
        pass

    def removePluginMenu(self, menu, action):
        pass


# --------------------------------------------------------------------------- mock server

def sse(events):
    return "".join("event: {}\ndata: {}\n\n".format(e.get("type", "message"), json.dumps(e))
                   for e in events)


ANSWER = ("Here is code:\n\n```python\nlayer = QgsVectorLayer('Point?crs=EPSG:4326', 'ai_made', "
          "'memory')\nQgsProject.instance().addMapLayer(layer)\nprint('made', layer.name())\n```\n")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, status, body, ctype="text/event-stream"):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/anthropic/v1/models"):
            self._send(200, json.dumps({"data": [{"id": "claude-opus-5-5", "max_tokens": 128000},
                                                 {"id": "claude-haiku-5-5", "max_tokens": 64000}],
                                        "has_more": False}), "application/json")
        else:
            self._send(404, json.dumps({"error": {"message": "nope"}}), "application/json")

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.startswith("/hang/"):
            # A complete answer, then the connection stays open (keep-alive, proxies).
            if "anthropic" in self.path:
                data = sse([{"type": "message_start", "message": {"model": "m"}},
                            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "all done"}},
                            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                            {"type": "message_stop"}])
            else:
                data = ("data: " + json.dumps({"choices": [{"delta": {"content": "all done"}, "finish_reason": "stop"}]})
                        + "\n\ndata: " + json.dumps({"choices": [], "usage": {"total_tokens": 3}}) + "\n\ndata: [DONE]\n\n")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(data.encode())
            self.wfile.flush()
            time.sleep(8)
            return
        captured.append({"path": self.path, "headers": dict(self.headers), "body": body})
        if self.path.startswith("/anthropic/"):
            if self.headers.get("x-api-key") == "bad":
                self._send(401, json.dumps({"type": "error", "error": {
                    "type": "authentication_error", "message": "invalid x-api-key"}}), "application/json")
                return
            last = [m for m in body["messages"] if m["role"] == "user"][-1]["content"]
            last_text = last[-1]["text"] if isinstance(last, list) else last
            answer = ANSWER
            if "stop adding emojis" in last_text:
                answer = "Understood, no more emojis.\n<remember>Never use emojis in answers</remember>\n"
            elif "emojis are fine again" in last_text:
                answer = "OK.\n<forget>Never use emojis in answers</forget>"
            elif "agent task" in json.dumps(body["messages"]):
                if "<execution_result" not in last_text:
                    answer = "Step 1:\n```python\nprint(undefined_thing)\n```\n"
                elif "status: error" in last_text:
                    answer = ("Fixing:\n```python\nl = QgsVectorLayer('Point?crs=EPSG:4326', 'agent_made', "
                              "'memory')\nQgsProject.instance().addMapLayer(l)\nprint('created', l.name())\n```\n")
                else:
                    answer = "Done: created the agent_made layer."
            events = [
                {"type": "message_start", "message": {"id": "msg_1", "model": "claude-opus-5-5", "content": []}},
                {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "SIG123"}},
                {"type": "content_block_stop", "index": 0},
                {"type": "ping"},
                {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
            ]
            for i in range(0, len(answer), 7):
                events.append({"type": "content_block_delta", "index": 1,
                               "delta": {"type": "text_delta", "text": answer[i:i + 7]}})
            events += [{"type": "content_block_stop", "index": 1},
                       {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
                       {"type": "message_stop"}]
            self._send(200, sse(events))
        elif self.path.startswith("/openai/"):
            chunks = [{"model": "gpt-x", "choices": [{"delta": {"content": w}}]} for w in ["Hello ", "from ", "OpenAI"]]
            chunks.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
            self._send(200, "".join("data: {}\r\n\r\n".format(json.dumps(c)) for c in chunks) + "data: [DONE]\r\n\r\n")
        elif self.path.startswith("/gemini/"):
            chunks = [{"candidates": [{"content": {"parts": [{"text": "thinking", "thought": True}, {"text": "Hi from "}]}}]},
                      {"candidates": [{"content": {"parts": [{"text": "Gemini"}]}, "finishReason": "STOP"}]}]
            self._send(200, "".join("data: {}\r\n\r\n".format(json.dumps(c)) for c in chunks))


def wait_until(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


# --------------------------------------------------------------------------- tests

def main():
    from qgis.core import Qgis, QgsFeature, QgsGeometry, QgsPointXY, QgsProject, QgsVectorLayer
    from qgis.PyQt.QtCore import QT_VERSION_STR, QUrl
    from qgis_assistant import providers
    from qgis_assistant.compat import set_setting
    from qgis_assistant.plugin import QgisAiAssistantPlugin

    print("Platform: {} | QGIS {} | Qt {} | Python {}".format(
        platform.platform(), Qgis.QGIS_VERSION, QT_VERSION_STR, platform.python_version()))
    iface = StubIface()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:{}".format(server.server_address[1])
    for pid in ("anthropic", "openai", "gemini"):
        set_setting("base_url/" + pid, "{}/{}/v1".format(base, pid))
    set_setting("auto_run", False)
    set_setting("language", "en")
    for env in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"):
        os.environ[env] = "test-key"

    lyr = QgsVectorLayer("Point?crs=EPSG:4326&field=name:string&field=pop:integer", "cities", "memory")
    feats = []
    for name, pop, x, y in [("Rabat", 577000, -6.84, 34.02), ("Fes", 1100000, -5.0, 34.03)]:
        f = QgsFeature(lyr.fields())
        f.setAttributes([name, pop])
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x, y)))
        feats.append(f)
    lyr.dataProvider().addFeatures(feats)
    QgsProject.instance().addMapLayer(lyr)
    iface.setActiveLayer(lyr)

    plugin = QgisAiAssistantPlugin(iface)
    plugin.initGui()
    plugin.toggle(True)
    dock = plugin.dock
    check("dock created", dock is not None)

    # --- model list (Anthropic auto-refreshes on start because a key exists)
    wait_until(lambda: dock.model_combo.count() == 2, 5)
    models = [dock.model_combo.itemText(i) for i in range(dock.model_combo.count())]
    check("anthropic models fetched", models == ["claude-opus-5-5", "claude-haiku-5-5"], models)
    check("max_tokens capped from model list", dock._max_tokens("claude-haiku-5-5") == 64000)
    dock.model_combo.setCurrentText("claude-opus-5-5")

    # --- Anthropic request shape and streaming
    dock.send("buffer the cities")
    check("call finished", wait_until(lambda: dock.call is None), "timeout")
    req = [c for c in captured if c["path"].endswith("/messages")][-1]
    h = {k.lower(): v for k, v in req["headers"].items()}
    b = req["body"]
    check("anthropic version header", h.get("anthropic-version") == "2026-10-21", h)
    check("fallback beta header", h.get("anthropic-beta") == "server-side-fallback-2026-07-01", h)
    check("fallbacks default", b.get("fallbacks") == "default")
    check("no sampling params", not any(k in b for k in ("temperature", "top_p", "top_k")))
    check("stream + max_tokens", b.get("stream") is True and b.get("max_tokens") == 64000, b.get("max_tokens"))
    utext = b["messages"][0]["content"][-1]["text"]
    check("context in user msg", "<qgis_context>" in utext and '"cities"' in utext and "pop:integer" in utext, utext[:400])
    check("no attribute values sent", "Rabat" not in utext and "sample:" not in utext, utext)
    reply = dock.messages[-1]
    check("reply text streamed", reply["text"] == ANSWER, repr(reply["text"]))
    check("thinking block kept", reply["provider_blocks"][0].get("signature") == "SIG123")
    check("one runnable code block", len(dock.code_blocks) == 1, dock.code_blocks)
    check("run link rendered", "qgisai:run/0" in dock.browser.toHtml())

    # --- manual run, failure, fix
    dock._on_link(QUrl("qgisai:run/0"))
    check("code created layer", len(QgsProject.instance().mapLayersByName("ai_made")) == 1)
    check("success note", "ran successfully" in dock.messages[-1]["text"]
          and "made ai_made" in dock.messages[-1]["text"], dock.messages[-1]["text"])
    dock._run_code("x = 1\nundefined_name + 1")
    note = dock.messages[-1]
    check("failure note", "NameError" in note["text"] and "qgisai:fix/" in note["text"], note["text"])
    dock._on_link(QUrl("qgisai:fix/{}".format(len(dock.messages) - 1)))
    check("fix call finished", wait_until(lambda: dock.call is None))
    req2 = captured[-1]["body"]
    roles = [m["role"] for m in req2["messages"]]
    check("history roles alternate", roles == ["user", "assistant", "user"], roles)
    check("thinking replayed unchanged", req2["messages"][1]["content"][0]
          == {"type": "thinking", "thinking": "", "signature": "SIG123"})
    check("traceback sent", "NameError" in req2["messages"][2]["content"][-1]["text"])
    check("no image when unchecked", all(c.get("type") != "image" for c in req2["messages"][2]["content"]))

    # --- map snapshot
    dock.map_check.setChecked(True)
    dock.send("what is on the map?")
    wait_until(lambda: dock.call is None)
    last_user = captured[-1]["body"]["messages"][-1]["content"]
    check("image attached", last_user[0].get("type") == "image" and len(last_user[0]["source"]["data"]) > 100,
          str(last_user[0])[:200])
    dock.map_check.setChecked(False)

    # --- OpenAI-compatible
    dock.provider_combo.setCurrentIndex(dock.provider_combo.findData("openai"))
    wait_until(lambda: False, 0.5)
    dock.model_combo.setCurrentText("gpt-x")
    dock.send("hello")
    wait_until(lambda: dock.call is None)
    ob = captured[-1]["body"]
    check("openai path", captured[-1]["path"] == "/openai/v1/chat/completions", captured[-1]["path"])
    check("openai system first", ob["messages"][0]["role"] == "system")
    hdrs = {k.lower(): v for k, v in captured[-1]["headers"].items()}
    check("openai bearer", hdrs.get("authorization") == "Bearer test-key")
    check("openai reply", dock.messages[-1]["text"] == "Hello from OpenAI", dock.messages[-1])

    # --- Gemini
    dock.provider_combo.setCurrentIndex(dock.provider_combo.findData("gemini"))
    wait_until(lambda: False, 0.5)
    dock.model_combo.setCurrentText("gemini-test")
    dock.send("hey")
    wait_until(lambda: dock.call is None)
    check("gemini path", captured[-1]["path"].startswith(
        "/gemini/v1/models/gemini-test:streamGenerateContent?alt=sse"), captured[-1]["path"])
    roles = [c["role"] for c in captured[-1]["body"]["contents"]]
    check("gemini roles alternate", all(roles[i] != roles[i + 1] for i in range(len(roles) - 1)), roles)
    check("gemini reply skips thoughts", dock.messages[-1]["text"] == "Hi from Gemini", dock.messages[-1]["text"])

    # --- HTTP error
    dock.provider_combo.setCurrentIndex(dock.provider_combo.findData("anthropic"))
    wait_until(lambda: False, 0.5)
    os.environ["ANTHROPIC_API_KEY"] = "bad"
    n = len(dock.messages)
    dock.send("this will fail")
    wait_until(lambda: dock.call is None)
    check("401 note", "HTTP 401" in dock.messages[-1]["text"] and "API key" in dock.messages[-1]["text"],
          dock.messages[-1]["text"])
    check("failed reply removed", dock.messages[-2]["role"] == "user", [m["role"] for m in dock.messages[n:]])

    # --- parsers and replay rules
    p = providers.AnthropicParser()
    p.feed({"type": "message_start", "message": {"model": "claude-opus-4-8"}})
    p.feed({"type": "content_block_start", "index": 0, "content_block": {
        "type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-4-8"}}})
    p.feed({"type": "message_delta", "delta": {"stop_reason": "refusal", "stop_details": {"category": "cyber"}}})
    r = p.result()
    check("refusal flagged", r["refused"] and "cyber" in r["notice"], r)
    blocks = [{"type": "thinking", "thinking": "a", "signature": "s"}, {"type": "text", "text": "x"},
              {"type": "fallback"}, {"type": "thinking", "thinking": "b", "signature": "t"},
              {"type": "text", "text": "y"}]
    check("fallback replay rule", [x["type"] for x in providers._anthropic_replay_blocks(blocks)]
          == ["text", "fallback", "thinking", "text"])

    # --- SSE with "\r\n" split across network reads (seen with some servers/proxies)
    from qgis_assistant.network import StreamingCall

    class FakeReply:
        def __init__(self, chunks):
            self.chunks = list(chunks)

        def readAll(self):
            return self.chunks.pop(0) if self.chunks else b""

        def attribute(self, _attr):
            return 200

    payload = "".join("data: {}\r\n\r\n".format(json.dumps({"choices": [{"delta": {"content": w}}]}))
                      for w in ("split ", "line ", "endings")).encode()
    cuts = [i for i in range(1, len(payload)) if payload[i - 1:i] == b"\r"]
    chunks = [payload[a:b] for a, b in zip([0] + cuts, cuts + [len(payload)])]
    call = StreamingCall("", {}, b"", providers.OpenAIParser())
    got = []
    call.delta.connect(got.append)
    call._reply = FakeReply(chunks)
    for _ in chunks:
        call._on_ready()
    check("SSE split CRLF parsed", "".join(got) == "split line endings", got)

    # --- local providers use IPv4 loopback (Windows resolves localhost to IPv6 first)
    check("ollama default is 127.0.0.1", providers.PROVIDERS_BY_ID["ollama"].base_url.startswith("http://127.0.0.1:"))
    check("lmstudio default is 127.0.0.1", providers.PROVIDERS_BY_ID["lmstudio"].base_url.startswith("http://127.0.0.1:"))

    # --- an answer is finished at its last event, even if the server keeps the connection open
    from qgis_assistant.network import StreamingCall
    for kind, parser in (("anthropic", providers.AnthropicParser()), ("openai", providers.OpenAIParser())):
        got = {}
        call = StreamingCall(base + "/hang/" + kind, {"content-type": "application/json"}, b"{}", parser, dock)
        call.done.connect(lambda r, g=got: g.update(result=r, t=time.time()))
        call.failed.connect(lambda e, g=got: g.update(error=e))
        t0 = time.time()
        call.start()
        wait_until(lambda: got, 6)
        check("{} answer ends at its last event".format(kind),
              got.get("result", {}).get("text") == "all done" and got["t"] - t0 < 3, got)

    # --- agent mode
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    dock.new_chat()
    dock.auto_check.setChecked(True)
    phases, bar_hidden_midway = [], []
    real_set_phase = dock._set_phase
    dock._set_phase = lambda label, reply=None: (phases.append(label), real_set_phase(label, reply))
    dock.send("agent task: make a layer")

    def agent_finished():
        done = (dock.call is None and dock.messages and dock.messages[-1]["role"] == "assistant"
                and dock.messages[-1]["text"].startswith("Done"))
        if not done:
            bar_hidden_midway.append(dock.busy_bar.isHidden())
        return done
    ok = wait_until(agent_finished, 20)
    check("agent loop finished with summary", ok, [(m["role"], m.get("text", "")[:60]) for m in dock.messages])
    autos = [m for m in dock.messages if m.get("auto")]
    check("two automatic runs (fail then fix)", [m["ok"] for m in autos] == [False, True],
          [(m.get("ok"), m.get("output")) for m in autos])
    check("agent created layer", len(QgsProject.instance().mapLayersByName("agent_made")) == 1)
    check("agent system prompt", "executes it immediately" in captured[-1]["body"]["system"])
    check("CRS rule in system prompt", "native:reprojectlayer" in captured[-1]["body"]["system"])
    check("error fed back to model", "NameError" in json.dumps(captured[-2]["body"]["messages"][-1]))
    check("no run links in agent mode", "qgisai:run/" not in dock.browser.toHtml())
    check("busy bar shown for the whole task", bar_hidden_midway and not any(bar_hidden_midway),
          bar_hidden_midway.count(True))
    del dock._set_phase
    expected = ["Sending to claude-opus-5-5...", "claude-opus-5-5 is thinking...", "claude-opus-5-5 is writing...",
                "Running step 1..."]
    check("phases shown while working", phases[:len(expected) + 1][:1] == expected[:1]
          and all(p in phases for p in expected)
          and phases.index("claude-opus-5-5 is thinking...") < phases.index("claude-opus-5-5 is writing..."),
          phases)
    check("clear end of task", dock.status.text().startswith("✓ Done in 0:") and dock.busy_bar.isHidden(),
          dock.status.text())

    # --- multiple chats
    from qgis.PyQt.QtWidgets import QInputDialog, QMessageBox
    from qgis_assistant import chats as chatstore
    from qgis_assistant.compat import qenum
    first_chat_id = dock.chats[-1]["id"]
    check("two chats exist", len(dock.chats) == 2, [c["title"] for c in dock.chats])
    check("chat auto-titled", dock.current["title"].startswith("agent task"), dock.current["title"])
    dock.runner.run("only_in_agent_chat = 42")
    dock.chat_combo.setCurrentIndex(dock.chat_combo.findData(first_chat_id))
    check("switched chat", dock.current["id"] == first_chat_id and dock.messages[0]["text"] == "buffer the cities")
    check("per-chat namespace", "only_in_agent_chat" not in dock.runner._namespace())
    dock.new_chat()
    n_before = len(dock.chats)
    dock.new_chat()
    check("empty new chat reused", len(dock.chats) == n_before)
    dock.chat_combo.setCurrentIndex(dock.chat_combo.findData(first_chat_id))
    check("empty chat dropped on switch", len(dock.chats) == 2, len(dock.chats))
    QInputDialog.getText = staticmethod(lambda *a, **k: ("Cities work", True))
    dock.rename_chat()
    check("renamed", dock.chat_combo.currentText() == "Cities work")
    dock._save_chats()
    stored = chatstore.load()
    check("chats saved to disk", sorted(c["title"] for c in stored) == sorted(c["title"] for c in dock.chats))
    check("images not stored", all("image_b64" not in m for c in stored for m in c["messages"]))
    check("had_image marker stored", any(m.get("had_image") for c in stored for m in c["messages"]))
    check("chat file in isolated folder", os.path.isfile(os.path.join(WORK, "chats", "chats.json")))
    QMessageBox.question = staticmethod(lambda *a, **k: qenum(QMessageBox, "StandardButton", "Yes"))
    dock.delete_chat()
    check("deleted chat", len(dock.chats) == 1 and dock.current["id"] != first_chat_id)

    # --- prompt history, with arrow keys as each OS sends them
    from qgis.PyQt.QtCore import Qt
    from qgis.PyQt.QtGui import QTextCursor
    from qgis.PyQt.QtTest import QTest
    key = lambda name: qenum(Qt, "Key", name)  # noqa: E731
    mod = lambda name: qenum(Qt, "KeyboardModifier", name)  # noqa: E731
    end = qenum(QTextCursor, "MoveOperation", "End")
    box = dock.input
    dock.auto_check.setChecked(False)
    for prompt in ("first typed prompt", "second typed prompt"):
        box.setPlainText(prompt)
        QTest.keyClick(box, key("Key_Return"))
        wait_until(lambda: dock.call is None)
    hist = list(box.history)
    check("typed prompts recorded", hist == ["first typed prompt", "second typed prompt"], hist)
    for label, m in (("plain (Windows/Linux)", mod("NoModifier")), ("keypad (macOS)", mod("KeypadModifier"))):
        box.setPlainText("half typed")
        box.moveCursor(end)
        QTest.keyClick(box, key("Key_Up"), m)
        up1 = box.toPlainText()
        QTest.keyClick(box, key("Key_Up"), m)
        up2 = box.toPlainText()
        QTest.keyClick(box, key("Key_Down"), m)
        QTest.keyClick(box, key("Key_Down"), m)
        check("history arrows " + label, (up1, up2, box.toPlainText())
              == (hist[-1], hist[-2], "half typed"), (up1, up2, box.toPlainText()))
    box.setPlainText("x")
    box.moveCursor(end)
    QTest.keyClick(box, key("Key_Up"), mod("ShiftModifier"))
    check("shift+up does not recall", box.toPlainText() == "x", box.toPlainText())
    QTest.keyClick(box, key("Key_Enter"), mod("KeypadModifier"))  # numpad Enter sends too
    check("numpad enter sends", wait_until(lambda: dock.call is None) and box.history[-1] == "x", box.history)
    box.clear()

    # --- memory of the user's preferences
    from qgis_assistant import memory
    from qgis_assistant.settings_dialog import SettingsDialog
    last_body = json.dumps(captured[-1]["body"])
    check("memory system rules", "<remember>" in last_body and "</user_preferences>" not in last_body, last_body[-300:])
    check("half-streamed tag hidden", memory.strip_tags("Sure.\n<remember>Never use em") == "Sure.")
    dock.new_chat()
    dock.send("stop adding emojis to every answer")
    wait_until(lambda: dock.call is None)
    saved = [i["text"] for i in memory.load()]
    check("preference remembered", saved == ["Never use emojis in answers"], saved)
    check("memory file in isolated folder", os.path.isfile(os.path.join(WORK, "chats", "memory.json")))
    shown = dock.browser.toPlainText()
    check("tag hidden, saved note shown", "<remember>" not in shown and "Remembered: Never use emojis" in shown
          and "Understood, no more emojis." in shown, shown[-300:])
    first = captured[-1]
    dock.send("stop adding emojis please")  # the model repeats itself: no duplicate
    wait_until(lambda: dock.call is None)
    second = captured[-1]
    b1, b2 = first["body"], second["body"]
    beta2 = {k.lower(): v for k, v in second["headers"].items()}.get("anthropic-beta", "")
    check("system prompt frozen after a memory change", b2["system"] == b1["system"])
    check("history append-only (thinking stays valid)", b2["messages"][:len(b1["messages"])] == b1["messages"]
          and b2["messages"][len(b1["messages"])]["content"][0].get("signature") == "SIG123",
          json.dumps(b2["messages"])[:600])
    upd = b2["messages"][-1]
    check("memory change sent as a system message after the user turn",
          upd["role"] == "system" and b2["messages"][-2]["role"] == "user"
          and "<user_preferences>\n- Never use emojis in answers\n</user_preferences>" in upd["content"], upd)
    check("invalid thinking dropped, not fatal",
          b2.get("thinking") == {"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}}
          and "thinking-binding-controls-2026-08-01" in beta2 and "server-side-fallback-2026-07-01" in beta2,
          second["headers"])
    check("system update not shown in chat", "Update from the plugin" not in dock.browser.toPlainText())
    check("no duplicate memory", len(memory.load()) == 1, memory.load())
    first_reply = next(i for i, m in enumerate(dock.messages) if m.get("memory_saved"))
    dock._on_link(QUrl("qgisai:forget/{}-0".format(first_reply)))
    check("undo forgets", memory.load() == [] and "qgisai:forget/" not in dock.browser.toHtml().split("Remembered")[1][:200],
          memory.load())
    memory.add("Answer in French")
    dock.send("emojis are fine again")  # nothing saved under that text: forget does nothing
    wait_until(lambda: dock.call is None)
    check("forget only matching", [i["text"] for i in memory.load()] == ["Answer in French"], memory.load())
    memory.add("Never use emojis in answers")
    dock.send("emojis are fine again")
    wait_until(lambda: dock.call is None)
    check("forget tag applied", [i["text"] for i in memory.load()] == ["Answer in French"]
          and "Forgot: Never use emojis" in dock.browser.toPlainText(), memory.load())
    set_setting("memory_learn", False)
    dock.send("stop adding emojis again")
    wait_until(lambda: dock.call is None)
    upd = captured[-1]["body"]["messages"][-1]
    check("learning off: nothing saved, preferences still applied",
          len(memory.load()) == 1 and upd["role"] == "system" and "<remember>" not in upd["content"]
          and "- Answer in French" in upd["content"], upd)
    check("same system prompt for the whole chat", captured[-1]["body"]["system"] == b1["system"])
    n_msgs, told = len(dock.messages), dict(dock.current["told"])
    dock.auto_check.setChecked(not dock.auto_check.isChecked())
    frozen, current = dock._system_prompts()
    check("auto-run change sent as an update", frozen == b1["system"] and dock.messages[-1]["role"] == "system"
          and "Auto-run" in dock.messages[-1]["text"] and current != frozen, dock.messages[-1])
    dock.auto_check.setChecked(not dock.auto_check.isChecked())
    del dock.messages[n_msgs:]
    dock.current["told"] = told
    dock.new_chat()
    dock.send("hello in a new chat")
    wait_until(lambda: dock.call is None)
    check("memory in the system prompt of new chats", "- Answer in French" in captured[-1]["body"]["system"]
          and all(m["role"] != "system" for m in captured[-1]["body"]["messages"]))
    set_setting("memory_learn", True)

    # history shapes for other models and providers
    hist = [{"role": "user", "text": "a"},
            {"role": "assistant", "text": "b", "provider_kind": "anthropic",
             "provider_blocks": [{"type": "thinking", "thinking": "", "signature": "S"}, {"type": "text", "text": "b"}]},
            {"role": "user", "text": "c"}, {"role": "system", "text": "UPDATE"}]
    anth = providers.PROVIDERS_BY_ID["anthropic"]

    def anthropic_body(model, h):
        return json.loads(providers.build_chat_request(anth, None, "k", model, "SYS", h, 1000)[2])

    old = anthropic_body("claude-sonnet-5", hist)
    check("older Claude: update inside the user turn", all(m["role"] != "system" for m in old["messages"])
          and old["messages"][-1]["content"][-1]["text"] == "<system_update>\nUPDATE\n</system_update>"
          and "thinking" not in old, old["messages"][-1])
    shifted = anthropic_body("claude-opus-5-5", hist + [{"role": "assistant", "text": "x", "exclude": True},
                                                       {"role": "user", "text": "d"}])
    check("update followed by a user turn goes inside the user turn",
          all(m["role"] != "system" for m in shifted["messages"])
          and "UPDATE" in json.dumps(shifted["messages"][2]), shifted["messages"])
    oa = json.loads(providers.build_chat_request(providers.PROVIDERS_BY_ID["openai"], None, "k", "gpt-x",
                                                 "SYS", hist, 1000)[2])
    check("other providers: current system prompt, no updates",
          oa["messages"][0] == {"role": "system", "content": "SYS"} and "UPDATE" not in json.dumps(oa)
          and [m["role"] for m in oa["messages"]] == ["system", "user", "assistant", "user"], oa["messages"])
    dlg = SettingsDialog(dock)
    check("settings lists memory", dlg.memory_list.count() == 1 and dlg.memory_list.item(0).text() == "Answer in French")
    dlg.memory_list.item(0).setSelected(True)
    dlg._delete_memory()
    dlg._save()
    check("settings deletes memory", memory.load() == [], memory.load())

    # --- long code keeps QGIS responsive and can be stopped
    from qgis.core import (QgsProcessingAlgorithm, QgsProcessingException, QgsProcessingOutputNumber,
                           QgsProcessingParameterNumber, QgsProcessingProvider)
    from qgis.PyQt.QtCore import QCoreApplication, QThread, QTimer
    from qgis_assistant.executor import CodeRunner

    class SlowAlg(QgsProcessingAlgorithm):
        def name(self):
            return "slow"

        def displayName(self):
            return "Slow"

        def createInstance(self):
            return SlowAlg()

        def initAlgorithm(self, config=None):
            self.addParameter(QgsProcessingParameterNumber("MS", "ms", defaultValue=500, minValue=-1))
            self.addOutput(QgsProcessingOutputNumber("ON_MAIN", "on main thread"))

        def processAlgorithm(self, parameters, context, feedback):
            ms = self.parameterAsInt(parameters, "MS", context)
            if ms < 0:
                raise QgsProcessingException("boom: negative duration")
            on_main = QThread.currentThread() == QCoreApplication.instance().thread()
            end = time.time() + ms / 1000.0
            while time.time() < end and not feedback.isCanceled():
                feedback.setProgress(100.0 * (1 - (end - time.time()) / (ms / 1000.0)))
                time.sleep(0.01)
            return {"ON_MAIN": 1 if on_main else 0}

    class TestProvider(QgsProcessingProvider):
        def id(self):
            return "aitest"

        def name(self):
            return "AI test"

        def loadAlgorithms(self):
            self.addAlgorithm(SlowAlg())

    test_provider = TestProvider()
    QgsApplication.processingRegistry().addProvider(test_provider)
    ticks = [0]
    ticker = QTimer()
    ticker.setInterval(20)
    ticker.timeout.connect(lambda: ticks.__setitem__(0, ticks[0] + 1))
    ticker.start()
    runner = CodeRunner(iface)

    ticks[0] = 0
    ok, out, err = runner.run('r = processing.run("aitest:slow", {"MS": 600})\nprint("on_main", r["ON_MAIN"])')
    check("processing runs in a background thread", ok and "on_main 0" in out, (out, err))
    check("QGIS responsive during processing", ticks[0] >= 10, ticks[0])

    n_layers = len(QgsProject.instance().mapLayers())
    ok, out, err = runner.run(
        'src = QgsProject.instance().mapLayersByName("cities")[0]\n'
        'r = processing.run("native:buffer", {"INPUT": src, "DISTANCE": 0.1, "OUTPUT": "TEMPORARY_OUTPUT"})\n'
        'QgsProject.instance().addMapLayer(r["OUTPUT"])\n'
        'print(r["OUTPUT"].featureCount(), r["OUTPUT"].thread() == QCoreApplication.instance().thread())')
    check("background result layer usable", ok and "2 True" in out
          and len(QgsProject.instance().mapLayers()) == n_layers + 1, (out, err))
    ok, out, err = runner.run(
        'processing.runAndLoadResults("native:buffer", {"INPUT": src, "DISTANCE": 0.2, "OUTPUT": "TEMPORARY_OUTPUT"})')
    check("runAndLoadResults loads into project", ok and len(QgsProject.instance().mapLayers()) == n_layers + 2,
          (out, err))
    ok, out, err = runner.run('processing.run("aitest:slow", {"MS": -1})')
    check("algorithm error reported", not ok and "boom: negative duration" in err and not runner.stopped, err)
    ok, out, err = runner.run(
        'processing.run("qgis:selectbyattribute", {"INPUT": src, "FIELD": "name", "OPERATOR": 0, "VALUE": "Rabat"})\n'
        'print("selected", src.selectedFeatureCount())')
    check("main-thread-only algorithm still works", ok and "selected 1" in out, (out, err))
    dl_path = os.path.join(WORK, "dl.json")
    ok, out, err = runner.run('processing.run("native:filedownloader", {{"URL": "{}/missing", "OUTPUT": r"{}"}})'
                              .format(base, dl_path))
    check("failed download reported, no crash", not ok and not runner.stopped and "Traceback" in err, (out, err))
    ok, out, err = runner.run('r = processing.run("native:filedownloader", {{"URL": "{}/anthropic/v1/models", '
                              '"OUTPUT": r"{}"}})\nprint("saved", open(r["OUTPUT"]).read()[:9])'.format(base, dl_path))
    check("download works", ok and 'saved {"data":' in out, (out, err))

    QTimer.singleShot(150, runner.stop)
    t0 = time.time()
    ok, out, err = runner.run('processing.run("aitest:slow", {"MS": 8000})\nprint("after")')
    check("stop cancels a background algorithm", runner.stopped and not ok and "after" not in out
          and time.time() - t0 < 3, (out, err, time.time() - t0))

    ticks[0] = 0
    QTimer.singleShot(300, runner.stop)
    t0 = time.time()
    ok, out, err = runner.run('i = 0\ntry:\n    while True:\n        i += len("x")\n'
                              'except Exception:\n    print("swallowed")')
    check("stop interrupts a python loop", runner.stopped and "swallowed" not in out and time.time() - t0 < 3,
          (out, err))
    check("QGIS responsive during python loop", ticks[0] >= 4, ticks[0])  # ~every 50 ms
    check("profile hook removed", sys.getprofile() is None)
    ok, out, err = runner.run('print("fine", i > 0)')
    check("next run unaffected by stop", ok and "fine True" in out and not runner.stopped, (out, err))

    dock.auto_check.setChecked(False)
    seen = {}

    def during_run():
        seen["button"] = dock.send_btn.text()
        seen["status"] = dock.status.text()
        n = len(dock.messages)
        dock.send("should be ignored while code runs")
        seen["ignored"] = len(dock.messages) == n
        dock.send_btn.click()  # Stop

    QTimer.singleShot(400, during_run)
    dock._run_code('processing.run("aitest:slow", {"MS": 8000})')
    check("send button becomes Stop while code runs", seen.get("button") == "Stop", seen)
    check("status shows progress", "Running the code... 0:00" in seen.get("status", "")
          and "%" in seen.get("status", ""), seen)
    check("no new message while code runs", seen.get("ignored"), seen)
    check("stopped note in chat", "stopped before it finished" in dock.messages[-1]["text"]
          and dock.send_btn.text() == "Send" and dock.settings_btn.isEnabled(), dock.messages[-1])
    ticker.stop()
    QgsApplication.processingRegistry().removeProvider(test_provider)

    # --- symbols that break Qt's text drawing on macOS (⚠️ = U+26A0 U+FE0F)
    dock._add_note("\u26a0\ufe0f careful: this paragraph must stay readable")
    shown = dock.browser.toPlainText()
    check("no emoji selector in the rendered chat", "\ufe0f" not in shown and "\u26a0 careful" in shown,
          repr(shown[-80:]))

    # --- a stray HTML tag from the model must not hide the rest of the chat
    dock._add_note("<br>\nThe service answered.\n\n```python\nif a<b: print('<b>')\n```")
    dock._add_note("**Text after the tag** stays visible")
    shown = dock.browser.toPlainText()
    check("stray HTML tag does not hide later text", "Text after the tag stays visible" in shown
          and "<br> The service answered." in shown and "if a<b: print('<b>')" in shown, repr(shown[-200:]))

    # --- readable in any theme (dark mode, QGIS themes, inconsistent palettes)
    from qgis.PyQt.QtGui import QColor, QPalette
    from qgis_assistant import theme
    role = lambda name: qenum(QPalette, "ColorRole", name)  # noqa: E731
    original_palette = QApplication.palette()

    def make_palette(window, text, base, link):
        pal = QPalette(original_palette)
        for r, c in (("Window", window), ("WindowText", text), ("Base", base), ("Text", text),
                     ("Button", window), ("ButtonText", text), ("Link", link)):
            pal.setColor(role(r), QColor(c))
        return pal

    def chat_contrast():
        bg, text, link, _ = dock._colors
        return min(theme.contrast(text, bg), theme.contrast(link, bg) + 1)

    # Black text on a dark background: what users saw in dark mode.
    QApplication.setPalette(make_palette("#1e1e1e", "#000000", "#262626", "#0000ff"))
    wait_until(lambda: False, 0.3)
    root_pal = dock.widget().palette()
    check("dark mode: chat text readable", chat_contrast() >= 4.5, [c.name() for c in dock._colors])
    check("dark mode: labels readable",
          theme.contrast(root_pal.color(role("WindowText")), root_pal.color(role("Window"))) >= 4.5,
          root_pal.color(role("WindowText")).name())
    check("chat uses checked colours", dock._colors[1].name() in dock.browser.styleSheet()
          and dock._colors[0].name() in dock.browser.styleSheet(), dock.browser.styleSheet())
    dlg = SettingsDialog(dock)
    check("dark mode: settings readable",
          theme.contrast(dlg.palette().color(role("WindowText")), dlg.palette().color(role("Window"))) >= 4.5)
    QApplication.setPalette(make_palette("#f0f0f0", "#ffffff", "#ffffff", "#ffff00"))  # white on white
    wait_until(lambda: False, 0.3)
    check("light theme: recoloured live", chat_contrast() >= 4.5 and dock._colors[0].name() == "#ffffff",
          [c.name() for c in dock._colors])
    QApplication.setPalette(original_palette)
    wait_until(lambda: False, 0.3)
    check("normal theme kept as is", dock._colors[1] == original_palette.color(role("Text"))
          or theme.contrast(original_palette.color(role("Text")), original_palette.color(role("Base"))) < 4.5)

    # --- translations
    from qgis_assistant import i18n
    from qgis_assistant.i18n import tr
    cases = {"fr_FR": "fr", "de-AT": "de", "pt-BR": "pt_BR", "pt_PT": "pt_PT", "pt": "pt_BR",
             "zh_CN": "zh-Hans", "zh-Hans-CN": "zh-Hans", "zh_TW": "zh-Hant", "zh-Hant-HK": "zh-Hant",
             "nn_NO": "nb", "nb-NO": "nb", "ar_MA": "ar", "en_US": "en", "xx_YY": "en", "": "en"}
    wrong = {k: i18n.match_locale(k) for k, v in cases.items() if i18n.match_locale(k) != v}
    check("system locale -> language mapping", not wrong, wrong)
    check("auto follows the system language", i18n.load("auto") == i18n.system_language())
    english = json.load(open(os.path.join(i18n.I18N_DIR, "en.json"), encoding="utf-8"))
    problems = {}
    for code in i18n.CODES:
        path = os.path.join(i18n.I18N_DIR, code + ".json")
        if not os.path.isfile(path):
            problems[code] = "missing file"
            continue
        cat = json.load(open(path, encoding="utf-8"))
        issues = sorted(set(english) ^ set(cat)) + [k for k in english if k in cat
                                                   and english[k].count("{}") != cat[k].count("{}")]
        if issues:
            problems[code] = issues[:5]
    check("all {} languages complete with matching placeholders".format(len(i18n.CODES)), not problems, problems)

    set_setting("language", "fr")
    i18n.load()
    check("french catalog loads", tr("send") == "Envoyer" and i18n.current() == "fr", tr("send"))
    titles_before = [c["title"] for c in dock.chats]
    plugin.retranslate()
    dock = plugin.dock
    check("panel rebuilt in french", dock is not None and dock.send_btn.text() == "Envoyer"
          and dock.auto_check.text() == "Exécution auto", dock and dock.send_btn.text())
    check("menu/action renamed", plugin.action.text() == "QGIS Assistant" and plugin.menu == "&QGIS Assistant")
    check("chats kept after language change", [c["title"] for c in dock.chats] == titles_before)
    dock.new_chat()
    check("welcome text in french", "Posez une question" in dock.browser.toPlainText(), dock.browser.toPlainText()[:200])
    set_setting("language", "zz")
    check("unknown language falls back to english", i18n.load() == "en" and tr("send") == "Send")
    set_setting("language", "en")
    i18n.load()

    plugin.unload()
    check("unload ok", plugin.dock is None)
    server.shutdown()


exit_code = 1
try:
    main()
except Exception:
    results.append("FAIL exception :: " + traceback.format_exc())
finally:
    failed = [r for r in results if r.startswith("FAIL")]
    print("\n".join(results))
    print("\n{} passed, {} failed".format(len(results) - len(failed), len(failed)))
    exit_code = 1 if failed else 0
    shutil.rmtree(WORK, ignore_errors=True)
    sys.stdout.flush()
    os._exit(exit_code)  # skip QGIS teardown, which can crash in headless runs
