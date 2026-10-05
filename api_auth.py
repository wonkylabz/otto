"""Authentication for Otto's HTTP API (issue #217).

`_csrf_ok` is a BROWSER guard: an Origin-less request is allowed, and an Origin is just a header.
Every run's shell has the network, so without this a run could `curl localhost:<port>` to approve
its own gate, release the global pause, or read the stores the deny-set masks on disk.

The credential is a per-install token in `data/.api/token` (0600, in a 0700 directory), which
`file_safety` denies to EVERY run, Otto-cwd included. Scripts send it as the `X-Otto-Token` header.

A browser never holds the token: it holds a SESSION — a random id whose sha256 is stored in
`data/.api/sessions.json`, tied to the token that minted it. Cookies are not port-scoped, so every
other localhost app the browser visits receives this cookie; a session is one revocable browser,
the token would be the install. Rotating the token ends every session. A browser gets a session
from a single-use code (`./run.sh login` -> `GET /login?code=`) or by pasting the token once.

Run as a script, this module prints that login link.
"""
import hashlib
import hmac
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

import config
import storage

HEADER = "X-Otto-Token"
COOKIE_PREFIX = "otto_token_"
LOGIN_CODE_TTL_S = 300
COOKIE_MAX_AGE_S = 365 * 24 * 3600

# A DIRECTORY, not a bare file: the sandboxes mask the whole directory, so a token re-created
# mid-run (a rotation) lands somewhere a running sandbox still cannot see. A mask over a bare
# file covers only the dentry it was mounted on.
_DIR = ".api"
_NAME = "token"
_PATH = None            # lazily resolved, like estop._PATH, so the suite can re-point it
_CACHE = {}             # path -> (mtime_ns, token)
_CODES = {}             # single-use login code -> expiry (monotonic)
_SESSIONS = "sessions.json"
_LOCK = threading.Lock()


def path():
    return _PATH or os.path.join(config.DATA_DIR, _DIR, _NAME)


def directory():
    return os.path.dirname(path())


def _read(p):
    try:
        with open(p) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def _create(p):
    d = os.path.dirname(p)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    tok = secrets.token_urlsafe(32)
    # mkstemp: a random name opened O_EXCL, so a planted symlink can't redirect the write.
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".token-")
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


def header_token(headers):
    return (headers.get(HEADER) or "").strip()


def cookie_session(headers, port):
    want = cookie_name(port)
    for part in (headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == want:
            return v.strip()
    return ""


def authed(headers, port):
    """A script's header token, or a browser's live session cookie."""
    h = header_token(headers)
    return valid(h) if h else valid_session(cookie_session(headers, port))


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


def _hash(sid):
    return hashlib.sha256(sid.encode()).hexdigest()


def _token_fp():
    return _hash(token())[:16]


def sessions_path():
    return os.path.join(directory(), _SESSIONS)


def _live(rec, now):
    return rec.get("tok") == _token_fp() and rec.get("created", 0) + COOKIE_MAX_AGE_S > now


def new_session(label):
    """Mint a browser session. Returns the cookie value; only its hash is stored."""
    sid, now = secrets.token_urlsafe(32), time.time()
    rec = {"created": now, "label": (label or "")[:120], "tok": _token_fp()}

    def add(d):
        d = {k: v for k, v in (d or {}).items() if _live(v, now)}   # prune dead ones on write
        d[_hash(sid)] = rec
        return d
    storage.mutate_json(sessions_path(), add, {})
    return sid


def valid_session(sid):
    if not sid:
        return False
    rec = storage.read_json(sessions_path(), {}).get(_hash(sid))
    return bool(rec) and _live(rec, time.time())


def list_sessions(current_sid=""):
    """Live sessions, newest first, keyed by a short public id (a prefix of the HASH, so the
    listing never exposes a cookie)."""
    now, cur = time.time(), _hash(current_sid) if current_sid else ""
    rows = [{"id": h[:12], "created": r["created"], "label": r.get("label", ""),
             "current": h == cur}
            for h, r in storage.read_json(sessions_path(), {}).items() if _live(r, now)]
    return sorted(rows, key=lambda r: -r["created"])


def revoke_session(public_id=None, sid=None):
    """End one session, by its public id (Admin) or its cookie value (logout). True if found."""
    target = _hash(sid)[:12] if sid else (public_id or "")
    if len(target) != 12:
        return False
    found = []

    def drop(d):
        keep = {h: r for h, r in (d or {}).items() if h[:12] != target}
        found.append(len(keep) != len(d or {}))
        return keep if found[0] else storage.UNCHANGED
    storage.mutate_json(sessions_path(), drop, {})
    return found[0]


def set_cookie_header(port, sid, secure=False):
    """`Secure` when the browser reached us over HTTPS (a TLS tunnel), so the cookie never
    rides a plaintext hop. Never on plain localhost: the browser would drop it."""
    return (f"{cookie_name(port)}={sid}; Path=/; HttpOnly; SameSite=Strict; "
            f"Max-Age={COOKIE_MAX_AGE_S}" + ("; Secure" if secure else ""))


def clear_cookie_header(port):
    return f"{cookie_name(port)}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"


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
