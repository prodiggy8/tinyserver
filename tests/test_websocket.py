import os

import pytest

from http_parse import BufferedReader
from websocket import (
    FragmentAssembler,
    Frame,
    HandshakeError,
    OPCODE_BINARY,
    OPCODE_CLOSE,
    OPCODE_CONTINUATION,
    OPCODE_PING,
    OPCODE_TEXT,
    ProtocolError,
    compute_accept,
    encode_frame,
    read_frame,
    validate_handshake,
)


class FakeSocket:
    """Same pattern as tests/test_http_parse.py's FakeSocket: delivers
    pre-chunked pieces from recv(), as a real socket might split a
    stream."""

    def __init__(self, pieces):
        self._pieces = list(pieces)

    def recv(self, bufsize):
        if not self._pieces:
            return b""
        piece = self._pieces.pop(0)
        if len(piece) > bufsize:
            self._pieces.insert(0, piece[bufsize:])
            return piece[:bufsize]
        return piece


def reader_from_bytes(data):
    return BufferedReader(FakeSocket([data]))


class FakeRequest:
    def __init__(self, method="GET", headers=None):
        self.method = method
        self.headers = headers or {}


def valid_headers(**overrides):
    headers = {
        "upgrade": "websocket",
        "connection": "Upgrade",
        "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
        "sec-websocket-version": "13",
    }
    headers.update(overrides)
    return headers


# ---- 1. Sec-WebSocket-Accept worked example ----

def test_compute_accept_rfc6455_worked_example():
    assert compute_accept("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_validate_handshake_success_returns_accept_value():
    request = FakeRequest(headers=valid_headers())
    assert validate_handshake(request) == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


# ---- 2. handshake rejections ----

def test_validate_handshake_missing_upgrade_is_400():
    request = FakeRequest(headers=valid_headers(upgrade=""))
    with pytest.raises(HandshakeError) as exc:
        validate_handshake(request)
    assert exc.value.status == 400


def test_validate_handshake_bad_version_is_426_with_header():
    request = FakeRequest(headers=valid_headers(**{"sec-websocket-version": "8"}))
    with pytest.raises(HandshakeError) as exc:
        validate_handshake(request)
    assert exc.value.status == 426
    assert ("Sec-WebSocket-Version", "13") in exc.value.headers


def test_validate_handshake_missing_key_is_400():
    headers = valid_headers()
    del headers["sec-websocket-key"]
    request = FakeRequest(headers=headers)
    with pytest.raises(HandshakeError) as exc:
        validate_handshake(request)
    assert exc.value.status == 400


def test_validate_handshake_short_key_is_400():
    request = FakeRequest(headers=valid_headers(**{"sec-websocket-key": "dG9vc2hvcnQ="}))
    with pytest.raises(HandshakeError) as exc:
        validate_handshake(request)
    assert exc.value.status == 400


def test_validate_handshake_post_is_400():
    request = FakeRequest(method="POST", headers=valid_headers())
    with pytest.raises(HandshakeError) as exc:
        validate_handshake(request)
    assert exc.value.status == 400


def test_validate_handshake_connection_without_upgrade_token_is_400():
    request = FakeRequest(headers=valid_headers(connection="keep-alive"))
    with pytest.raises(HandshakeError) as exc:
        validate_handshake(request)
    assert exc.value.status == 400


# ---- 3. frame round-trips at all length forms ----

@pytest.mark.parametrize("size", [0, 125, 126, 65535, 65536])
def test_frame_round_trip_masked_text(size):
    payload = os.urandom(size)
    mask_key = os.urandom(4)
    encoded = encode_frame(OPCODE_TEXT, payload, fin=True, mask_key=mask_key)
    frame = read_frame(reader_from_bytes(encoded))
    assert frame.fin is True
    assert frame.opcode == OPCODE_TEXT
    assert frame.payload == payload


def test_decode_masked_client_text_frame_recovers_payload():
    mask_key = bytes([0x12, 0x34, 0x56, 0x78])
    encoded = encode_frame(OPCODE_TEXT, "hello", mask_key=mask_key)
    frame = read_frame(reader_from_bytes(encoded))
    assert frame.payload == b"hello"


# ---- 4. protocol violations ----

def test_unmasked_client_frame_is_1002():
    encoded = encode_frame(OPCODE_TEXT, b"hi", mask_key=None)
    with pytest.raises(ProtocolError) as exc:
        read_frame(reader_from_bytes(encoded))
    assert exc.value.close_code == 1002


def test_invalid_utf8_text_is_1007():
    mask_key = os.urandom(4)
    encoded = encode_frame(OPCODE_TEXT, b"\xff\xfe", mask_key=mask_key)
    frame = read_frame(reader_from_bytes(encoded))
    assembler = FragmentAssembler()
    with pytest.raises(ProtocolError) as exc:
        assembler.feed(frame)
    assert exc.value.close_code == 1007


def test_oversized_control_frame_is_1002():
    mask_key = os.urandom(4)
    encoded = encode_frame(OPCODE_PING, os.urandom(200), mask_key=mask_key)
    with pytest.raises(ProtocolError) as exc:
        read_frame(reader_from_bytes(encoded))
    assert exc.value.close_code == 1002


def test_reserved_bit_set_is_1002():
    mask_key = os.urandom(4)
    encoded = bytearray(encode_frame(OPCODE_TEXT, b"hi", mask_key=mask_key))
    encoded[0] |= 0x40  # set RSV1
    with pytest.raises(ProtocolError) as exc:
        read_frame(reader_from_bytes(bytes(encoded)))
    assert exc.value.close_code == 1002


def test_unknown_opcode_is_1002():
    mask_key = os.urandom(4)
    encoded = encode_frame(0x3, b"hi", mask_key=mask_key)
    with pytest.raises(ProtocolError) as exc:
        read_frame(reader_from_bytes(encoded))
    assert exc.value.close_code == 1002


def test_oversized_payload_is_1009():
    mask_key = os.urandom(4)
    encoded = encode_frame(OPCODE_TEXT, os.urandom(64 * 1024 + 1), mask_key=mask_key)
    with pytest.raises(ProtocolError) as exc:
        read_frame(reader_from_bytes(encoded))
    assert exc.value.close_code == 1009


def test_binary_opcode_is_1003():
    frame = Frame(fin=True, opcode=OPCODE_BINARY, payload=b"\x01\x02")
    assembler = FragmentAssembler()
    with pytest.raises(ProtocolError) as exc:
        assembler.feed(frame)
    assert exc.value.close_code == 1003


# ---- 5. fragmentation reassembly with interleaved control frame ----

def test_fragmentation_reassembly_with_interleaved_ping():
    assembler = FragmentAssembler()

    start = Frame(fin=False, opcode=OPCODE_TEXT, payload=b"hel")
    assert assembler.feed(start) is None

    ping = Frame(fin=True, opcode=OPCODE_PING, payload=b"")
    assert ping.opcode not in (OPCODE_CONTINUATION, OPCODE_TEXT)
    assert assembler.feed(ping) is None

    middle = Frame(fin=False, opcode=OPCODE_CONTINUATION, payload=b"lo w")
    assert assembler.feed(middle) is None

    end = Frame(fin=True, opcode=OPCODE_CONTINUATION, payload=b"orld")
    assert assembler.feed(end) == "hello world"


def test_continuation_without_start_is_1002():
    assembler = FragmentAssembler()
    frame = Frame(fin=True, opcode=OPCODE_CONTINUATION, payload=b"x")
    with pytest.raises(ProtocolError) as exc:
        assembler.feed(frame)
    assert exc.value.close_code == 1002


def test_data_frame_during_fragmented_message_is_1002():
    assembler = FragmentAssembler()
    assembler.feed(Frame(fin=False, opcode=OPCODE_TEXT, payload=b"a"))
    with pytest.raises(ProtocolError) as exc:
        assembler.feed(Frame(fin=True, opcode=OPCODE_TEXT, payload=b"b"))
    assert exc.value.close_code == 1002


# ---- encode_close helper ----

def test_encode_close_carries_status_code_and_reason():
    import struct

    from websocket import encode_close

    encoded = encode_close(1001, "bye")
    assert encoded[0] == 0x80 | OPCODE_CLOSE  # FIN + close opcode, unmasked
    length = encoded[1]
    body = encoded[2:2 + length]
    assert struct.unpack("!H", body[:2])[0] == 1001
    assert body[2:] == b"bye"
