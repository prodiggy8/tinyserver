"""Chat layer: connection registry, broadcast, message store, name
issuing, and the per-connection frame-processing loop for /ws.

Everything here is injectable (paths, limits, intervals) so tests don't
need real sockets, real files, or real time. src/app.py (2.5) wires this
up: it validates the handshake, checks the connection cap, sends the 101
response, calls `ConnectionRegistry.register`, enqueues the welcome frame,
then calls `serve_connection` on the hijacking thread.

Threading model (see IMPLEMENTATION_PLAN.md for the full rationale):
- The reader thread (the one that hijacked the connection) is the only
  thread that reads the socket and the only thread that closes it.
- Each connection has its own writer thread, started by `register`, that
  is the only thread that writes frames to that socket. `Connection.send`
  is a non-blocking queue put; a full queue means the client is too slow
  and the connection is dropped rather than blocking the broadcaster.
- `ConnectionRegistry.drop` only *shuts down* the socket (to unblock the
  reader's blocking recv from another thread) rather than closing it; the
  reader thread still performs the actual close in its own finally block.
"""

import json
import os
import queue
import random
import socket
import threading
import time

from websocket import (
    OPCODE_CLOSE,
    OPCODE_PING,
    OPCODE_PONG,
    OPCODE_TEXT,
    FragmentAssembler,
    ProtocolError,
    encode_close,
    encode_frame,
    read_frame,
)

_SRC_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STORE_PATH = os.path.join(os.path.dirname(_SRC_DIR), "data", "messages.jsonl")
MAX_STORED_MESSAGES = 100

DEFAULT_MAX_CONNECTIONS = 128
QUEUE_MAXSIZE = 64

RATE_LIMIT_COUNT = 5
RATE_LIMIT_WINDOW = 10.0

MAX_TEXT_LEN = 500

DEFAULT_PING_INTERVAL = 20.0
DEFAULT_PING_TIMEOUT = 60.0

WORDS_A = [
    "quiet", "brave", "lucky", "calm", "swift", "bright", "gentle", "bold",
    "curious", "sunny", "cosmic", "wandering", "silent", "jolly", "clever",
    "mellow",
]
WORDS_B = [
    "falcon", "otter", "comet", "willow", "harbor", "meadow", "ember",
    "cedar", "raven", "tundra", "sparrow", "lantern", "glacier", "canyon",
    "juniper", "thistle",
]


def generate_name(rng=random):
    """Generate a `<word><word><number>` name, e.g. `quietfalcon42`,
    matching `^[a-z]+[0-9]{2}$`."""
    word = rng.choice(WORDS_A) + rng.choice(WORDS_B)
    number = rng.randrange(100)
    return "{}{:02d}".format(word, number)


class MessageStore:
    """Append-only message log backed by a JSONL file, guarded by a lock
    covering both the in-memory list and the file append. On construction,
    loads the last `max_messages` from `path` (tolerating a missing file
    or a corrupt/partial final line) and truncates the file to match, so
    it cannot grow unbounded across restarts.
    """

    def __init__(self, path=DEFAULT_STORE_PATH, max_messages=MAX_STORED_MESSAGES):
        self._path = path
        self._max = max_messages
        self._lock = threading.Lock()
        directory = os.path.dirname(self._path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._messages = self._load()
        self._rewrite()

    def _load(self):
        if not os.path.exists(self._path):
            return []
        messages = []
        with open(self._path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    messages.append(json.loads(line))
                except ValueError:
                    continue
        return messages[-self._max:]

    def _rewrite(self):
        tmp_path = self._path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            for message in self._messages:
                f.write(json.dumps(message))
                f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, self._path)

    def append(self, message):
        with self._lock:
            self._messages.append(message)
            self._messages = self._messages[-self._max:]
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(json.dumps(message))
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())

    def recent(self):
        with self._lock:
            return list(self._messages)


class Connection:
    """One registered WebSocket connection: identity, outbound queue, and
    the writer thread draining it. `last_seen`/`ping_outstanding` track
    the ping/pong lifecycle and are updated from the reader thread
    (`touch`) and the ping scheduler thread (`mark_ping_sent`), so both
    are guarded by a lock.
    """

    def __init__(self, conn_id, sock, name):
        self.id = conn_id
        self.sock = sock
        self.name = name
        self.queue = queue.Queue(maxsize=QUEUE_MAXSIZE)
        self.writer_thread = None
        self._state_lock = threading.Lock()
        self._last_seen = time.monotonic()
        self._ping_outstanding = False

    def touch(self):
        with self._state_lock:
            self._last_seen = time.monotonic()
            self._ping_outstanding = False

    def mark_ping_sent(self):
        with self._state_lock:
            self._ping_outstanding = True

    def idle_seconds(self):
        with self._state_lock:
            return time.monotonic() - self._last_seen

    def ping_outstanding(self):
        with self._state_lock:
            return self._ping_outstanding

    def start_writer(self):
        self.writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self.writer_thread.start()

    def _writer_loop(self):
        while True:
            item = self.queue.get()
            if item is None:
                return
            try:
                self.sock.sendall(item)
            except OSError:
                return

    def enqueue(self, frame_bytes):
        """Non-blocking put. Returns False if the queue is full (the
        caller should drop this connection rather than block)."""
        try:
            self.queue.put_nowait(frame_bytes)
            return True
        except queue.Full:
            return False

    def stop_writer(self):
        try:
            self.queue.put_nowait(None)
        except queue.Full:
            # Writer will exit on its own once a send fails against a
            # shut-down socket; no need to force room for the sentinel.
            pass


class ConnectionRegistry:
    """Thread-safe registry of live connections. Broadcast snapshots the
    registry under the lock, then enqueues outside of it, so a disconnect
    during broadcast cannot corrupt iteration and a blocking put on one
    connection cannot stall delivery to the others.
    """

    def __init__(self, max_connections=DEFAULT_MAX_CONNECTIONS):
        self._lock = threading.Lock()
        self._connections = {}
        self._max_connections = max_connections
        self._next_id = 0

    def count(self):
        with self._lock:
            return len(self._connections)

    def at_capacity(self):
        """Cheap peek used by a caller that must decide whether to send a
        101 or a 503 *before* calling `register` (see `register`'s
        docstring for why the two can't be the same call). Racy by design:
        a handshake landing in the tiny window between this check and the
        matching `register` call can still get refused there instead."""
        with self._lock:
            return len(self._connections) >= self._max_connections

    def register(self, sock, name, build_initial_frames=None):
        """Add a new connection and start its writer thread. Returns None
        (refusing the connection) if the registry is at capacity.

        Must only be called AFTER any raw bytes the caller writes directly
        to `sock` (e.g. the 101 response) — this starts the writer thread,
        which becomes the only thread allowed to write to `sock` from then
        on, so writing to it first would race the writer.

        `build_initial_frames(conn, count)`, if given, is called with the
        registry lock still held (`count` already includes this
        connection) and returns an iterable of frame bytes enqueued before
        the connection is visible to any other thread's broadcast — this
        is what guarantees e.g. a `welcome` frame is queued ahead of the
        join `visitors` broadcast instead of racing it.
        """
        with self._lock:
            if len(self._connections) >= self._max_connections:
                return None
            conn_id = self._next_id
            self._next_id += 1
            conn = Connection(conn_id, sock, name)
            self._connections[conn_id] = conn
            if build_initial_frames is not None:
                for frame_bytes in build_initial_frames(conn, len(self._connections)):
                    conn.enqueue(frame_bytes)
        conn.start_writer()
        self._broadcast_visitor_count()
        return conn

    def unregister(self, conn):
        """Idempotent: returns True only for the call that actually
        removes the connection, so only that caller broadcasts the
        updated visitor count."""
        with self._lock:
            if conn.id not in self._connections:
                return False
            del self._connections[conn.id]
        conn.stop_writer()
        self._broadcast_visitor_count()
        return True

    def snapshot(self):
        with self._lock:
            return list(self._connections.values())

    def broadcast(self, frame_bytes, exclude=None):
        for conn in self.snapshot():
            if conn is exclude:
                continue
            if not conn.enqueue(frame_bytes):
                self.drop(conn, 1008, "queue_full")

    def _broadcast_visitor_count(self, exclude=None):
        payload = json.dumps({"type": "visitors", "count": self.count()}).encode("utf-8")
        frame_bytes = encode_frame(OPCODE_TEXT, payload)
        for conn in self.snapshot():
            if conn is exclude:
                continue
            conn.enqueue(frame_bytes)

    def drop(self, conn, code=1000, reason=b""):
        """Force-disconnect `conn`: best-effort enqueue a close frame,
        unregister it, and (only on the call that actually removed it)
        shut down the socket to unblock the reader thread's recv — the
        reader thread still performs the real close()."""
        conn.enqueue(encode_close(code, reason))
        removed = self.unregister(conn)
        if removed:
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        return removed

    def shutdown_all(self, timeout=2.0):
        """Drop every connection with close status 1001 and join their
        writer threads (server shutdown)."""
        conns = self.snapshot()
        for conn in conns:
            self.drop(conn, 1001, "server shutting down")
        for conn in conns:
            if conn.writer_thread is not None:
                conn.writer_thread.join(timeout=timeout)


class PingScheduler:
    """Process-wide thread that pings idle connections and drops ones
    that never respond. Never touches a socket directly — it only
    enqueues (via Connection.enqueue) and asks the registry to drop."""

    def __init__(self, registry, interval=DEFAULT_PING_INTERVAL, timeout=DEFAULT_PING_TIMEOUT):
        self.registry = registry
        self.interval = interval
        self.timeout = timeout
        self._stop_event = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _loop(self):
        while not self._stop_event.wait(self.interval):
            self.tick()

    def tick(self):
        for conn in self.registry.snapshot():
            idle = conn.idle_seconds()
            if idle >= self.timeout:
                self.registry.drop(conn, 1000, "ping_timeout")
            elif idle >= self.interval and not conn.ping_outstanding():
                conn.mark_ping_sent()
                conn.enqueue(encode_frame(OPCODE_PING, b""))


def welcome_frame(name, messages, visitors):
    payload = json.dumps({
        "type": "welcome",
        "name": name,
        "messages": messages,
        "visitors": visitors,
    }).encode("utf-8")
    return encode_frame(OPCODE_TEXT, payload)


def _error_frame(reason):
    payload = json.dumps({"type": "error", "reason": reason}).encode("utf-8")
    return encode_frame(OPCODE_TEXT, payload)


def _message_frame(name, text, ts):
    payload = json.dumps({"type": "message", "name": name, "text": text, "ts": ts}).encode("utf-8")
    return encode_frame(OPCODE_TEXT, payload)


class RateLimiter:
    """5 messages per 10-second sliding window, per connection."""

    def __init__(self, count=RATE_LIMIT_COUNT, window=RATE_LIMIT_WINDOW):
        self._count = count
        self._window = window
        self._times = []

    def allow(self, now=None):
        now = time.monotonic() if now is None else now
        while self._times and now - self._times[0] > self._window:
            self._times.pop(0)
        if len(self._times) >= self._count:
            return False
        self._times.append(now)
        return True


def handle_message(text, conn, store, registry, rate_limiter):
    """Dispatch one decoded text-frame message per spec §4. Enqueues an
    error frame on the connection for malformed input, over the rate
    limit, or over the length limit; otherwise stores and broadcasts."""
    try:
        obj = json.loads(text)
    except ValueError:
        conn.enqueue(_error_frame("bad_request"))
        return

    if not isinstance(obj, dict) or obj.get("type") != "post":
        conn.enqueue(_error_frame("bad_request"))
        return

    msg_text = obj.get("text")
    if not isinstance(msg_text, str):
        conn.enqueue(_error_frame("bad_request"))
        return

    if not rate_limiter.allow():
        conn.enqueue(_error_frame("rate_limited"))
        return

    if len(msg_text) > MAX_TEXT_LEN:
        conn.enqueue(_error_frame("too_long"))
        return

    ts = time.time()
    message = {"name": conn.name, "text": msg_text, "ts": ts}
    store.append(message)
    registry.broadcast(_message_frame(conn.name, msg_text, ts))


def serve_connection(sock, reader, conn, store, registry, rate_limiter=None):
    """Run the frame read loop for a hijacked WebSocket connection until
    close/EOF/error, then unregister and close the socket. Runs on the
    thread that performed the hijack (the "reader" thread) — see the
    module docstring for the threading model.
    """
    if rate_limiter is None:
        rate_limiter = RateLimiter()
    assembler = FragmentAssembler()

    try:
        while True:
            try:
                frame = read_frame(reader)
            except ProtocolError as exc:
                conn.enqueue(encode_close(exc.close_code))
                break
            except OSError:
                break

            if frame is None:
                break

            conn.touch()

            if frame.opcode == OPCODE_CLOSE:
                conn.enqueue(encode_close(1000))
                break
            if frame.opcode == OPCODE_PING:
                conn.enqueue(encode_frame(OPCODE_PONG, frame.payload))
                continue
            if frame.opcode == OPCODE_PONG:
                continue

            try:
                text = assembler.feed(frame)
            except ProtocolError as exc:
                conn.enqueue(encode_close(exc.close_code))
                break

            if text is None:
                continue

            handle_message(text, conn, store, registry, rate_limiter)
    finally:
        registry.unregister(conn)
        if conn.writer_thread is not None:
            conn.writer_thread.join(timeout=2.0)
        try:
            sock.close()
        except OSError:
            pass
