# IMPLEMENTATION_PLAN.md

## Step 1 — raw-socket HTTP/1.1 server + demo app: COMPLETE

`specs/http-server.md`'s 13 acceptance criteria are green.

Architecture: `http_parse.py`/`response.py` are pure functions; `server.py`'s
`HttpServer` takes an injectable handler (default: lazily-imported
`app.router.dispatch`); `router.py`'s `Router` takes an injectable
`static_handler` (default: `static.serve`); `app.py` wires it all together
for `script/server`. Full historical checklist elided here — see git history
(`step1: *` commits) if detail is needed.

## Step 2 — live chat over hand-rolled WebSockets: COMPLETE

All 15 acceptance criteria in `specs/message-wall.md` are green (185/185
full suite). Full historical checklist elided here — see git history
(`step2: *` commits) for detail. Summary of what was built:

- `src/websocket.py` — RFC 6455 handshake validation + frame codec (pure
  functions). `tests/test_websocket.py`.
- `src/server.py`/`router.py` — connection hijacking: a handler can return
  the `HIJACKED` sentinel to take over the raw socket (WebSocket upgrade)
  without the HTTP loop closing it.
- `src/cookies.py` — hand-rolled `Cookie` header parsing + `Set-Cookie`
  emission (no `http.cookies`). `tests/test_cookies.py`.
- `src/chat.py` — connection registry, broadcast, message store
  (`data/messages.jsonl`, gitignored, last 100 messages), name issuing,
  rate limiting, ping/pong, the per-connection frame loop. Threading model:
  one reader thread per connection (owns the socket read + close), one
  writer thread per connection (owns all writes, drains a bounded queue),
  one process-wide ping scheduler thread (never touches sockets directly,
  only enqueues via the registry). `tests/test_chat.py`.
- `src/app.py` — `build_router(store, registry)` factory wires `/`,
  `/api/messages`, `/ws` routes; module scope calls it once with the real
  `MessageStore`/`ConnectionRegistry` singletons for `script/server`. The
  factory shape (not building handlers directly at module scope) exists
  so tests can construct isolated store/registry instances instead of
  sharing — and polluting — the real `data/messages.jsonl`.
- `public/chat.js` + `index.html`/`style.css` — chat UI, renders all
  server-supplied text via `textContent`/`createElement` (no `innerHTML`
  anywhere in the file — enforced by a source-check test, so don't
  reintroduce the substring even in a comment).
- `tests/test_websocket_acceptance.py` — end-to-end raw-socket WebSocket
  client (`WsClient`/`connect()` helper) driving a real `HttpServer`,
  covering welcome frame, message relay, visitor counts, abrupt
  disconnect, persistence across a simulated restart (two `build_router`+
  `HttpServer` pairs sharing one tmp_path store file), rate/length limits,
  cookie name persistence, XSS round-trip, close handshake, and the
  `bad_request` error frame.

Non-obvious behavior worth remembering: `ConnectionRegistry.register`
always sends the `welcome` frame immediately followed by a self-join
`visitors` count broadcast — even a lone connection gets two frames on
connect, not one. Any new raw-socket test client must drain both before
treating the next frame as a reply.
