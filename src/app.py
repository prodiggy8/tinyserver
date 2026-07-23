"""Demo app: route registrations for the CMU CS student homepage plus the
live chat extension (specs/message-wall.md).

Wires src/router.py's `Router` to src/static.py's static file serving and
registers the dynamic routes from specs/http-server.md §6 and
specs/message-wall.md §1/§5. `router` is what src/server.py's `HttpServer`
defaults to (see `_app_handler`); `shutdown()` is what `HttpServer.close()`
calls (via the `shutdown_hook` src/server.py's `main()` wires up) to drop
every live WebSocket connection on server stop.
"""

import json
import time

import chat
import static
from cookies import build_set_cookie, is_valid_chatname, parse_cookie_header
from response import error_page, serialize_response
from router import Router
from server import HIJACKED
from websocket import HandshakeError, validate_handshake

START_TIME = time.time()

store = chat.MessageStore()
registry = chat.ConnectionRegistry()
ping_scheduler = chat.PingScheduler(registry)
ping_scheduler.start()


def uptime_handler(request):
    body = json.dumps({"uptime_seconds": time.time() - START_TIME}).encode("utf-8")
    return 200, [("Content-Type", "application/json")], body


def echo_handler(request):
    body = json.dumps({
        "length": len(request.body),
        "body": request.body.decode("utf-8", errors="replace"),
    }).encode("utf-8")
    return 200, [("Content-Type", "application/json")], body


def _chatname_from_request(request):
    cookies = parse_cookie_header(request.headers.get("cookie", ""))
    name = cookies.get("chatname")
    if is_valid_chatname(name):
        return name, False
    return chat.generate_name(), True


def index_handler(request):
    """Wraps static.serve for `/` so it can issue a `chatname` cookie —
    the static layer itself has no cookie awareness (2.5 review note)."""
    result = static.serve("GET", "/")
    if result is None:
        return 404, [("Content-Type", "text/html; charset=utf-8")], error_page(404).encode("utf-8")
    status, headers, body = result
    name, is_new = _chatname_from_request(request)
    if is_new:
        headers = list(headers) + [("Set-Cookie", build_set_cookie("chatname", name))]
    return status, headers, body


def messages_handler(request):
    body = json.dumps(store.recent()).encode("utf-8")
    headers = [
        ("Content-Type", "application/json"),
        ("X-Content-Type-Options", "nosniff"),
    ]
    return 200, headers, body


def _welcome_frames(conn, count):
    return [chat.welcome_frame(conn.name, store.recent(), count)]


def ws_handler(request):
    """Validate + perform the WebSocket handshake, then run the chat
    connection's frame loop on this (the hijacking) thread until it closes.
    Returns the HIJACKED sentinel so the HTTP loop never touches the socket
    again; on a failed handshake or a full registry it returns a normal
    (status, headers, body) tuple instead, leaving HTTP keep-alive intact.

    Ordering is load-bearing: `registry.register` starts this connection's
    writer thread, which becomes the only thread allowed to write to the
    socket — so the raw 101 response MUST be sent, in full, before
    `register` is called, or the writer thread could race it onto the wire.
    The capacity check is therefore a separate up-front peek (`at_capacity`,
    racy by design — see its docstring) rather than relying solely on
    `register`'s atomic check, since that check is too late to prevent a
    503 body from being sent after a 101 already went out.
    """
    try:
        accept = validate_handshake(request)
    except HandshakeError as exc:
        headers = list(exc.headers) + [("Content-Type", "text/html; charset=utf-8")]
        return exc.status, headers, error_page(exc.status).encode("utf-8")

    if registry.at_capacity():
        return 503, [("Content-Type", "text/html; charset=utf-8")], error_page(503).encode("utf-8")

    name, _is_new = _chatname_from_request(request)

    response_headers = [
        ("Upgrade", "websocket"),
        ("Connection", "Upgrade"),
        ("Sec-WebSocket-Accept", accept),
    ]
    response_bytes = serialize_response(101, response_headers, b"", version=request.version)
    try:
        request.sock.sendall(response_bytes)
    except OSError:
        try:
            request.sock.close()
        except OSError:
            pass
        return HIJACKED

    conn = registry.register(request.sock, name, build_initial_frames=_welcome_frames)
    if conn is None:
        # Capacity filled in the tiny race window between the check above
        # and this call; the 101 line is already on the wire, so the best
        # we can do is close without a body.
        try:
            request.sock.close()
        except OSError:
            pass
        return HIJACKED

    chat.serve_connection(request.sock, request.reader, conn, store, registry)
    return HIJACKED


def shutdown():
    """Called once on server stop (src/server.py's HttpServer.close(), via
    the injected shutdown_hook set up in main()): stop pinging and drop
    every live connection with close status 1001."""
    ping_scheduler.stop()
    registry.shutdown_all()


router = Router(static_handler=static.serve)
router.get("/", index_handler)
router.get("/api/uptime", uptime_handler)
router.post("/api/echo", echo_handler)
router.get("/api/messages", messages_handler)
router.get("/ws", ws_handler)
