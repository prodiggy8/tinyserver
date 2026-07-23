import json
import socket

import pytest

from server import HttpServer


@pytest.fixture
def running_server():
    srv = HttpServer(host="127.0.0.1", port=0, idle_timeout=5.0)
    port = srv.start()
    yield port
    srv.stop()


def connect(port):
    return socket.create_connection(("127.0.0.1", port), timeout=2.0)


def recv_response(sock, timeout=2.0):
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


# --- Static site --------------------------------------------------------

def test_home_page_is_html_with_student_name(running_server):
    sock = connect(running_server)
    sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    head, body = recv_response(sock)
    assert head.startswith(b"HTTP/1.1 200")
    assert b"text/html" in head
    assert b"Alex Rivera" in body
    sock.close()


def test_projects_page_is_html_with_student_name(running_server):
    sock = connect(running_server)
    sock.sendall(b"GET /projects.html HTTP/1.1\r\nHost: x\r\n\r\n")
    head, body = recv_response(sock)
    assert head.startswith(b"HTTP/1.1 200")
    assert b"text/html" in head
    assert b"Alex Rivera" in body
    sock.close()


def test_style_css_is_served_as_text_css(running_server):
    sock = connect(running_server)
    sock.sendall(b"GET /style.css HTTP/1.1\r\nHost: x\r\n\r\n")
    head, _body = recv_response(sock)
    assert head.startswith(b"HTTP/1.1 200")
    assert b"text/css" in head
    sock.close()


def test_missing_file_is_404(running_server):
    sock = connect(running_server)
    sock.sendall(b"GET /nope.html HTTP/1.1\r\nHost: x\r\n\r\n")
    head, _body = recv_response(sock)
    assert head.startswith(b"HTTP/1.1 404")
    sock.close()


# --- GET /api/uptime -------------------------------------------------------

def test_uptime_endpoint_returns_numeric_uptime(running_server):
    sock = connect(running_server)
    sock.sendall(b"GET /api/uptime HTTP/1.1\r\nHost: x\r\n\r\n")
    head, body = recv_response(sock)
    assert head.startswith(b"HTTP/1.1 200")
    assert b"application/json" in head
    payload = json.loads(body)
    assert isinstance(payload["uptime_seconds"], (int, float))
    assert payload["uptime_seconds"] >= 0
    sock.close()


# --- POST /api/echo ---------------------------------------------------------

def test_echo_with_content_length_body(running_server):
    sock = connect(running_server)
    body = b"hello"
    request = (
        b"POST /api/echo HTTP/1.1\r\n"
        b"Host: x\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"\r\n" + body
    )
    sock.sendall(request)
    head, resp_body = recv_response(sock)
    assert head.startswith(b"HTTP/1.1 200")
    payload = json.loads(resp_body)
    assert payload == {"length": 5, "body": "hello"}
    sock.close()


def test_echo_with_chunked_body(running_server):
    sock = connect(running_server)
    chunks = [b"hello, ", b"chunked world!"]
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
    head, resp_body = recv_response(sock)
    assert head.startswith(b"HTTP/1.1 200")
    payload = json.loads(resp_body)
    full = b"".join(chunks)
    assert payload["length"] == len(full)
    assert payload["body"] == full.decode("utf-8")
    sock.close()


def test_delete_on_echo_is_405_with_allow_post(running_server):
    sock = connect(running_server)
    sock.sendall(b"DELETE /api/echo HTTP/1.1\r\nHost: x\r\n\r\n")
    head, _body = recv_response(sock)
    assert head.startswith(b"HTTP/1.1 405")
    assert b"Allow: POST" in head
    sock.close()
