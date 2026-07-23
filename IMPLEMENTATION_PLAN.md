# IMPLEMENTATION_PLAN.md — Step 1: raw-socket HTTP/1.1 server + demo app

Reviewed 2026-07-22 against `specs/http-server.md`. All spec sections §1–6
and all 13 acceptance criteria map to at least one task below.

Architecture recap (why each module's default is what it is — see each
section's `Done:` notes for detail): `http_parse.py`/`response.py` are pure
functions; `server.py`'s `HttpServer` takes its request handler as an
injectable callable (default: lazily-imported `app.router.dispatch`) so it
was built/tested before `router.py` existed; `router.py`'s `Router` takes an
injectable `static_handler` (default: a no-op) for the same reason relative
to `static.py`; `app.py` wires `Router(static_handler=static.serve)` plus
the two API routes and is what ties everything together for `script/server`.

Next unchecked priority: §8 (end-to-end acceptance test module mirroring all
13 curl criteria + `running.md`) — the last section.

Notes for future iterations:
- Step 2 (AJAX comment section + SSE/long-polling) is explicitly out of scope
  for Step 1 and has no spec yet — it gets its own specify stage later. Do NOT
  build it or author its spec as part of this plan.
- Concurrency choice (spec allows either): thread-per-connection.
- Tests may use `http.client`/`urllib`/raw sockets as clients; `src/` must
  never import HTTP modules (see CLAUDE.md hard constraints).
- `pytest` is not available system-wide (pip is externally-managed on this
  machine). `script/test` auto-provisions a local `.venv` on first run and
  installs pytest into it — don't assume a bare `pytest` command works;
  always go through `./script/test`.
- `public/index.html`/`projects.html` use the placeholder name "Alex Rivera"
  (spec: "placeholder text is fine") — the acceptance tests in §8 should
  assert against that same string.

## 1. Scaffolding

- [x] Create `script/test` (runs pytest over `tests/`, exits nonzero on
      failure) and `script/server` (runs `python3 src/server.py`), both
      executable; create `src/`, `tests/`, `public/` dirs with a trivial
      smoke test so `./script/test` passes on a fresh clone.
      Done: `script/test` auto-creates a `.venv` and runs `pytest tests/`;
      `script/server` runs `python3 src/server.py "$@"`.
- [x] Add a guard test that scans `src/*.py` imports and fails if any
      forbidden module is imported: http, http.server, http.client,
      socketserver, wsgiref, and **any** `urllib.*`; also fail on the
      substring `asyncio.start_server` anywhere in src.
      Done: `tests/test_no_forbidden_imports.py` — AST-based import scan
      plus a substring scan for `asyncio.start_server`.

## 2. Request parsing (`src/http_parse.py` — pure functions, unit-testable without sockets)

- [x] Request-line parsing: `METHOD SP target SP HTTP-version CRLF`; malformed
      → 400; version `HTTP/1.1`/`HTTP/1.0` accepted, others → 505; request
      line > 8 KiB → 414.
- [x] Header parsing: case-insensitive names, up to empty CRLF line; line
      without a colon → 400; total header block > 32 KiB → 431.
- [x] Path handling: split off the query string (raw, undecoded) FIRST, then
      percent-decode only the path (hand-rolled; `urllib.parse` is off-limits
      per the guard test). Invalid escapes (`%zz`, truncated `%4`) are left
      literal rather than raising.
- [x] Content-Length body reading: read exactly N bytes; non-numeric/negative
      Content-Length → 400; decoded body > 1 MiB → 413.
- [x] Chunked transfer decoding: hex chunk-size lines (incl. extensions after
      `;`), chunk data, terminating `0` chunk, ignore trailers; malformed
      framing → 400; decoded total > 1 MiB → 413.
- [x] Incremental socket reading helper: buffer-based reader pulling header
      block/body off a socket-like object across odd-sized `recv()`s.
      Done: `BufferedReader`, plus orchestration helpers
      `read_request_head`/`read_body` that apply the 8 KiB/32 KiB/1 MiB
      limits and raise `HttpError`/`ConnectionClosed` — what `server.py`'s
      connection loop calls directly. 34 unit tests in
      `tests/test_http_parse.py`.

## 3. Response construction (`src/response.py`)

- [x] Response serialization: status line, headers, CRLF endings, body bytes;
      always sets `Content-Length`/`Date`/`Server`; helpers for common
      statuses (200, 400, 404, 405, 413, 414, 431, 500, 505) with a small
      HTML error page body for errors. `Date` is a hand-rolled RFC 7231
      IMF-fixdate from `time.gmtime()` with hardcoded English day/month names
      (locale-independent — do NOT use `strftime`/`email.utils.formatdate`).
      Done: `serialize_response(status, headers, body, version=)`;
      `ok_response`/`error_response` plus per-status wrappers
      (`method_not_allowed` sets `Allow`); `error_page(status, detail=None)`
      is public so other modules (`router.py`) can reuse the same HTML
      template for tuple-style `(status, headers, body)` results instead of
      fully-serialized bytes. `headers` accepts a dict or list of pairs. 16
      unit tests in `tests/test_response.py` (incl. a `de_DE.UTF-8`-locale
      run of `format_http_date`).

## 4. Connection handling (`src/server.py`)

- [x] Socket listener: bind `127.0.0.1:8080` by default; port from `PORT` env
      var or `--port` flag (flag wins); `SO_REUSEADDR`; thread-per-connection
      dispatch; clean shutdown on KeyboardInterrupt/closing the listen socket.
      Handler and idle timeout are injectable; port-0 binds expose the bound
      port via `.port` for tests.
- [x] Keep-alive semantics: HTTP/1.1 keep-alive by default, close on
      `Connection: close`; HTTP/1.0 close by default, keep alive on
      `Connection: keep-alive`; multiple sequential requests over one
      connection.
- [x] Idle timeout: 5 s on keep-alive connections (injectable), close
      silently via `socket.timeout`.
- [x] Error responses at the connection layer: `HttpError` → matching
      400/413/414/431/505 response, then close (`CLOSING_ERROR_STATUSES`).
      Done: `HttpServer` (bind/start/serve_forever/stop) +
      `_handle_connection`. Handler contract: callable taking a `Request`
      (method/path/query/headers/body/version) and returning
      `(status, headers, body)` — the server appends the `Connection` header
      itself, so handlers must NOT set one; HEAD-stripping and static
      404-fallback are entirely the router/static layer's job. Handler
      exceptions → 500, connection stays alive. 10 integration tests in
      `tests/test_server.py` (raw sockets + stub/raising handlers).

## 5. Routing layer (`src/router.py`)

- [x] Route registry keyed by `(method, exact path)`; handler receives a
      `Request` and returns `(status, headers, body)`; dynamic routes take
      precedence over static files.
- [x] Method handling: `HEAD` dispatches to the `GET` handler (or static
      fallback) and strips the body, keeping status/headers identical to
      GET; a path that's "known" via another dynamic method OR a static file
      → 405 with `Allow` (e.g. `POST /style.css` → `405 Allow: GET, HEAD`,
      ordered via `PREFERRED_METHOD_ORDER`); otherwise 404.
- [x] Handler exception safety: `Router.dispatch` does NOT catch handler
      exceptions — they propagate to `server.py`'s existing 500 handling, so
      no extra code was needed here; verified with a real
      `Router`+`HttpServer` integration test.
      Done: `Router` — `add_route`/`get`/`post`, `dispatch(request)` matches
      `HttpServer`'s handler contract exactly (`HttpServer(handler=router.dispatch)`
      wires directly). `static_handler` is `callable(method, path) ->
      (status, headers, body) | None`, always called with `method="GET"`,
      at most once per request. 13 tests in `tests/test_router.py`.

## 6. Static file serving (`src/static.py`)

- [x] Serve files from `public/` for unmatched GETs; `/` →
      `public/index.html`; directory path → its `index.html` if present else
      404 (via `None`); missing file → `None` (`Router` turns this into 404).
- [x] MIME table: html, css, js, json, txt, png, jpg/jpeg, gif, svg, ico,
      woff2 (hand-rolled dict, not `mimetypes`); unknown →
      `application/octet-stream`.
- [x] Path-traversal protection: resolved path must stay inside `root`.
      Done: `serve(method, path, root=PUBLIC_DIR)` matches `Router`'s
      `static_handler` contract exactly. `_resolve` strips leading `/` from
      the (already percent-decoded) path before joining onto `root`, then
      `os.path.realpath`s the result and rejects anything that isn't `root`
      itself or under `root + os.sep` — this catches `..`, already-decoded
      `%2e%2e`, and confines `//etc/passwd`-style targets to
      `root/etc/passwd` rather than escaping. `root` is an injectable param
      so tests use a `tmp_path` fixture. 23 tests in `tests/test_static.py`.

## 7. Demo app (`src/app.py` + `public/`)

- [x] Static site: black-and-white `public/index.html` (name, bio, courses,
      projects link), `public/style.css`, `public/projects.html` linked from
      home; no colors, no images.
- [x] `GET /api/uptime` → 200 `application/json` `{"uptime_seconds": <float>}`
      measured from server start (`app.START_TIME`).
- [x] `POST /api/echo` → 200 JSON `{"length": <int>, "body": "<text>"}` for
      both Content-Length and chunked request bodies (`length` is the byte
      length of the raw body).
      Done: `src/app.py` builds `router = Router(static_handler=static.serve)`
      and registers `GET /api/uptime`, `POST /api/echo`; `server.py`'s
      `HttpServer` now defaults its handler to `app.router.dispatch`
      (lazily imported) instead of a 404 stub. 8 integration tests in
      `tests/test_app.py` covering the static site, uptime, and echo
      (Content-Length + chunked) over a real `HttpServer`. Manually verified
      with `curl` against a live run, incl. `/../CLAUDE.md` and
      `/%2e%2e/CLAUDE.md` → 404.

## 8. End-to-end acceptance + docs

- [ ] Acceptance test module mirroring all 13 curl criteria in
      `specs/http-server.md` §Acceptance (using http.client/raw sockets as
      clients), so `./script/test` proves the spec end to end on a fresh
      clone.
      Verify: `./script/test` green; each criterion is a named test. Note:
      criterion #9 (connection reuse) is testable without curl via one
      `http.client.HTTPConnection` issuing two requests, or a raw socket
      sending two requests and reading two responses.
- [ ] Write `running.md`: how a grader on a fresh clone starts the server,
      uses the app, and runs the tests.
      Verify: commands in the doc are the same ones the acceptance tests
      exercise (manual read-through; scriptable check optional).
