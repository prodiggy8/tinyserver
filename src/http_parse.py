"""Pure request-parsing functions: request line, headers, path/query,
Content-Length and chunked bodies, plus an incremental socket reader.

BufferedReader talks to a socket-like object only through `recv(bufsize)`,
so these functions are unit-testable with a fake socket, without any real
network connection.
"""

MAX_REQUEST_LINE = 8 * 1024
MAX_HEADER_BLOCK = 32 * 1024
MAX_BODY = 1 * 1024 * 1024


class HttpError(Exception):
    """Raised for malformed input; carries the HTTP status code to send."""

    def __init__(self, status, message=""):
        super().__init__(message or str(status))
        self.status = status


class ConnectionClosed(Exception):
    """Raised when the peer closes the connection before a full message
    (or the next message) is available. Not an error: this is the normal
    way a keep-alive connection ends."""


def parse_request_line(line):
    """Parse a request-line (bytes, without trailing CRLF).

    Returns (method, target, version) as str. Raises HttpError(400) for a
    malformed line, HttpError(505) for an unsupported version.
    """
    try:
        text = line.decode("latin-1")
    except UnicodeDecodeError:
        raise HttpError(400, "invalid request line encoding")

    parts = text.split(" ")
    if len(parts) != 3:
        raise HttpError(400, "malformed request line")

    method, target, version = parts
    if not method or not target or not version:
        raise HttpError(400, "malformed request line")

    if version not in ("HTTP/1.1", "HTTP/1.0"):
        raise HttpError(505, "unsupported HTTP version")

    return method, target, version


def parse_headers(lines):
    """Parse a list of header lines (bytes, no trailing CRLF, no blank
    terminator line included).

    Returns a dict mapping lowercased header name -> value (str). Duplicate
    headers are joined with ", " per RFC 7230 §3.2.2. Raises HttpError(400)
    on a line with no colon.
    """
    headers = {}
    for raw in lines:
        try:
            text = raw.decode("latin-1")
        except UnicodeDecodeError:
            raise HttpError(400, "invalid header encoding")

        colon = text.find(":")
        if colon <= 0:
            raise HttpError(400, "malformed header line")

        name = text[:colon].strip().lower()
        value = text[colon + 1:].strip()
        if not name:
            raise HttpError(400, "malformed header line")

        if name in headers:
            headers[name] = headers[name] + ", " + value
        else:
            headers[name] = value

    return headers


def _hex_digit_value(byte):
    if 0x30 <= byte <= 0x39:
        return byte - 0x30
    if 0x41 <= byte <= 0x46:
        return byte - 0x41 + 10
    if 0x61 <= byte <= 0x66:
        return byte - 0x61 + 10
    return None


def percent_decode(s):
    """Hand-rolled percent-decoding of a path string. Invalid escapes
    (`%zz`, truncated `%4`) are left literal rather than raising. Decodes
    the resulting byte sequence as UTF-8 with errors="replace"."""
    raw = s.encode("latin-1")
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        b = raw[i]
        if b == 0x25 and i + 2 < n:  # '%'
            hi = _hex_digit_value(raw[i + 1])
            lo = _hex_digit_value(raw[i + 2])
            if hi is not None and lo is not None:
                out.append((hi << 4) | lo)
                i += 3
                continue
        out.append(b)
        i += 1
    return out.decode("utf-8", errors="replace")


def split_target(target):
    """Split a request-target into (path, raw_query): split off the query
    string FIRST (left raw/undecoded), then percent-decode only the path."""
    q_index = target.find("?")
    if q_index == -1:
        raw_path, raw_query = target, ""
    else:
        raw_path, raw_query = target[:q_index], target[q_index + 1:]

    path = percent_decode(raw_path)
    return path, raw_query


def parse_content_length(headers):
    """Return the Content-Length as int, or None if absent. Raises
    HttpError(400) if present but non-numeric or negative."""
    value = headers.get("content-length")
    if value is None:
        return None
    if not value.isdigit():
        raise HttpError(400, "invalid Content-Length")
    return int(value)


def decode_chunked(reader):
    """Read and decode a chunked request body from `reader` (a
    BufferedReader). Ignores trailers. Raises HttpError(400) on malformed
    framing, HttpError(413) if the decoded body exceeds MAX_BODY."""
    body = bytearray()
    while True:
        size_line = reader.read_line()
        if size_line is None:
            raise ConnectionClosed("EOF in chunked size line")

        size_text = size_line.split(b";", 1)[0].strip()
        if not size_text:
            raise HttpError(400, "malformed chunked encoding: empty size line")
        try:
            size = int(size_text, 16)
        except ValueError:
            raise HttpError(400, "malformed chunked encoding: bad chunk size")
        if size < 0:
            raise HttpError(400, "malformed chunked encoding: negative chunk size")

        if size == 0:
            # Consume (and ignore) trailer lines up to the empty terminator.
            while True:
                trailer_line = reader.read_line()
                if trailer_line is None:
                    raise ConnectionClosed("EOF in chunked trailers")
                if trailer_line == b"":
                    break
            break

        if len(body) + size > MAX_BODY:
            raise HttpError(413, "chunked body too large")

        chunk = reader.read_exact(size)
        if chunk is None:
            raise ConnectionClosed("EOF in chunked data")

        crlf = reader.read_exact(2)
        if crlf is None:
            raise ConnectionClosed("EOF after chunked data")
        if crlf != b"\r\n":
            raise HttpError(400, "malformed chunked encoding: missing chunk CRLF")

        body.extend(chunk)

    return bytes(body)


class BufferedReader:
    """Incremental reader over a socket-like object exposing `recv(bufsize)`.

    Buffers bytes so header blocks and bodies can be pulled off in whatever
    sized pieces the transport happens to deliver them, including leftover
    (pipelined) bytes left over after a request has been consumed.
    """

    def __init__(self, sock, bufsize=4096):
        self._sock = sock
        self._bufsize = bufsize
        self._buf = b""
        self._eof = False

    def _fill(self):
        if self._eof:
            return False
        chunk = self._sock.recv(self._bufsize)
        if not chunk:
            self._eof = True
            return False
        self._buf += chunk
        return True

    def has_buffered_data(self):
        return len(self._buf) > 0

    def read_line(self, max_len=None):
        """Read up to and including the next CRLF, returning the line
        WITHOUT the trailing CRLF. Returns None on EOF before a full line
        is available. Raises `_LineTooLong` if the line (found or not yet
        terminated) exceeds `max_len` bytes; the caller maps this to the
        appropriate HTTP status (414 for the request line, 431 for a
        header line)."""
        while True:
            idx = self._buf.find(b"\r\n")
            if idx != -1:
                if max_len is not None and idx > max_len:
                    raise _LineTooLong(self._buf)
                line = self._buf[:idx]
                self._buf = self._buf[idx + 2:]
                return line
            if max_len is not None and len(self._buf) > max_len:
                raise _LineTooLong(self._buf)
            if not self._fill():
                return None

    def read_exact(self, n):
        """Read exactly n bytes. Returns None on EOF before n bytes are
        available."""
        while len(self._buf) < n:
            if not self._fill():
                return None
        data = self._buf[:n]
        self._buf = self._buf[n:]
        return data


class _LineTooLong(Exception):
    def __init__(self, partial):
        self.partial = partial


def read_request_head(reader):
    """Read the request-line and header block off `reader`.

    Returns (method, path, raw_query, version, headers_dict), or None if
    the connection closed cleanly before any bytes of a new request
    arrived (the normal end of a keep-alive connection).

    Raises HttpError(414) if the request line exceeds MAX_REQUEST_LINE,
    HttpError(431) if the header block exceeds MAX_HEADER_BLOCK,
    HttpError(400)/HttpError(505) from the line/header parsers.
    """
    try:
        line = reader.read_line(max_len=MAX_REQUEST_LINE)
    except _LineTooLong:
        raise HttpError(414, "request line too long")

    if line is None:
        return None
    if line == b"":
        # RFC 7230 §3.5: a client MAY send a leading empty line; skip it.
        try:
            line = reader.read_line(max_len=MAX_REQUEST_LINE)
        except _LineTooLong:
            raise HttpError(414, "request line too long")
        if line is None:
            return None

    method, target, version = parse_request_line(line)
    path, raw_query = split_target(target)

    header_lines = []
    total = 0
    while True:
        try:
            hline = reader.read_line(max_len=MAX_HEADER_BLOCK)
        except _LineTooLong:
            raise HttpError(431, "header block too large")
        if hline is None:
            raise ConnectionClosed("EOF in header block")
        if hline == b"":
            break
        total += len(hline) + 2
        if total > MAX_HEADER_BLOCK:
            raise HttpError(431, "header block too large")
        header_lines.append(hline)

    headers = parse_headers(header_lines)
    return method, path, raw_query, version, headers


def read_body(reader, headers):
    """Read the request body per `headers` (Content-Length or chunked).

    Returns bytes (b"" if there is no body). Raises HttpError(400) for a
    bad Content-Length, HttpError(413) if the body exceeds MAX_BODY, and
    ConnectionClosed if the peer disconnects mid-body.
    """
    transfer_encoding = headers.get("transfer-encoding", "").lower()
    if "chunked" in transfer_encoding:
        return decode_chunked(reader)

    content_length = parse_content_length(headers)
    if not content_length:
        return b""

    if content_length > MAX_BODY:
        raise HttpError(413, "request body too large")

    body = reader.read_exact(content_length)
    if body is None:
        raise ConnectionClosed("EOF in request body")
    return body
