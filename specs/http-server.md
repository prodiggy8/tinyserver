# Spec: tiny HTTP/1.1 server on raw sockets (Step 1 — base system)

## Overview

An HTTP/1.1 server written in Python from scratch on raw TCP sockets, serving
a small demo site: a simple black-and-white personal homepage for a CS student
at CMU. No HTTP libraries anywhere in `src/` (see CLAUDE.md for the forbidden
and allowed module lists). Tests may use `http.client`/`curl` as clients.

## Architecture

- `src/server.py` — socket listener + connection handling (entry point)
- `src/http_parse.py` — request parsing (request line, headers, body, chunked)
- `src/response.py` — response construction/serialization
- `src/router.py` — routing layer
- `src/static.py` — static file serving
- `src/app.py` — demo app: route registrations for the homepage site
- `public/` — static assets (HTML/CSS for the demo site)
- Handled connections must run concurrently (thread-per-connection or
  `selectors`-based event loop — implementer's choice).
- Server listens on `127.0.0.1:8080` by default; port configurable via
  `PORT` env var or `--port` flag.

## Functional requirements

### 1. Request parsing

- Parse request line: `METHOD SP request-target SP HTTP-version CRLF`.
  Malformed request line → `400 Bad Request`.
- HTTP version: accept `HTTP/1.1` and `HTTP/1.0`; anything else →
  `505 HTTP Version Not Supported`.
- Parse headers up to an empty CRLF line. Header names are case-insensitive.
  Malformed header line (no colon) → `400`.
- Limits: request line ≤ 8 KiB → else `414 URI Too Long`; total header block
  ≤ 32 KiB → else `431 Request Header Fields Too Large`.
- Body: read exactly `Content-Length` bytes when present. Invalid
  (non-numeric, negative) `Content-Length` → `400`.
- Chunked transfer encoding for REQUEST bodies: when
  `Transfer-Encoding: chunked`, decode chunk-size lines (hex) + chunk data +
  terminating `0` chunk; ignore trailers. Malformed chunk framing → `400`.
- Body size limit: 1 MiB decoded → else `413 Content Too Large`.
- Percent-decode the request path; separate and expose the query string.

### 2. Methods

- `GET` and `POST` are supported. `HEAD` responds like `GET` without a body.
- Any other known method on an existing route → `405 Method Not Allowed`
  with an `Allow` header listing supported methods.

### 3. Connection management

- HTTP/1.1: keep-alive by default; honor `Connection: close` from the client
  and close after responding.
- HTTP/1.0: close by default; keep alive only on `Connection: keep-alive`.
- Idle timeout of 5 seconds on keep-alive connections; close silently.
- Every response includes correct `Content-Length` (or closes the connection)
  so clients can delimit messages; responses include `Date` and `Server`
  headers, CRLF line endings throughout.
- After a `400`/`413`/`414`/`431`/`505` the server closes the connection.

### 4. Static file serving

- Serve files from `public/` for GET requests not matched by a dynamic route.
- `/` serves `public/index.html`; a path that is a directory serves its
  `index.html` if present, else `404`.
- MIME types by extension: html, css, js, json, txt, png, jpg/jpeg, gif, svg,
  ico, woff2; unknown extensions → `application/octet-stream`.
- Path-traversal protection: resolved file path must stay inside `public/`
  (reject `..`, encoded traversal like `%2e%2e`, absolute paths) → `404`.
- Missing file → `404` with a small HTML error page.

### 5. Routing layer

- Register handlers by `(method, exact path)`. A handler receives a request
  object (method, path, query, headers, body) and returns status, headers,
  and body. Unhandled exceptions in a handler → `500` (connection survives).
- Dynamic routes take precedence over static files.

### 6. Demo app: CMU CS student homepage

- Black-and-white design only (no colors, no images required): `index.html` +
  `style.css` in `public/`, plus a `projects.html` page linked from home.
  Content: name, short bio, courses, projects — placeholder text is fine.
- One dynamic GET route: `GET /api/uptime` returns
  `{"uptime_seconds": <float>}` as `application/json`.
- One dynamic POST route: `POST /api/echo` reads the request body and returns
  JSON `{"length": <int>, "body": "<body as text>"}` — this exercises
  Content-Length and chunked body parsing end to end.

## Acceptance criteria (curl-verifiable)

1. `curl -s http://localhost:8080/` → 200, `Content-Type: text/html`, page
   contains the student's name.
2. `curl -s http://localhost:8080/style.css` → 200, `Content-Type: text/css`.
3. `curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/nope.html` → 404.
4. `curl -s --path-as-is -o /dev/null -w '%{http_code}' http://localhost:8080/../CLAUDE.md` → 404.
5. `curl -s http://localhost:8080/api/uptime` → 200 JSON with numeric
   `uptime_seconds`.
6. `curl -s -X POST -d 'hello' http://localhost:8080/api/echo` → 200 JSON
   `{"length": 5, "body": "hello"}`.
7. `curl -s -X POST -H 'Transfer-Encoding: chunked' --data-binary @file http://localhost:8080/api/echo`
   → 200 with correct decoded length.
8. `curl -s -X DELETE -o /dev/null -w '%{http_code}' http://localhost:8080/api/echo`
   → 405, response has `Allow: POST`.
9. Two requests over one connection (`curl -s http://localhost:8080/ http://localhost:8080/api/uptime -v`)
   reuse the connection (keep-alive).
10. Raw-socket test: sending `GARBAGE\r\n\r\n` → response starts
    `HTTP/1.1 400`.
11. Raw-socket test: `GET / HTTP/2.0` request line → `505`.
12. `HEAD /` returns the same headers as `GET /` with an empty body.
13. All of the above pass via `./script/test` on a fresh clone with no
    third-party dependencies beyond pytest.

## Out of scope (Step 1)

- TLS/HTTPS, HTTP/2, HTTP/3
- Response compression (gzip), caching/ETags/conditional requests
- Authentication, cookies, sessions
- CGI, WSGI, or any framework integration
- Multipart form parsing
- The AJAX comment section and any live-update mechanism (Server-Sent Events
  or long-polling) — that is Step 2 (extension)
