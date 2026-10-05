"""Authentication for Otto's HTTP API (issue #217).

`_csrf_ok` is a BROWSER guard: an Origin-less request is allowed, and an Origin is just a header.
Every run's shell has the network, so without this a run could `curl localhost:<port>` to approve
its own gate, release the global pause, or read the stores the deny-set masks on disk.

The credential is a per-install token in `data/.api-token` (0600), which `file_safety` denies to
EVERY run, Otto-cwd included. A client presents it as the `X-Otto-Token` header (scripts) or as a
cookie (the browser). The browser never sees the token in a URL: `./run.sh login` spends the token
on `POST /api/login-link` for a single-use code, and `GET /login?code=` swaps that for an HttpOnly
cookie — so browser history holds only a spent code.

Run as a script, this module prints that login link.
"""
import hmac
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.request

import config

HEADER = "X-Otto-Token"
COOKIE_PREFIX = "otto_token_"
LOGIN_CODE_TTL_S = 300
COOKIE_MAX_AGE_S = 365 * 24 * 3600

_NAME = ".api-token"
_PATH = None            # lazily resolved, like estop._PATH, so the suite can re-point it
_CACHE = {}             # path -> (mtime_ns, token)
_CODES = {}             # single-use login code -> expiry (monotonic)
_LOCK = threading.Lock()


def path():
    return _PATH or os.path.join(config.DATA_DIR, _NAME)


def _read(p):
    try:
        with open(p) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def _create(p):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tok = secrets.token_urlsafe(32)
    tmp = f"{p}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(tok + "\n")
    try:
        os.link(tmp, p)             # never replaces: a concurrent creator's token wins
    except FileExistsError:
        if not _read(p):            # an empty leftover holds no token — overwrite it
            os.replace(tmp, p)
            return tok
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return _read(p)


def token():
    """The install's token, created on first use. Re-read when the file changes, so deleting it
    rotates the token without a restart (every browser then logs in again)."""
    p = path()
    with _LOCK:
        try:
            mtime = os.stat(p).st_mtime_ns
        except FileNotFoundError:
            mtime = None
        hit = _CACHE.get(p)
        if hit and mtime is not None and hit[0] == mtime:
            return hit[1]
        tok = _read(p) or _create(p)
        _CACHE[p] = (os.stat(p).st_mtime_ns, tok)
        return tok


def cookie_name(port):
    """Cookies are not port-scoped, so the port is in the name: two Otto instances on one host
    (a scratch server beside the live one) would otherwise overwrite each other's login."""
    return f"{COOKIE_PREFIX}{port}"


def presented(headers, port):
    """The credential a request carries: the header first, else this instance's cookie."""
    h = (headers.get(HEADER) or "").strip()
    if h:
        return h
    want = cookie_name(port)
    for part in (headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == want:
            return v.strip()
    return ""


def valid(value):
    return bool(value) and hmac.compare_digest(value.encode(), token().encode())


def mint_login_code():
    code = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _LOCK:
        for c in [c for c, exp in _CODES.items() if exp < now]:
            del _CODES[c]
        _CODES[code] = now + LOGIN_CODE_TTL_S
    return code


def redeem_login_code(code):
    with _LOCK:
        exp = _CODES.pop(code or "", None)
    return exp is not None and exp >= time.monotonic()


def set_cookie_header(port):
    return (f"{cookie_name(port)}={token()}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={COOKIE_MAX_AGE_S}")


def login_urls(start=None, span=40, timeout=2):
    """Find the serving Otto and ask it for a one-time login link. The server walks
    PORT..PORT+39 when one is busy, so the port a link needs is whichever accepts our token."""
    start = start or int(os.environ.get("PORT", "8765"))
    tok = token()
    for port in range(start, start + span):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/login-link", method="POST", data=b"{}",
            headers={"Content-Type": "application/json", HEADER: tok})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                urls = json.loads(r.read() or b"{}").get("urls")
        except (OSError, ValueError):
            continue
        if urls:
            return urls
    return None


if __name__ == "__main__":
    urls = login_urls()
    if not urls:
        sys.exit("No running Otto accepted this install's token — is ./run.sh up?")
    print(f"Open ONE of these within {LOGIN_CODE_TTL_S // 60} min (single use):")
    print("\n".join(urls))
