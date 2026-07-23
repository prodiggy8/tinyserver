import os

import pytest

import static
from router import Router
from server import Request


@pytest.fixture
def public_dir(tmp_path):
    root = tmp_path / "public"
    root.mkdir()
    (root / "index.html").write_text("<html>home</html>")
    (root / "style.css").write_text("body { color: black; }")
    (root / "data.json").write_text('{"a": 1}')
    (root / "notes.txt").write_text("plain text")
    (root / "photo.jpg").write_bytes(b"\xff\xd8\xff")
    (root / "icon.mystery").write_bytes(b"???")

    sub = root / "sub"
    sub.mkdir()
    (sub / "index.html").write_text("<html>sub index</html>")

    empty_dir = root / "empty"
    empty_dir.mkdir()

    (tmp_path / "secret.txt").write_text("outside root")

    return str(root)


# --- Basic serving -------------------------------------------------------

def test_root_path_serves_index_html(public_dir):
    result = static.serve("GET", "/", root=public_dir)
    assert result is not None
    status, headers, body = result
    assert status == 200
    assert body == b"<html>home</html>"
    assert ("Content-Type", "text/html; charset=utf-8") in headers


def test_serves_named_file(public_dir):
    status, headers, body = static.serve("GET", "/style.css", root=public_dir)
    assert status == 200
    assert body == b"body { color: black; }"
    assert ("Content-Type", "text/css") in headers


def test_directory_path_serves_its_index_html(public_dir):
    status, headers, body = static.serve("GET", "/sub", root=public_dir)
    assert status == 200
    assert body == b"<html>sub index</html>"


def test_directory_path_with_trailing_slash_serves_index_html(public_dir):
    status, headers, body = static.serve("GET", "/sub/", root=public_dir)
    assert status == 200
    assert body == b"<html>sub index</html>"


def test_directory_without_index_html_is_none(public_dir):
    assert static.serve("GET", "/empty", root=public_dir) is None
    assert static.serve("GET", "/empty/", root=public_dir) is None


def test_missing_file_is_none(public_dir):
    assert static.serve("GET", "/nope.html", root=public_dir) is None


# --- MIME table ------------------------------------------------------------

@pytest.mark.parametrize(
    "path,expected",
    [
        ("/index.html", "text/html; charset=utf-8"),
        ("/style.css", "text/css"),
        ("/data.json", "application/json"),
        ("/notes.txt", "text/plain; charset=utf-8"),
        ("/photo.jpg", "image/jpeg"),
    ],
)
def test_mime_types_by_extension(public_dir, path, expected):
    _status, headers, _body = static.serve("GET", path, root=public_dir)
    assert dict(headers)["Content-Type"] == expected


def test_unknown_extension_is_octet_stream(public_dir):
    _status, headers, _body = static.serve("GET", "/icon.mystery", root=public_dir)
    assert dict(headers)["Content-Type"] == "application/octet-stream"


@pytest.mark.parametrize(
    "ext,expected",
    [
        (".jpeg", "image/jpeg"),
        (".gif", "image/gif"),
        (".svg", "image/svg+xml"),
        (".ico", "image/x-icon"),
        (".woff2", "font/woff2"),
        (".js", "application/javascript"),
    ],
)
def test_mime_type_helper_covers_full_table(ext, expected):
    assert static.mime_type("whatever" + ext) == expected


# --- Path-traversal protection ----------------------------------------------

def test_dot_dot_traversal_is_rejected(public_dir):
    assert static.serve("GET", "/../secret.txt", root=public_dir) is None


def test_nested_dot_dot_traversal_is_rejected(public_dir):
    assert static.serve("GET", "/sub/../../secret.txt", root=public_dir) is None


def test_double_leading_slash_stays_confined_to_root(public_dir):
    # Already-decoded by the time it reaches static.py; //etc/passwd should
    # resolve as root/etc/passwd (missing), not escape to the real /etc/passwd.
    assert static.serve("GET", "//etc/passwd", root=public_dir) is None


def test_percent_encoded_traversal_after_decoding_is_rejected(public_dir):
    # http_parse.percent_decode runs before this point, so %2e%2e arrives
    # here already turned into "..".
    assert static.serve("GET", "/%2e%2e/secret.txt".replace("%2e", "."), root=public_dir) is None


# --- Router integration ------------------------------------------------------

def test_wired_into_router_serves_and_traps_traversal(public_dir):
    router = Router(static_handler=lambda method, path: static.serve(method, path, root=public_dir))

    status, headers, body = router.dispatch(Request("GET", "/", "", {}, b"", "HTTP/1.1"))
    assert status == 200
    assert body == b"<html>home</html>"

    status, headers, body = router.dispatch(Request("GET", "/../secret.txt", "", {}, b"", "HTTP/1.1"))
    assert status == 404

    status, headers, body = router.dispatch(Request("HEAD", "/", "", {}, b"", "HTTP/1.1"))
    assert status == 200
    assert body == b""
    assert dict(headers)["Content-Type"] == "text/html; charset=utf-8"

    status, headers, body = router.dispatch(Request("POST", "/style.css", "", {}, b"", "HTTP/1.1"))
    assert status == 405
    assert dict(headers)["Allow"] == "GET, HEAD"
