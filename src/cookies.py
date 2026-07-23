"""Cookie parsing + Set-Cookie emission (pure functions, unit-testable
without sockets). Hand-rolled per specs/message-wall.md — no http.cookies
or other library.
"""

import re

CHATNAME_RE = re.compile(r"^[a-z]+[0-9]{2}$")
MAX_CHATNAME_LEN = 32


def parse_cookie_header(header_value):
    """Parse a `Cookie` request header value into a name->value dict.

    Splits pairs on ";" and each pair on the first "=". Malformed
    fragments (no "=", empty name) are skipped rather than raising — a
    garbled Cookie header should not break the request.
    """
    cookies = {}
    if not header_value:
        return cookies
    for part in header_value.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        value = value.strip()
        if not name:
            continue
        cookies[name] = value
    return cookies


def is_valid_chatname(value):
    """True if `value` is a well-formed chatname cookie value: matches
    ^[a-z]+[0-9]{2}$ and is at most MAX_CHATNAME_LEN characters."""
    if value is None or len(value) > MAX_CHATNAME_LEN:
        return False
    return bool(CHATNAME_RE.match(value))


def build_set_cookie(name, value, path="/", max_age=31536000, same_site="Lax"):
    """Build a Set-Cookie header value, e.g.
    'chatname=quietfalcon42; Path=/; Max-Age=31536000; SameSite=Lax'."""
    return "{}={}; Path={}; Max-Age={}; SameSite={}".format(
        name, value, path, max_age, same_site
    )
