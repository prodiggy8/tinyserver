# IMPLEMENTATION_PLAN.md — Step 1: raw-socket HTTP/1.1 server + demo app

Reviewed 2026-07-22 against `specs/http-server.md` (review stage). Coverage
check: all spec sections §1–6 and all 13 acceptance criteria map to at least
one task below. Review changes: pinned hand-rolled percent-decoding and banned
all `urllib.*` in the guard test (§1); pinned locale-independent `Date`
construction (§3); made the request handler injectable so §4 can be tested
before §5 exists (§4 dependency note).

Status: task 1 (scaffolding) done 2026-07-22 — `src/`, `tests/`, `public/`
created, `script/test`/`script/server` in place, guard + smoke tests green.
Task 2 (request parsing) done 2026-07-22 — `src/http_parse.py` implements
request-line/header parsing, path/query split with hand-rolled
percent-decoding, Content-Length and chunked body reading, and a
`BufferedReader` incremental socket reader; 34 unit tests in
`tests/test_http_parse.py` (plus `tests/conftest.py` adding `src/` to
`sys.path` for test imports).
Task 3 (response construction) done 2026-07-22 — `src/response.py`
implements `serialize_response` (status line + headers + CRLF + body,
auto-fills `Content-Length`/`Date`/`Server`), a hand-rolled
`format_http_date` (locale-independent, tested under `de_DE.UTF-8`), an
`ok_response` helper, and error-page builders for 400/404/405/413/414/431/
500/505 (`method_not_allowed` also sets `Allow`); 16 unit tests in
`tests/test_response.py`.
Task 4 (connection handling) done 2026-07-22 — `src/server.py` implements
`HttpServer` (bind/start/serve_forever/stop, `SO_REUSEADDR`,
thread-per-connection, `PORT` env var / `--port` flag with flag winning,
port-0 → bound ephemeral port exposed via `.port`) and the per-connection
loop `_handle_connection` (keep-alive per HTTP version + `Connection`
header, 5 s idle timeout via injectable `idle_timeout` → silent close on
`socket.timeout`, `HttpError` → status response + close per
`CLOSING_ERROR_STATUSES` {400,413,414,431,505}, handler exceptions → 500
with connection kept alive). Handler contract: callable taking a `Request`
(method/path/query/headers/body/version) and returning
`(status, headers, body)` — matches §5's eventual router shape, so the
router can be dropped in as the `handler=` argument directly.
`default_handler` (404 for everything) is the constructor default until
§5/§7 wire in real routing. 10 integration tests in `tests/test_server.py`
(raw sockets + a stub/raising handler), covering all 4 task bullets:
concurrent slow/fast connections, keep-alive reuse + `Connection: close` +
HTTP/1.0 default-close/keep-alive-header, shortened-timeout idle close,
malformed-request 400 + `HTTP/2.0` 505 (both followed by EOF), and
handler-exception 500 with connection survival. Manually verified with
`curl -v` that keep-alive reuse ("Re-using existing connection") works
against a live run. Next unchecked priority: §5 routing layer
(`src/router.py`) — build a `Router` whose `dispatch(request)` matches
`HttpServer`'s handler contract, then wire `HttpServer(handler=router.dispatch)`
in `src/app.py`/`script/server`.

Note for §5 integration: `src/server.py`'s injected handler receives a
`Request` (method, path, query, headers, body, version — see
`server.Request`) and must return `(status, headers, body)`; the server
already appends the `Connection` header itself, so router/app handlers
should NOT set one. HEAD-stripping and the static-file 404 fallback are
entirely the router/static layer's job — `server.py` has no method- or
path-specific logic.

Notes for future iterations:
- Step 2 (AJAX comment section + SSE/long-polling) is explicitly out of scope
  for Step 1 and has no spec yet — it gets its own specify stage later. Do NOT
  build it or author its spec as part of this plan.
- Concurrency choice (spec allows either): thread-per-connection — simplest
  correct option; revisit only if a task below forces it.
- Tests may use `http.client`/`urllib`/raw sockets as clients; `src/` must
  never import HTTP modules (see CLAUDE.md hard constraints).
- `pytest` is not available system-wide (pip is externally-managed on this
  machine). `script/test` auto-provisions a local `.venv` on first run and
  installs pytest into it — don't assume a bare `pytest` command works;
  always go through `./script/test`.

## 1. Scaffolding

- [x] Create `script/test` (runs pytest over `tests/`, exits nonzero on
      failure) and `script/server` (runs `python3 src/server.py`), both
      executable; create `src/`, `tests/`, `public/` dirs with a trivial
      smoke test so `./script/test` passes on a fresh clone.
      Verify: `./script/test` exits 0; `./script/test` exits nonzero when a
      failing test is present.
      Done: `script/test` auto-creates a `.venv` (pytest not available
      system-wide; environment is externally-managed) and runs
      `pytest tests/`; `script/server` runs `python3 src/server.py "$@"`.
      Verified exit codes 0/nonzero manually (see commit).
- [x] Add a guard test that scans `src/*.py` imports and fails if any
      forbidden module is imported: http, http.server, http.client,
      socketserver, wsgiref, and **any** `urllib.*` (not just
      `urllib.request` — `urllib.parse.unquote`/`quote` would hand us
      percent-decoding, which the spec expects us to implement); also fail on
      the substring `asyncio.start_server` anywhere in src (PROMPT_build.md
      forbids high-level asyncio HTTP/server helpers even though `selectors`
      is fine).
      Verify: test passes on clean src; fails if `import http` or
      `from urllib.parse import unquote` is added.
      Done: `tests/test_no_forbidden_imports.py` — AST-based import scan
      (catches `import X` and `from X import Y` forms, so `unquote` bare name
      is also caught since `from urllib.parse import unquote` records module
      `urllib.parse`) plus a substring scan for `asyncio.start_server`.
      Manually verified both failure modes trigger a nonzero `./script/test`.

## 2. Request parsing (`src/http_parse.py` — pure functions, unit-testable without sockets)

- [x] Request-line parsing: `METHOD SP target SP HTTP-version CRLF`; malformed
      → 400 error; version `HTTP/1.1`/`HTTP/1.0` accepted, others → 505;
      request line > 8 KiB → 414.
      Verify: unit tests for valid lines, missing parts, bad version, long line.
- [x] Header parsing: case-insensitive names, up to empty CRLF line; line
      without a colon → 400; total header block > 32 KiB → 431.
      Verify: unit tests incl. case-insensitive lookup and oversized block.
- [x] Path handling: split off the query string (raw, left undecoded) FIRST,
      then percent-decode only the path. Percent-decoding is hand-rolled
      (scan for `%`, parse two hex digits, decode resulting bytes as UTF-8
      with `errors="replace"`); `urllib.parse` is off-limits per the guard
      test. Invalid escapes (`%zz`, truncated `%4`) are left literal rather
      than raising.
      Verify: unit tests for `/a%20b?x=1&y=2` → path `/a b`, query `x=1&y=2`;
      `%2e%2e` → `..` (feeds the traversal tests in §6); invalid escapes pass
      through unchanged; a `%3F` in the path does not create a query split.
- [x] Content-Length body reading: read exactly N bytes; non-numeric or
      negative Content-Length → 400; decoded body > 1 MiB → 413.
      Verify: unit tests with exact/short/oversized bodies.
- [x] Chunked transfer decoding: hex chunk-size lines (incl. chunk extensions
      after `;`), chunk data, terminating `0` chunk, ignore trailers; malformed
      framing → 400; decoded total > 1 MiB → 413.
      Verify: unit tests for multi-chunk body, bad hex size, missing CRLF,
      trailers present.
- [x] Incremental socket reading helper: buffer-based reader that pulls header
      block and body off a socket-like object (handles bytes split across
      recv() calls and pipelined leftover bytes).
      Verify: unit tests with a fake socket delivering data in odd-sized pieces.
      Done: `BufferedReader` in `src/http_parse.py`, plus orchestration
      helpers `read_request_head`/`read_body` that apply the 8 KiB/32 KiB/
      1 MiB limits and raise `HttpError`/`ConnectionClosed` as appropriate —
      these are what §4's connection loop will call directly.

## 3. Response construction (`src/response.py`)

- [x] Response serialization: status line, headers, CRLF endings, body bytes;
      always sets `Content-Length`, `Date`, and `Server` headers; helpers for
      common statuses (200, 400, 404, 405, 413, 414, 431, 500, 505) with a
      small HTML error page body for errors. `Date` is RFC 7231 IMF-fixdate
      built by hand from `time.gmtime()` with hardcoded English day/month
      name arrays — do NOT use `strftime("%a, %d %b %Y")` (locale-dependent:
      a non-English locale silently emits an invalid HTTP date) and do NOT
      use `email.utils.formatdate` (keeps provenance obvious, consistent with
      the hand-rolled MIME table in §6).
      Verify: unit tests parse serialized bytes and check exact framing; a
      `Date` test asserts the RFC 7231 shape (e.g. regex
      `^[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} GMT$`)
      and passes under a non-English locale.
      Done: `serialize_response(status, headers, body, version=)` builds the
      byte stream; `ok_response`/`error_response` plus per-status wrappers
      (`bad_request`, `not_found`, `method_not_allowed` w/ `Allow`,
      `content_too_large`, `uri_too_long`, `header_fields_too_large`,
      `internal_server_error`, `version_not_supported`) cover the spec's
      status set. `headers` param accepts a dict or list of (name, value)
      pairs — §5 route handlers should return whichever is convenient;
      `serialize_response` only fills in Content-Length/Date/Server when the
      caller hasn't already supplied them, so a route can override e.g.
      Content-Length for a HEAD response later. 16 unit tests in
      `tests/test_response.py`, including a `de_DE.UTF-8`-locale run of
      `format_http_date` (skipped if that locale isn't installed).

## 4. Connection handling (`src/server.py`)

Dependency note: §4 is built and tested BEFORE the router (§5) exists, so the
server must take its request handler as an injectable callable (constructor
argument, defaulting to the real app wiring once §5/§7 land). All §4 tests use
a stub handler that returns a fixed 200 — they must not depend on routing,
static files, or the demo app. The same injection point is what makes the
idle-timeout test below practical.

- [x] Socket listener: bind `127.0.0.1:8080` by default; port from `PORT` env
      var or `--port` flag (flag wins); `SO_REUSEADDR`; thread-per-connection
      dispatch; clean shutdown on KeyboardInterrupt. Handler callable and
      keep-alive timeout are injectable (see dependency note); expose the
      bound port when binding to port 0 so tests can use ephemeral ports.
      Verify: integration test starts server on an ephemeral port, two
      concurrent clients get responses (one slow handler doesn't block the
      other).
      Done: `HttpServer` in `src/server.py`; see task-4 summary above.
- [x] Keep-alive semantics: HTTP/1.1 keep-alive by default, close on
      `Connection: close`; HTTP/1.0 close by default, keep alive on
      `Connection: keep-alive`; multiple sequential requests served over one
      connection.
      Verify: raw-socket test sends two requests on one connection, gets two
      responses; `Connection: close` request → server closes.
      Done: `_wants_keepalive` in `src/server.py`.
- [x] Idle timeout: 5 s on keep-alive connections, close silently.
      Verify: test with shortened timeout (make it injectable) — connection
      closes with no bytes sent after idling.
      Done: `idle_timeout` constructor arg, applied via `sock.settimeout`.
- [x] Error responses at the connection layer: parse errors map to
      400/413/414/431/505 responses and the connection is closed afterwards.
      Verify: raw-socket tests — `GARBAGE\r\n\r\n` → `HTTP/1.1 400`, then EOF
      (acceptance #10); `GET / HTTP/2.0` → 505 (acceptance #11).
      Done: `_send_closing_error` in `src/server.py`.

## 5. Routing layer (`src/router.py`)

- [x] Route registry keyed by `(method, exact path)`; handler receives request
      object (method, path, query, headers, body) and returns
      (status, headers, body); dynamic routes take precedence over static
      files.
      Verify: unit tests registering routes and dispatching fake requests.
      Done: `Router` in `src/router.py` — `add_route`/`get`/`post` register
      into a `(METHOD, path) -> handler` dict; `dispatch(request)` matches
      `HttpServer`'s handler contract exactly, so `HttpServer(handler=router.dispatch)`
      wires directly. `static_handler` is an injectable
      `callable(method, path) -> (status, headers, body) | None` (default: a
      stub returning `None`, i.e. nothing is static) — same
      dependency-injection pattern as §4, so this module is unit-testable
      before `src/static.py` exists, per a fake static handler in
      `tests/test_router.py`. Dynamic routes are checked before the static
      fallback, giving them precedence at the same path.
- [x] Method handling: `HEAD` dispatches to the `GET` handler and strips the
      body (headers, incl. Content-Length, identical to GET) — this must also
      apply on the static-file fallback path, since acceptance #12 is `HEAD /`
      (a static route); known method on a path that exists with other methods
      → 405 with `Allow` header listing supported methods. Spec interpretation
      (pinned here so build iterations don't waffle): a static path that
      exists is "an existing route" — e.g. `POST /style.css` → 405 with
      `Allow: GET, HEAD`; a non-GET/HEAD/POST-registered path that exists
      nowhere → plain 404.
      Verify: unit tests; acceptance #8 (`DELETE /api/echo` → 405,
      `Allow: POST`) and #12 (HEAD / == GET / minus body).
      Done: `Router.dispatch` maps `HEAD` -> looks up the `GET` handler (or
      static fallback), then strips the body via `_finish` while keeping
      status/headers (incl. Content-Length, since `serialize_response` sees
      the same body length the GET path would have produced) identical to
      GET. The 405-vs-404 decision unions dynamic-route methods at that path
      with `{"GET", "HEAD"}` when a static file exists there; `Allow` is
      ordered via `PREFERRED_METHOD_ORDER` (GET, HEAD, POST, ...) so
      `Allow: GET, HEAD` matches the spec's exact wording. Also handles a
      method that IS GET/HEAD hitting a POST-only path → 405 (not 404),
      which the spec implies but doesn't give an explicit acceptance
      criterion for.
- [x] Handler exception safety: unhandled exception in a handler → 500
      response, connection survives (next request on same connection works).
      Verify: integration test with a deliberately-throwing route.
      Done: `Router.dispatch` does NOT catch handler exceptions — they
      propagate to `src/server.py`'s connection loop, which already wraps
      the `handler(request)` call in try/except → 500 + connection survives
      (built in task 4). `tests/test_router.py`'s
      `test_handler_exception_via_real_server_returns_500_and_survives` wires
      a real `Router` into a real `HttpServer` to prove the composition
      works end to end, not just at the server layer in isolation.

Note for §6/§7 integration: `Router(static_handler=...)` expects a callable
`(method, path) -> (status, headers, body) | None`, always called with
`method="GET"` (existence-checked once per request, only when needed — not
called at all for a path with an exact dynamic-route hit). `src/static.py`'s
serving function should match that shape directly so `src/app.py` can do
`Router(static_handler=static.serve)` with no adapter. `error_page(status,
detail=None)` in `src/response.py` is now public (was `_error_page`) so
`router.py`'s 404/405 bodies reuse the same small HTML template used by the
connection-layer error responses.

## 6. Static file serving (`src/static.py`)

- [x] Serve files from `public/` for unmatched GETs; `/` →
      `public/index.html`; directory path → its `index.html` if present else
      404; missing file → 404 with small HTML error page.
      Verify: unit/integration tests for each case.
      Done: `serve(method, path, root=PUBLIC_DIR)` matches
      `router.py`'s `static_handler` contract exactly — returns
      `(200, headers, body)` or `None`; `None` means "nothing to serve"
      (missing file, directory with no `index.html`, or traversal — see
      below), and `Router` is what turns `None` into the actual 404 (with
      `error_page`), so static.py never builds an error response itself.
      `root` is an injectable param (defaults to the real `public/` dir
      resolved relative to `src/`) so tests use a `tmp_path` fixture instead
      of depending on the real `public/` contents (which §7 populates).
      23 unit/integration tests in `tests/test_static.py`, incl. wiring a
      real `Router(static_handler=...)` against a fake root.
- [x] MIME table: html, css, js, json, txt, png, jpg/jpeg, gif, svg, ico,
      woff2; unknown → `application/octet-stream`. (Hand-rolled dict — do not
      use `mimetypes` to keep provenance obvious, though it is not forbidden.)
      Verify: unit tests per extension.
      Done: `MIME_TYPES` dict + `mime_type(path)` helper in `src/static.py`,
      keyed by lowercased extension; unmapped extensions fall back to
      `DEFAULT_MIME_TYPE = "application/octet-stream"`.
- [x] Path-traversal protection: resolved path must stay inside `public/`;
      reject `..`, percent-encoded traversal (`%2e%2e` — note decoding happens
      before routing), absolute paths → 404.
      Verify: tests with `--path-as-is`-style raw targets `/../CLAUDE.md`,
      `/%2e%2e/CLAUDE.md`, `//etc/passwd` (acceptance #4).
      Done: `_resolve` strips all leading `/` from the (already
      percent-decoded) path before `os.path.join`-ing it onto `root`, so a
      request like `//etc/passwd` becomes `root/etc/passwd` (confined, just
      missing) rather than an absolute filesystem path; `os.path.realpath`
      then collapses any `..` and the result is rejected with `None` unless
      it's `root` itself or starts with `root + os.sep`. `%2e%2e` traversal
      is already turned into literal `..` by `http_parse.percent_decode`
      before this module ever sees the path, so no extra decoding is needed
      here (verified in `tests/test_static.py`).

## 7. Demo app (`src/app.py` + `public/`)

- [ ] Static site: black-and-white `public/index.html` (name, short bio,
      courses, projects link), `public/style.css`, `public/projects.html`
      linked from home; no colors, no images.
      Verify: test asserts 200 + `text/html` on `/` and `/projects.html`,
      page contains the student's name (acceptance #1–2).
- [ ] `GET /api/uptime` → 200 `application/json`
      `{"uptime_seconds": <float>}` measured from server start.
      Verify: test parses JSON, checks numeric ≥ 0 (acceptance #5).
- [ ] `POST /api/echo` → 200 JSON `{"length": <int>, "body": "<text>"}` for
      both Content-Length and chunked request bodies.
      Verify: acceptance #6 (`-d 'hello'` → length 5) and #7 (chunked upload
      → correct decoded length).

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
