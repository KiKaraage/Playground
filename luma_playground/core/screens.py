# SPDX-License-Identifier: Apache-2.0
"""Screens in Preview: a VM, another of your machines, or an app on a headless display, shown inline.

Anything that offers its screen over VNC can be a Preview: a virtual machine started with
``qemu-system-x86_64 ... -vnc 127.0.0.1:1`` (port 5901), a Mac with Screen Sharing on, a Linux
app on a headless compositor (``weston --backend=vnc``, ``wayvnc``), or a Windows machine running
a VNC server. ``.playground/run.toml`` says where it is:

    [app]
    kind = "vnc"
    label = "Windows test machine"
    command = "qemu-system-x86_64 -m 4G -drive file=.playground/local/win.qcow2 -vnc 127.0.0.1:1"   # optional
    host = "127.0.0.1"
    screen_port = 5901
    password_env = "WIN_VNC_PASSWORD"     # optional: the variable that holds the password

``ScreenBridge`` serves the noVNC viewer (downloaded once into Playground's data folder, pinned and
checked like Playground's own Node.js) on 127.0.0.1 and relays its WebSocket to the screen's VNC port, so the Preview's
browser view shows the screen and you can use it with the mouse and keyboard. The password is
never put in the address: the viewer asks for it, or the bridge reads it from ``password_env``.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import logging
import mimetypes
import os
from pathlib import Path
import socket
import struct
import threading
import urllib.parse

log = logging.getLogger(__name__)

# The noVNC viewer (MPL-2.0), its own release: pinned, checked, and unpacked once into Playground's data folder.
NOVNC_VERSION = "1.5.0"
NOVNC_URL = f"https://github.com/novnc/noVNC/archive/refs/tags/v{NOVNC_VERSION}.tar.gz"
NOVNC_SHA256 = "6a73e41f98388a5348b7902f54b02d177cb73b7e5eb0a7a0dcf688cc2c79b42a"
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_install_lock = threading.Lock()


class ScreenError(RuntimeError):
    pass


def viewer_dir() -> Path:
    from . import paths
    return paths.data_dir() / "runtime" / f"noVNC-{NOVNC_VERSION}"


def ensure_viewer(*, opener=None) -> Path:
    """The noVNC viewer, downloaded the first time it's needed and checked against its sha256."""
    folder = viewer_dir()
    if (folder / "vnc.html").is_file():
        return folder
    import hashlib as _hashlib
    import shutil
    import tempfile
    import urllib.request
    from ..agents import registry
    with _install_lock:
        if (folder / "vnc.html").is_file():
            return folder
        folder.parent.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=".novnc-", dir=str(folder.parent)))
        try:
            archive = work / "novnc.tar.gz"
            digest = _hashlib.sha256()
            request = urllib.request.Request(NOVNC_URL, headers={"User-Agent": "Luma-Playground"})
            try:
                with (opener or urllib.request.urlopen)(request, timeout=120) as response, open(archive, "wb") as out:
                    while chunk := response.read(1 << 18):
                        digest.update(chunk)
                        out.write(chunk)
            except OSError as error:
                raise ScreenError("Playground couldn’t download the screen viewer. Check the internet connection "
                                  "and try again.") from error
            if digest.hexdigest() != NOVNC_SHA256:
                raise ScreenError("The screen viewer’s download didn’t match its checksum, so Playground threw it away.")
            unpacked = work / "unpacked"
            unpacked.mkdir()
            try:
                registry._extract(archive, NOVNC_URL, unpacked)
            except registry.RegistryError as error:
                raise ScreenError(str(error)) from error
            top = unpacked / f"noVNC-{NOVNC_VERSION}"
            if not (top / "vnc.html").is_file():
                raise ScreenError("The screen viewer downloaded, but not as expected.")
            if folder.exists():
                shutil.rmtree(folder)
            os.replace(top, folder)
        finally:
            shutil.rmtree(work, ignore_errors=True)
    return folder


class ScreenBridge:
    """The viewer on http://127.0.0.1:<port>/ and a WebSocket relay to host:screen_port."""

    def __init__(self, host: str, screen_port: int, *, port: int = 0, password: str = "",
                 viewer: Path | None = None) -> None:
        self.host, self.screen_port = host, int(screen_port)
        self.port = port
        self.password = password
        self.viewer = viewer
        self._server: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        query = urllib.parse.urlencode({"autoconnect": "1", "resize": "scale", "reconnect": "1",
                                        "reconnect_delay": "1500", "path": "websockify", "show_dot": "1"})
        return f"http://127.0.0.1:{self.port}/vnc.html?{query}"

    def start(self) -> None:
        if self.viewer is None:
            self.viewer = ensure_viewer()
        bridge = self

        class Handler(_Handler):
            pass
        Handler.bridge = bridge
        self._server = _Server(("127.0.0.1", self.port), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, name="screen-bridge", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()

    def screen_answers(self, timeout: float = 1.0) -> bool:
        try:
            with socket.create_connection((self.host, self.screen_port), timeout=timeout) as sock:
                sock.settimeout(timeout)
                return sock.recv(12).startswith(b"RFB ")      # a VNC server says hello first
        except OSError:
            return False


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self) -> None:          # no reverse DNS lookup (it can stall the bind)
        import socketserver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class _Handler(http.server.BaseHTTPRequestHandler):
    bridge: ScreenBridge

    def log_message(self, *_args) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - http.server's name
        path = urllib.parse.urlparse(self.path).path
        if path.rstrip("/") == "/websockify" and self.headers.get("Upgrade", "").lower() == "websocket":
            self._relay()
            return
        self._static(path)

    def _static(self, path: str) -> None:
        root = self.bridge.viewer
        relative = urllib.parse.unquote(path).lstrip("/") or "vnc.html"
        target = (root / relative).resolve()
        if root is None or not str(target).startswith(str(root.resolve()) + os.sep) or not target.is_file():
            self.send_error(404)
            return
        data = target.read_bytes()
        self.send_response(200)
        kind = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".js":
            kind = "text/javascript"
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ── RFC 6455, just enough for noVNC's binary frames ──

    def _relay(self) -> None:
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        try:
            screen = socket.create_connection((self.bridge.host, self.bridge.screen_port), timeout=10)
        except OSError:
            self.send_error(502, "The screen isn't answering")
            return
        screen.settimeout(None)
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        if "binary" in (self.headers.get("Sec-WebSocket-Protocol") or ""):
            self.send_header("Sec-WebSocket-Protocol", "binary")
        self.end_headers()
        client = self.connection
        done = threading.Event()

        def screen_to_client() -> None:
            try:
                while not done.is_set():
                    data = screen.recv(65536)
                    if not data:
                        break
                    client.sendall(_frame(data, opcode=0x2))
            except OSError:
                pass
            finally:
                done.set()

        threading.Thread(target=screen_to_client, daemon=True, name="screen-out").start()
        try:
            while not done.is_set():
                opcode, payload = _read_frame(self.rfile)
                if opcode is None or opcode == 0x8:
                    break
                if opcode == 0x9:
                    client.sendall(_frame(payload, opcode=0xA))
                elif opcode in (0x1, 0x2, 0x0):
                    screen.sendall(payload)
        except OSError:
            pass
        finally:
            done.set()
            for sock in (screen,):
                try:
                    sock.close()
                except OSError:
                    pass


def _frame(payload: bytes, opcode: int = 0x2) -> bytes:
    head = bytearray([0x80 | opcode])
    size = len(payload)
    if size < 126:
        head.append(size)
    elif size < 1 << 16:
        head.append(126)
        head += struct.pack("!H", size)
    else:
        head.append(127)
        head += struct.pack("!Q", size)
    return bytes(head) + payload


def _read_exact(stream, size: int) -> bytes | None:
    data = b""
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def _read_frame(stream) -> tuple[int | None, bytes]:
    head = _read_exact(stream, 2)
    if head is None:
        return None, b""
    opcode = head[0] & 0x0F
    masked = head[1] & 0x80
    size = head[1] & 0x7F
    if size == 126:
        more = _read_exact(stream, 2)
        if more is None:
            return None, b""
        size = struct.unpack("!H", more)[0]
    elif size == 127:
        more = _read_exact(stream, 8)
        if more is None:
            return None, b""
        size = struct.unpack("!Q", more)[0]
    mask = _read_exact(stream, 4) if masked else b"\0\0\0\0"
    payload = _read_exact(stream, size) if size else b""
    if mask is None or payload is None:
        return None, b""
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload
