"""Static file serving from public/.

`serve(method, path)` matches src/router.py's injectable `static_handler`
contract exactly: returns `(status, headers, body)` for a servable file, or
`None` if there is nothing to serve at `path` (missing file, directory with
no `index.html`, or a path that would resolve outside `root`). Returning
`None` rather than building a 404 here keeps error-page construction in one
place — `Router` already turns `None` into a 404 (or folds it into a 405
`Allow` computation) using the same `error_page` helper the connection
layer uses.
"""

import os

PUBLIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "public"
)

MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css",
    ".js": "application/javascript",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}
DEFAULT_MIME_TYPE = "application/octet-stream"


def mime_type(path):
    _, ext = os.path.splitext(path)
    return MIME_TYPES.get(ext.lower(), DEFAULT_MIME_TYPE)


def _resolve(path, root):
    """Resolve a decoded, URL-absolute `path` to a filesystem path inside
    `root`. Returns None if it would escape `root` — this is what rejects
    `..`, percent-encoded traversal (already decoded by the time it gets
    here — see http_parse.percent_decode), and absolute-looking paths like
    `//etc/passwd` (its leading slashes are stripped, so it resolves as
    `root/etc/passwd`, which stays inside `root`)."""
    root = os.path.realpath(root)
    relative = path.lstrip("/")
    candidate = os.path.realpath(os.path.join(root, relative))
    if candidate != root and not candidate.startswith(root + os.sep):
        return None
    return candidate


def serve(method, path, root=PUBLIC_DIR):
    resolved = _resolve(path, root)
    if resolved is None:
        return None

    if os.path.isdir(resolved):
        resolved = os.path.join(resolved, "index.html")

    if not os.path.isfile(resolved):
        return None

    try:
        with open(resolved, "rb") as f:
            body = f.read()
    except OSError:
        return None

    return 200, [("Content-Type", mime_type(resolved))], body
