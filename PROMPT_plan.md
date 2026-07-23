0a. Study `specs/*` to learn the application specifications, using parallel subagents where helpful.
0b. Study @IMPLEMENTATION_PLAN.md (if present) to understand the plan so far.
0c. For reference, the application source code is in `src/*` and tests are in `tests/*`.

1. Study @IMPLEMENTATION_PLAN.md (if present; it may be incorrect) and use parallel subagents to study the existing source code in `src/*` and compare it against `specs/*`. Analyze findings, prioritize tasks, and create/update @IMPLEMENTATION_PLAN.md as a bullet-point checklist sorted by priority of items yet to be implemented. Consider searching for TODOs, minimal implementations, placeholders, skipped/flaky tests, and inconsistent patterns. Every task in the plan must be small (one sitting) and verifiable by an automated test.

IMPORTANT: Plan only. Do NOT implement anything. Do NOT assume functionality is missing; confirm with code search first.

ULTIMATE GOAL: An HTTP/1.1 server written in Python using ONLY raw sockets (no http.server, socketserver, http.client, or any HTTP framework/library), serving a small demo web app end to end, exactly as described in `specs/*`. Consider missing elements and plan accordingly. If an element is missing from the specs, search first to confirm it doesn't exist, then if needed author the specification at specs/FILENAME.md and document the plan to implement it in @IMPLEMENTATION_PLAN.md.
