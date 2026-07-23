import locale
import re

import pytest

from response import (
    bad_request,
    content_too_large,
    error_response,
    format_http_date,
    header_fields_too_large,
    internal_server_error,
    method_not_allowed,
    not_found,
    ok_response,
    serialize_response,
    uri_too_long,
    version_not_supported,
)

DATE_RE = re.compile(
    rb"^[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} GMT$"
)


def split_response(raw):
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status_line = lines[0]
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(b": ")
        headers[name.decode("latin-1").lower()] = value.decode("latin-1")
    return status_line, headers, body


def test_serialize_basic_framing():
    raw = serialize_response(200, [("Content-Type", "text/plain")], b"hello")
    status_line, headers, body = split_response(raw)
    assert status_line == b"HTTP/1.1 200 OK"
    assert body == b"hello"
    assert headers["content-length"] == "5"
    assert headers["content-type"] == "text/plain"
    assert "server" in headers
    assert DATE_RE.match(headers["date"].encode("latin-1"))


def test_serialize_uses_crlf_throughout():
    raw = serialize_response(200, [], b"x")
    assert b"\n" not in raw.replace(b"\r\n", b"")


def test_serialize_no_body_content_length_zero():
    raw = serialize_response(204, [], b"")
    _, headers, body = split_response(raw)
    assert headers["content-length"] == "0"
    assert body == b""


def test_serialize_explicit_content_length_not_overridden():
    raw = serialize_response(200, [("Content-Length", "999")], b"abc")
    _, headers, _ = split_response(raw)
    assert headers["content-length"] == "999"


def test_ok_response_defaults_content_type():
    raw = ok_response("<h1>hi</h1>")
    status_line, headers, body = split_response(raw)
    assert status_line == b"HTTP/1.1 200 OK"
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert body == b"<h1>hi</h1>"


@pytest.mark.parametrize(
    "builder,status",
    [
        (bad_request, 400),
        (not_found, 404),
        (content_too_large, 413),
        (uri_too_long, 414),
        (header_fields_too_large, 431),
        (internal_server_error, 500),
        (version_not_supported, 505),
    ],
)
def test_error_helpers_status_and_html_body(builder, status):
    raw = builder()
    status_line, headers, body = split_response(raw)
    assert status_line.startswith("HTTP/1.1 {}".format(status).encode())
    assert headers["content-type"] == "text/html; charset=utf-8"
    assert int(headers["content-length"]) == len(body)
    assert b"<html>" in body.lower()
    assert str(status).encode() in body


def test_method_not_allowed_sets_allow_header():
    raw = method_not_allowed(["GET", "HEAD"])
    status_line, headers, _ = split_response(raw)
    assert status_line == b"HTTP/1.1 405 Method Not Allowed"
    assert headers["allow"] == "GET, HEAD"


def test_error_response_unknown_status_reason_blank():
    raw = error_response(599)
    status_line, _, _ = split_response(raw)
    assert status_line == b"HTTP/1.1 599 "


def test_format_http_date_matches_rfc7231_shape():
    date = format_http_date(0)
    assert DATE_RE.match(date.encode("latin-1"))
    assert date == "Thu, 01 Jan 1970 00:00:00 GMT"


def test_format_http_date_locale_independent():
    try:
        locale.setlocale(locale.LC_TIME, "de_DE.UTF-8")
    except locale.Error:
        pytest.skip("de_DE.UTF-8 locale not available on this system")
    try:
        date = format_http_date(0)
        assert DATE_RE.match(date.encode("latin-1"))
        assert date == "Thu, 01 Jan 1970 00:00:00 GMT"
    finally:
        locale.setlocale(locale.LC_TIME, "C")
