# tinyserver

An HTTP/1.1 server built from raw TCP sockets in Python (no HTTP libraries),
with a small demo web app on top. Built for 17-636 using Claude Code driven by
a Ralph Wiggum loop (adapted from the loop shown in class:
https://github.com/gwincr11/ralph-wiggum-tutorial).

- **Step 1 (base):** raw-socket HTTP/1.1 server — request parsing, keep-alive,
  static files, routing — serving a demo app.
- **Step 2 (extension):** a live message wall — WebSockets (RFC 6455)
  implemented by hand, with messages persisted so they outlive the session.
