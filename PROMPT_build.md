0a. Study `specs/*` to learn the application specifications.
0b. Study @IMPLEMENTATION_PLAN.md.
0c. The application source code is in `src/*` and tests are in `tests/*`.

1. Your task is to implement functionality per the specifications. Follow @IMPLEMENTATION_PLAN.md and choose the most important unchecked item to address. Before making changes, search the codebase — do not assume something is not implemented.
2. After implementing the item, run the full test suite with `./script/test`. Only mark the item complete in @IMPLEMENTATION_PLAN.md if all tests pass. If `./script/test` does not exist yet, create it (it should run the whole test suite and exit nonzero on failure).
3. When you discover issues or learn something that affects future work, immediately update @IMPLEMENTATION_PLAN.md with your findings. When an issue is resolved, remove the item.
4. When the tests pass, update @IMPLEMENTATION_PLAN.md, then `git add -A` and `git commit` with a message describing the changes.

Rules:
- HARD CONSTRAINT: the server must use only raw sockets. The modules http, http.server, http.client, socketserver, urllib.request, wsgiref, asyncio.start_server high-level HTTP helpers, and any third-party HTTP library are FORBIDDEN in src/. Allowed: socket, selectors, threading, os, sys, pathlib, json, struct, hashlib, base64, and other non-HTTP stdlib modules. Tests MAY use http.client/urllib to act as a client against the server.
- Implement functionality completely. Placeholders and stubs waste effort and time redoing the same work.
- If tests unrelated to your current work fail, resolve them as part of the increment.
- Keep @running.md current: it must explain, for a grader on a fresh clone, exactly how to start the server, use the app, and run the tests.
- Keep @CLAUDE.md operational only (how to run/test things) — status updates and progress notes belong in IMPLEMENTATION_PLAN.md. A bloated CLAUDE.md pollutes every future loop's context.
- When @IMPLEMENTATION_PLAN.md becomes large, periodically clean out completed items.
- If you find inconsistencies in specs/*, update the specs and note the change in IMPLEMENTATION_PLAN.md.
- IMPORTANT: once the full implementation is complete, every plan item is checked off, and `./script/test` passes, output `<promise>DONE</promise>`
