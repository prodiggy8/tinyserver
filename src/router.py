"""Routing layer: exact (method, path) route registry.

`Router.dispatch` is the callable src/server.py's `HttpServer` takes as its
`handler` argument (same `Request -> (status, headers, body)` contract) —
unhandled exceptions in a registered handler are NOT caught here; they
propagate up to src/server.py's connection loop, which already turns them
into a 500 with the connection kept alive.

Static file serving lives in src/static.py. The router takes it as an
injectable `static_handler` callable (same dependency-injection pattern as
src/server.py's injectable app handler) so this module is unit-testable
with a fake static handler before src/static.py exists, and so app.py can
wire `Router(static_handler=static.serve)` once it does.
"""

from response import error_page

PREFERRED_METHOD_ORDER = ["GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"]


def _no_static_handler(method, path):
    """Default static_handler: nothing is static until src/static.py is
    wired in (constructor default)."""
    return None


def _sort_methods(methods):
    ordered = [m for m in PREFERRED_METHOD_ORDER if m in methods]
    ordered.extend(sorted(m for m in methods if m not in PREFERRED_METHOD_ORDER))
    return ordered


def _not_found_result():
    return 404, [("Content-Type", "text/html; charset=utf-8")], error_page(404)


def _method_not_allowed_result(allowed_methods):
    headers = [
        ("Allow", ", ".join(allowed_methods)),
        ("Content-Type", "text/html; charset=utf-8"),
    ]
    return 405, headers, error_page(405)


class Router:
    """Route registry keyed by (method, exact path)."""

    def __init__(self, static_handler=_no_static_handler):
        self._routes = {}
        self.static_handler = static_handler

    def add_route(self, method, path, handler):
        """Register `handler(request) -> (status, headers, body)` for an
        exact `(method, path)` pair."""
        self._routes[(method.upper(), path)] = handler

    def get(self, path, handler):
        self.add_route("GET", path, handler)

    def post(self, path, handler):
        self.add_route("POST", path, handler)

    def dispatch(self, request):
        """Dispatch `request` to a registered handler, the injected static
        handler, or a 404/405 result. `HEAD` dispatches to the `GET`
        handler (dynamic or static) and strips the body, keeping the
        status/headers (including Content-Length) identical to `GET`."""
        method = request.method.upper()
        path = request.path
        lookup_method = "GET" if method == "HEAD" else method

        route_handler = self._routes.get((lookup_method, path))
        if route_handler is not None:
            status, headers, body = route_handler(request)
            return self._finish(method, status, headers, body)

        if lookup_method == "GET":
            static_response = self.static_handler("GET", path)
            if static_response is not None:
                return self._finish(method, *static_response)
            static_exists = False
        else:
            static_exists = self.static_handler("GET", path) is not None

        allowed = {m for (m, p) in self._routes if p == path}
        if static_exists:
            allowed |= {"GET", "HEAD"}

        if allowed:
            return self._finish(method, *_method_not_allowed_result(_sort_methods(allowed)))
        return self._finish(method, *_not_found_result())

    @staticmethod
    def _finish(method, status, headers, body):
        if method == "HEAD":
            # Content-Length must match what GET would have sent. Set it
            # explicitly from the real body now, before the body is
            # dropped — src/response.py's serialize_response only
            # auto-fills Content-Length when it's absent, so this survives
            # to the wire instead of being recomputed as 0.
            headers = list(headers)
            if not any(name.lower() == "content-length" for name, _ in headers):
                headers.append(("Content-Length", str(len(body))))
            body = b""
        return status, headers, body
