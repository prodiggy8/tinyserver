# IMPLEMENTATION_PLAN.md — Step 1: raw-socket HTTP/1.1 server + demo app

Reviewed 2026-07-22 against `specs/http-server.md` (review stage). Coverage
check: all spec sections §1–6 and all 13 acceptance criteria map to at least
one task below. Review changes: pinned hand-rolled percent-decoding and banned
all `urllib.*` in the guard test (§1); pinned locale-independent `Date`
construction (§3); made the request handler injectable so §4 can be tested
before §5 exists (§4 dependency note).

Status: task 1 (scaffolding) done 2026-07-22 — `src/`, `tests/`, `public/`
created, `script/test`/`script/server` in place, guard + smoke tests green.
Next unchecked priority: §2 request parsing (`src/http_parse.py`).

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

- [ ] Request-line parsing: `METHOD SP target SP HTTP-version CRLF`; malformed
      → 400 error; version `HTTP/1.1`/`HTTP/1.0` accepted, others → 505;
      request line > 8 KiB → 414.
      Verify: unit tests for valid lines, missing parts, bad version, long line.
- [ ] Header parsing: case-insensitive names, up to empty CRLF line; line
      without a colon → 400; total header block > 32 KiB → 431.
      Verify: unit tests incl. case-insensitive lookup and oversized block.
- [ ] Path handling: split off the query string (raw, left undecoded) FIRST,
      then percent-decode only the path. Percent-decoding is hand-rolled
      (scan for `%`, parse two hex digits, decode resulting bytes as UTF-8
      with `errors="replace"`); `urllib.parse` is off-limits per the guard
      test. Invalid escapes (`%zz`, truncated `%4`) are left literal rather
      than raising.
      Verify: unit tests for `/a%20b?x=1&y=2` → path `/a b`, query `x=1&y=2`;
      `%2e%2e` → `..` (feeds the traversal tests in §6); invalid escapes pass
      through unchanged; a `%3F` in the path does not create a query split.
- [ ] Content-Length body reading: read exactly N bytes; non-numeric or
      negative Content-Length → 400; decoded body > 1 MiB → 413.
      Verify: unit tests with exact/short/oversized bodies.
- [ ] Chunked transfer decoding: hex chunk-size lines (incl. chunk extensions
      after `;`), chunk data, terminating `0` chunk, ignore trailers; malformed
      framing → 400; decoded total > 1 MiB → 413.
      Verify: unit tests for multi-chunk body, bad hex size, missing CRLF,
      trailers present.
- [ ] Incremental socket reading helper: buffer-based reader that pulls header
      block and body off a socket-like object (handles bytes split across
      recv() calls and pipelined leftover bytes).
      Verify: unit tests with a fake socket delivering data in odd-sized pieces.

## 3. Response construction (`src/response.py`)

- [ ] Response serialization: status line, headers, CRLF endings, body bytes;
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

## 4. Connection handling (`src/server.py`)

Dependency note: §4 is built and tested BEFORE the router (§5) exists, so the
server must take its request handler as an injectable callable (constructor
argument, defaulting to the real app wiring once §5/§7 land). All §4 tests use
a stub handler that returns a fixed 200 — they must not depend on routing,
static files, or the demo app. The same injection point is what makes the
idle-timeout test below practical.

- [ ] Socket listener: bind `127.0.0.1:8080` by default; port from `PORT` env
      var or `--port` flag (flag wins); `SO_REUSEADDR`; thread-per-connection
      dispatch; clean shutdown on KeyboardInterrupt. Handler callable and
      keep-alive timeout are injectable (see dependency note); expose the
      bound port when binding to port 0 so tests can use ephemeral ports.
      Verify: integration test starts server on an ephemeral port, two
      concurrent clients get responses (one slow handler doesn't block the
      other).
- [ ] Keep-alive semantics: HTTP/1.1 keep-alive by default, close on
      `Connection: close`; HTTP/1.0 close by default, keep alive on
      `Connection: keep-alive`; multiple sequential requests served over one
      connection.
      Verify: raw-socket test sends two requests on one connection, gets two
      responses; `Connection: close` request → server closes.
- [ ] Idle timeout: 5 s on keep-alive connections, close silently.
      Verify: test with shortened timeout (make it injectable) — connection
      closes with no bytes sent after idling.
- [ ] Error responses at the connection layer: parse errors map to
      400/413/414/431/505 responses and the connection is closed afterwards.
      Verify: raw-socket tests — `GARBAGE\r\n\r\n` → `HTTP/1.1 400`, then EOF
      (acceptance #10); `GET / HTTP/2.0` → 505 (acceptance #11).

## 5. Routing layer (`src/router.py`)

- [ ] Route registry keyed by `(method, exact path)`; handler receives request
      object (method, path, query, headers, body) and returns
      (status, headers, body); dynamic routes take precedence over static
      files.
      Verify: unit tests registering routes and dispatching fake requests.
- [ ] Method handling: `HEAD` dispatches to the `GET` handler and strips the
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
- [ ] Handler exception safety: unhandled exception in a handler → 500
      response, connection survives (next request on same connection works).
      Verify: integration test with a deliberately-throwing route.

## 6. Static file serving (`src/static.py`)

- [ ] Serve files from `public/` for unmatched GETs; `/` →
      `public/index.html`; directory path → its `index.html` if present else
      404; missing file → 404 with small HTML error page.
      Verify: unit/integration tests for each case.
- [ ] MIME table: html, css, js, json, txt, png, jpg/jpeg, gif, svg, ico,
      woff2; unknown → `application/octet-stream`. (Hand-rolled dict — do not
      use `mimetypes` to keep provenance obvious, though it is not forbidden.)
      Verify: unit tests per extension.
- [ ] Path-traversal protection: resolved path must stay inside `public/`;
      reject `..`, percent-encoded traversal (`%2e%2e` — note decoding happens
      before routing), absolute paths → 404.
      Verify: tests with `--path-as-is`-style raw targets `/../CLAUDE.md`,
      `/%2e%2e/CLAUDE.md`, `//etc/passwd` (acceptance #4).

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
