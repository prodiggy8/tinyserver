"""Response construction: status-line + header + body serialization.

Pure functions, no sockets — `src/server.py`'s connection loop writes the
bytes these functions return directly to the client socket.
"""

import time

SERVER_NAME = "hw4-http-server/1.0"

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTH_NAMES = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

REASON_PHRASES = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Content Too Large",
    414: "URI Too Long",
    431: "Request Header Fields Too Large",
    500: "Internal Server Error",
    505: "HTTP Version Not Supported",
}


def format_http_date(t=None):
    """RFC 7231 IMF-fixdate, e.g. 'Sun, 06 Nov 1994 08:49:37 GMT'.

    Built by hand from `time.gmtime()` with hardcoded English names —
    `strftime("%a, %d %b %Y")` is locale-dependent (a non-English locale
    would silently emit an invalid HTTP date), and `email.utils.formatdate`
    is avoided to keep provenance obvious.
    """
    if t is None:
        t = time.time()
    tm = time.gmtime(t)
    return "{day}, {mday:02d} {mon} {year:04d} {hour:02d}:{min:02d}:{sec:02d} GMT".format(
        day=DAY_NAMES[tm.tm_wday],
        mday=tm.tm_mday,
        mon=MONTH_NAMES[tm.tm_mon - 1],
        year=tm.tm_year,
        hour=tm.tm_hour,
        min=tm.tm_min,
        sec=tm.tm_sec,
    )


def serialize_response(status, headers=None, body=b"", version="HTTP/1.1"):
    """Serialize a status line + headers + body into raw response bytes.

    `headers` is an iterable of (name, value) pairs (dict also accepted).
    `Content-Length`, `Date`, and `Server` are filled in when not already
    present in `headers`. `body` may be str (encoded as UTF-8) or bytes.
    """
    if isinstance(body, str):
        body = body.encode("utf-8")

    if headers is None:
        pairs = []
    elif isinstance(headers, dict):
        pairs = list(headers.items())
    else:
        pairs = list(headers)

    names = {name.lower() for name, _ in pairs}
    if "content-length" not in names:
        pairs.append(("Content-Length", str(len(body))))
    if "date" not in names:
        pairs.append(("Date", format_http_date()))
    if "server" not in names:
        pairs.append(("Server", SERVER_NAME))

    reason = REASON_PHRASES.get(status, "")
    lines = ["{} {} {}".format(version, status, reason).encode("latin-1")]
    for name, value in pairs:
        lines.append("{}: {}".format(name, value).encode("latin-1"))

    return b"\r\n".join(lines) + b"\r\n\r\n" + body


def ok_response(body, content_type="text/html; charset=utf-8", headers=None,
                 version="HTTP/1.1"):
    """Build a 200 OK response."""
    pairs = list(headers) if headers else []
    pairs.append(("Content-Type", content_type))
    return serialize_response(200, pairs, body, version=version)


def _error_page(status, detail=None):
    reason = REASON_PHRASES.get(status, "")
    detail_html = "<p>{}</p>".format(detail) if detail else ""
    return (
        "<!DOCTYPE html><html><head><title>{status} {reason}</title></head>"
        "<body><h1>{status} {reason}</h1>{detail}</body></html>"
    ).format(status=status, reason=reason, detail=detail_html)


def error_response(status, headers=None, detail=None, version="HTTP/1.1"):
    """Build an error response with a small HTML error page body."""
    pairs = list(headers) if headers else []
    pairs.append(("Content-Type", "text/html; charset=utf-8"))
    body = _error_page(status, detail=detail)
    return serialize_response(status, pairs, body, version=version)


def bad_request(detail=None, version="HTTP/1.1"):
    return error_response(400, detail=detail, version=version)


def not_found(detail=None, version="HTTP/1.1"):
    return error_response(404, detail=detail, version=version)


def method_not_allowed(allowed_methods, detail=None, version="HTTP/1.1"):
    headers = [("Allow", ", ".join(allowed_methods))]
    return error_response(405, headers=headers, detail=detail, version=version)


def content_too_large(detail=None, version="HTTP/1.1"):
    return error_response(413, detail=detail, version=version)


def uri_too_long(detail=None, version="HTTP/1.1"):
    return error_response(414, detail=detail, version=version)


def header_fields_too_large(detail=None, version="HTTP/1.1"):
    return error_response(431, detail=detail, version=version)


def internal_server_error(detail=None, version="HTTP/1.1"):
    return error_response(500, detail=detail, version=version)


def version_not_supported(detail=None, version="HTTP/1.1"):
    return error_response(505, detail=detail, version=version)
