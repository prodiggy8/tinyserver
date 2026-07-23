# CLAUDE.md — operational reference (keep this file operational-only and brief)

## Project

An HTTP/1.1 server written in Python 3.12 on raw sockets only, serving a small
demo web app. Specifications live in `specs/`. Task list lives in
`IMPLEMENTATION_PLAN.md`.

## Layout

- `src/` — server + app source (Python, stdlib only, NO HTTP libraries)
- `tests/` — test suite (tests may use http.client/urllib as clients)
- `specs/` — specifications the implementation must satisfy
- `script/` — runnable commands (`script/test`, `script/server`)

## Commands

- Run tests: `./script/test`
- Run server: `./script/server` (serves on http://localhost:8080)

## Hard constraints

- FORBIDDEN in `src/`: http, http.server, http.client, socketserver,
  urllib.request, wsgiref, any third-party HTTP/web library.
- Allowed in `src/`: socket, selectors, threading, struct, hashlib, base64,
  json, os, sys, pathlib, and other non-HTTP stdlib modules.
- No third-party dependencies for the server; pytest is allowed for tests.
