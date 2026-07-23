"""Connection handling: socket listener + thread-per-connection request
loop. Entry point for running the server.

The request handler is injectable (constructor argument) so this module
can be built and tested before src/router.py + src/app.py exist; the real
app wiring plugs in later as the default. The handler contract matches
src/router.py's eventual shape: a callable taking a `Request` and
returning `(status, headers, body)`.
"""

import os
import socket
import sys
import threading

from http_parse import (
    BufferedReader,
    ConnectionClosed,
    HttpError,
    read_body,
    read_request_head,
)
from response import error_response, serialize_response

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
IDLE_TIMEOUT = 5.0

# Statuses http_parse.py's HttpError can carry; per spec, the connection is
# closed after any of these regardless of the client's keep-alive wishes.
CLOSING_ERROR_STATUSES = {400, 413, 414, 431, 505}

# A handler returns this instead of a (status, headers, body) tuple to take
# over the raw socket (e.g. a WebSocket upgrade). `Router.dispatch` passes it
# through unchanged before attempting to unpack a 3-tuple; `_handle_connection`
# skips response serialization and returns without closing the socket — the
# handler now owns it (reading, writing, and eventually closing it).
HIJACKED = object()


class Request:
    """Request object passed to the injected handler. `sock` and `reader`
    (the same BufferedReader the HTTP loop used, which may already hold
    bytes read past this request) let a handler hijack the connection —
    see HIJACKED above."""

    __slots__ = ("method", "path", "query", "headers", "body", "version", "sock", "reader")

    def __init__(self, method, path, query, headers, body, version, sock=None, reader=None):
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body
        self.version = version
        self.sock = sock
        self.reader = reader


def _app_handler(request):
    """Constructor default: the real demo app. Imported lazily so modules
    that only need `HttpServer` (most of the test suite) don't pay for
    importing src/app.py, and to avoid any import-order coupling between
    the two modules."""
    import app
    return app.router.dispatch(request)


def _wants_keepalive(version, headers):
    connection = headers.get("connection", "").lower()
    if version == "HTTP/1.0":
        return "keep-alive" in connection
    return "close" not in connection


def _send_closing_error(sock, status, version):
    response = error_response(status, headers=[("Connection", "close")], version=version)
    try:
        sock.sendall(response)
    except OSError:
        pass


def _handle_connection(sock, handler, idle_timeout):
    reader = BufferedReader(sock)
    hijacked = False
    try:
        while True:
            sock.settimeout(idle_timeout)
            try:
                head = read_request_head(reader)
            except socket.timeout:
                return
            except HttpError as exc:
                _send_closing_error(sock, exc.status, "HTTP/1.1")
                return
            except ConnectionClosed:
                return

            if head is None:
                return

            method, path, raw_query, version, headers = head

            try:
                body = read_body(reader, headers)
            except socket.timeout:
                return
            except HttpError as exc:
                _send_closing_error(sock, exc.status, version)
                return
            except ConnectionClosed:
                return

            keep_alive = _wants_keepalive(version, headers)
            connection_header = ("Connection", "keep-alive" if keep_alive else "close")

            request = Request(method, path, raw_query, headers, body, version, sock, reader)
            try:
                result = handler(request)
                if result is HIJACKED:
                    hijacked = True
                    return
                status, resp_headers, resp_body = result
                pairs = list(resp_headers) if resp_headers else []
                pairs.append(connection_header)
                response = serialize_response(status, pairs, resp_body, version=version)
            except Exception:
                response = error_response(500, headers=[connection_header], version=version)

            try:
                sock.sendall(response)
            except OSError:
                return

            if not keep_alive:
                return
    finally:
        if not hijacked:
            try:
                sock.close()
            except OSError:
                pass


class HttpServer:
    """Binds a listening socket and dispatches one thread per accepted
    connection. `handler` and `idle_timeout` are injectable so tests don't
    need routing/static files/the demo app, and so the idle-timeout test
    can use a short timeout."""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, handler=_app_handler,
                 idle_timeout=IDLE_TIMEOUT, backlog=128, shutdown_hook=None):
        self.host = host
        self.port = port
        self.handler = handler
        self.idle_timeout = idle_timeout
        self.backlog = backlog
        self.shutdown_hook = shutdown_hook
        self._sock = None
        self._accept_thread = None

    def bind(self):
        """Create and bind the listening socket; safe to call once before
        `start()`/`serve_forever()`. Returns the bound port (useful when
        constructed with port=0 for an ephemeral port)."""
        if self._sock is not None:
            return self.port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(self.backlog)
        self._sock = sock
        self.port = sock.getsockname()[1]
        return self.port

    def _accept_loop(self):
        while True:
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                return
            t = threading.Thread(
                target=_handle_connection,
                args=(conn, self.handler, self.idle_timeout),
                daemon=True,
            )
            t.start()

    def serve_forever(self):
        """Blocking accept loop; returns cleanly on KeyboardInterrupt or
        once the listening socket is closed."""
        self.bind()
        try:
            self._accept_loop()
        except KeyboardInterrupt:
            pass
        finally:
            self.close()

    def start(self):
        """Run the accept loop on a background daemon thread (for tests
        and embedding). Returns the bound port."""
        self.bind()
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        return self.port

    def stop(self):
        """Stop accepting new connections; in-flight connections finish on
        their own threads."""
        self.close()

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
            if self.shutdown_hook is not None:
                self.shutdown_hook()


def _parse_port(argv, default_port):
    port = default_port
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
            i += 2
        elif arg.startswith("--port="):
            port = int(arg.split("=", 1)[1])
            i += 1
        else:
            i += 1
    return port


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    default_port = int(os.environ.get("PORT", DEFAULT_PORT))
    port = _parse_port(argv, default_port)

    import app
    server = HttpServer(host=DEFAULT_HOST, port=port, handler=app.router.dispatch,
                         shutdown_hook=app.shutdown)
    bound_port = server.bind()
    print("Listening on http://{}:{}".format(DEFAULT_HOST, bound_port))
    server.serve_forever()


if __name__ == "__main__":
    main()
