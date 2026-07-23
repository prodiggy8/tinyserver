# Running this project

A from-scratch HTTP/1.1 server on raw TCP sockets (Python 3.12, stdlib
only — no `http.server`/`http.client`/`socketserver`/`urllib.request`/etc.
in `src/`), serving a small demo homepage. See `specs/http-server.md` for
the full spec and `IMPLEMENTATION_PLAN.md` for what's built.

## Requirements

- Python 3.12+ (no other runtime dependency to *run* the server).
- `pytest` to run the tests — `script/test` provisions this automatically
  (see below), so nothing needs to be installed by hand.

## Running the server

```sh
./script/server
```

Serves on `http://localhost:8080` by default. To use a different port:

```sh
PORT=9000 ./script/server
# or
./script/server --port 9000
```

Stop it with Ctrl-C.

## Using the app

With the server running (defaults below assume port 8080):

- `http://localhost:8080/` — homepage (name, bio, courses, projects link).
- `http://localhost:8080/projects.html` — projects page.
- `http://localhost:8080/style.css` — stylesheet.
- `GET http://localhost:8080/api/uptime` — JSON `{"uptime_seconds": <float>}`.
- `POST http://localhost:8080/api/echo` — echoes the request body back as
  JSON `{"length": <int>, "body": "<text>"}`. Works with both a
  `Content-Length` body and `Transfer-Encoding: chunked`.

Example with curl:

```sh
curl -s http://localhost:8080/
curl -s http://localhost:8080/api/uptime
curl -s -X POST -d 'hello' http://localhost:8080/api/echo
curl -s -X POST -H 'Transfer-Encoding: chunked' --data-binary @somefile http://localhost:8080/api/echo
```

## Running the tests

```sh
./script/test
```

This provisions a local `.venv` with `pytest` on first run (the system
Python is externally-managed, so this avoids any global `pip install`),
then runs the full suite in `tests/`, including:

- Unit tests for request parsing, response serialization, routing, and
  static file serving.
- Integration tests that start a real server on an ephemeral port and
  drive it over raw sockets / `http.client`.
- `tests/test_acceptance.py` — one test per numbered criterion in
  `specs/http-server.md`'s Acceptance section, so a green `./script/test`
  is a full spec check on a fresh clone.
- `tests/test_no_forbidden_imports.py` — a guard test that fails the
  build if `src/` ever imports a forbidden HTTP module.

`./script/test` exits nonzero if any test fails.
