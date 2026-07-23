"""RFC 6455 WebSocket handshake validation and frame codec.

Pure functions/classes: no socket I/O. `read_frame` takes any object
exposing `read_exact(n)` (http_parse.BufferedReader satisfies this, so
src/chat.py drives it against the real connection while tests drive it
against a fake socket the same way tests/test_http_parse.py does).
"""

import base64
import hashlib
import struct

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OPCODE_CONTINUATION = 0x0
OPCODE_TEXT = 0x1
OPCODE_BINARY = 0x2
OPCODE_CLOSE = 0x8
OPCODE_PING = 0x9
OPCODE_PONG = 0xA

CONTROL_OPCODES = {OPCODE_CLOSE, OPCODE_PING, OPCODE_PONG}
KNOWN_OPCODES = {
    OPCODE_CONTINUATION, OPCODE_TEXT, OPCODE_BINARY,
    OPCODE_CLOSE, OPCODE_PING, OPCODE_PONG,
}

MAX_PAYLOAD = 64 * 1024
MAX_CONTROL_PAYLOAD = 125


def compute_accept(key):
    """Sec-WebSocket-Accept per RFC 6455 §1.3: base64(SHA-1(key + GUID))."""
    digest = hashlib.sha1((key + GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


class HandshakeError(Exception):
    """Raised by `validate_handshake` for a request that isn't a valid
    WebSocket upgrade. `status` is 400 or 426; `headers` carries the extra
    headers a rejection response must include (e.g. Sec-WebSocket-Version
    on a 426)."""

    def __init__(self, status, headers=None):
        super().__init__(status)
        self.status = status
        self.headers = headers or []


def validate_handshake(request):
    """Validate a parsed Request (src/server.py's Request) as a WebSocket
    upgrade handshake. Returns the computed Sec-WebSocket-Accept value on
    success. Raises HandshakeError(400) for a malformed upgrade request,
    HandshakeError(426, [("Sec-WebSocket-Version", "13")]) for an
    unsupported version.
    """
    if request.method.upper() != "GET":
        raise HandshakeError(400)

    upgrade = request.headers.get("upgrade", "")
    if "websocket" not in upgrade.lower():
        raise HandshakeError(400)

    connection = request.headers.get("connection", "")
    tokens = [t.strip().lower() for t in connection.split(",")]
    if "upgrade" not in tokens:
        raise HandshakeError(400)

    version = request.headers.get("sec-websocket-version", "")
    if version != "13":
        raise HandshakeError(426, headers=[("Sec-WebSocket-Version", "13")])

    key = request.headers.get("sec-websocket-key")
    if not key:
        raise HandshakeError(400)
    try:
        decoded = base64.b64decode(key, validate=True)
    except Exception:
        raise HandshakeError(400)
    if len(decoded) != 16:
        raise HandshakeError(400)

    return compute_accept(key)


class ProtocolError(Exception):
    """A frame or message violates the protocol. `close_code` is the
    WebSocket close status the caller should send before dropping the
    connection (1002/1003/1007/1009)."""

    def __init__(self, close_code):
        super().__init__(close_code)
        self.close_code = close_code


class Frame:
    __slots__ = ("fin", "opcode", "payload")

    def __init__(self, fin, opcode, payload):
        self.fin = fin
        self.opcode = opcode
        self.payload = payload

    def __eq__(self, other):
        return (
            isinstance(other, Frame)
            and self.fin == other.fin
            and self.opcode == other.opcode
            and self.payload == other.payload
        )

    def __repr__(self):
        return "Frame(fin={!r}, opcode={!r}, payload={!r})".format(
            self.fin, self.opcode, self.payload)


def _apply_mask(data, mask_key):
    return bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))


def encode_frame(opcode, payload=b"", fin=True, mask_key=None):
    """Encode one frame. Server->client frames are unmasked (mask_key=None,
    the default); pass a 4-byte mask_key to build masked client-style
    frames (used by tests exercising the decoder)."""
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    length = len(payload)

    b0 = (0x80 if fin else 0x00) | (opcode & 0x0F)
    mask_bit = 0x80 if mask_key is not None else 0x00

    if length < 126:
        header = bytes([b0, mask_bit | length])
    elif length < 65536:
        header = bytes([b0, mask_bit | 126]) + struct.pack("!H", length)
    else:
        header = bytes([b0, mask_bit | 127]) + struct.pack("!Q", length)

    if mask_key is not None:
        header += mask_key
        payload = _apply_mask(payload, mask_key)

    return header + payload


def encode_close(code, reason=""):
    if isinstance(reason, str):
        reason = reason.encode("utf-8")
    return encode_frame(OPCODE_CLOSE, struct.pack("!H", code) + reason)


def read_frame(reader):
    """Read and decode one frame from `reader` (anything with
    `read_exact(n) -> bytes | None`). Returns None on EOF before a
    complete frame arrives (treat like a closed connection). Raises
    ProtocolError for a structural violation: reserved bit set, unknown
    opcode, unmasked client frame, an oversized/fragmented control frame,
    or a payload over the 64 KiB limit.
    """
    header = reader.read_exact(2)
    if header is None:
        return None
    b0, b1 = header
    fin = bool(b0 & 0x80)
    rsv = b0 & 0x70
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    length = b1 & 0x7F

    if rsv != 0:
        raise ProtocolError(1002)
    if opcode not in KNOWN_OPCODES:
        raise ProtocolError(1002)

    if length == 126:
        ext = reader.read_exact(2)
        if ext is None:
            return None
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = reader.read_exact(8)
        if ext is None:
            return None
        length = struct.unpack("!Q", ext)[0]

    is_control = opcode in CONTROL_OPCODES
    if is_control and (length > MAX_CONTROL_PAYLOAD or not fin):
        raise ProtocolError(1002)

    if length > MAX_PAYLOAD:
        raise ProtocolError(1009)

    if not masked:
        raise ProtocolError(1002)

    mask_key = reader.read_exact(4)
    if mask_key is None:
        return None
    payload = reader.read_exact(length) if length else b""
    if payload is None:
        return None
    payload = _apply_mask(payload, mask_key)

    return Frame(fin=fin, opcode=opcode, payload=payload)


class FragmentAssembler:
    """Reassembles fragmented messages (opcode 0x0 continuation frames)
    fed one decoded Frame at a time. Control frames pass straight through
    without disturbing in-progress reassembly state, per RFC 6455 §5.4.

    `feed(frame)` returns:
    - None if more frames are needed (a fragment, or a control frame the
      caller should act on immediately — check `frame.opcode` yourself
      before calling feed() to tell the two apart).
    - the decoded str when a text message completes (single frame or the
      final continuation of a fragmented one).

    Raises ProtocolError(1002) for a continuation frame with no message in
    progress, or a new data frame while one is already in progress.
    Raises ProtocolError(1003) for a binary opcode (this app is text-only).
    Raises ProtocolError(1007) for invalid UTF-8 in a completed text
    message.
    """

    def __init__(self):
        self._opcode = None
        self._chunks = []

    def feed(self, frame):
        if frame.opcode in CONTROL_OPCODES:
            return None

        if frame.opcode == OPCODE_CONTINUATION:
            if self._opcode is None:
                raise ProtocolError(1002)
            self._chunks.append(frame.payload)
            if not frame.fin:
                return None
            opcode = self._opcode
            payload = b"".join(self._chunks)
            self._opcode = None
            self._chunks = []
            return self._complete(opcode, payload)

        if self._opcode is not None:
            raise ProtocolError(1002)

        if frame.opcode == OPCODE_BINARY:
            raise ProtocolError(1003)

        if frame.fin:
            return self._complete(frame.opcode, frame.payload)

        self._opcode = frame.opcode
        self._chunks.append(frame.payload)
        return None

    @staticmethod
    def _complete(opcode, payload):
        if opcode == OPCODE_BINARY:
            raise ProtocolError(1003)
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError:
            raise ProtocolError(1007)
