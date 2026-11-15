#!/usr/bin/env python3
"""toolgate: the in-page login in front of Kyle's tools on the VPS.

nginx asks this service before serving any page (auth_request):

    /_gate/check/admin   204 if the session cookie belongs to an admin login
    /_gate/check/team    204 for a team or admin login
                         401 no valid session (nginx then sends the browser to
                         the login page), 403 signed in but not allowed

and sends browsers here for the pages people see:

    /_gate/start         nginx's 401 handler: redirect to the login page,
                         remembering where the visitor was going
    /_gate/login         the sign-in form (GET) and its submission (POST)
    /_gate/logout        sign out on this device
    /_gate/denied        nginx's 403 handler

Logins come from the existing htpasswd files, so nobody needs a new password:
the admin file (kyle) opens everything, the team file (veit) opens what nginx
marks "team". Changing or removing a password in those files ends that
login's sessions on every device, because each session carries a fingerprint
of the password hash it was issued for.

Standard library only; runs on the VPS's Python 3.8.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import secrets
import sys
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, urlsplit

# --------------------------------------------------------------------------
# Settings (environment, set in toolgate.service)

ADMIN_FILE = os.environ.get("GATE_ADMIN_FILE", "/etc/nginx/.admin-tools-htpasswd")
TEAM_FILE = os.environ.get("GATE_TEAM_FILE", "/etc/nginx/.veit-tools-htpasswd")
KEY_FILE = os.environ.get("GATE_KEY_FILE", "/etc/toolgate/secret")
MAIN_HOST = os.environ.get("GATE_MAIN_HOST", "107-173-7-177.sslip.io")
# Hosts a login may send people back to: the main host and its subdomains.
COOKIE_DOMAIN = os.environ.get("GATE_COOKIE_DOMAIN", MAIN_HOST)
LISTEN = os.environ.get("GATE_LISTEN", "127.0.0.1:8600")
INSECURE_COOKIE = os.environ.get("GATE_INSECURE_COOKIE") == "1"  # tests only

COOKIE = "kt_session"
LONG_SESSION = 30 * 24 * 3600   # "Keep me signed in"
SHORT_SESSION = 12 * 3600       # otherwise (and the cookie ends with the browser)

# Brute-force brakes: per client address and per login name.
FAIL_WINDOW = 15 * 60
MAX_FAILS_PER_IP = 5
MAX_FAILS_PER_USER = 20

ROLE_RANK = {"team": 1, "admin": 2}

# --------------------------------------------------------------------------
# Password hashes


_APR_ALPHABET = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _to64(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_APR_ALPHABET[value & 0x3F])
        value >>= 6
    return "".join(out)


def md5_crypt(password: bytes, salt: bytes, magic: bytes) -> str:
    """MD5-crypt, as used by htpasswd's $apr1$ (and glibc's $1$)."""
    salt = salt[:8]
    ctx = hashlib.md5(password + magic + salt)
    alt = hashlib.md5(password + salt + password).digest()
    plen = len(password)
    while plen > 0:
        ctx.update(alt[: min(16, plen)])
        plen -= 16
    i = len(password)
    while i:
        ctx.update(b"\x00" if i & 1 else password[:1])
        i >>= 1
    final = ctx.digest()
    for r in range(1000):
        c = hashlib.md5()
        c.update(password if r & 1 else final)
        if r % 3:
            c.update(salt)
        if r % 7:
            c.update(password)
        c.update(final if r & 1 else password)
        final = c.digest()
    f = final
    encoded = (
        _to64((f[0] << 16) | (f[6] << 8) | f[12], 4)
        + _to64((f[1] << 16) | (f[7] << 8) | f[13], 4)
        + _to64((f[2] << 16) | (f[8] << 8) | f[14], 4)
        + _to64((f[3] << 16) | (f[9] << 8) | f[15], 4)
        + _to64((f[4] << 16) | (f[10] << 8) | f[5], 4)
        + _to64(f[11], 2)
    )
    return magic.decode() + salt.decode() + "$" + encoded


def verify_password(password: str, stored: str) -> bool:
    pw = password.encode("utf-8")
    if stored.startswith("$apr1$") or stored.startswith("$1$"):
        magic = b"$apr1$" if stored.startswith("$apr1$") else b"$1$"
        salt = stored[len(magic):].split("$", 1)[0].encode()
        return hmac.compare_digest(md5_crypt(pw, salt, magic), stored)
    if stored.startswith(("$2y$", "$2b$", "$2a$")):
        try:
            import bcrypt  # optional: only needed for htpasswd -B
        except ImportError:
            log("bcrypt hash found but the bcrypt module is not installed")
            return False
        return bcrypt.checkpw(pw, stored.replace("$2y$", "$2b$", 1).encode())
    if stored.startswith("{SHA}"):
        digest = base64.b64encode(hashlib.sha1(pw).digest()).decode()
        return hmac.compare_digest("{SHA}" + digest, stored)
    log("unsupported hash format in a password file")
    return False


# --------------------------------------------------------------------------
# Users (re-read whenever a password file changes)


class Users:
    def __init__(self, admin_file: str, team_file: str):
        self.files = [(admin_file, "admin"), (team_file, "team")]
        self._lock = threading.Lock()
        self._stamp: Tuple = ()
        self._users: Dict[str, Tuple[str, str]] = {}

    def _load(self) -> Dict[str, Tuple[str, str]]:
        stamp = []
        for path, _ in self.files:
            try:
                st = os.stat(path)
                stamp.append((path, st.st_mtime_ns, st.st_size))
            except OSError:
                stamp.append((path, None, None))
        stamp_t = tuple(stamp)
        with self._lock:
            if stamp_t == self._stamp:
                return self._users
            users: Dict[str, Tuple[str, str]] = {}
            # Team first, so a name in both files ends up admin.
            for path, role in reversed(self.files):
                try:
                    with open(path, encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line or line.startswith("#") or ":" not in line:
                                continue
                            name, hashed = line.split(":", 1)
                            users[name.lower()] = (hashed, role)
                except OSError as ex:
                    log("cannot read %s: %s" % (path, ex))
            self._users, self._stamp = users, stamp_t
            return users

    def get(self, name: str) -> Optional[Tuple[str, str]]:
        return self._load().get(name)


def fingerprint(hashed: str) -> str:
    return hashlib.sha256(hashed.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Session tokens: base64(json) "." base64(hmac)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Sessions:
    def __init__(self, key: bytes, users: Users):
        self.key = key
        self.users = users

    def issue(self, name: str, hashed: str, lifetime: int) -> str:
        body = _b64(json.dumps(
            {"u": name, "f": fingerprint(hashed), "exp": int(time.time()) + lifetime},
            separators=(",", ":")).encode())
        sig = _b64(hmac.new(self.key, body.encode(), hashlib.sha256).digest())
        return body + "." + sig

    def check(self, token: str) -> Optional[Tuple[str, str]]:
        """Return (name, role) for a valid token, else None."""
        try:
            body, sig = token.split(".", 1)
            good = _b64(hmac.new(self.key, body.encode(), hashlib.sha256).digest())
            if not hmac.compare_digest(sig, good):
                return None
            data = json.loads(_unb64(body))
            if int(data["exp"]) < time.time():
                return None
            entry = self.users.get(str(data["u"]))
            if not entry or not hmac.compare_digest(fingerprint(entry[0]), str(data["f"])):
                return None
            return str(data["u"]), entry[1]
        except Exception:
            return None


# --------------------------------------------------------------------------
# Failed-login brakes


class Brakes:
    def __init__(self):
        self._lock = threading.Lock()
        self._fails: Dict[str, List[float]] = {}

    def _recent(self, key: str, now: float) -> List[float]:
        items = [t for t in self._fails.get(key, []) if now - t < FAIL_WINDOW]
        if items:
            self._fails[key] = items
        else:
            self._fails.pop(key, None)
        return items

    def blocked(self, ip: str, name: str) -> int:
        """Seconds until another try is allowed (0 = allowed now)."""
        now = time.time()
        with self._lock:
            waits = []
            for key, limit in (("ip:" + ip, MAX_FAILS_PER_IP), ("user:" + name, MAX_FAILS_PER_USER)):
                items = self._recent(key, now)
                if len(items) >= limit:
                    waits.append(int(FAIL_WINDOW - (now - items[-limit])) + 1)
            return max(waits) if waits else 0

    def fail(self, ip: str, name: str) -> None:
        now = time.time()
        with self._lock:
            for key in ("ip:" + ip, "user:" + name):
                self._fails.setdefault(key, []).append(now)

    def clear(self, ip: str) -> None:
        with self._lock:
            self._fails.pop("ip:" + ip, None)


# --------------------------------------------------------------------------
# Pages

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{title}</title>
<style>
  :root {{ --bg:#f6f5f1; --card:#fff; --text:#1d1d1b; --muted:#6b6a64; --line:#e3e1da;
          --accent:#2f6f4e; --on-accent:#fff; --err:#a3341f; --err-soft:#f8e3de; }}
  @media (prefers-color-scheme: dark) {{
    :root {{ --bg:#161614; --card:#20201d; --text:#ecebe6; --muted:#a09e96; --line:#33332f;
            --accent:#7cc49c; --on-accent:#10241a; --err:#f0a08e; --err-soft:#3b211b; }}
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:var(--bg);
         color:var(--text); font:16px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; padding:24px 16px; }}
  .card {{ width:100%; max-width:380px; background:var(--card); border:1px solid var(--line);
          border-radius:14px; padding:28px 24px; }}
  h1 {{ font-size:1.35rem; margin:0 0 4px; }}
  p.sub {{ color:var(--muted); margin:0 0 20px; font-size:.95rem; }}
  label {{ display:block; font-size:.9rem; font-weight:600; margin:14px 0 6px; }}
  input[type=text], input[type=password] {{ width:100%; padding:11px 12px; font:inherit; color:inherit;
          background:var(--bg); border:1px solid var(--line); border-radius:8px; }}
  input:focus {{ outline:2px solid var(--accent); outline-offset:1px; }}
  .keep {{ display:flex; gap:8px; align-items:center; font-weight:400; margin-top:16px; }}
  button, a.btn {{ display:block; width:100%; margin-top:20px; padding:12px; font:inherit; font-weight:600;
          text-align:center; text-decoration:none; color:var(--on-accent); background:var(--accent);
          border:0; border-radius:8px; cursor:pointer; }}
  .err {{ background:var(--err-soft); color:var(--err); padding:10px 12px; border-radius:8px;
         font-size:.92rem; margin-bottom:6px; }}
  .alt {{ margin-top:14px; text-align:center; font-size:.9rem; }}
  .alt a {{ color:var(--muted); }}
</style>
</head>
<body>
<main class="card">
{body}
</main>
</body>
</html>
"""


def login_page(next_url: str, error: str = "", name: str = "") -> str:
    err = '<p class="err" role="alert">%s</p>' % html.escape(error) if error else ""
    body = """
  <h1>Sign in</h1>
  <p class="sub">Kyle's tools</p>
  {err}
  <form method="post" action="/_gate/login">
    <input type="hidden" name="next" value="{next}">
    <label for="u">Login</label>
    <input id="u" name="username" type="text" autocomplete="username" autocapitalize="none"
           spellcheck="false" required value="{name}" {uf}>
    <label for="p">Password</label>
    <input id="p" name="password" type="password" autocomplete="current-password" required {pf}>
    <label class="keep"><input type="checkbox" name="keep" value="1" checked> Keep me signed in on this device</label>
    <button type="submit">Sign in</button>
  </form>""".format(err=err, next=html.escape(next_url), name=html.escape(name),
                    uf="" if name else "autofocus", pf="autofocus" if name else "")
    return PAGE.format(title="Sign in", body=body)


def denied_page(name: str) -> str:
    body = """
  <h1>Not available</h1>
  <p class="sub">You're signed in as <strong>{name}</strong>, and this login can't open this page.</p>
  <a class="btn" href="https://{host}/">Go to the converter</a>
  <p class="alt"><a href="/_gate/logout">Sign in with a different login</a></p>""".format(
        name=html.escape(name), host=html.escape(MAIN_HOST))
    return PAGE.format(title="Not available", body=body)


# --------------------------------------------------------------------------
# HTTP


def log(msg: str) -> None:
    sys.stderr.write("toolgate: %s\n" % msg)
    sys.stderr.flush()


def safe_next(url: str) -> str:
    """Only send people back to our own hosts over https; else the main page."""
    default = "https://%s/" % MAIN_HOST
    if not url:
        return default
    try:
        parts = urlsplit(url)
    except ValueError:
        return default
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
        return default
    if host != COOKIE_DOMAIN and not host.endswith("." + COOKIE_DOMAIN):
        return default
    if parts.path.startswith("/_gate/"):
        return default
    return url


class Handler(BaseHTTPRequestHandler):
    server_version = "toolgate"
    sys_version = ""
    sessions: Sessions
    users: Users
    brakes: Brakes

    # quieter, single-line logs without query strings
    def log_message(self, fmt, *args):  # noqa: N802
        pass

    def _client_ip(self) -> str:
        return self.headers.get("X-Real-IP") or self.client_address[0]

    def _session(self) -> Optional[Tuple[str, str]]:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = cookies.SimpleCookie()
        try:
            jar.load(raw)
        except cookies.CookieError:
            return None
        morsel = jar.get(COOKIE)
        return self.sessions.check(morsel.value) if morsel else None

    def _send(self, code: int, body: str = "", headers: Optional[Dict[str, str]] = None,
              cookie: Optional[str] = None) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        if body:
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def _cookie(self, value: str, max_age: Optional[int]) -> str:
        parts = ["%s=%s" % (COOKIE, value), "Domain=%s" % COOKIE_DOMAIN, "Path=/",
                 "HttpOnly", "SameSite=Lax"]
        if not INSECURE_COOKIE:
            parts.append("Secure")
        if max_age is not None:
            parts.append("Max-Age=%d" % max_age)
        return "; ".join(parts)

    def _redirect(self, url: str, cookie: Optional[str] = None) -> None:
        self._send(302, "", {"Location": url}, cookie)

    # ---- routes

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        query = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}

        if path in ("/_gate/check/admin", "/_gate/check/team"):
            need = path.rsplit("/", 1)[1]
            who = self._session()
            if not who:
                return self._send(401)
            if ROLE_RANK[who[1]] < ROLE_RANK[need]:
                return self._send(403)
            return self._send(204, "", {"X-Gate-User": who[0]})

        if path == "/_gate/start":
            target = safe_next(self.headers.get("X-Original-URL", ""))
            return self._redirect("https://%s/_gate/login?next=%s" % (MAIN_HOST, quote(target, safe="")))

        if path == "/_gate/login":
            next_url = safe_next(query.get("next", [""])[0])
            who = self._session()
            if who:
                return self._redirect(next_url)
            return self._send(200, login_page(next_url))

        if path == "/_gate/logout":
            return self._redirect("https://%s/_gate/login" % MAIN_HOST, self._cookie("", 0))

        if path == "/_gate/denied":
            who = self._session()
            return self._send(403, denied_page(who[0] if who else "?"))

        return self._send(404, "Not found")

    def do_POST(self):  # noqa: N802
        if self.path.split("?", 1)[0] != "/_gate/login":
            return self._send(404, "Not found")
        origin = self.headers.get("Origin")
        if origin and origin not in ("https://" + MAIN_HOST, "null") and not INSECURE_COOKIE:
            return self._send(403, "Bad origin")
        try:
            length = min(int(self.headers.get("Content-Length") or 0), 8192)
        except ValueError:
            length = 0
        form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
        # Login names are lowercase in the password files; phones often capitalise.
        name = form.get("username", [""])[0].strip().lower()
        password = form.get("password", [""])[0]
        keep = form.get("keep", [""])[0] == "1"
        next_url = safe_next(form.get("next", [""])[0])
        ip = self._client_ip()

        wait = self.brakes.blocked(ip, name)
        if wait:
            log("blocked login attempt for %r from %s" % (name, ip))
            mins = max(1, (wait + 59) // 60)
            return self._send(429, login_page(
                next_url, "Too many wrong tries. Try again in %d minute%s." % (mins, "" if mins == 1 else "s"),
                name))

        entry = self.users.get(name)
        # Check a dummy hash for unknown names so both paths take similar time.
        ok = verify_password(password, entry[0] if entry else "$apr1$aaaaaaaa$0000000000000000000000")
        if not (entry and ok):
            self.brakes.fail(ip, name)
            log("failed login for %r from %s" % (name, ip))
            time.sleep(0.7)
            return self._send(401, login_page(next_url, "That login and password don't match.", name))

        self.brakes.clear(ip)
        lifetime = LONG_SESSION if keep else SHORT_SESSION
        token = self.sessions.issue(name, entry[0], lifetime)
        log("signed in %r (%s) from %s" % (name, entry[1], ip))
        return self._redirect(next_url, self._cookie(token, lifetime if keep else None))


def load_key(path: str) -> bytes:
    with open(path, "rb") as fh:
        key = fh.read().strip()
    if len(key) < 32:
        raise SystemExit("toolgate: secret in %s is too short" % path)
    return key


def make_server(key: bytes, admin_file: str, team_file: str, listen: str) -> ThreadingHTTPServer:
    users = Users(admin_file, team_file)
    Handler.users = users
    Handler.sessions = Sessions(key, users)
    Handler.brakes = Brakes()
    host, port = listen.rsplit(":", 1)
    server = ThreadingHTTPServer((host, int(port)), Handler)
    server.daemon_threads = True
    return server


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "new-secret":
        print(secrets.token_urlsafe(48))
        return
    server = make_server(load_key(KEY_FILE), ADMIN_FILE, TEAM_FILE, LISTEN)
    log("listening on %s" % LISTEN)
    server.serve_forever()


if __name__ == "__main__":
    main()
