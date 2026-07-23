# Spec: live chat over hand-rolled WebSockets (Step 2 — extension)

## Overview

A live chat on the homepage. Visitors see recent messages on page load, post
new ones, and receive others' messages without a reload. Messages persist to
disk and survive a restart, so the page is useful when nobody else is online.
A live count of connected visitors is displayed.

WebSockets are implemented from scratch per RFC 6455 on top of the Step 1
raw-socket server. Same constraints as Step 1 (CLAUDE.md): no websocket or
HTTP libraries in `src/`. `hashlib` and `base64` are used for the handshake.

## Architecture

- `src/websocket.py` — handshake validation + frame codec (pure functions,
  unit-testable without sockets)
- `src/chat.py` — connection registry, broadcast, message store, name issuing
- `src/server.py` — extended so a handler can take over (hijack) a connection
- `src/app.py` — registers the `/ws` upgrade route and `GET /api/messages`
- `public/index.html`, `public/chat.js`, `public/style.css` — chat UI

## Functional requirements

### 1. Upgrade handshake

- `GET /ws` with a valid upgrade request → `101 Switching Protocols` with
  `Upgrade: websocket`, `Connection: Upgrade`, and `Sec-WebSocket-Accept` =
  base64(SHA-1(`Sec-WebSocket-Key` + `258EAFA5-E914-47DA-95CA-C5AB0DC85B11`)).
- The handshake fails with `400 Bad Request` when: `Upgrade` is absent or not
  `websocket` (case-insensitive); `Connection` does not contain `upgrade`;
  `Sec-WebSocket-Key` is absent or is not 16 bytes when base64-decoded;
  `Sec-WebSocket-Version` is not `13` (respond `426 Upgrade Required` with
  `Sec-WebSocket-Version: 13`); the method is not `GET`.
- A failed handshake leaves the connection in normal HTTP mode (keep-alive
  rules unchanged); a successful one hands the socket to the chat layer and
  removes it from the HTTP request loop.

### 2. Frame codec

- Decode: FIN, RSV1-3, opcode, MASK bit, and all three payload-length forms
  (7-bit, 7+16-bit, 7+64-bit). Client-to-server frames MUST be masked; unmask
  with the 4-byte key. An unmasked client frame → close with status 1002.
- Encode: server-to-client frames are unmasked, with the same three length
  forms selected by payload size.
- Opcodes: text (0x1), binary (0x2), close (0x8), ping (0x9), pong (0xA).
  Binary messages are rejected with close status 1003 (this app is text-only).
- Fragmentation: continuation frames (opcode 0x0) are reassembled; a control
  frame may be interleaved between fragments and must be handled immediately.
  Control frames are never fragmented and carry ≤ 125 bytes — violations →
  close status 1002.
- Reserved bits set, unknown opcodes, or a payload length above the message
  limit (see §6) → close status 1002 / 1009 respectively.
- Text payloads must be valid UTF-8 → otherwise close status 1007.

### 3. Connection lifecycle

- Close handshake: on receiving a close frame, echo a close frame and close
  the socket. On shutdown, the server sends close status 1001.
- Ping/pong: the server pings idle connections every 20 s; a connection with
  no pong (or any frame) for 60 s is dropped. Client pings are answered with a
  pong carrying the same payload.
- Abrupt disconnect (TCP reset, browser tab closed, network loss): the read
  raises or returns 0 bytes; the connection is removed from the registry, its
  socket closed, and the visitor count updated. No traceback reaches the log
  as an error, and no other connection is affected.
- Slow client: each connection has a bounded outbound queue (64 messages). A
  client whose queue is full is dropped with close status 1008 rather than
  blocking the broadcaster. Broadcast never blocks on a single socket.

### 4. Chat protocol (JSON payloads inside text frames)

Client → server:
- `{"type": "post", "text": "<message>"}`

Server → client:
- `{"type": "welcome", "name": "<name>", "messages": [<recent messages>],
   "visitors": <int>}` — sent immediately after the handshake
- `{"type": "message", "name": "<name>", "text": "<text>", "ts": <epoch>}`
- `{"type": "visitors", "count": <int>}` — on every join and leave
- `{"type": "error", "reason": "<rate_limited|too_long|bad_request>"}`

Malformed JSON or an unknown `type` → an `error` frame; the connection stays
open.

### 5. Identity and persistence

- Each visitor is anonymous and is assigned a name of the form
  `<word><word><number>`, e.g. `quietfalcon42` — two words from word lists in
  the source plus a 2-digit number.
- The name persists across visits and restarts: `GET /` issues a
  `Set-Cookie: chatname=<name>; Path=/; Max-Age=31536000; SameSite=Lax` when
  the request has no valid `chatname` cookie. The handshake reads the name
  from the `Cookie` header. This requires `Cookie` header parsing and
  `Set-Cookie` emission, neither of which existed in Step 1.
- A cookie value that does not match `^[a-z]+[0-9]{2}$` (≤ 32 chars) is
  ignored and a fresh name is issued. Names are display-only and are never
  used for authorization; spoofing is out of scope (§Out of scope).
- Messages persist to `data/messages.jsonl`, one JSON object per line
  (`name`, `text`, `ts`), appended under a lock and flushed before the write
  is acknowledged. On startup the file is read and the last 100 messages are
  loaded; the file is truncated to the last 100 on startup so it cannot grow
  without bound. A missing or partially-written final line is skipped rather
  than crashing startup.
- `GET /api/messages` returns the same recent messages as JSON, so the page
  shows history even if the WebSocket connection fails.

### 6. Limits and safety

- Message text: ≤ 500 characters after decoding; longer → `error` frame with
  `too_long`, message not stored. Frames whose payload exceeds 64 KiB → close
  status 1009.
- Rate limit: 5 messages per 10 seconds per connection. Over the limit →
  `error` frame with `rate_limited`; the message is not stored or broadcast.
  The connection stays open.
- Maximum 128 concurrent WebSocket connections; beyond that the handshake is
  refused with `503 Service Unavailable`.
- XSS: message text is stored raw and escaped at render time in the browser
  by assigning to `textContent`, never `innerHTML`. The server additionally
  rejects nothing on content grounds — escaping is the renderer's job — but
  `GET /api/messages` sets `Content-Type: application/json` and
  `X-Content-Type-Options: nosniff` so the payload is never interpreted as
  HTML.

### 7. Thread safety

- The connection registry is guarded by a lock; iteration for broadcast
  happens over a snapshot so a disconnect during broadcast cannot corrupt it.
- Each connection has its own write lock so frames from concurrent broadcasts
  cannot interleave on one socket.
- The message store is guarded by a lock covering both the in-memory list and
  the file append.

## Acceptance criteria (pytest-testable)

Frame codec and handshake (unit, no sockets):

1. `Sec-WebSocket-Accept` matches the RFC 6455 §1.3 worked example: key
   `dGhlIHNhbXBsZSBub25jZQ==` → `s3pPLMBiTxaQ9kYGzzhZRbK+xOo=`.
2. Handshake rejection cases each produce the specified status: missing
   `Upgrade` → 400, bad version → 426 with `Sec-WebSocket-Version: 13`,
   short/absent key → 400, `POST /ws` → 400.
3. Decoding a masked client text frame recovers the original payload;
   round-trip encode/decode holds for payloads of 0, 125, 126, 65535, and
   65536 bytes (exercising all three length forms).
4. An unmasked client frame → close 1002; invalid UTF-8 text → close 1007;
   a 200-byte control frame → close 1002; a reserved bit set → close 1002.
5. A message split into three fragments (text + continuation + FIN
   continuation) reassembles, including a ping interleaved between fragments.

End-to-end (a pytest WebSocket client built on raw sockets, driving a real
server on an ephemeral port):

6. Handshake succeeds against `/ws` and the first frame received is a
   `welcome` carrying a `<word><word><number>` name, a message list, and a
   visitor count.
7. Two connected clients: a `post` from client A is delivered to client B as a
   `message` frame with A's name, within 1 second.
8. Visitor count: with two clients connected both observe `visitors` = 2;
   after one disconnects the other receives `visitors` = 1.
9. Abrupt disconnect: client A's socket is closed without a close frame;
   client B still receives subsequent messages and an updated count, and the
   server keeps serving normal HTTP requests.
10. Persistence: a posted message is present in `GET /api/messages`, and after
    the server is stopped and restarted a new client's `welcome` still
    contains it.
11. Rate limit: 6 posts in under 10 seconds → the 6th returns an `error` frame
    with `rate_limited` and is absent from `GET /api/messages`.
12. Length limit: a 501-character post → `error` with `too_long` and is not
    stored; a 500-character post succeeds.
13. Name persistence: `GET /` returns a `Set-Cookie: chatname=...`; replaying
    that cookie on a later handshake yields the same name in `welcome`, while
    a request with no cookie yields a different name.
14. XSS: a message containing `<script>alert(1)</script>` round-trips through
    the store and `GET /api/messages` as literal text (the JSON response
    contains the escaped-as-data string, and `public/chat.js` uses
    `textContent`, asserted by a source check for the absence of `innerHTML`).
15. Step 1 is unbroken: the full Step 1 acceptance suite still passes, and the
    forbidden-import guard test still passes with `src/websocket.py` and
    `src/chat.py` present.

## Out of scope

- `permessage-deflate` and any other extension negotiation
  (`Sec-WebSocket-Extensions` is ignored), and subprotocol negotiation
- TLS (`wss://`), HTTP/2, and the WebSocket-over-HTTP/2 bootstrap
- Authenticated identity, moderation, message deletion, and name spoofing
  prevention — names are display-only
- Private messages, rooms/channels, typing indicators, read receipts
- Message history beyond the last 100, search, and pagination
- Horizontal scaling: the registry and store are in-process, single-node
