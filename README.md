# tinyserver

An HTTP/1.1 server built from raw TCP sockets in Python (no HTTP libraries),
with a small demo web app on top. Built for 17-636 using Claude Code driven by
a Ralph Wiggum loop (adapted from the loop shown in class:
https://github.com/gwincr11/ralph-wiggum-tutorial).

- **Step 1 (base):** raw-socket HTTP/1.1 server — request parsing, keep-alive,
  static files, routing — serving a demo app.
- **Step 2 (extension):** a live message wall — WebSockets (RFC 6455)
  implemented by hand, with messages persisted so they outlive the session.

Each step goes through four stages: **specify → plan (looped) → review →
build (looped)**, with a commit at every stage boundary.

See `running.md` for how to run it, `prompts.txt` for every prompt used, and
`reflection.md` for the writeup.

## The loop

`./loop.sh` runs Claude Code headless (`claude -p`) on a fixed prompt file
until the agent outputs a completion promise or an iteration cap is hit:

```bash
./loop.sh -m plan -n 3    # planning mode: maintains IMPLEMENTATION_PLAN.md
./loop.sh -m build        # build mode: implements plan items, tests, commits
```
