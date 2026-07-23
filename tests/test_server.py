import socket
import threading
import time

import pytest

from server import HttpServer


def stub_handler(request):
    if request.path == "/slow":
        time.sleep(0.5)
        return 200, [("Content-Type", "text/plain")], b"slow"
    return 200, [("Content-Type", "text/plain")], b"ok"


def raising_handler(request):
    raise ValueError("boom")


@pytest.fixture
def running_server():
    srv = HttpServer(host="127.0.0.1", port=0, handler=stub_handler, idle_timeout=5.0)
    port = srv.start()
    yield srv, port
    srv.stop()


def connect(port, timeout=2.0):
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    return sock


def recv_response(sock, timeout=2.0):
    """Minimal raw-socket response reader for tests: returns (head_bytes,
    body_bytes). Reads exactly Content-Length body bytes if present."""
    sock.settimeout(timeout)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            return data, b""
        data += chunk
    head, rest = data.split(b"\r\n\r\n", 1)
    headers = {}
    for line in head.split(b"\r\n")[1:]:
        if b":" in line:
            k, v = line.split(b":", 1)
            headers[k.strip().lower()] = v.strip()
    body = rest
    cl = headers.get(b"content-length")
    if cl is not None:
        need = int(cl) - len(body)
        while need > 0:
            chunk = sock.recv(4096)
            if not chunk:
                break
            body += chunk
            need -= len(chunk)
    return head, body


def send_request(sock, method, path, headers=None, body=b"", version="HTTP/1.1"):
    lines = ["{} {} {}".format(method, path, version)]
    headers = dict(headers or {})
    if body and "Content-Length" not in headers:
        headers["Content-Length"] = str(len(body))
    for name, value in headers.items():
        lines.append("{}: {}".format(name, value))
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")
    if isinstance(body, str):
        body = body.encode("utf-8")
    sock.sendall(request + body)


# --- Socket listener -------------------------------------------------------

def test_binds_ephemeral_port_and_serves(running_server):
    _srv, port = running_server
    assert port != 0
    sock = connect(port)
    send_request(sock, "GET", "/")
    head, body = recv_response(sock)
    assert head.startswith(b"HTTP/1.1 200")
    assert body == b"ok"
    sock.close()


def test_concurrent_connections_slow_handler_does_not_block_others(running_server):
    _srv, port = running_server
    results = {}

    def do_slow():
        sock = connect(port)
        start = time.time()
        send_request(sock, "GET", "/slow")
        recv_response(sock)
        results["slow_elapsed"] = time.time() - start
        sock.close()

    slow_thread = threading.Thread(target=do_slow)
    slow_thread.start()
    time.sleep(0.1)  # let the slow request start first

    start = time.time()
    fast_sock = connect(port)
    send_request(fast_sock, "GET", "/")
    head, body = recv_response(fast_sock)
    fast_elapsed = time.time() - start
    fast_sock.close()

    slow_thread.join()

    assert head.startswith(b"HTTP/1.1 200")
    assert body == b"ok"
    assert fast_elapsed < 0.3  # fast request wasn't stuck behind the slow one
    assert results["slow_elapsed"] >= 0.5


# --- Keep-alive semantics ----------------------------------------------------

def test_keep_alive_serves_two_requests_on_one_connection(running_server):
    _srv, port = running_server
    sock = connect(port)

    send_request(sock, "GET", "/")
    head1, body1 = recv_response(sock)
    assert head1.startswith(b"HTTP/1.1 200")
    assert body1 == b"ok"

    send_request(sock, "GET", "/")
    head2, body2 = recv_response(sock)
    assert head2.startswith(b"HTTP/1.1 200")
    assert body2 == b"ok"

    sock.close()


def test_connection_close_header_closes_connection(running_server):
    _srv, port = running_server
    sock = connect(port)

    send_request(sock, "GET", "/", headers={"Connection": "close"})
    head, _body = recv_response(sock)
    assert head.startswith(b"HTTP/1.1 200")

    # Server must close its side; a subsequent recv reaches EOF.
    sock.settimeout(2.0)
    assert sock.recv(1) == b""
    sock.close()


def test_http_10_closes_by_default(running_server):
    _srv, port = running_server
    sock = connect(port)

    send_request(sock, "GET", "/", version="HTTP/1.0")
    head, _body = recv_response(sock)
    assert head.startswith(b"HTTP/1.0 200")

    sock.settimeout(2.0)
    assert sock.recv(1) == b""
    sock.close()


def test_http_10_keep_alive_header_keeps_connection_open(running_server):
    _srv, port = running_server
    sock = connect(port)

    send_request(sock, "GET", "/", headers={"Connection": "keep-alive"}, version="HTTP/1.0")
    head1, _body1 = recv_response(sock)
    assert head1.startswith(b"HTTP/1.0 200")

    send_request(sock, "GET", "/", headers={"Connection": "keep-alive"}, version="HTTP/1.0")
    head2, _body2 = recv_response(sock)
    assert head2.startswith(b"HTTP/1.0 200")

    sock.close()


# --- Idle timeout ------------------------------------------------------------

def test_idle_timeout_closes_silently():
    srv = HttpServer(host="127.0.0.1", port=0, handler=stub_handler, idle_timeout=0.2)
    port = srv.start()
    try:
        sock = connect(port)
        time.sleep(0.5)
        sock.settimeout(2.0)
        assert sock.recv(1) == b""
        sock.close()
    finally:
        srv.stop()


# --- Error responses at the connection layer ---------------------------------

def test_garbage_request_gets_400_then_closes(running_server):
    _srv, port = running_server
    sock = connect(port)
    sock.sendall(b"GARBAGE\r\n\r\n")
    head, _body = recv_response(sock)
    assert head.startswith(b"HTTP/1.1 400")

    sock.settimeout(2.0)
    assert sock.recv(1) == b""
    sock.close()


def test_unsupported_version_gets_505(running_server):
    _srv, port = running_server
    sock = connect(port)
    sock.sendall(b"GET / HTTP/2.0\r\n\r\n")
    head, _body = recv_response(sock)
    assert head.startswith(b"HTTP/1.1 505")

    sock.settimeout(2.0)
    assert sock.recv(1) == b""
    sock.close()


# --- Handler exception safety -------------------------------------------------

def test_handler_exception_returns_500_and_connection_survives():
    srv = HttpServer(host="127.0.0.1", port=0, handler=raising_handler, idle_timeout=5.0)
    port = srv.start()
    try:
        sock = connect(port)
        send_request(sock, "GET", "/")
        head, _body = recv_response(sock)
        assert head.startswith(b"HTTP/1.1 500")

        send_request(sock, "GET", "/")
        head2, _body2 = recv_response(sock)
        assert head2.startswith(b"HTTP/1.1 500")
        sock.close()
    finally:
        srv.stop()
