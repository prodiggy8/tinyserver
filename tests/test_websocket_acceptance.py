"""End-to-end acceptance suite for specs/message-wall.md's criteria #6-15:
a raw-socket WebSocket test client (handshake + frame codec built on top of
src/websocket.py's encode/decode helpers) driving a real HttpServer wired
via src/app.py's `build_router` factory on an ephemeral port.

Each test builds its own store/registry (an isolated tmp_path-backed
MessageStore, a fresh ConnectionRegistry) via `make_server`, so tests never
touch the real data/messages.jsonl and can run concurrently without
cross-test pollution — the module-level singletons in src/app.py are
never imported here.
"""

import base64
import http.client
import json
import os
import socket
import struct
import time

from app import build_router
from chat import ConnectionRegistry, MessageStore
from cookies import is_valid_chatname
from server import HttpServer
from websocket import OPCODE_CLOSE, OPCODE_PING, OPCODE_PONG, OPCODE_TEXT, encode_frame

HANDSHAKE_KEY = base64.b64encode(b"0123456789012345").decode("ascii")


def make_server(tmp_path, store_path=None, max_connections=128, idle_timeout=5.0):
    path = store_path or str(tmp_path / "messages.jsonl")
    store = MessageStore(path=path)
    registry = ConnectionRegistry(max_connections=max_connections)
    router = build_router(store, registry)
    srv = HttpServer(host="127.0.0.1", port=0, handler=router.dispatch,
                     idle_timeout=idle_timeout)
    port = srv.start()
    return srv, port, path


class WsClient:
    """A minimal raw-socket WebSocket client: performs the handshake,
    tracks cookies manually, and sends/receives single-frame text messages
    (masked on send, per RFC 6455 client requirements; unmasked expected
    on receive, matching src/websocket.py's server-side encode_frame)."""

    def __init__(self, port, cookie=None, timeout=2.0):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self._buf = b""
        headers = (
            "GET /ws HTTP/1.1\r\n"
            "Host: x\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: {}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
        ).format(HANDSHAKE_KEY)
        if cookie:
            headers += "Cookie: chatname={}\r\n".format(cookie)
        headers += "\r\n"
        self.sock.sendall(headers.encode("ascii"))
        head = self._read_head()
        assert head.startswith(b"HTTP/1.1 101"), head

    def _read_head(self):
        while b"\r\n\r\n" not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            self._buf += chunk
        head, _, rest = self._buf.partition(b"\r\n\r\n")
        self._buf = rest
        return head

    def _read_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("socket closed")
            self._buf += chunk
        data = self._buf[:n]
        self._buf = self._buf[n:]
        return data

    def read_frame(self):
        header = self._read_exact(2)
        b0, b1 = header
        opcode = b0 & 0x0F
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        payload = self._read_exact(length) if length else b""
        return opcode, payload

    def read_json(self, timeout=2.0):
        self.sock.settimeout(timeout)
        while True:
            opcode, payload = self.read_frame()
            if opcode == OPCODE_TEXT:
                return json.loads(payload)
            if opcode == OPCODE_PING:
                self.send_raw(encode_frame(OPCODE_PONG, payload, mask_key=b"\x01\x02\x03\x04"))
                continue
            raise AssertionError("unexpected non-text frame opcode {}".format(opcode))

    def send_raw(self, frame_bytes):
        self.sock.sendall(frame_bytes)

    def post(self, text):
        payload = json.dumps({"type": "post", "text": text}).encode("utf-8")
        self.send_raw(encode_frame(OPCODE_TEXT, payload, mask_key=b"\x01\x02\x03\x04"))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def connect(port, cookie=None):
    """Handshake, then drain the two frames every registration always
    sends in order: the `welcome` frame (built under the registry lock,
    per 2.5's `build_initial_frames`) followed immediately by the
    self-join `visitors` count broadcast that `ConnectionRegistry.register`
    always sends afterward, including to the connection that just joined.
    Tests that only care about steady-state traffic call this instead of
    a single `read_json()` so that self-join frame doesn't linger in the
    queue and get mistaken for a later reply."""
    client = WsClient(port, cookie=cookie)
    welcome = client.read_json()
    own_join_visitors = client.read_json()
    assert own_join_visitors == {"type": "visitors", "count": welcome["visitors"]}
    return client, welcome


# --- 6. Handshake + welcome frame -------------------------------------------

def test_acceptance_6_welcome_frame_contents(tmp_path):
    srv, port, _path = make_server(tmp_path)
    try:
        client = WsClient(port)
        welcome = client.read_json()
        assert welcome["type"] == "welcome"
        assert is_valid_chatname(welcome["name"])
        assert isinstance(welcome["messages"], list)
        assert isinstance(welcome["visitors"], int)
        assert welcome["visitors"] >= 1
        client.close()
    finally:
        srv.stop()


def test_idle_websocket_survives_past_the_http_idle_timeout(tmp_path):
    """Regression: the hijacked socket kept the HTTP keep-alive idle
    timeout (server._handle_connection's sock.settimeout), so read_frame
    raised socket.timeout — caught by the read loop's `except OSError` —
    and every idle chat client was dropped after idle_timeout seconds. In
    production that meant a 5-second disconnect, long before the 60-second
    ping deadline: the other person in the chat vanished while you typed.
    Liveness belongs to PingScheduler, so a read timeout must not end the
    connection."""
    srv, port, _path = make_server(tmp_path, idle_timeout=0.3)
    try:
        a, _welcome_a = connect(port)
        b, _welcome_b = connect(port)
        a.read_json()  # b's join

        time.sleep(1.2)  # 4x the idle timeout

        b.post("still here?")
        relayed = a.read_json()
        assert relayed["type"] == "message"
        assert relayed["text"] == "still here?"
        a.close()
        b.close()
    finally:
        srv.stop()


# --- 7. Message relay between two clients -----------------------------------

def test_acceptance_7_message_relayed_within_one_second(tmp_path):
    srv, port, _path = make_server(tmp_path)
    try:
        a, welcome_a = connect(port)
        b, _welcome_b = connect(port)

        start = time.monotonic()
        a.post("hello from A")
        msg = b.read_json()
        elapsed = time.monotonic() - start

        assert elapsed < 1.0
        assert msg["type"] == "message"
        assert msg["name"] == welcome_a["name"]
        assert msg["text"] == "hello from A"
        a.close()
        b.close()
    finally:
        srv.stop()


# --- 8. Visitor count on join/leave ------------------------------------------

def test_acceptance_8_visitor_count_updates_on_join_and_leave(tmp_path):
    srv, port, _path = make_server(tmp_path)
    try:
        a, _welcome_a = connect(port)  # visitors=1

        b, welcome_b = connect(port)
        assert welcome_b["visitors"] == 2

        visitors_a = a.read_json()
        assert visitors_a == {"type": "visitors", "count": 2}

        b.close()

        visitors_after_leave = a.read_json()
        assert visitors_after_leave == {"type": "visitors", "count": 1}
        a.close()
    finally:
        srv.stop()


# --- 9. Abrupt disconnect resilience -----------------------------------------

def test_acceptance_9_abrupt_disconnect_keeps_server_and_other_client_alive(tmp_path):
    srv, port, _path = make_server(tmp_path)
    try:
        a, _welcome_a = connect(port)
        b, _welcome_b = connect(port)

        a.sock.shutdown(socket.SHUT_RDWR)
        a.sock.close()

        visitors = b.read_json()
        assert visitors == {"type": "visitors", "count": 1}

        b.post("still alive")
        msg = b.read_json()
        assert msg["type"] == "message"
        assert msg["text"] == "still alive"

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/")
        resp = conn.getresponse()
        assert resp.status == 200
        resp.read()
        conn.close()
        b.close()
    finally:
        srv.stop()


# --- 10. Persistence across restart ------------------------------------------

def test_acceptance_10_persistence_across_restart(tmp_path):
    store_path = str(tmp_path / "messages.jsonl")
    srv, port, _path = make_server(tmp_path, store_path=store_path)
    try:
        client, _welcome = connect(port)
        client.post("persisted message")
        client.read_json()  # our own broadcast message frame
        client.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/api/messages")
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        assert any(m["text"] == "persisted message" for m in body)
    finally:
        srv.stop()

    srv2, port2, _path2 = make_server(tmp_path, store_path=store_path)
    try:
        client2, welcome = connect(port2)
        assert any(m["text"] == "persisted message" for m in welcome["messages"])
        client2.close()
    finally:
        srv2.stop()


# --- 11. Rate limit -----------------------------------------------------------

def test_acceptance_11_rate_limit_sixth_post_in_window(tmp_path):
    srv, port, _path = make_server(tmp_path)
    try:
        client, _welcome = connect(port)

        for i in range(5):
            client.post("msg%d" % i)
            reply = client.read_json()
            assert reply["type"] == "message"

        client.post("one too many")
        reply = client.read_json()
        assert reply == {"type": "error", "reason": "rate_limited"}

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/api/messages")
        resp = conn.getresponse()
        body = json.loads(resp.read())
        conn.close()
        assert not any(m["text"] == "one too many" for m in body)
        client.close()
    finally:
        srv.stop()


# --- 12. Length limit -----------------------------------------------------------

def test_acceptance_12_length_limit(tmp_path):
    srv, port, _path = make_server(tmp_path)
    try:
        client, _welcome = connect(port)

        client.post("a" * 501)
        reply = client.read_json()
        assert reply == {"type": "error", "reason": "too_long"}

        client.post("b" * 500)
        reply = client.read_json()
        assert reply["type"] == "message"
        assert reply["text"] == "b" * 500

        client.close()
    finally:
        srv.stop()


# --- 13. Name persistence via cookie -----------------------------------------

def test_acceptance_13_name_persists_via_cookie(tmp_path):
    srv, port, _path = make_server(tmp_path)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/")
        resp = conn.getresponse()
        resp.read()
        set_cookie = resp.getheader("Set-Cookie")
        conn.close()
        assert set_cookie is not None
        assert set_cookie.startswith("chatname=")
        name = set_cookie.split(";")[0].split("=", 1)[1]

        client1, welcome1 = connect(port, cookie=name)
        assert welcome1["name"] == name
        client1.close()

        client2, welcome2 = connect(port)  # no cookie -> fresh name
        assert welcome2["name"] != name
        client2.close()
    finally:
        srv.stop()


# --- 14. XSS round-trip through the store ------------------------------------

def test_acceptance_14_xss_message_round_trips_as_literal_text(tmp_path):
    srv, port, _path = make_server(tmp_path)
    try:
        client, _welcome = connect(port)
        payload = "<script>alert(1)</script>"
        client.post(payload)
        reply = client.read_json()
        assert reply["text"] == payload
        client.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        conn.request("GET", "/api/messages")
        resp = conn.getresponse()
        assert resp.getheader("Content-Type") == "application/json"
        assert resp.getheader("X-Content-Type-Options") == "nosniff"
        body = json.loads(resp.read())
        conn.close()
        assert any(m["text"] == payload for m in body)
    finally:
        srv.stop()


def test_acceptance_14_chat_js_never_uses_inner_html():
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    chat_js_path = os.path.join(project_root, "public", "chat.js")
    with open(chat_js_path, "r", encoding="utf-8") as f:
        source = f.read()
    assert "innerHTML" not in source


# --- 15. Step 1 unbroken + forbidden-import guard ----------------------------
# Both tests/test_acceptance.py (Step 1's full acceptance suite) and
# tests/test_no_forbidden_imports.py run in the same ./script/test session
# as this file, so a green run already demonstrates #15; no separate check
# is needed here.


# --- Close handshake / ping-pong (spec §3, not separately numbered) ---------

def test_close_handshake_echoes_close_frame(tmp_path):
    srv, port, _path = make_server(tmp_path)
    try:
        client, _welcome = connect(port)
        client.send_raw(encode_frame(OPCODE_CLOSE, struct.pack("!H", 1000), mask_key=b"\x01\x02\x03\x04"))
        client.sock.settimeout(2.0)
        opcode, payload = client.read_frame()
        assert opcode == OPCODE_CLOSE
        client.close()
    finally:
        srv.stop()


def test_bad_request_error_frame_on_malformed_json(tmp_path):
    srv, port, _path = make_server(tmp_path)
    try:
        client, _welcome = connect(port)
        client.send_raw(encode_frame(OPCODE_TEXT, b"not json", mask_key=b"\x01\x02\x03\x04"))
        reply = client.read_json()
        assert reply == {"type": "error", "reason": "bad_request"}
        client.close()
    finally:
        srv.stop()
