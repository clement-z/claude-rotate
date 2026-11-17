"""siwc_login.py, the Sign in with ChatGPT helper: the private file writer, the login
flow against a stand-in authorization server on 127.0.0.1, its refusals, `show`, and
the .gitignore entries for the credential file. No real host is contacted, no browser
is opened (webbrowser.open is replaced by a thread that plays the browser) and every
token is fake.

  python3 tests/test_siwc_login.py
"""
import base64
import contextlib
import hashlib
import http.client
import http.server
import io
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
for var in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "SIWC_BIND"):
    os.environ.pop(var, None)
import siwc_login as S  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="siwc-login-test-"))
S.DEFAULT_OUT = str(TMP / "default-home" / "openai.credential.json")
S.AUTH_BASE = "http://127.0.0.1:9"  # replaced by the stand-in's address before each login
ISSUED = "oaiapp_issued_fake"
OPENAI_ISSUER = "https://auth.openai.com"  # spelt out, not read from the module under test
FULL_SCOPE = "chatgpt.tokens.use.direct email offline_access openid profile resource.invoke"
PASS = 0


def check(name, cond, detail=None):
    global PASS
    print(("ok  " if cond else "FAIL") + " " + name)
    if not cond:
        if detail is not None:
            print(f"     {detail!r}")
        sys.exit(1)
    PASS += 1


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def leftovers(directory):
    return sorted(p.name for p in Path(directory).glob(".siwc-*.tmp"))


class AuthServer:
    """Stand-in for auth.openai.com's authorize and token endpoints. `scenario` bends
    the answers: iss, aud, nonce, exp_in, scope, error, callback_client_id (absent:
    the issued id for a registration, none for a reauthorization; None: left out)."""

    def __init__(self):
        self.codes, self.tokens, self.scenario = {}, [], {}
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, headers=(), body=b""):
                self.send_response(code)
                for k, v in headers:
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                if url.path != "/api/accounts/authorize":
                    return self.reply(404)
                q = dict(urllib.parse.parse_qsl(url.query))
                sc = outer.scenario
                if sc.get("error"):
                    back = {"error": "access_denied", "error_description": "the user declined", "state": q["state"]}
                else:
                    code = os.urandom(8).hex()
                    outer.codes[code] = q
                    back = {"code": code, "state": q["state"], "scope": FULL_SCOPE}
                    dynamic = q.get("client_id") == "dynamic_agent_client"
                    cid = sc["callback_client_id"] if "callback_client_id" in sc else (ISSUED if dynamic else None)
                    if cid is not None:
                        back["client_id"] = cid
                self.reply(302, [("Location", q["redirect_uri"] + "?" + urllib.parse.urlencode(back))])

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                f = dict(urllib.parse.parse_qsl(raw))
                q = outer.codes.pop(f.get("code"), None)
                sc = outer.scenario
                ok = (self.path == "/api/accounts/oauth/token" and q is not None
                      and f.get("grant_type") == "authorization_code"
                      and q.get("code_challenge_method") == "S256"
                      and b64url(hashlib.sha256(f.get("code_verifier", "").encode()).digest()) == q.get("code_challenge")
                      and f.get("redirect_uri") == q.get("redirect_uri") and f.get("resource") == S.RESOURCE
                      and f.get("client_id") == sc.get("client_id", ISSUED))
                outer.tokens.append(ok)
                if not ok:
                    return self.reply(400, [("Content-Type", "application/json")], b'{"error": "invalid_grant"}')
                now = int(time.time())
                claims = {"iss": sc.get("iss", OPENAI_ISSUER), "aud": sc.get("aud", f["client_id"]), "sub": "user-fake",
                          "email": "fake@example.com", "nonce": sc.get("nonce", q["nonce"]),
                          "exp": now + sc.get("exp_in", 3600)}
                id_token = b64url(b'{"alg":"none"}') + "." + b64url(json.dumps(claims).encode()) + "."
                body = {"access_token": "acc-FAKE-" + os.urandom(6).hex(), "refresh_token": "ref-FAKE-" + os.urandom(6).hex(),
                        "id_token": id_token, "token_type": "Bearer", "expires_in": 3600,
                        "scope": sc.get("scope", FULL_SCOPE), "earliest_refresh_at": now}
                body.update(getattr(outer, "reply_extra", None) or {})
                outer.last = body
                self.reply(200, [("Content-Type", "application/json")], json.dumps(body).encode())

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def http_get(port, target):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", target)
        resp = conn.getresponse()
        resp.read()
        return resp.status, resp.getheader("Location")
    finally:
        conn.close()


def login(srv, out, *extra, scenario=None, wrong_state_first=False, timeout=8):
    """out=None runs without --out, so the default output path is used."""
    """Run `siwc_login.py login` in-process. The patched webbrowser.open starts a
    thread that authorizes at the stand-in and delivers the callback it returns."""
    srv.scenario = scenario or {}
    S.AUTH_BASE = srv.base
    S.Callback.result = None  # class state: the CLI runs once per process, this test does not
    port = free_port()
    seen = {}

    def browse(url):
        seen["url"] = url
        try:
            parts = urllib.parse.urlsplit(url)
            status, location = http_get(srv.port, parts.path + "?" + parts.query)
            cb = urllib.parse.urlsplit(location)
            if wrong_state_first:
                seen["wrong_state"] = http_get(port, cb.path + "?" + urllib.parse.urlencode(
                    {"code": "stolen", "state": "another-sign-in"}))[0]
                seen["waiting"] = S.Callback.result is None
            seen["callback"] = http_get(port, cb.path + "?" + cb.query)[0]
        except Exception as exc:  # reported by the caller; the watchdog ends the wait
            seen["error"] = repr(exc)

    def fake_open(url):
        seen["thread"] = threading.Thread(target=browse, args=(url,), daemon=True)
        seen["thread"].start()
        return True

    def watchdog():
        if S.Callback.result is None:
            S.Callback.result = {"error": "test-watchdog"}

    timer = threading.Timer(timeout, watchdog)
    timer.start()
    stdout, stderr, code = io.StringIO(), io.StringIO(), 0
    argv = ["siwc_login.py", "login", *(["--out", str(out)] if out is not None else []), "--port", str(port), *extra]
    try:
        with mock.patch.object(sys, "argv", argv), mock.patch.object(S.webbrowser, "open", side_effect=fake_open) as op, \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                S.main()
            except SystemExit as exc:
                code = exc.code
    finally:
        timer.cancel()
    if "thread" in seen:
        seen["thread"].join(5)
    seen["opened"] = op.call_count
    seen["port"] = port
    return code, stdout.getvalue(), stderr.getvalue(), seen


def test_write_private():
    d = TMP / "wp"
    d.mkdir()
    path = str(d / "cred.json")
    old = os.umask(0)
    try:
        S.write_private(path, "first\n")
    finally:
        os.umask(old)
    check("write_private: a new file has mode 600 under umask 0", mode(path) == 0o600 and Path(path).read_text() == "first\n",
          oct(mode(path)))
    S.write_private(path, "second\n")
    check("write_private: a replaced file is kept as .prev, the live name holds the new content",
          Path(path).read_text() == "second\n" and Path(path + ".prev").read_text() == "first\n")
    os.chmod(path, 0o644)
    S.write_private(path, "third\n")
    check("write_private: an old file left at mode 644 is kept as .prev with mode 600",
          mode(path + ".prev") == 0o600 and mode(path) == 0o600, oct(mode(path + ".prev")))
    check("write_private: the next replacement moves .prev along",
          Path(path).read_text() == "third\n" and Path(path + ".prev").read_text() == "second\n")
    with mock.patch("os.fsync", side_effect=OSError(28, "No space left on device")):
        try:
            S.write_private(path, "fourth\n")
            raised = False
        except OSError:
            raised = True
    check("write_private: a failure mid-write leaves the old file untouched and no .siwc-*.tmp behind",
          raised and Path(path).read_text() == "third\n" and Path(path + ".prev").read_text() == "second\n"
          and leftovers(d) == [], (raised, leftovers(d)))

    elsewhere = TMP / "elsewhere"
    elsewhere.mkdir()
    target = elsewhere / "target.json"
    target.write_text("untouched\n")
    link = d / "link.json"
    link.symlink_to(target)
    S.write_private(str(link), "new\n")
    check("write_private: a symlink at the output is replaced by a regular file, its target not written",
          not link.is_symlink() and link.is_file() and link.read_text() == "new\n" and mode(link) == 0o600
          and target.read_text() == "untouched\n")
    dangling = d / "dangling.json"
    dangling.symlink_to(elsewhere / "created-through-link.json")
    S.write_private(str(dangling), "new\n")
    check("write_private: a dangling symlink is replaced and nothing is created at its target",
          not dangling.is_symlink() and dangling.read_text() == "new\n"
          and not (elsewhere / "created-through-link.json").exists())
    prev_link = d / "prevlink.json"
    prev_link.write_text("live\n")
    Path(str(prev_link) + ".prev").symlink_to(target)
    S.write_private(str(prev_link), "newer\n")
    check("write_private: a symlink at .prev is replaced, its target not written",
          Path(str(prev_link) + ".prev").read_text() == "live\n" and not Path(str(prev_link) + ".prev").is_symlink()
          and target.read_text() == "untouched\n")


def test_login_flow(srv):
    out = TMP / "flow" / "openai.credential.json"
    out.parent.mkdir()
    before = len(srv.tokens)
    code, stdout, stderr, seen = login(srv, out, wrong_state_first=True)
    check("login: completes with exit 0", code in (0, None) and "error" not in seen, (code, stderr, seen))
    url = seen.get("url", "")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    check("login: the authorize URL goes to AUTH_BASE/api/accounts/authorize",
          url.startswith(srv.base + "/api/accounts/authorize?") and seen["opened"] == 1, url)
    check("login: PKCE S256 with a 43-character base64url challenge",
          q.get("code_challenge_method") == "S256" and len(q.get("code_challenge", "")) == 43
          and set(q["code_challenge"]) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"), q)
    check("login: state and nonce are present, long and distinct",
          len(q.get("state", "")) >= 32 and len(q.get("nonce", "")) >= 32 and q["state"] != q["nonce"], q)
    check("login: dynamic registration (client_id=dynamic_agent_client, agent name hint, new host id)",
          q.get("client_id") == "dynamic_agent_client" and q.get("agent_name_hint") == "claude-rotate"
          and q.get("ext_agent_host_id", "").startswith("urn:uuid:") and q.get("response_type") == "code", q)
    check("login: resource, scopes and the loopback redirect URI",
          q.get("resource") == "https://api.openai.com/v1" and q.get("scope", "").split() == S.SCOPES.split()
          and {"openid", "offline_access", "chatgpt.tokens.use.direct"} <= set(q["scope"].split())
          and q.get("redirect_uri") == f"http://127.0.0.1:{seen['port']}/auth/callback", q)
    check("login: a callback with another state gets 400 and the login keeps waiting",
          seen.get("wrong_state") == 400 and seen.get("waiting") is True and seen.get("callback") == 200, seen)
    check("login: the stand-in token endpoint verified the code_verifier", srv.tokens[before:] == [True], srv.tokens)
    rec = json.loads(out.read_text())
    now = time.time()
    check("login: the credential file has mode 600", mode(out) == 0o600, oct(mode(out)))
    check("login: the file has client_id, ext_agent_host_id, scopes, expires_at and the tokens",
          rec.get("client_id") == ISSUED and rec.get("ext_agent_host_id") == q["ext_agent_host_id"]
          and rec.get("scopes") == sorted(FULL_SCOPE.split()) and abs(rec.get("expires_at", 0) - (now + 3600)) < 30
          and rec.get("access_token") == srv.last["access_token"] and rec.get("refresh_token") == srv.last["refresh_token"]
          and rec.get("email") == "fake@example.com" and rec.get("issuer") == OPENAI_ISSUER, rec)
    check("login: no token in the output and no plan-scope warning",
          not any(srv.last[k] in stdout + stderr for k in ("access_token", "refresh_token", "id_token"))
          and "WARNING" not in stdout and ISSUED in stdout, stdout)
    check("login: no temporary file is left", leftovers(out.parent) == [])

    # Reauthorization of that agent writes over the file and keeps the old one as .prev.
    first = out.read_text()
    code, stdout, stderr, seen = login(srv, out, "--client-id", "oaiapp_existing", "--host-id", "urn:uuid:existing-host",
                                       scenario={"client_id": "oaiapp_existing", "callback_client_id": "oaiapp_existing",
                                                 "aud": ["other-audience", "oaiapp_existing"],
                                                 "scope": "openid offline_access"})
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(seen.get("url", "")).query))
    rec = json.loads(out.read_text())
    check("login --client-id --host-id: reauthorizes the named agent without a name hint",
          code in (0, None) and q.get("client_id") == "oaiapp_existing" and "agent_name_hint" not in q
          and q.get("ext_agent_host_id") == "urn:uuid:existing-host", (code, stderr, q))
    check("login --client-id --host-id: writes that client and host, accepts an audience list, keeps .prev",
          rec.get("client_id") == "oaiapp_existing" and rec.get("ext_agent_host_id") == "urn:uuid:existing-host"
          and Path(str(out) + ".prev").read_text() == first, rec)
    check("login: a missing plan scope is warned about", f"WARNING: scope {S.PLAN_SCOPE} was not granted" in stdout, stdout)
    return out


def test_login_refusals(srv):
    host = ("--client-id", "oaiapp_existing", "--host-id", "urn:uuid:existing-host")
    cases = [
        ("an ID token from another issuer", (), {"iss": "https://auth.example.invalid"}, "issuer is not OpenAI's", True),
        ("an ID token for another audience", (), {"aud": "oaiapp_someone_else"}, "audience is not the issued client_id", True),
        ("an ID token with a wrong nonce", (), {"nonce": "another-nonce"}, "nonce does not match", True),
        ("an expired ID token", (), {"exp_in": -60}, "ID token is expired", True),
        ("a token endpoint error", (), {"client_id": "oaiapp_not_this_one"},
         "token endpoint answered 400 (invalid_grant)", True),
        ("a callback with error=access_denied", (), {"error": True}, "authorization refused: access_denied", False),
        ("a registration whose callback has no client_id", (), {"callback_client_id": None},
         "registration incomplete", False),
        ("a registration whose callback echoes dynamic_agent_client", (), {"callback_client_id": "dynamic_agent_client"},
         "registration incomplete", False),
        ("a reauthorization whose callback names another client_id", host,
         {"client_id": "oaiapp_existing", "callback_client_id": "oaiapp_other"}, "names another client_id", False),
    ]
    for n, (label, extra, scenario, message, reaches_token) in enumerate(cases):
        out = TMP / f"refused-{n}" / "openai.credential.json"
        out.parent.mkdir()
        before = len(srv.tokens)
        code, stdout, stderr, seen = login(srv, out, *extra, scenario=scenario)
        check(f"login refuses {label} and writes no file",
              code == 1 and message in stderr and "error" not in seen and not out.exists()
              and list(out.parent.iterdir()) == [] and (len(srv.tokens) > before) == reaches_token,
              (code, stderr, seen, srv.tokens[before:]))
    for flag in (("--client-id", "oaiapp_existing"), ("--host-id", "urn:uuid:existing-host")):
        out = TMP / f"half-{flag[0][2:]}" / "openai.credential.json"
        out.parent.mkdir()
        code, stdout, stderr, seen = login(srv, out, *flag)
        check(f"login refuses {flag[0]} alone before listening or opening a browser",
              code == 1 and "--client-id and --host-id go together" in stderr and seen["opened"] == 0
              and not out.exists(), (code, stderr))
    out = TMP / "cli-half" / "openai.credential.json"
    out.parent.mkdir()
    r = subprocess.run([sys.executable, "-I", str(ROOT / "siwc_login.py"), "login", "--no-browser", "--client-id", "x",
                        "--out", str(out), "--port", str(free_port())], capture_output=True, text=True, timeout=20,
                       env=dict(os.environ, HOME=str(TMP), SIWC_AUTH_BASE=srv.base))
    check("the login command line refuses --client-id without --host-id",
          r.returncode == 1 and "go together" in r.stderr and not out.exists(), r)


def test_show(path):
    rec = json.loads(Path(path).read_text())
    r = subprocess.run([sys.executable, "-I", str(ROOT / "siwc_login.py"), "show", str(path)], capture_output=True,
                       text=True, timeout=20, env=dict(os.environ, HOME=str(TMP)))
    check("show prints account, client, host and expiry",
          r.returncode == 0 and "account:      fake@example.com" in r.stdout
          and f"client_id:    {rec['client_id']}" in r.stdout and f"host id:      {rec['ext_agent_host_id']}" in r.stdout
          and "min left" in r.stdout, r)
    check("show prints no token", not any(rec[k] in r.stdout + r.stderr for k in ("access_token", "refresh_token", "id_token")))


def test_default_out(srv):
    default = TMP / "home-default" / ".config" / "claude-rotate" / "openai.credential.json"
    saved, S.DEFAULT_OUT = S.DEFAULT_OUT, str(default)
    try:
        code, stdout, stderr, seen = login(srv, None)
    finally:
        S.DEFAULT_OUT = saved
    check("login without --out writes the default path, its directory created with mode 700",
          code == 0 and default.is_file() and mode(default) == 0o600 and mode(default.parent) == 0o700
          and json.loads(default.read_text()).get("client_id") == ISSUED, (code, stderr))


def test_expires_in(srv):
    for label, value in (("a NaN", float("nan")), ("a zero", 0), ("a string", "3600"), ("a 400-digit", 10 ** 400)):
        out = TMP / f"expires-{label.split()[1]}.json"
        original = srv.reply_extra if hasattr(srv, "reply_extra") else None
        srv.reply_extra = {"expires_in": value}
        try:
            code, stdout, stderr, seen = login(srv, out)
        finally:
            srv.reply_extra = original
        check(f"login refuses {label} expires_in, writing nothing",
              code == 1 and "no usable expires_in" in stderr and not out.exists(), (code, stderr))


def test_gitignore():
    for name in ("openai.credential.json", "openai.credential.json.prev",
                 "openai-credential.json", "openai-credential.json.prev"):
        r = subprocess.run(["git", "check-ignore", "-q", name], cwd=ROOT, capture_output=True, text=True, timeout=20)
        check(f"git check-ignore reports {name} as ignored at the repository root", r.returncode == 0, r)


srv = AuthServer()
try:
    test_write_private()
    written = test_login_flow(srv)
    test_login_refusals(srv)
    test_show(written)
    test_default_out(srv)
    test_expires_in(srv)
    test_gitignore()
    check("nothing was written at the default output path", not os.path.lexists(S.DEFAULT_OUT))
finally:
    srv.close()
    shutil.rmtree(TMP, ignore_errors=True)
print(f"\nall {PASS} checks passed")
