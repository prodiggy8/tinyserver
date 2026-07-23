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

### 2.1 WebSocket handshake + frame codec (`src/websocket.py` — pure functions, unit-testable without sockets)

- [ ] `Sec-WebSocket-Accept` computation (SHA-1 of key + GUID
      `258EAFA5-E914-47DA-95CA-C5AB0DC85B11`, base64-encoded). Verify against
      the RFC 6455 §1.3 worked example: `dGhlIHNhbXBsZSBub25jZQ==` →
      `s3pPLMBiTxaQ9kYGzzhZRbK+xOo=` (acceptance #1).
- [ ] Handshake request validation against a parsed `Request`: method must be
      `GET`; `Upgrade` must contain `websocket` (case-insensitive) else 400;
      `Connection` must contain `upgrade` (case-insensitive token) else 400;
      `Sec-WebSocket-Key` must be present and base64-decode to exactly 16
      bytes else 400; `Sec-WebSocket-Version` must be `13` else `426` (with
      response header `Sec-WebSocket-Version: 13`). Return either "accept"
      (with the computed header value) or a `(status, headers, body)`-style
      rejection so app.py can hand it straight to the router (acceptance #2).
      Note for 2.5: the success-path `101` response must itself carry all
      three headers — `Upgrade: websocket`, `Connection: Upgrade`, and
      `Sec-WebSocket-Accept: <computed>` (spec §1) — not just the accept
      header alone.
- [ ] Frame decoding: FIN, RSV1-3, opcode, MASK bit, and all three
      payload-length forms (7-bit, 7+16-bit, 7+64-bit); unmask client
      payloads with the 4-byte key. Round-trip encode/decode at payload
      sizes 0, 125, 126, 65535, 65536 bytes, exercising each length form
      (acceptance #3).
- [ ] Frame encoding: unmasked server→client frames, selecting the same
      three length forms by payload size.
- [ ] Frame/protocol validation, each distinguishable so `chat.py` can map it
      to the right close status: unmasked client frame → 1002; a control
      frame (opcode 0x8/0x9/0xA) with payload > 125 bytes → 1002; a reserved
      bit set → 1002; an unknown opcode → 1002; payload above the 64 KiB
      limit (§6) → 1009; opcode 0x2 (binary) → 1003 (text-only app); text
      payload that is not valid UTF-8 → 1007 (acceptance #4).
- [ ] Fragmentation reassembly: continuation frames (opcode 0x0) accumulate
      until a FIN frame completes the message; a control frame may be
      interleaved between fragments and must be surfaced/handled immediately
      without disturbing reassembly state; control frames are never
      fragmented (acceptance #5).

### 2.2 Connection hijacking (`src/server.py`, `src/router.py`)

- [ ] Give the connection-loop handler a way to take over the raw socket:
      expose the socket AND the live `BufferedReader` on `Request`
      (`Request` is defined in `src/server.py:34`), and introduce a sentinel
      result that `_handle_connection` recognizes to mean "the handler
      already owns this socket" — skip response serialization/send and
      return from the loop *without* closing the socket. `Router.dispatch`
      must check for the sentinel BEFORE `status, headers, body =
      route_handler(request)` unpacks the result (`src/router.py:74`) and
      return it unchanged, bypassing `_finish`. Cover with a unit test: a
      stub handler that hijacks and writes raw bytes directly, asserting the
      HTTP loop sends nothing extra and does not close the socket out from
      under the handler.
      Review note — three things the hijack must hand over, each a real bug
      if missed:
      (a) *The existing reader, not a new one.* `_handle_connection` creates
      one `BufferedReader` per connection (`src/server.py:73`) and it may
      already hold bytes past the request (`has_buffered_data()`). A browser
      can send its first WebSocket frame in the same segment as the
      handshake, so constructing a fresh reader in `chat.py` would silently
      drop that frame. Pass the same reader through.
      (b) *Reset the socket timeout.* The loop sets
      `sock.settimeout(idle_timeout)` (5 s, `src/server.py:76`) for HTTP
      keep-alive. Inherited unchanged, every idle WebSocket read would raise
      `socket.timeout` after 5 s. The chat layer must set its own timeout
      (the pong deadline) immediately after hijacking.
      (c) *Ownership of closing.* Once hijacked, the HTTP loop must not
      close the socket; the chat reader thread closes it when its loop ends.
      Assert this in the unit test.
- [ ] Add reason phrases for `101 Switching Protocols`, `426 Upgrade
      Required`, and `503 Service Unavailable` to `response.py`'s
      `REASON_PHRASES` (needed for the handshake's success/failure paths and
      the connection-cap rejection in 2.4).

### 2.3 Cookie parsing + Set-Cookie emission

- [ ] Parse the `Cookie` request header into a name→value dict (hand-rolled
      split on `; ` / `=`, no library) — expose via a small helper reusable
      by both the `/` route (2.5) and the `/ws` handshake (2.4/2.5).
- [ ] Helper to build a `Set-Cookie: chatname=<name>; Path=/;
      Max-Age=31536000; SameSite=Lax` header value.
- [ ] Cookie validation: a `chatname` cookie value must match
      `^[a-z]+[0-9]{2}$` and be ≤ 32 chars, else treat as absent and issue a
      fresh name (acceptance #13).

### 2.4 Chat layer (`src/chat.py`)

- [ ] Name generation: `<word><word><number>` from small hardcoded word
      lists plus a 2-digit number (e.g. `quietfalcon42`), matching
      `^[a-z]+[0-9]{2}$`.
- [ ] Message store: append-only `data/messages.jsonl` (one JSON object per
      line: `name`, `text`, `ts`) guarded by a lock covering both the
      in-memory list and the file append, flushed before the write is
      acknowledged; on startup, load the last 100 messages, tolerating two
      distinct cases without crashing: the file not existing yet (fresh
      clone/first run) and the file existing but its final line being
      partially written (crash mid-append, so only that last line is
      skipped, the rest load normally); truncate the file to the last 100
      lines on startup so it cannot grow unbounded. Create `data/` on
      startup if it doesn't exist (a fresh clone has no `data/` dir yet —
      add `data/.gitkeep` too).
      Review note: add `data/*.jsonl` to `.gitignore`. Without it the store
      gets committed, a fresh clone starts with whoever's messages were
      pushed, and acceptance #10 (persistence across restart) passes for the
      wrong reason.
- [ ] Per-connection send path: a bounded outbound queue (64 messages) and a
      dedicated writer thread that drains it and is the only thread writing
      frames to that socket.
      Review note: the queue needs a consumer, and the writer thread IS that
      consumer — this was the plan's central concurrency gap. A write lock
      alone does not satisfy spec §3's "broadcast never blocks on a single
      socket": a slow client's `sendall` blocks while holding the lock, so
      the broadcaster stalls on the very connection the bound was meant to
      isolate. With a single writer thread per connection, `send()` is just
      a non-blocking `put` and frame interleaving is impossible by
      construction (spec §7 satisfied without a lock on the normal path).
      A `put` onto a full queue drops that connection with close status 1008.
      Verify: a test whose fake socket blocks on `sendall` — a broadcast to
      two connections still reaches the healthy one promptly, and the stalled
      one is dropped with 1008 once its queue fills.
- [ ] Connection registry: thread-safe add/remove keyed by connection;
      broadcast takes a snapshot under the lock, releases it, then enqueues
      (never holds the registry lock across socket I/O or a blocking put);
      unregister is idempotent — the reader thread, writer thread, and ping
      scheduler may each try to drop the same connection, and only the first
      one broadcasts the updated visitor count.
      Verify: concurrent disconnect during a broadcast leaves the registry
      consistent; unregistering the same connection twice broadcasts one
      count update, not two.
- [ ] Connection cap: refuse the handshake with `503` once 128 connections
      are registered (checked before committing to the 101 response/hijack
      in 2.5). The cap must be injectable so the test does not need to open
      128 sockets.
      Verify: with the cap set to 2, a third handshake gets `503` and normal
      HTTP keep-alive is unaffected on that connection. (Spec §6 requirement
      with no numbered criterion.)
- [ ] Rate limiting: 5 messages per 10 seconds per connection; over the
      limit → `{"type":"error","reason":"rate_limited"}`, message not stored
      or broadcast, connection stays open (acceptance #11).
- [ ] Length limit: message text > 500 characters after decoding →
      `{"type":"error","reason":"too_long"}`, not stored; exactly 500
      succeeds (acceptance #12).
- [ ] Chat message protocol dispatch: `{"type":"post","text":...}` handling;
      malformed JSON or an unknown `type` → `{"type":"error",
      "reason":"bad_request"}`, connection stays open. Server→client push
      frames per spec §4: `{"type":"message","name":...,"text":...,
      "ts":...}` on broadcast, `{"type":"visitors","count":...}` on
      join/leave.
      Verify: a text frame containing invalid JSON, and one with
      `{"type":"nonsense"}`, each get a `bad_request` error frame back and
      the connection stays usable for a subsequent successful post. (Spec §4
      requirement with no numbered criterion.)
- [ ] Ping/pong lifecycle: one process-wide scheduler thread pings idle
      connections every 20 s (by enqueuing, never writing directly); a
      connection with no pong or other frame for 60 s is dropped;
      client-initiated pings are answered with a pong carrying the same
      payload.
      Review note: the 20 s/60 s intervals MUST be injectable, exactly as
      Step 1 made the HTTP idle timeout injectable — otherwise this is
      untestable in pytest without a 60-second sleep.
      Verify: with intervals shortened to ~0.05 s/0.15 s, a client that
      never pongs is dropped, and one that does pong survives; a client ping
      gets a pong with the same payload back. (Spec §3 requirement with no
      numbered acceptance criterion.)
- [ ] Close handshake: on receiving a close frame, echo a close frame then
      close the socket; on server shutdown, send close status 1001 to all
      registered connections and join the writer threads.
      Verify: a client sending a close frame receives one back and the
      socket closes; `HttpServer.stop()` with a connection open delivers a
      1001 close frame. (Spec §3 requirement with no numbered criterion.)
- [ ] Abrupt-disconnect handling: a read that raises or returns 0 bytes
      removes the connection from the registry, closes its socket, and
      broadcasts an updated visitor count — no traceback reaches the log as
      an error, and no other connection is affected (acceptance #9).

### 2.5 App wiring (`src/app.py`)

- [ ] Register `GET /ws`: runs 2.1's handshake validation (reading the
      `chatname` cookie per 2.3, checking 2.4's connection cap) — on success,
      sends the 101 response directly on the socket, registers the
      connection (starting its writer thread), sends the `welcome` frame
      (`name`, recent `messages`, `visitors` count), then runs the frame
      read loop on this same thread until close/EOF/error, unregisters, and
      closes the socket. It returns the 2.2 sentinel so the HTTP loop never
      touches the connection again. On failure it returns a normal
      `(400/426/503, headers, body)` tuple through the router, leaving HTTP
      keep-alive semantics untouched.
      Review note: the read loop running on the hijacking thread is what
      makes the connection live — the earlier wording ("hijacks into the
      registry") left no thread reading frames, so a registered connection
      would receive broadcasts but never process an incoming post, and the
      socket would leak.
- [ ] Register `GET /api/messages`: returns the same recent messages as
      JSON with `Content-Type: application/json` and
      `X-Content-Type-Options: nosniff`.
- [ ] `GET /` gains `Set-Cookie` issuance when the request has no valid
      `chatname` cookie (2.3) — needs a dynamic route (or a thin wrapper
      around `static.serve`) since the current `/` response comes from the
      static-file layer, which has no cookie awareness.

### 2.6 Chat UI (`public/`)

- [ ] `public/chat.js`: opens the `/ws` connection, renders `welcome`/
      `message`/`visitors`/`error` frames, sends `post` frames from a form.
      Renders all user-supplied text via `textContent`, never `innerHTML`
      (acceptance #14 includes a source-check for the absence of
      `innerHTML`).
- [ ] `public/index.html` + `style.css`: add a chat section (message list,
      post input, live visitor count) consistent with the existing
      black-and-white design; load `chat.js`.

### 2.7 Tests

Review note on sequencing: `PROMPT_build.md` requires `./script/test` to pass
before any item is checked off, so the unit tests below are written WITH the
section they cover (2.1's tests land with 2.1, 2.4's with 2.4), not saved for
a final pass. They are listed together here only so the coverage is auditable
in one place. The genuinely last-to-arrive item is the end-to-end acceptance
module, which needs 2.1–2.6 in place.

- [ ] `tests/test_websocket.py`: unit tests for all of 2.1 — accept-header
      worked example, each handshake rejection case (400s + 426 with the
      version header), frame round-trips at all 5 sizes, each close-code
      error case (unmasked/oversized-control/reserved-bit/unknown-opcode/
      oversized-payload/binary/invalid-utf8), and fragmentation with an
      interleaved ping (acceptance #1-5).
- [ ] `tests/test_chat.py`: unit tests for 2.3/2.4 — name format, cookie
      validation regex, rate limiting, length limit, store truncation/reload
      including both a missing file (first run) and a corrupted/partial
      final line, and broadcast snapshotting under a concurrent disconnect.
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
