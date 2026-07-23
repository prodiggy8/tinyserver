# IMPLEMENTATION_PLAN.md — Step 1: raw-socket HTTP/1.1 server + demo app

Status: greenfield. Re-verified 2026-07-22 (later pass) by full file listing:
still **no** `src/`, `tests/`, or `public/` directory and no Python files
anywhere; an **empty** `script/` directory now exists (harmless — task 1 fills
it). Spec: `specs/http-server.md` (single spec file; complete for Step 1 — no
missing specs to author). Tasks are ordered by priority/dependency; each is one
sitting and verifiable via automated tests run by `./script/test`.

Notes for future iterations:
- Step 2 (AJAX comment section + SSE/long-polling) is explicitly out of scope
  for Step 1 and has no spec yet — it gets its own specify stage later. Do NOT
  build it or author its spec as part of this plan.
- Concurrency choice (spec allows either): thread-per-connection — simplest
  correct option; revisit only if a task below forces it.
- Tests may use `http.client`/`urllib`/raw sockets as clients; `src/` must
  never import HTTP modules (see CLAUDE.md hard constraints).

## 1. Scaffolding

- [ ] Create `script/test` (runs pytest over `tests/`, exits nonzero on
      failure) and `script/server` (runs `python3 src/server.py`), both
      executable; create `src/`, `tests/`, `public/` dirs with a trivial
      smoke test so `./script/test` passes on a fresh clone.
      Verify: `./script/test` exits 0; `./script/test` exits nonzero when a
      failing test is present.
- [ ] Add a guard test that scans `src/*.py` imports and fails if any
      forbidden module (http, http.server, http.client, socketserver,
      urllib.request, wsgiref) is imported; also fail on the substring
      `asyncio.start_server` anywhere in src (PROMPT_build.md forbids
      high-level asyncio HTTP/server helpers even though `selectors` is fine).
      Verify: test passes on clean src; fails if `import http` is added.

## 2. Request parsing (`src/http_parse.py` — pure functions, unit-testable without sockets)

- [ ] Request-line parsing: `METHOD SP target SP HTTP-version CRLF`; malformed
      → 400 error; version `HTTP/1.1`/`HTTP/1.0` accepted, others → 505;
      request line > 8 KiB → 414.
      Verify: unit tests for valid lines, missing parts, bad version, long line.
- [ ] Header parsing: case-insensitive names, up to empty CRLF line; line
      without a colon → 400; total header block > 32 KiB → 431.
      Verify: unit tests incl. case-insensitive lookup and oversized block.
- [ ] Path handling: percent-decode the request path; split off and expose the
      query string (raw).
      Verify: unit tests for `/a%20b?x=1&y=2` → path `/a b`, query `x=1&y=2`.
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
      always sets `Content-Length`, `Date` (RFC 7231 IMF-fixdate, computed
      without HTTP libs — `time`/`email.utils` ok), and `Server` headers;
      helpers for common statuses (200, 400, 404, 405, 413, 414, 431, 500,
      505) with a small HTML error page body for errors.
      Verify: unit tests parse serialized bytes and check exact framing.

## 4. Connection handling (`src/server.py`)

- [ ] Socket listener: bind `127.0.0.1:8080` by default; port from `PORT` env
      var or `--port` flag (flag wins); `SO_REUSEADDR`; thread-per-connection
      dispatch; clean shutdown on KeyboardInterrupt.
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
