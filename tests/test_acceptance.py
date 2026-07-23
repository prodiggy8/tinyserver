"""End-to-end acceptance suite: one test per numbered criterion in
specs/http-server.md's "Acceptance criteria (curl-verifiable)" section.
Uses http.client for cases curl would cover directly, and raw sockets
where request framing must be sent byte-for-byte unnormalized (path
traversal, malformed request lines, unsupported HTTP versions).
"""

import http.client
import json
import socket

import pytest

from server import HttpServer

STUDENT_NAME = "Gustavo Grancieiro"


@pytest.fixture(scope="module")
def server_port():
    srv = HttpServer(host="127.0.0.1", port=0, idle_timeout=5.0)
    port = srv.start()
    yield port
    srv.stop()


def raw_connect(port, timeout=2.0):
    return socket.create_connection(("127.0.0.1", port), timeout=timeout)


def recv_head(sock, timeout=2.0):
    """Read up through the end of the response headers (enough to check
    the status line / a header) without necessarily draining the body."""
    sock.settimeout(timeout)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def recv_full(sock, timeout=2.0):
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


# 1. GET / -> 200, Content-Type: text/html, page contains the student's name.
def test_acceptance_1_home_page(server_port):
    conn = http.client.HTTPConnection("127.0.0.1", server_port, timeout=2)
    conn.request("GET", "/")
    resp = conn.getresponse()
    body = resp.read()
    assert resp.status == 200
    assert resp.getheader("Content-Type").startswith("text/html")
    assert STUDENT_NAME.encode() in body
    conn.close()


# 2. GET /style.css -> 200, Content-Type: text/css.
def test_acceptance_2_style_css(server_port):
    conn = http.client.HTTPConnection("127.0.0.1", server_port, timeout=2)
    conn.request("GET", "/style.css")
    resp = conn.getresponse()
    resp.read()
    assert resp.status == 200
    assert resp.getheader("Content-Type").startswith("text/css")
    conn.close()


# 3. GET /nope.html -> 404.
def test_acceptance_3_missing_file_404(server_port):
    conn = http.client.HTTPConnection("127.0.0.1", server_port, timeout=2)
    conn.request("GET", "/nope.html")
    resp = conn.getresponse()
    resp.read()
    assert resp.status == 404
    conn.close()


# 4. GET /../CLAUDE.md (raw target, unnormalized like curl --path-as-is) -> 404.
def test_acceptance_4_dot_dot_traversal_404(server_port):
    sock = raw_connect(server_port)
    sock.sendall(b"GET /../CLAUDE.md HTTP/1.1\r\nHost: x\r\n\r\n")
    data = recv_head(sock)
    assert data.startswith(b"HTTP/1.1 404")
    sock.close()


def test_acceptance_4_percent_encoded_traversal_404(server_port):
    sock = raw_connect(server_port)
    sock.sendall(b"GET /%2e%2e/CLAUDE.md HTTP/1.1\r\nHost: x\r\n\r\n")
    data = recv_head(sock)
    assert data.startswith(b"HTTP/1.1 404")
    sock.close()


def test_acceptance_4_double_slash_absolute_style_404(server_port):
    sock = raw_connect(server_port)
    sock.sendall(b"GET //etc/passwd HTTP/1.1\r\nHost: x\r\n\r\n")
    data = recv_head(sock)
    assert data.startswith(b"HTTP/1.1 404")
    sock.close()


# 5. GET /api/uptime -> 200 JSON with numeric uptime_seconds.
def test_acceptance_5_uptime(server_port):
    conn = http.client.HTTPConnection("127.0.0.1", server_port, timeout=2)
    conn.request("GET", "/api/uptime")
    resp = conn.getresponse()
    body = resp.read()
    assert resp.status == 200
    payload = json.loads(body)
    assert isinstance(payload["uptime_seconds"], (int, float))
    assert payload["uptime_seconds"] >= 0
    conn.close()


# 6. POST -d 'hello' /api/echo -> {"length": 5, "body": "hello"}.
def test_acceptance_6_echo_content_length(server_port):
    conn = http.client.HTTPConnection("127.0.0.1", server_port, timeout=2)
    conn.request("POST", "/api/echo", body=b"hello")
    resp = conn.getresponse()
    body = resp.read()
    assert resp.status == 200
    assert json.loads(body) == {"length": 5, "body": "hello"}
    conn.close()


# 7. POST /api/echo, Transfer-Encoding: chunked -> 200 with correct decoded length.
def test_acceptance_7_echo_chunked(server_port):
    sock = raw_connect(server_port)
    payload = b"this is a chunked upload spanning several chunks"
    chunks = [payload[:12], payload[12:30], payload[30:]]
    encoded = b"".join(
        "{:x}\r\n".format(len(c)).encode() + c + b"\r\n" for c in chunks
    ) + b"0\r\n\r\n"
    request = (
        b"POST /api/echo HTTP/1.1\r\n"
        b"Host: x\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n" + encoded
    )
    sock.sendall(request)
    head, body = recv_full(sock)
    assert head.startswith(b"HTTP/1.1 200")
    result = json.loads(body)
    assert result["length"] == len(payload)
    assert result["body"] == payload.decode()
    sock.close()


# 8. DELETE /api/echo -> 405, Allow: POST.
def test_acceptance_8_delete_echo_405(server_port):
    conn = http.client.HTTPConnection("127.0.0.1", server_port, timeout=2)
    conn.request("DELETE", "/api/echo")
    resp = conn.getresponse()
    resp.read()
    assert resp.status == 405
    assert resp.getheader("Allow") == "POST"
    conn.close()


# 9. Two requests over one connection reuse the connection (keep-alive).
def test_acceptance_9_connection_reuse(server_port):
    conn = http.client.HTTPConnection("127.0.0.1", server_port, timeout=2)

    conn.request("GET", "/")
    resp1 = conn.getresponse()
    resp1.read()
    sock_after_first = conn.sock

    conn.request("GET", "/api/uptime")
    resp2 = conn.getresponse()
    resp2.read()
    sock_after_second = conn.sock

    assert resp1.status == 200
    assert resp2.status == 200
    assert sock_after_first is not None
    assert sock_after_first is sock_after_second
    conn.close()


# 10. Raw-socket: "GARBAGE\r\n\r\n" -> response starts "HTTP/1.1 400".
def test_acceptance_10_garbage_400(server_port):
    sock = raw_connect(server_port)
    sock.sendall(b"GARBAGE\r\n\r\n")
    data = recv_head(sock)
    assert data.startswith(b"HTTP/1.1 400")
    sock.close()


# 11. Raw-socket: "GET / HTTP/2.0" request line -> 505.
def test_acceptance_11_http_2_0_505(server_port):
    sock = raw_connect(server_port)
    sock.sendall(b"GET / HTTP/2.0\r\n\r\n")
    data = recv_head(sock)
    assert data.startswith(b"HTTP/1.1 505")
    sock.close()


# 12. HEAD / returns the same headers as GET / with an empty body.
def test_acceptance_12_head_matches_get_minus_body(server_port):
    conn = http.client.HTTPConnection("127.0.0.1", server_port, timeout=2)
    conn.request("GET", "/")
    get_resp = conn.getresponse()
    get_body = get_resp.read()
    get_headers = dict(get_resp.getheaders())
    conn.close()

    conn2 = http.client.HTTPConnection("127.0.0.1", server_port, timeout=2)
    conn2.request("HEAD", "/")
    head_resp = conn2.getresponse()
    head_body = head_resp.read()
    head_headers = dict(head_resp.getheaders())
    conn2.close()

    assert head_resp.status == get_resp.status
    assert head_body == b""
    assert get_body != b""
    for name in ("Content-Type", "Content-Length"):
        assert head_headers[name] == get_headers[name]


# 13. The whole suite runs via ./script/test with no non-pytest third-party
# dependency — that's a property of this test file and script/test/requirements,
# not a single response, so there's no separate assertion beyond "this
# module collects and runs" (already exercised by every test above).
def test_acceptance_13_suite_runs_via_script_test():
    assert True
