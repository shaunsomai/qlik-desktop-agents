"""Minimal Qlik Engine JSON API client for Qlik Sense Desktop.

Stdlib only, deliberately: this ships inside a Claude Code plugin, so a
pip install step would be one more thing between a user and a working setup.
Desktop exposes the engine on 127.0.0.1:4848 over an unauthenticated plain
WebSocket, which is the one case where hand-rolling RFC6455 is reasonable --
no TLS, no proxies, no auth, one request in flight at a time.
"""

from __future__ import annotations

import base64
import itertools
import json
import os
import socket
import struct

DEFAULT_HOST = os.environ.get("QLIK_DESKTOP_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("QLIK_DESKTOP_PORT", "4848"))
DEFAULT_TIMEOUT = float(os.environ.get("QLIK_DESKTOP_TIMEOUT", "60"))

GLOBAL_HANDLE = -1


class EngineError(Exception):
    """An error returned by the engine, or a transport failure talking to it."""

    def __init__(self, message, code=None, parameter=None):
        super().__init__(message)
        self.code = code
        self.parameter = parameter


class _WebSocket:
    """Client-side RFC6455 over a raw socket. Text frames only."""

    def __init__(self, host, port, path, timeout=DEFAULT_TIMEOUT):
        self.host = host
        self.port = port
        self.path = path
        self.timeout = timeout
        self._sock = None
        self._buf = b""

    def connect(self):
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as exc:
            raise EngineError(
                "Cannot reach the Qlik Sense Desktop engine at "
                "{0}:{1} ({2}). Is Qlik Sense Desktop running?".format(self.host, self.port, exc)
            ) from exc
        self._sock.settimeout(self.timeout)

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            "GET {path} HTTP/1.1\r\n"
            "Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Origin: http://{host}:{port}\r\n"
            "\r\n"
        ).format(path=self.path, host=self.host, port=self.port, key=key)
        self._sock.sendall(request.encode("ascii"))

        header = self._read_until(b"\r\n\r\n")
        status_line = header.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101" not in status_line:
            raise EngineError("WebSocket upgrade refused by the engine: " + status_line)

    def _read_until(self, marker):
        while marker not in self._buf:
            chunk = self._sock.recv(8192)
            if not chunk:
                raise EngineError("Engine closed the connection during the handshake.")
            self._buf += chunk
        head, self._buf = self._buf.split(marker, 1)
        return head + marker

    def _read_exact(self, count):
        while len(self._buf) < count:
            chunk = self._sock.recv(max(8192, count - len(self._buf)))
            if not chunk:
                raise EngineError("Engine closed the connection unexpectedly.")
            self._buf += chunk
        out, self._buf = self._buf[:count], self._buf[count:]
        return out

    def send_text(self, text):
        payload = text.encode("utf-8")
        frame = bytearray()
        frame.append(0x81)  # FIN + text opcode
        length = len(payload)
        if length < 126:
            frame.append(0x80 | length)
        elif length < (1 << 16):
            frame.append(0x80 | 126)
            frame += struct.pack(">H", length)
        else:
            frame.append(0x80 | 127)
            frame += struct.pack(">Q", length)
        mask = os.urandom(4)
        frame += mask
        frame += bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(frame))

    def _read_frame(self):
        b0, b1 = self._read_exact(2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            (length,) = struct.unpack(">H", self._read_exact(2))
        elif length == 127:
            (length,) = struct.unpack(">Q", self._read_exact(8))
        mask = self._read_exact(4) if masked else None
        payload = self._read_exact(length) if length else b""
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def recv_text(self):
        """Return the next complete text message, transparently handling
        fragmentation and control frames. Engine payloads routinely exceed
        64 KiB, so the multi-frame path is the normal path, not an edge case.
        """
        parts = []
        while True:
            fin, opcode, payload = self._read_frame()
            if opcode == 0x8:  # close
                raise EngineError("Engine closed the WebSocket connection.")
            if opcode == 0x9:  # ping -> pong with the same payload
                self._send_control(0xA, payload)
                continue
            if opcode == 0xA:  # unsolicited pong
                continue
            if opcode in (0x0, 0x1, 0x2):
                parts.append(payload)
                if fin:
                    return b"".join(parts).decode("utf-8")
                continue
            raise EngineError("Unexpected WebSocket opcode from the engine: " + hex(opcode))

    def _send_control(self, opcode, payload):
        frame = bytearray([0x80 | opcode, 0x80 | len(payload)])
        mask = os.urandom(4)
        frame += mask
        frame += bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(bytes(frame))

    def close(self):
        if self._sock is None:
            return
        try:
            self._send_control(0x8, b"")
        except OSError:
            pass
        try:
            self._sock.close()
        finally:
            self._sock = None


class EngineSession:
    """One WebSocket to the Desktop engine, plus JSON-RPC bookkeeping."""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):
        self._ws = _WebSocket(host, port, "/app/engineData", timeout)
        self._ids = itertools.count(1)
        self._open_docs = {}
        self._connected = False

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def connect(self):
        if not self._connected:
            self._ws.connect()
            self._connected = True

    def set_timeout(self, timeout):
        """Widen or narrow the socket budget on a live connection."""
        self._ws.timeout = timeout
        if self._ws._sock is not None:
            self._ws._sock.settimeout(timeout)

    def close(self):
        if self._connected:
            self._ws.close()
            self._connected = False
            self._open_docs.clear()

    def rpc(self, method, handle=GLOBAL_HANDLE, params=None):
        """Send one JSON-RPC call and return its result payload.

        The engine interleaves unsolicited change and progress notifications
        with responses, so read until the id matches rather than assuming the
        next message is ours.
        """
        self.connect()
        request_id = next(self._ids)
        self._ws.send_text(json.dumps({
            "jsonrpc": "2.0",
            "id": request_id,
            "handle": handle,
            "method": method,
            "params": params if params is not None else [],
        }))
        while True:
            message = json.loads(self._ws.recv_text())
            if message.get("id") != request_id:
                continue  # OnConnected, OnDocSaved, change notifications
            if "error" in message:
                err = message["error"]
                raise EngineError(
                    "{0} failed: {1}".format(method, err.get("message", "unknown engine error")),
                    code=err.get("code"),
                    parameter=err.get("parameter"),
                )
            return message.get("result", {})

    # -- global ------------------------------------------------------------

    def engine_version(self):
        return self.rpc("EngineVersion").get("qVersion", {})

    def product_version(self):
        try:
            return self.rpc("ProductVersion").get("qReturn", "")
        except EngineError:
            return ""

    def doc_list(self):
        return self.rpc("GetDocList").get("qDocList", [])

    def open_doc(self, app_id, no_data=False):
        """Return a doc handle, opening the app if needed. app_id is the
        qDocId from GetDocList, which on Desktop is the full .qvf path.
        """
        cache_key = (app_id, no_data)
        if cache_key in self._open_docs:
            return self._open_docs[cache_key]
        result = self.rpc("OpenDoc", GLOBAL_HANDLE, [app_id, "", "", "", no_data])
        handle = result["qReturn"]["qHandle"]
        self._open_docs[cache_key] = handle
        return handle

    def create_app(self, name):
        return self.rpc("CreateApp", GLOBAL_HANDLE, [name])

    # -- doc helpers -------------------------------------------------------

    def session_object_layout(self, doc, definition):
        """Create a session object, read its layout, destroy it. Session
        objects are how the engine exposes sheet, measure, dimension and field
        inventories; leaving them behind leaks engine memory.
        """
        created = self.rpc("CreateSessionObject", doc, [definition])
        obj_handle = created["qReturn"]["qHandle"]
        obj_id = definition["qInfo"].get("qId")
        try:
            return self.rpc("GetLayout", obj_handle).get("qLayout", {})
        finally:
            if obj_id:
                try:
                    self.rpc("DestroySessionObject", doc, [obj_id])
                except EngineError:
                    pass

    def object_handle(self, doc, object_id):
        return self.rpc("GetObject", doc, [object_id])["qReturn"]["qHandle"]

    def save(self, doc):
        return self.rpc("DoSave", doc, [""])


_SHARED = None


def shared_session():
    """One long-lived connection for the whole server process.

    Selections, open-doc handles and session objects all belong to the
    WebSocket, so opening a fresh connection per tool call would throw away
    exactly the state the selection tools exist to manage.
    """
    global _SHARED
    if _SHARED is None:
        _SHARED = EngineSession()
    return _SHARED


def reset_shared_session():
    global _SHARED
    if _SHARED is not None:
        try:
            _SHARED.close()
        except Exception:  # noqa: BLE001 - teardown must not mask the real error
            pass
    _SHARED = None


if __name__ == "__main__":  # smoke test against a running Desktop engine
    with EngineSession() as session:
        print("engine version :", session.engine_version().get("qComponentVersion", "?"))
        print("product version:", session.product_version())
        docs = session.doc_list()
        print("apps           :", len(docs))
        for doc in docs:
            print("   -", doc.get("qTitle"), "|", doc.get("qDocId"))
