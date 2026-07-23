import json
import struct
import time

from cookies import is_valid_chatname
from http_parse import BufferedReader
from websocket import (
    OPCODE_CLOSE,
    OPCODE_PING,
    OPCODE_TEXT,
    encode_frame as ws_encode_frame,
)

from chat import (
    Connection,
    ConnectionRegistry,
    MessageStore,
    PingScheduler,
    RateLimiter,
    generate_name,
    handle_message,
    serve_connection,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------

class RecordingSocket:
    """Records every frame sent via sendall(); close()/shutdown() are
    no-ops that just record that they happened."""

    def __init__(self):
        self.sent = []
        self.closed = False
        self.shutdown_calls = []

    def sendall(self, data):
        if self.closed:
            raise OSError("send on closed socket")
        self.sent.append(data)

    def shutdown(self, how):
        self.shutdown_calls.append(how)

    def close(self):
        self.closed = True


class BlockingSocket:
    """A socket whose sendall() blocks forever — simulates a client whose
    TCP receive buffer is permanently full."""

    def __init__(self):
        self.sent = []
        self._gate = __import__("threading").Event()

    def sendall(self, data):
        self._gate.wait()
        self.sent.append(data)

    def shutdown(self, how):
        pass

    def close(self):
        pass


class RaisingRecvSocket:
    """A socket-like object whose recv() always raises, simulating a TCP
    reset / abrupt disconnect."""

    def recv(self, n):
        raise OSError("connection reset")


class _OneShotSocket:
    """Delivers a fixed byte string via recv(), then EOF."""

    def __init__(self, data):
        self._data = data

    def recv(self, n):
        chunk, self._data = self._data[:n], self._data[n:]
        return chunk


class DummyConn:
    def __init__(self, name="quietfalcon12"):
        self.name = name
        self.sent = []

    def enqueue(self, frame):
        self.sent.append(frame)
        return True


def decode_server_frame(frame_bytes):
    """Minimal decoder for the server's own (always-unmasked, single-frame)
    output — used only to inspect frames built by chat.py in tests."""
    b0, b1 = frame_bytes[0], frame_bytes[1]
    opcode = b0 & 0x0F
    length = b1 & 0x7F
    offset = 2
    if length == 126:
        length = struct.unpack("!H", frame_bytes[2:4])[0]
        offset = 4
    elif length == 127:
        length = struct.unpack("!Q", frame_bytes[2:10])[0]
        offset = 10
    payload = frame_bytes[offset:offset + length]
    return opcode, payload


def client_frame(opcode, payload=b"", mask_key=b"\x01\x02\x03\x04"):
    return ws_encode_frame(opcode, payload, mask_key=mask_key)


def wait_until(predicate, timeout=2.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def last_error_reason(conn):
    opcode, payload = decode_server_frame(conn.sent[-1])
    assert opcode == OPCODE_TEXT
    obj = json.loads(payload)
    assert obj["type"] == "error"
    return obj["reason"]


# ---------------------------------------------------------------------------
# Name generation
# ---------------------------------------------------------------------------

def test_generate_name_matches_chatname_format():
    for _ in range(200):
        name = generate_name()
        assert is_valid_chatname(name)


# ---------------------------------------------------------------------------
# MessageStore
# ---------------------------------------------------------------------------

def test_store_missing_file_starts_empty(tmp_path):
    store = MessageStore(path=str(tmp_path / "messages.jsonl"))
    assert store.recent() == []


def test_store_append_and_truncate_to_last_n(tmp_path):
    path = str(tmp_path / "messages.jsonl")
    store = MessageStore(path=path, max_messages=3)
    for i in range(5):
        store.append({"name": "a", "text": "msg%d" % i, "ts": i})
    recent = store.recent()
    assert [m["text"] for m in recent] == ["msg2", "msg3", "msg4"]

    # Simulate a restart: a fresh MessageStore over the same file.
    reloaded = MessageStore(path=path, max_messages=3)
    assert [m["text"] for m in reloaded.recent()] == ["msg2", "msg3", "msg4"]


def test_store_tolerates_partial_final_line(tmp_path):
    path = tmp_path / "messages.jsonl"
    good_lines = [
        json.dumps({"name": "a", "text": "one", "ts": 1}),
        json.dumps({"name": "a", "text": "two", "ts": 2}),
    ]
    partial = '{"name": "a", "text": "cut off'
    path.write_text("\n".join(good_lines + [partial]) + "\n", encoding="utf-8")

    store = MessageStore(path=str(path), max_messages=100)
    assert [m["text"] for m in store.recent()] == ["one", "two"]

    # Startup truncation rewrote the file without the corrupt line.
    rewritten = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(rewritten) == 2


# ---------------------------------------------------------------------------
# ConnectionRegistry
# ---------------------------------------------------------------------------

def test_connection_cap_refuses_registration_when_full():
    registry = ConnectionRegistry(max_connections=2)
    a = registry.register(RecordingSocket(), "quietfalcon20")
    b = registry.register(RecordingSocket(), "brightotter21")
    assert a is not None and b is not None
    c = registry.register(RecordingSocket(), "sunnyraven22")
    assert c is None
    assert registry.count() == 2


def test_unregister_is_idempotent_and_broadcasts_visitor_count_once():
    registry = ConnectionRegistry()
    sock = RecordingSocket()
    other_sock = RecordingSocket()
    conn = registry.register(sock, "quietfalcon23")
    other = registry.register(other_sock, "brightotter24")

    # Writer threads deliver asynchronously — wait for the registration
    # broadcast to actually land before clearing, or the clear can race it.
    assert wait_until(lambda: len(other_sock.sent) >= 1)
    other_sock.sent.clear()
    assert registry.unregister(conn) is True
    assert registry.unregister(conn) is False

    def has_one_visitor_frame():
        frames = [f for f in other_sock.sent if decode_server_frame(f)[0] == OPCODE_TEXT
                  and json.loads(decode_server_frame(f)[1]).get("type") == "visitors"]
        return len(frames) == 1

    assert wait_until(has_one_visitor_frame)


def test_broadcast_drops_a_slow_connection_without_blocking_the_others():
    registry = ConnectionRegistry(max_connections=10)
    blocking_sock = BlockingSocket()
    healthy_sock = RecordingSocket()
    registry.register(blocking_sock, "quietfalcon25")
    healthy_conn = registry.register(healthy_sock, "brightotter26")

    for i in range(80):
        registry.broadcast(ws_encode_frame(OPCODE_TEXT, ("msg%d" % i).encode("utf-8")))
        # Yield so the healthy connection's writer thread gets a chance to
        # drain its queue between enqueues — its fake sendall() never
        # blocks on real I/O (which would release the GIL on its own), so
        # a tight loop here could otherwise starve it too.
        time.sleep(0.001)

    assert wait_until(lambda: registry.count() == 1)
    assert registry.snapshot() == [healthy_conn]
    assert len(healthy_sock.sent) >= 70


# ---------------------------------------------------------------------------
# Ping scheduler
# ---------------------------------------------------------------------------

def test_ping_scheduler_drops_idle_connection_but_keeps_one_that_pongs():
    registry = ConnectionRegistry()
    idle_sock = RecordingSocket()
    live_sock = RecordingSocket()
    registry.register(idle_sock, "quietfalcon27")
    live_conn = registry.register(live_sock, "brightotter28")
    scheduler = PingScheduler(registry, interval=0.05, timeout=0.15)

    for _ in range(6):
        time.sleep(0.05)
        scheduler.tick()
        live_conn.touch()  # simulate this connection responding each tick

    assert registry.count() == 1
    assert registry.snapshot()[0] is live_conn
    assert any(decode_server_frame(f)[0] == OPCODE_PING for f in live_sock.sent)


# ---------------------------------------------------------------------------
# Rate limit / length limit / protocol dispatch
# ---------------------------------------------------------------------------

def test_handle_message_bad_request_then_recovers(tmp_path):
    store = MessageStore(path=str(tmp_path / "messages.jsonl"))
    registry = ConnectionRegistry()
    conn = DummyConn()
    limiter = RateLimiter()

    handle_message("not json", conn, store, registry, limiter)
    assert last_error_reason(conn) == "bad_request"

    handle_message(json.dumps({"type": "nonsense"}), conn, store, registry, limiter)
    assert last_error_reason(conn) == "bad_request"

    before = len(conn.sent)
    handle_message(json.dumps({"type": "post", "text": "hello"}), conn, store, registry, limiter)
    assert len(conn.sent) == before  # no new error frame
    assert store.recent()[-1]["text"] == "hello"


def test_handle_message_rate_limited_after_five_in_window(tmp_path):
    store = MessageStore(path=str(tmp_path / "messages.jsonl"))
    registry = ConnectionRegistry()
    conn = DummyConn()
    limiter = RateLimiter()

    for i in range(5):
        handle_message(json.dumps({"type": "post", "text": "msg%d" % i}), conn, store, registry, limiter)
    assert conn.sent == []

    handle_message(json.dumps({"type": "post", "text": "one too many"}), conn, store, registry, limiter)
    assert last_error_reason(conn) == "rate_limited"
    assert len(store.recent()) == 5


def test_handle_message_length_limit(tmp_path):
    store = MessageStore(path=str(tmp_path / "messages.jsonl"))
    registry = ConnectionRegistry()

    ok_conn = DummyConn("quietfalcon29")
    handle_message(json.dumps({"type": "post", "text": "a" * 500}), ok_conn, store, registry, RateLimiter())
    assert ok_conn.sent == []
    assert store.recent()[-1]["text"] == "a" * 500

    long_conn = DummyConn("brightotter30")
    handle_message(json.dumps({"type": "post", "text": "a" * 501}), long_conn, store, registry, RateLimiter())
    assert last_error_reason(long_conn) == "too_long"
    assert len(store.recent()) == 1


# ---------------------------------------------------------------------------
# serve_connection: close handshake + abrupt disconnect
# ---------------------------------------------------------------------------

def test_serve_connection_echoes_close_frame_and_shuts_down(tmp_path):
    store = MessageStore(path=str(tmp_path / "messages.jsonl"))
    registry = ConnectionRegistry()
    sock = RecordingSocket()
    conn = registry.register(sock, "quietfalcon31")

    close_bytes = client_frame(OPCODE_CLOSE, struct.pack("!H", 1000))
    reader = BufferedReader(_OneShotSocket(close_bytes))

    serve_connection(sock, reader, conn, store, registry)

    assert registry.count() == 0
    assert sock.closed is True
    assert any(decode_server_frame(f)[0] == OPCODE_CLOSE for f in sock.sent)


def test_serve_connection_handles_abrupt_disconnect_without_raising(tmp_path):
    store = MessageStore(path=str(tmp_path / "messages.jsonl"))
    registry = ConnectionRegistry()
    sock = RecordingSocket()
    conn = registry.register(sock, "quietfalcon32")

    reader = BufferedReader(RaisingRecvSocket())

    serve_connection(sock, reader, conn, store, registry)  # must not raise

    assert registry.count() == 0
    assert sock.closed is True


def test_serve_connection_posts_a_message_and_pongs_a_ping(tmp_path):
    store = MessageStore(path=str(tmp_path / "messages.jsonl"))
    registry = ConnectionRegistry()
    sock = RecordingSocket()
    conn = registry.register(sock, "quietfalcon33")

    post = client_frame(OPCODE_TEXT, json.dumps({"type": "post", "text": "hi"}).encode("utf-8"))
    ping = client_frame(OPCODE_PING, b"abc")
    close = client_frame(OPCODE_CLOSE, struct.pack("!H", 1000))
    reader = BufferedReader(_OneShotSocket(post + ping + close))

    serve_connection(sock, reader, conn, store, registry)

    assert store.recent()[-1]["text"] == "hi"
    from websocket import OPCODE_PONG
    assert any(decode_server_frame(f) == (OPCODE_PONG, b"abc") for f in sock.sent)
