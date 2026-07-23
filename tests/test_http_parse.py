import pytest

from http_parse import (
    HttpError,
    ConnectionClosed,
    BufferedReader,
    parse_request_line,
    parse_headers,
    percent_decode,
    split_target,
    parse_content_length,
    decode_chunked,
    read_request_head,
    read_body,
    MAX_REQUEST_LINE,
    MAX_HEADER_BLOCK,
    MAX_BODY,
)


class FakeSocket:
    """Delivers pre-chunked byte pieces from recv(), like a real socket
    might split a stream across arbitrary-sized reads."""

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


def reader_from_bytes(data, piece_size=None):
    if piece_size is None:
        pieces = [data]
    else:
        pieces = [data[i:i + piece_size] for i in range(0, len(data), piece_size)]
    return BufferedReader(FakeSocket(pieces))


# ---- request-line parsing ----

def test_parse_request_line_valid():
    assert parse_request_line(b"GET /foo HTTP/1.1") == ("GET", "/foo", "HTTP/1.1")


def test_parse_request_line_http_1_0():
    assert parse_request_line(b"GET / HTTP/1.0") == ("GET", "/", "HTTP/1.0")


def test_parse_request_line_missing_parts():
    with pytest.raises(HttpError) as exc:
        parse_request_line(b"GET /foo")
    assert exc.value.status == 400


def test_parse_request_line_bad_version():
    with pytest.raises(HttpError) as exc:
        parse_request_line(b"GET / HTTP/2.0")
    assert exc.value.status == 505


def test_parse_request_line_garbage():
    with pytest.raises(HttpError) as exc:
        parse_request_line(b"GARBAGE")
    assert exc.value.status == 400


def test_request_line_too_long_via_reader():
    line = b"GET /" + b"a" * (MAX_REQUEST_LINE + 100) + b" HTTP/1.1\r\n\r\n"
    reader = reader_from_bytes(line)
    with pytest.raises(HttpError) as exc:
        read_request_head(reader)
    assert exc.value.status == 414


# ---- header parsing ----

def test_parse_headers_case_insensitive():
    headers = parse_headers([b"Content-Type: text/plain", b"X-Foo: bar"])
    assert headers["content-type"] == "text/plain"
    assert headers["x-foo"] == "bar"


def test_parse_headers_no_colon():
    with pytest.raises(HttpError) as exc:
        parse_headers([b"NoColonHere"])
    assert exc.value.status == 400


def test_header_block_too_large_via_reader():
    head = b"GET / HTTP/1.1\r\n"
    big_header = b"X-Big: " + b"a" * (MAX_HEADER_BLOCK) + b"\r\n\r\n"
    reader = reader_from_bytes(head + big_header)
    with pytest.raises(HttpError) as exc:
        read_request_head(reader)
    assert exc.value.status == 431


# ---- path/query handling ----

def test_split_target_basic():
    path, query = split_target("/a%20b?x=1&y=2")
    assert path == "/a b"
    assert query == "x=1&y=2"


def test_split_target_dot_dot():
    path, query = split_target("/%2e%2e")
    assert path == "/.."
    assert query == ""


def test_split_target_invalid_escape_passthrough():
    path, _ = split_target("/%zz")
    assert path == "/%zz"


def test_split_target_truncated_escape_passthrough():
    path, _ = split_target("/%4")
    assert path == "/%4"


def test_split_target_question_mark_in_path_not_split_early():
    # %3F is an encoded '?' in the path; it must not create a query split.
    path, query = split_target("/foo%3Fbar")
    assert path == "/foo?bar"
    assert query == ""


def test_percent_decode_no_percent():
    assert percent_decode("/plain") == "/plain"


# ---- Content-Length body ----

def test_parse_content_length_present():
    assert parse_content_length({"content-length": "5"}) == 5


def test_parse_content_length_absent():
    assert parse_content_length({}) is None


def test_parse_content_length_non_numeric():
    with pytest.raises(HttpError) as exc:
        parse_content_length({"content-length": "abc"})
    assert exc.value.status == 400


def test_parse_content_length_negative():
    with pytest.raises(HttpError) as exc:
        parse_content_length({"content-length": "-5"})
    assert exc.value.status == 400


def test_read_body_exact_length():
    reader = reader_from_bytes(b"hello")
    body = read_body(reader, {"content-length": "5"})
    assert body == b"hello"


def test_read_body_short_raises_connection_closed():
    reader = reader_from_bytes(b"hel")
    with pytest.raises(ConnectionClosed):
        read_body(reader, {"content-length": "5"})


def test_read_body_oversized():
    with pytest.raises(HttpError) as exc:
        read_body(reader_from_bytes(b"x"), {"content-length": str(MAX_BODY + 1)})
    assert exc.value.status == 413


# ---- chunked body ----

def test_decode_chunked_multi_chunk():
    data = b"4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n"
    reader = reader_from_bytes(data)
    assert decode_chunked(reader) == b"Wikipedia"


def test_decode_chunked_with_extension():
    data = b"4;foo=bar\r\nWiki\r\n0\r\n\r\n"
    reader = reader_from_bytes(data)
    assert decode_chunked(reader) == b"Wiki"


def test_decode_chunked_with_trailers_ignored():
    data = b"4\r\nWiki\r\n0\r\nX-Trailer: ignored\r\n\r\n"
    reader = reader_from_bytes(data)
    assert decode_chunked(reader) == b"Wiki"


def test_decode_chunked_bad_hex_size():
    data = b"zz\r\ndata\r\n0\r\n\r\n"
    reader = reader_from_bytes(data)
    with pytest.raises(HttpError) as exc:
        decode_chunked(reader)
    assert exc.value.status == 400


def test_decode_chunked_missing_crlf():
    data = b"4\r\nWikiXX0\r\n\r\n"
    reader = reader_from_bytes(data)
    with pytest.raises(HttpError) as exc:
        decode_chunked(reader)
    assert exc.value.status == 400


def test_decode_chunked_too_large():
    big_chunk_size = MAX_BODY + 1
    data = ("%x\r\n" % big_chunk_size).encode() + b"a" * big_chunk_size + b"\r\n0\r\n\r\n"
    reader = reader_from_bytes(data)
    with pytest.raises(HttpError) as exc:
        decode_chunked(reader)
    assert exc.value.status == 413


def test_read_body_dispatches_to_chunked():
    data = b"4\r\ntest\r\n0\r\n\r\n"
    reader = reader_from_bytes(data)
    body = read_body(reader, {"transfer-encoding": "chunked"})
    assert body == b"test"


# ---- incremental reader over odd-sized pieces ----

def test_read_request_head_split_across_recv_calls():
    raw = b"GET /foo?x=1 HTTP/1.1\r\nHost: example.com\r\nX-A: 1\r\n\r\n"
    reader = reader_from_bytes(raw, piece_size=3)
    method, path, raw_query, version, headers = read_request_head(reader)
    assert method == "GET"
    assert path == "/foo"
    assert raw_query == "x=1"
    assert version == "HTTP/1.1"
    assert headers["host"] == "example.com"
    assert headers["x-a"] == "1"


def test_read_request_head_pipelined_leftover_bytes_preserved():
    raw = b"GET /a HTTP/1.1\r\n\r\nGET /b HTTP/1.1\r\n\r\n"
    reader = reader_from_bytes(raw, piece_size=7)
    method, path, _, _, _ = read_request_head(reader)
    assert path == "/a"
    method2, path2, _, _, _ = read_request_head(reader)
    assert path2 == "/b"


def test_read_request_head_clean_eof_returns_none():
    reader = reader_from_bytes(b"")
    assert read_request_head(reader) is None


def test_read_exact_across_recv_calls():
    reader = reader_from_bytes(b"0123456789", piece_size=2)
    assert reader.read_exact(7) == b"0123456"
