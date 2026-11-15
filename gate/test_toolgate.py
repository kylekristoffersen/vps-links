"""Tests for toolgate: hashes, sessions, roles, brakes and redirects.

Run: python gate/test_toolgate.py   (no network, no real password files)
If openssl is on PATH, the apr1 hashes are also checked against it.
"""

import http.client
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import quote, urlencode

os.environ["GATE_INSECURE_COOKIE"] = "1"
os.environ["GATE_MAIN_HOST"] = "tools.example.test"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import toolgate  # noqa: E402

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


# ---- hashes
known = toolgate.md5_crypt(b"password", b"saltsalt", b"$apr1$")
openssl = shutil.which("openssl")
if openssl:
    out = subprocess.run([openssl, "passwd", "-apr1", "-salt", "saltsalt", "password"],
                         capture_output=True, text=True).stdout.strip()
    check("apr1 matches openssl", out == known)
    out1 = subprocess.run([openssl, "passwd", "-1", "-salt", "abcdefgh", "Tr0ub4dor&3 long passphrase!"],
                          capture_output=True, text=True).stdout.strip()
    check("md5-crypt $1$ matches openssl (long password)",
          out1 == toolgate.md5_crypt("Tr0ub4dor&3 long passphrase!".encode(), b"abcdefgh", b"$1$"))
else:
    print("SKIP  openssl not found")
check("verify right password", toolgate.verify_password("password", known))
check("verify wrong password", not toolgate.verify_password("passw0rd", known))
check("verify {SHA}", toolgate.verify_password("x", "{SHA}EfatjsUqKYSrqv18O1FlA3hcIHI="))
check("unknown format refused", not toolgate.verify_password("x", "plain"))

# ---- safe_next
sn = toolgate.safe_next
check("next: own host kept", sn("https://tools.example.test/crossbuilder/") == "https://tools.example.test/crossbuilder/")
check("next: subdomain kept", sn("https://rss.tools.example.test/x?a=1") == "https://rss.tools.example.test/x?a=1")
for bad in ["https://evil.test/", "http://tools.example.test/", "//evil.test/", "https://tools.example.test.evil.test/",
            "https://user@tools.example.test/", "https://tools.example.test:8443/", "javascript:alert(1)",
            "https://xtools.example.test/", "https://tools.example.test/_gate/logout"]:
    check("next: refused %s" % bad, sn(bad) == "https://tools.example.test/")

# ---- live server
tmp = tempfile.mkdtemp()
admin_f = os.path.join(tmp, "admin")
team_f = os.path.join(tmp, "team")


def write(path, user, pw, salt=b"abcd1234"):
    with open(path, "w") as fh:
        fh.write("%s:%s\n" % (user, toolgate.md5_crypt(pw.encode(), salt, b"$apr1$")))


write(admin_f, "kyle", "admin-pass")
write(team_f, "veit", "team-pass")
server = toolgate.make_server(b"k" * 48, admin_f, team_f, "127.0.0.1:0")
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()


def req(method, path, body=None, cookie=None, headers=None, ip="10.0.0.1"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    h = {"X-Real-IP": ip}
    if cookie:
        h["Cookie"] = "kt_session=" + cookie
    if body is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded"
    h.update(headers or {})
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read().decode()
    return r.status, dict(r.getheaders()), data


def login(user, pw, keep="1", nxt="https://tools.example.test/links/", ip="10.0.0.1"):
    form = {"username": user, "password": pw, "next": nxt}
    if keep:
        form["keep"] = keep
    return req("POST", "/_gate/login", urlencode(form), ip=ip)


def token_of(headers):
    sc = headers.get("Set-Cookie", "")
    return sc.split(";", 1)[0].split("=", 1)[1] if "kt_session=" in sc else ""


st, _, _ = req("GET", "/_gate/check/team")
check("no cookie -> 401", st == 401)
st, _, _ = req("GET", "/_gate/check/admin", cookie="garbage.value")
check("bad cookie -> 401", st == 401)

st, h, _ = req("GET", "/_gate/start", headers={"X-Original-URL": "https://tools.example.test/crossbuilder/?x=1&y=2"})
check("start -> login with encoded next",
      st == 302 and h["Location"] == "https://tools.example.test/_gate/login?next="
      + quote("https://tools.example.test/crossbuilder/?x=1&y=2", safe=""))
st, _, page = req("GET", "/_gate/login?next=" + quote("https://tools.example.test/links/", safe=""))
check("login page shows form with next", st == 200 and 'value="https://tools.example.test/links/"' in page)
st, _, page = req("GET", "/_gate/login?next=" + quote('https://tools.example.test/"><script>', safe=""))
check("login page escapes next", "<script>" not in page)

st, h, _ = login("kyle", "admin-pass")
admin_tok = token_of(h)
check("admin login -> 302 to next", st == 302 and h["Location"] == "https://tools.example.test/links/")
sc = h.get("Set-Cookie", "")
check("cookie is HttpOnly, Lax, domain, 30 days",
      "HttpOnly" in sc and "SameSite=Lax" in sc and "Domain=tools.example.test" in sc and "Max-Age=2592000" in sc)
st, h, _ = req("GET", "/_gate/check/admin", cookie=admin_tok)
check("admin -> admin pages 204", st == 204 and h.get("X-Gate-User") == "kyle")
st, _, _ = req("GET", "/_gate/check/team", cookie=admin_tok)
check("admin -> team pages 204", st == 204)

st, h, _ = login("  Kyle ", "admin-pass")
check("login name ignores capitals and spaces", st == 302 and token_of(h))
st, h, _ = login("veit", "team-pass", keep="")
team_tok = token_of(h)
check("team login without keep -> session cookie", st == 302 and "Max-Age" not in h.get("Set-Cookie", "x"))
st, _, _ = req("GET", "/_gate/check/team", cookie=team_tok)
check("team -> team pages 204", st == 204)
st, _, _ = req("GET", "/_gate/check/admin", cookie=team_tok)
check("team -> admin pages 403", st == 403)
st, _, page = req("GET", "/_gate/denied", cookie=team_tok)
check("denied page names the login", st == 403 and "veit" in page)

st, h, _ = login("kyle", "admin-pass", nxt="https://evil.test/")
check("login refuses outside next", h.get("Location") == "https://tools.example.test/")
st, h, _ = req("GET", "/_gate/login?next=" + quote("https://tools.example.test/crossbuilder/", safe=""), cookie=admin_tok)
check("already signed in -> straight to next", st == 302 and h["Location"].endswith("/crossbuilder/"))

tampered = admin_tok[:-2] + ("AA" if not admin_tok.endswith("AA") else "BB")
st, _, _ = req("GET", "/_gate/check/admin", cookie=tampered)
check("tampered token -> 401", st == 401)

# password change ends sessions
time.sleep(0.01)
write(admin_f, "kyle", "new-admin-pass", salt=b"zzzz9999")
os.utime(admin_f, (time.time() + 5, time.time() + 5))
st, _, _ = req("GET", "/_gate/check/admin", cookie=admin_tok)
check("password change ends old session", st == 401)
st, h, _ = login("kyle", "new-admin-pass")
check("new password works", st == 302 and token_of(h))

# removing a user ends sessions
with open(team_f, "w") as fh:
    fh.write("")
os.utime(team_f, (time.time() + 10, time.time() + 10))
st, _, _ = req("GET", "/_gate/check/team", cookie=team_tok)
check("removed login -> 401", st == 401)

# brakes
toolgate.time.sleep = lambda s: None  # skip the failure delay in tests
for i in range(5):
    st, _, page = login("kyle", "wrong", ip="10.9.9.9")
check("wrong password -> 401 with message", st == 401 and "don&#x27;t match" in page)
st, _, page = login("kyle", "new-admin-pass", ip="10.9.9.9")
check("6th try from same address blocked, even with right password", st == 429 and "Too many" in page)
st, _, _ = login("kyle", "new-admin-pass", ip="10.8.8.8")
check("other address still allowed", st == 302)
st, _, _ = login("nobody", "x", ip="10.7.7.7")
check("unknown login -> 401", st == 401)

st, _, _ = req("POST", "/_gate/login", urlencode({"username": "kyle", "password": "x"}), headers={"Origin": "https://evil.test"})
os.environ.pop("GATE_INSECURE_COOKIE")
toolgate.INSECURE_COOKIE = False
st, _, _ = req("POST", "/_gate/login", urlencode({"username": "kyle", "password": "x"}),
               headers={"Origin": "https://evil.test"}, ip="10.6.6.6")
check("cross-site login post refused", st == 403)

st, h, _ = req("GET", "/_gate/logout")
check("logout clears cookie", st == 302 and "Max-Age=0" in h.get("Set-Cookie", ""))
st, _, _ = req("GET", "/anything")
check("other paths 404", st == 404)

server.shutdown()
shutil.rmtree(tmp)
print()
print("all checks passed" if not FAILS else "%d FAILED" % len(FAILS))
sys.exit(1 if FAILS else 0)
