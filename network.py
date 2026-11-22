"""Asynchronous HTTP through QGIS's network manager (honours QGIS proxy/SSL settings)."""

import json

from qgis.core import QgsNetworkAccessManager
from qgis.PyQt.QtCore import QByteArray, QObject, QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest

from .compat import qenum
from .i18n import tr
from .providers import ProviderError

_STATUS_ATTR = qenum(QNetworkRequest, "Attribute", "HttpStatusCodeAttribute")
_NO_ERROR = qenum(QNetworkReply, "NetworkError", "NoError")


def _request(url, headers):
    req = QNetworkRequest(QUrl(url))
    for k, v in headers.items():
        req.setRawHeader(k.encode("utf-8"), v.encode("utf-8"))
    return req


def _status(reply):
    value = reply.attribute(_STATUS_ATTR)
    try:
        return int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 0


def describe_http_error(status, body, fallback):
    msg = ""
    try:
        data = json.loads(body.decode("utf-8", "replace"))
        if isinstance(data, list) and data:
            data = data[0]
        err = data.get("error", data) if isinstance(data, dict) else data
        if isinstance(err, dict):
            msg = err.get("message") or err.get("msg") or json.dumps(err)
        elif err:
            msg = str(err)
    except (ValueError, AttributeError):
        msg = body.decode("utf-8", "replace").strip()[:500]
    msg = msg or fallback or tr("err_failed")
    if status in (401, 403):
        msg += " - " + tr("err_check_key")
    elif status == 404:
        msg += " - " + tr("err_check_model")
    elif status == 0:
        msg += " - " + tr("err_unreachable")
    return "HTTP {}: {}".format(status, msg) if status else msg


class StreamingCall(QObject):
    """POST a streaming request and parse its Server-Sent Events."""

    delta = pyqtSignal(str)
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, url, headers, body, parser, parent=None):
        super().__init__(parent)
        self._url, self._headers, self._body, self._parser = url, headers, body, parser
        self._reply = None
        self._buf = b""
        self._err_body = b""
        self._stream_error = None
        self._cancelled = False
        self._finished = False
        self._ended_early = False

    def start(self):
        self._reply = QgsNetworkAccessManager.instance().post(
            _request(self._url, self._headers), QByteArray(self._body))
        self._reply.readyRead.connect(self._on_ready)
        self._reply.finished.connect(self._on_finished)

    def cancel(self):
        self._cancelled = True
        if self._reply is not None and not self._finished:
            self._reply.abort()

    def _on_ready(self):
        if not self._finished:
            self._read()

    def _read(self):
        data = bytes(self._reply.readAll())
        if _status(self._reply) >= 400:
            self._err_body += data
            return
        # Normalise after joining, so a "\r\n" split across two reads is still caught.
        self._buf = (self._buf + data).replace(b"\r\n", b"\n")
        while b"\n\n" in self._buf and not self._ended_early:
            raw, self._buf = self._buf.split(b"\n\n", 1)
            self._handle_event(raw)

    def _handle_event(self, raw):
        if self._stream_error:
            return
        lines = raw.decode("utf-8", "replace").split("\n")
        payload = "\n".join(line[5:].lstrip() for line in lines if line.startswith("data:"))
        if payload == "[DONE]":
            self._parser.complete = True
        elif payload:
            try:
                obj = json.loads(payload)
            except ValueError:
                return
            try:
                text = self._parser.feed(obj)
            except ProviderError as e:
                self._stream_error = str(e)
                text = ""
            if text:
                self.delta.emit(text)
        if (getattr(self._parser, "complete", False) or self._stream_error) \
                and not self._finished and not self._cancelled:
            # The answer is over: don't wait for the server to close the connection
            # (keep-alive, proxies and local servers can hold it open).
            self._finish_now()

    def _finish_now(self):
        self._finished = self._ended_early = True
        reply = self._reply
        for signal, slot in ((reply.readyRead, self._on_ready), (reply.finished, self._on_finished)):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        reply.abort()
        reply.deleteLater()
        if self._stream_error:
            self.failed.emit(self._stream_error)
        else:
            self.done.emit(self._parser.result())
        self.deleteLater()

    def _on_finished(self):
        if self._finished:
            return
        self._finished = True
        reply = self._reply
        self._read()
        if self._buf.strip() and _status(reply) < 400:
            self._handle_event(self._buf)
            self._buf = b""
        status, error, error_text = _status(reply), reply.error(), reply.errorString()
        reply.deleteLater()

        if self._cancelled:
            result = self._parser.result(cancelled=True)
            result["cancelled"] = True
            self.done.emit(result)
        elif status >= 400 or error != _NO_ERROR:
            self.failed.emit(describe_http_error(status, self._err_body, error_text))
        elif self._stream_error:
            self.failed.emit(self._stream_error)
        else:
            self.done.emit(self._parser.result())
        self.deleteLater()


class JsonCall(QObject):
    """GET a JSON document."""

    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, url, headers, parent=None):
        super().__init__(parent)
        self._url, self._headers = url, headers
        self._reply = None

    def start(self):
        self._reply = QgsNetworkAccessManager.instance().get(_request(self._url, self._headers))
        self._reply.finished.connect(self._on_finished)

    def _on_finished(self):
        reply = self._reply
        body = bytes(reply.readAll())
        status, error, error_text = _status(reply), reply.error(), reply.errorString()
        reply.deleteLater()
        if status >= 400 or error != _NO_ERROR:
            self.failed.emit(describe_http_error(status, body, error_text))
            self.deleteLater()
            return
        try:
            data = json.loads(body.decode("utf-8"))
        except ValueError:
            self.failed.emit(tr("err_not_json"))
        else:
            self.done.emit(data)
        self.deleteLater()
