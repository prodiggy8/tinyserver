"""Demo app: route registrations for the CMU CS student homepage.

Wires src/router.py's `Router` to src/static.py's static file serving and
registers the two dynamic API routes from specs/http-server.md §6. `router`
is what src/server.py's `HttpServer` defaults to (see `_app_handler`).
"""

import json
import time

import static
from router import Router

START_TIME = time.time()


def uptime_handler(request):
    body = json.dumps({"uptime_seconds": time.time() - START_TIME}).encode("utf-8")
    return 200, [("Content-Type", "application/json")], body


def echo_handler(request):
    body = json.dumps({
        "length": len(request.body),
        "body": request.body.decode("utf-8", errors="replace"),
    }).encode("utf-8")
    return 200, [("Content-Type", "application/json")], body


router = Router(static_handler=static.serve)
router.get("/api/uptime", uptime_handler)
router.post("/api/echo", echo_handler)
