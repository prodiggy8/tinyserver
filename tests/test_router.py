import pytest

from router import Router
from server import HttpServer, Request


def make_request(method, path, query="", headers=None, body=b"", version="HTTP/1.1"):
    return Request(method, path, query, headers or {}, body, version)


def echo_handler(request):
    return 200, [("Content-Type", "text/plain")], b"echo:" + request.body


def uptime_handler(request):
    return 200, [("Content-Type", "application/json")], b'{"uptime_seconds": 1.0}'


def raising_handler(request):
    raise ValueError("boom")


def make_fake_static(files):
    """files: dict path -> (content_type, body bytes)."""

    def static_handler(method, path):
        entry = files.get(path)
        if entry is None:
            return None
        content_type, body = entry
        return 200, [("Content-Type", content_type)], body

    return static_handler


# --- Route registry / dispatch ------------------------------------------------

def test_dispatches_registered_get_route():
    router = Router()
    router.get("/api/uptime", uptime_handler)

    status, headers, body = router.dispatch(make_request("GET", "/api/uptime"))

    assert status == 200
    assert body == b'{"uptime_seconds": 1.0}'
    assert ("Content-Type", "application/json") in headers


def test_dispatches_registered_post_route_with_body():
    router = Router()
    router.post("/api/echo", echo_handler)

    status, headers, body = router.dispatch(make_request("POST", "/api/echo", body=b"hello"))

    assert status == 200
    assert body == b"echo:hello"


def test_unregistered_path_with_no_static_is_404():
    router = Router()

    status, headers, body = router.dispatch(make_request("GET", "/nope"))

    assert status == 404


# --- Method handling: HEAD, 405 -----------------------------------------------

def test_head_dispatches_to_get_handler_and_strips_body():
    router = Router()
    router.get("/api/uptime", uptime_handler)

    get_status, get_headers, get_body = router.dispatch(make_request("GET", "/api/uptime"))
    head_status, head_headers, head_body = router.dispatch(make_request("HEAD", "/api/uptime"))

    assert head_status == get_status
    # HEAD headers must match GET's, plus an explicit Content-Length equal
    # to what GET's body length would have been (the connection layer
    # can't recompute it from the now-empty HEAD body).
    assert dict(head_headers)["Content-Type"] == dict(get_headers)["Content-Type"]
    assert dict(head_headers)["Content-Length"] == str(len(get_body))
    assert head_body == b""
    assert get_body != b""


def test_known_method_on_dynamic_path_with_other_methods_is_405():
    router = Router()
    router.post("/api/echo", echo_handler)

    status, headers, body = router.dispatch(make_request("DELETE", "/api/echo"))

    assert status == 405
    assert dict(headers)["Allow"] == "POST"


def test_get_on_post_only_path_is_405_not_404():
    router = Router()
    router.post("/api/echo", echo_handler)

    status, headers, body = router.dispatch(make_request("GET", "/api/echo"))

    assert status == 405
    assert dict(headers)["Allow"] == "POST"


def test_static_path_is_an_existing_route_for_405_purposes():
    router = Router(static_handler=make_fake_static({"/style.css": ("text/css", b"body{}")}))

    status, headers, body = router.dispatch(make_request("POST", "/style.css"))

    assert status == 405
    assert dict(headers)["Allow"] == "GET, HEAD"


def test_static_get_is_served_via_static_handler():
    router = Router(static_handler=make_fake_static({"/style.css": ("text/css", b"body{}")}))

    status, headers, body = router.dispatch(make_request("GET", "/style.css"))

    assert status == 200
    assert body == b"body{}"
    assert ("Content-Type", "text/css") in headers


def test_head_on_static_path_strips_body_keeps_headers():
    router = Router(static_handler=make_fake_static({"/style.css": ("text/css", b"body{}")}))

    get_status, get_headers, get_body = router.dispatch(make_request("GET", "/style.css"))
    head_status, head_headers, head_body = router.dispatch(make_request("HEAD", "/style.css"))

    assert head_status == get_status
    assert dict(head_headers)["Content-Type"] == dict(get_headers)["Content-Type"]
    assert dict(head_headers)["Content-Length"] == str(len(get_body))
    assert head_body == b""


def test_dynamic_route_takes_precedence_over_static_file_at_same_path():
    router = Router(static_handler=make_fake_static({"/api/uptime": ("text/plain", b"static")}))
    router.get("/api/uptime", uptime_handler)

    status, headers, body = router.dispatch(make_request("GET", "/api/uptime"))

    assert body == b'{"uptime_seconds": 1.0}'


def test_missing_static_and_no_dynamic_route_is_404():
    router = Router(static_handler=make_fake_static({}))

    status, headers, body = router.dispatch(make_request("GET", "/missing.html"))

    assert status == 404


# --- Handler exception safety --------------------------------------------------

def test_dispatch_propagates_handler_exceptions_uncaught():
    router = Router()
    router.get("/boom", raising_handler)

    with pytest.raises(ValueError):
        router.dispatch(make_request("GET", "/boom"))


def test_handler_exception_via_real_server_returns_500_and_survives():
    import socket

    router = Router()
    router.get("/boom", raising_handler)
    router.get("/ok", uptime_handler)

    srv = HttpServer(host="127.0.0.1", port=0, handler=router.dispatch, idle_timeout=5.0)
    port = srv.start()
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=2.0)
        sock.sendall(b"GET /boom HTTP/1.1\r\n\r\n")
        sock.settimeout(2.0)
        data = sock.recv(4096)
        assert data.startswith(b"HTTP/1.1 500")

        sock.sendall(b"GET /ok HTTP/1.1\r\n\r\n")
        data2 = sock.recv(4096)
        assert data2.startswith(b"HTTP/1.1 200")
        sock.close()
    finally:
        srv.stop()
