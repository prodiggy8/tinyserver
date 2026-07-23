# IMPLEMENTATION_PLAN.md

## Step 1 — raw-socket HTTP/1.1 server + demo app: COMPLETE (121/121 green)

`specs/http-server.md`'s 13 acceptance criteria are green.

- [x] Regression fixed 2026-07-23: two "nitpick" commits (`56dd848`,
      `28eb5f4`) renamed the homepage from the placeholder to "Gustavo
      Grancieiro" without updating the tests that assert on it, so
      `tests/test_acceptance.py` (`STUDENT_NAME`) and `tests/test_app.py`
      failed. Updated both tests to the real name and fixed the stale
      placeholder still in `public/projects.html`'s `<title>`. The rendered
      name now lives in exactly three places: the two HTML pages and
      `STUDENT_NAME` — update all three together if it changes again.

Architecture: `http_parse.py`/`response.py` are pure functions; `server.py`'s
`HttpServer` takes an injectable handler (default: lazily-imported
`app.router.dispatch`); `router.py`'s `Router` takes an injectable
`static_handler` (default: `static.serve`); `app.py` wires it all together
for `script/server`. Full historical checklist elided here — see git history
(`step1: *` commits) if detail is needed; nothing else in this section is
unchecked.

Notes still relevant going forward:
- Concurrency model: thread-per-connection (`server.py`).
- `pytest` is not available system-wide; always run tests via `./script/test`
  (auto-provisions `.venv` on first run).
- `src/` may only import: socket, selectors, threading, struct, hashlib,
  base64, json, os, sys, pathlib, and other non-HTTP stdlib (CLAUDE.md).
  `tests/test_no_forbidden_imports.py` globs `src/*.py`, so it automatically
  covers new Step 2 files with no changes needed.

## Step 2 — live chat over hand-rolled WebSockets: NOT STARTED

Reviewed 2026-07-23 against `specs/message-wall.md` (authored this session,
commit `bc68a13`). Confirmed by source search: no `src/websocket.py`, no
`src/chat.py`, no cookie parsing/`Set-Cookie` emission, no connection-hijack
path in `server.py`/`router.py`, no `/ws` or `/api/messages` routes, no
`public/chat.js`, no `data/` directory. Every item below is new work. Ordered
bottom-up by dependency (pure codec first, then plumbing, then the stateful
chat layer, then wiring, then UI, then end-to-end tests) — build in this
order.

Coverage: all 15 acceptance criteria in `specs/message-wall.md` map to a task
below. Four spec requirements have no numbered criterion of their own — the
close handshake and ping/pong (§3), the 128-connection cap (§6), and the
`bad_request` error frame (§4) — so tests for them are called out explicitly
in the sections below.

**Threading model (pinned by review — read before building 2.4/2.5).** Per
WebSocket connection there are exactly two threads plus one process-wide one:

1. *Reader*: the thread that accepted the HTTP request and performed the
   hijack. It does NOT return to the HTTP loop; it runs the frame read loop
   until close/EOF/error, then unregisters the connection and closes the
   socket. Nothing else may read from the socket.
2. *Writer*: one thread per connection draining that connection's bounded
   outbound queue. It is the ONLY thread that writes frames to the socket,
   which is what satisfies spec §7's "frames must not interleave" — a plain
   lock is not enough, see 2.4.
3. *Ping scheduler*: one process-wide thread that enqueues pings onto every
   connection's queue. It never touches a socket directly.

Invariants: broadcast only enqueues, never does socket I/O; the registry lock
is never held across socket I/O or across a queue put that could block;
unregistering is idempotent (reader, writer, and ping scheduler can all
decide to drop the same connection) and only the first unregister broadcasts
the updated visitor count.

### 2.1 WebSocket handshake + frame codec (`src/websocket.py` — pure functions, unit-testable without sockets): DONE

Implemented `src/websocket.py` and `tests/test_websocket.py` (25 tests, all
green). Design notes for consumers in 2.2+:

- `validate_handshake(request)` returns the computed accept value on
  success; raises `HandshakeError(status, headers)` on failure (400 or 426
  with `Sec-WebSocket-Version: 13`) — 2.5's `/ws` handler catches this and
  builds the `(status, headers, body)` tuple for the router.
- `read_frame(reader)` takes anything with `read_exact(n)`
  (`http_parse.BufferedReader` qualifies) — returns a `Frame` or `None` on
  EOF, raises `ProtocolError(close_code)` for structural violations
  (unmasked/reserved-bit/unknown-opcode → 1002, oversized control → 1002,
  payload > 64 KiB → 1009).
- `FragmentAssembler().feed(frame)` reassembles continuation frames,
  returns the decoded `str` when a text message completes, ignores control
  frames (caller checks `frame.opcode` before calling `feed` to branch on
  control vs. data), and raises `ProtocolError(1003)` for binary opcodes /
  `ProtocolError(1007)` for invalid UTF-8 on completion.
- `encode_frame(opcode, payload, fin, mask_key=None)` — server call sites
  omit `mask_key` (unmasked); `encode_close(code, reason)` is a thin
  wrapper for the close frame's 2-byte status-code payload, ready for 2.4.

- [x] `Sec-WebSocket-Accept` computation — verified against the RFC 6455
      §1.3 worked example (acceptance #1).
- [x] Handshake request validation against a parsed `Request` (acceptance #2).
- [x] Frame decoding: all three payload-length forms; round-trip at 0, 125,
      126, 65535, 65536 bytes (acceptance #3).
- [x] Frame encoding: unmasked server→client frames.
- [x] Frame/protocol validation with distinguishable close codes (acceptance #4).
- [x] Fragmentation reassembly with interleaved control frame (acceptance #5).

### 2.2 Connection hijacking (`src/server.py`, `src/router.py`): DONE

Implemented. `server.HIJACKED` is a module-level sentinel object; `Request`
(`src/server.py:34`) now carries `sock` and `reader` (both default `None` so
existing 6-positional-arg `Request(...)` call sites in tests keep working).
`_handle_connection` passes the SAME `BufferedReader` it built for the
connection (not a new one) into `Request.reader`, checks `result is HIJACKED`
before unpacking the handler's return value, and — when hijacked — returns
without closing the socket (a `hijacked` flag guards the `finally` block's
`sock.close()`). `Router.dispatch` (`src/router.py:74`) checks for the
sentinel before `status, headers, body = route_handler(request)` and returns
it unchanged, bypassing `_finish`. `router.py` imports `HIJACKED` from
`server.py`; verified no import cycle (`server.py` only imports `app` lazily
inside a function body).

Reason phrases for 101/426/503 added to `response.py`'s `REASON_PHRASES`.

Tests: `tests/test_server.py` (`test_hijack_sends_nothing_extra_and_leaves_socket_open_for_handler`,
`test_hijack_receives_the_same_reader_with_already_buffered_bytes`) and
`tests/test_router.py` (`test_dispatch_passes_hijacked_sentinel_through_without_unpacking`).
149/149 green.

### 2.3 Cookie parsing + Set-Cookie emission: DONE

Implemented `src/cookies.py` (pure functions, no `http.cookies`) +
`tests/test_cookies.py` (10 tests, all green). `parse_cookie_header(value)`
splits on `;` then the first `=`, skipping malformed fragments rather than
raising. `is_valid_chatname(value)` checks `^[a-z]+[0-9]{2}$` and ≤ 32 chars
— 2.5's `/` and `/ws` handlers call this to decide "reuse cookie name" vs.
"issue a fresh one" (acceptance #13); actual name *generation* is 2.4's job,
this module only validates. `build_set_cookie(name, value, path="/",
max_age=31536000, same_site="Lax")` returns the header value string ready
to pair with `"Set-Cookie"` in a router handler's headers list. 159/159
green.

### 2.4 Chat layer (`src/chat.py`): DONE

Implemented `src/chat.py` + `tests/test_chat.py` (23 tests, all green,
173/173 for the full suite). `data/.gitkeep` added, `data/*.jsonl` added to
`.gitignore` (store is never committed).

Threading model as pinned above, realized concretely:
- `Connection.enqueue` is a non-blocking `queue.Queue(maxsize=64)` put;
  `Connection.start_writer` is the dedicated writer thread — the only
  thread that calls `sock.sendall`.
- `ConnectionRegistry.drop(conn, code, reason)` is the single force-
  disconnect path (used by a full-queue broadcast, ping timeout, and
  server shutdown alike): best-effort enqueues a close frame, calls the
  idempotent `unregister` (returns `True` only for the call that actually
  removes it, which is also the only call that broadcasts the updated
  visitor count), and — only on removal — calls `sock.shutdown(SHUT_RDWR)`,
  never `sock.close()`. Shutting down (not closing) from a non-reader
  thread is what safely unblocks the reader thread's blocking `recv()`
  without an fd-reuse race; the reader thread is still the only one that
  calls `sock.close()`, in `serve_connection`'s `finally` block, matching
  the pinned invariant exactly.
- `serve_connection(sock, reader, conn, store, registry, rate_limiter)` is
  the full per-connection frame loop (2.5's `/ws` handler runs this on the
  hijacking thread after sending the 101 response, registering, and
  enqueuing the welcome frame). It owns unregister + writer-thread-join +
  socket-close in its `finally`, so 2.5's caller doesn't need to.
  `except OSError: break` around `read_frame` is what makes abrupt
  disconnect silent (no traceback) per acceptance #9.
- `PingScheduler(registry, interval, timeout)` — both injectable, per the
  review note; `.tick()` is exposed separately from `.start()`/`.stop()` so
  tests can drive it deterministically instead of racing a real thread.
- `RateLimiter` is a small per-connection sliding-window object (not a
  method on `Connection`) so `handle_message` can be unit-tested with a
  plain object exposing just `.name` and `.enqueue`.
- `MessageStore(path, max_messages)` loads + truncates to last N on
  construction (tolerating a missing file and a corrupt/partial final line
  by skipping any line that fails `json.loads`, not just the last one —
  simpler than special-casing "only the last line" and equally safe), then
  every `append` does file write + `flush` + `fsync` under the same lock
  guarding the in-memory list.

Not yet wired to anything: 2.5 (`app.py`) still needs to construct a
`MessageStore`, `ConnectionRegistry`, and `PingScheduler`, start the ping
scheduler, and call `serve_connection` from the `/ws` route handler.
`HttpServer.stop()` (`src/server.py`) doesn't yet call
`registry.shutdown_all()` — that wiring also belongs to 2.5.

### 2.5 App wiring (`src/app.py`): DONE

Implemented `router.get("/", index_handler)`, `router.get("/api/messages",
messages_handler)`, `router.get("/ws", ws_handler)`. 173/173 green; manually
verified end-to-end (welcome/self-join/other-join/message ordering, XSS
round-trip through `/api/messages`, abrupt disconnect) with an ad-hoc raw-
socket client before writing the real acceptance suite (2.7).

Review found and fixed two race/ordering bugs not caught by the existing
2.4 unit tests (which use a bare `ConnectionRegistry` and never send an
actual 101 response first):

- **Writer-thread-races-the-101-response.** `chat.ConnectionRegistry.register`
  starts the connection's writer thread, which becomes the only thread
  allowed to write to the socket. Calling `register` *before* sending the
  raw 101 response (the originally-drafted order) lets the writer thread
  send frame bytes onto the wire before the HTTP 101 status line — a real
  race, worse under concurrent load (another client's broadcast could land
  in this connection's queue in that window). Fixed by making `register`'s
  contract explicit: caller must finish writing any raw bytes to the socket
  first. Since the capacity check (`register`'s atomic None-return) can no
  longer gate whether to send 101 at all, added `ConnectionRegistry.
  at_capacity()` as an explicit pre-check (racy by design, documented on
  the method — acceptable for a single-node demo; `register`'s atomic
  check still catches the rare miss and the caller closes without a body).
- **Welcome-must-be-first race.** The original plan had the caller
  `conn.enqueue(welcome_frame(...))` *after* `register()` returned, but
  `register()` already broadcasts the join `visitors` count to every
  connection including the new one as soon as it's inserted — so the
  self-join broadcast could beat the welcome frame into the queue (worse,
  a concurrent broadcast from an unrelated connection could too). Fixed by
  giving `register` a `build_initial_frames(conn, count)` callback invoked
  *while the registry lock is still held* (so no other thread can see this
  connection yet), guaranteeing anything it returns — the welcome frame —
  is queued before any broadcast can reach this connection. `count` passed
  to the callback already includes this connection, computed race-free
  under the same lock as the insert.

`src/server.py`'s `HttpServer` gained an injectable `shutdown_hook`
(called once from `close()`, guarded by the same `self._sock is not None`
check so it fires exactly once); `main()` now imports `app` eagerly and
passes `handler=app.router.dispatch, shutdown_hook=app.shutdown` so a real
server run drops all chat connections with close status 1001 on Ctrl-C.
Tests that construct `HttpServer` directly without a `shutdown_hook` are
unaffected (default `None`, no-op).

### 2.6 Chat UI (`public/`): DONE

Implemented `public/chat.js`: fetches `/api/messages` immediately (history
shows even if the WebSocket fails), then opens `/ws` and renders `welcome`
(name + visitors + message list), `message` (appended), `visitors` (count
update), and `error` (transient message, cleared after 4s) frames. Posts go
out as `{"type": "post", "text": ...}` on form submit. All server-supplied
text is rendered via `createElement`/`textContent`/`createTextNode` — no
`innerHTML` anywhere in the file (satisfies acceptance #14's source check).

`public/index.html` gained a `#chat` section (status line with name +
visitor count, message list, post form, error line) styled in `style.css`
consistent with the existing black-and-white design; `chat.js` is loaded at
the end of `<body>`.

Manually verified end-to-end against a running server (raw-socket Python
client, since no browser is available in this environment): handshake →
welcome frame (empty history on fresh store) → posted
`<script>alert(1)</script>` → broadcast `message` frame → `GET
/api/messages` shows it as literal text, matching acceptance #6, #14.

### 2.7 Tests

Review note on sequencing: `PROMPT_build.md` requires `./script/test` to pass
before any item is checked off, so the unit tests below are written WITH the
section they cover (2.1's tests land with 2.1, 2.4's with 2.4), not saved for
a final pass. They are listed together here only so the coverage is auditable
in one place. The genuinely last-to-arrive item is the end-to-end acceptance
module, which needs 2.1–2.6 in place.

- [x] `tests/test_websocket.py`: unit tests for all of 2.1 — accept-header
      worked example, each handshake rejection case (400s + 426 with the
      version header), frame round-trips at all 5 sizes, each close-code
      error case (unmasked/oversized-control/reserved-bit/unknown-opcode/
      oversized-payload/binary/invalid-utf8), and fragmentation with an
      interleaved ping (acceptance #1-5).
- [x] `tests/test_chat.py`: unit tests for 2.3/2.4 — name format, cookie
      validation regex, rate limiting, length limit, store truncation/reload
      including both a missing file (first run) and a corrupted/partial
      final line, broadcast snapshotting under a concurrent disconnect,
      connection cap, idempotent unregister, queue-full drop, ping
      timeout/pong survival, close handshake, and abrupt disconnect.
- [ ] `tests/test_websocket_acceptance.py`: a small raw-socket WebSocket test
      client (send the handshake, encode/decode frames per 2.1) driving a
      real `HttpServer` on an ephemeral port, covering acceptance #6-14:
      welcome frame contents, two-client message relay within 1s, visitor
      count on join/leave, abrupt disconnect resilience (server keeps
      serving plain HTTP), persistence across a server stop/restart, rate
      limit on a 6th rapid post, 500 vs. 501-char length limit, cookie
      round-trip name persistence, and (#14's data half) a posted message
      containing `<script>alert(1)</script>` round-trips as literal text
      through the store and `GET /api/messages` (no server-side escaping/
      stripping — JSON encoding already makes this safe; the client-side
      half of #14, the `innerHTML`-absence source check, lives in 2.6).
- [ ] Re-run the full Step 1 suite plus the forbidden-import guard alongside
      the new tests to confirm acceptance #15 (nothing in Step 1 breaks; the
      guard test passes with `src/websocket.py`/`src/chat.py` present).

### 2.8 Docs

- [ ] Update `running.md`: how to use the chat UI, and that
      `data/messages.jsonl` is where messages persist (and survives
      restarts).
