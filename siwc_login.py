#!/usr/bin/env python3
"""Sign in with ChatGPT for claude-rotate's openai-siwc provider. Standard library only.

Run it on a machine with a browser, then move the file to the server and delete it
here (OpenAI's documented pattern for self-hosted machines: sign in locally, move
the credential over a secure channel, let the server own later refreshes):

  python3 siwc_login.py login [--out FILE] [--port 1455] [--no-browser]
                              [--agent-name claude-rotate] [--client-id ID --host-id ID]
  python3 siwc_login.py show FILE      account, client, scopes and expiry, never tokens

  scp FILE server:   then on the server:
  sudo claude-rotate-admin install-credential openai --file FILE && rm FILE

The gateway is the "host" of the registered agent. --client-id and --host-id sign in
again to the agent registered earlier (both shown on the server by
claude-rotate-admin status and in /rotate/status); without them a new agent is
registered. Once the file is on the server, delete every other copy: a second holder
refreshing the same credential kills it for both.

Environment, for stand-in servers only:
  SIWC_AUTH_BASE  default https://auth.openai.com
  SIWC_BIND       address the callback listener binds (default 127.0.0.1)

Flow: https://developers.openai.com/siwc/token-sharing-open-source (read 2026-11-17),
ported from the devbox siwc.py test helper.
"""
import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from datetime import datetime, timezone

AUTH_BASE = os.environ.get("SIWC_AUTH_BASE", "https://auth.openai.com").rstrip("/")
RESOURCE = "https://api.openai.com/v1"  # the token's audience, fixed by the flow
SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
PLAN_SCOPE = "chatgpt.tokens.use.direct"
CALLBACK_PATH = "/auth/callback"


def die(msg):
    print(f"siwc_login: {msg}", file=sys.stderr)
    sys.exit(1)


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwt_payload(token):
    """Claims of a JWT, unverified: the ID token comes straight from the token endpoint
    over TLS, which OIDC accepts in place of a signature check for this client."""
    part = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def post_form(url, fields):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            code = json.loads(e.read()).get("error")
        except (ValueError, AttributeError):
            code = None
        die(f"token endpoint answered {e.code}" + (f" ({code})" if isinstance(code, str) else ""))


def write_private(path, text):
    """Atomic owner-only write; a file it replaces is kept as <path>.prev."""
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(path):
        os.replace(path, path + ".prev")
    os.replace(tmp, path)


class Callback(http.server.BaseHTTPRequestHandler):
    result = None

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        if url.path != CALLBACK_PATH:
            self.send_error(404)
            return
        Callback.result = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Sign-in received. You can close this tab and go back to the terminal.\n")

    def log_message(self, *args):
        pass


def cmd_login(args):
    if bool(args.client_id) != bool(args.host_id):
        die("--client-id and --host-id go together")
    redirect_uri = f"http://127.0.0.1:{args.port}{CALLBACK_PATH}"
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(64)
    params = {
        "response_type": "code", "redirect_uri": redirect_uri, "scope": SCOPES,
        "resource": RESOURCE, "state": state, "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": b64url(hashlib.sha256(verifier.encode()).digest()),
        # One id per host; the host is the gateway, wherever this command runs.
        "ext_agent_host_id": args.host_id or f"urn:uuid:{uuid.uuid4()}",
    }
    if args.client_id:
        params["client_id"] = args.client_id  # reauthorization of the agent registered earlier
    else:
        params["client_id"], params["agent_name_hint"] = "dynamic_agent_client", args.agent_name
    url = f"{AUTH_BASE}/api/accounts/authorize?" + urllib.parse.urlencode(params)

    server = http.server.HTTPServer((os.environ.get("SIWC_BIND", "127.0.0.1"), args.port), Callback)
    server.timeout = 1
    print("Open this address in your browser and approve the request:\n")
    print(url + "\n")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    print(f"Waiting for the callback on {redirect_uri} (5 minutes) ...", flush=True)
    deadline = time.time() + 300
    while Callback.result is None and time.time() < deadline:
        server.handle_request()
    server.server_close()
    cb = Callback.result
    if cb is None:
        die("no callback received")
    if cb.get("state") != state:
        die("callback state does not match; attempt dropped")
    if cb.get("error"):
        die(f"authorization refused: {cb['error']} {cb.get('error_description', '')}")
    if "code" not in cb:
        die("callback has no code")
    if params["client_id"] == "dynamic_agent_client":
        client_id = cb.get("client_id")
        if not client_id or client_id == "dynamic_agent_client":
            die("registration incomplete: the callback carries no issued client_id")
    else:
        client_id = params["client_id"]
        if cb.get("client_id") and cb["client_id"] != client_id:
            die("the callback names another client_id; result rejected")

    resp = post_form(f"{AUTH_BASE}/api/accounts/oauth/token", {
        "grant_type": "authorization_code", "client_id": client_id, "code": cb["code"],
        "code_verifier": verifier, "redirect_uri": redirect_uri, "resource": RESOURCE})
    for key in ("access_token", "refresh_token", "id_token"):
        if not resp.get(key):
            die(f"token response lacks {key}")
    claims = jwt_payload(resp["id_token"])
    aud = claims.get("aud")
    if client_id not in (aud if isinstance(aud, list) else [aud]):
        die("ID token audience is not the issued client_id")
    if claims.get("nonce") != nonce:
        die("ID token nonce does not match")
    if claims.get("exp", 0) < time.time():
        die("ID token is expired")

    now = int(time.time())
    rec = {"email": claims.get("email"), "issuer": claims.get("iss"), "subject": claims.get("sub"),
           "client_id": client_id, "ext_agent_host_id": params["ext_agent_host_id"]}
    for key in ("access_token", "refresh_token", "id_token", "token_type", "expires_in", "earliest_refresh_at"):
        if resp.get(key) is not None:
            rec[key] = resp[key]
    rec["scopes"] = sorted(str(resp.get("scope", "")).split())
    rec["expires_at"] = now + int(resp.get("expires_in", 3600))
    rec["saved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rec["refresh_saved_at"] = now
    write_private(args.out, json.dumps(rec, indent=2) + "\n")
    print(f"Signed in as {rec['email']}; client {client_id}; host {rec['ext_agent_host_id']}.")
    if PLAN_SCOPE not in rec["scopes"]:
        print(f"WARNING: scope {PLAN_SCOPE} was not granted: plan usage is not enabled for this agent.")
    print(f"Credential written to {args.out} (mode 600). Move it to the server, install it with "
          "claude-rotate-admin install-credential, then delete every other copy. The access token "
          "lasts an hour; the server refreshes it from there.")


def cmd_show(args):
    try:
        with open(args.file) as f:
            rec = json.load(f)
    except (OSError, ValueError) as exc:
        die(f"cannot read {args.file}: {exc}")
    now = int(time.time())
    print(f"account:      {rec.get('email')}")
    print(f"client_id:    {rec.get('client_id')}")
    print(f"host id:      {rec.get('ext_agent_host_id')}")
    print(f"scopes:       {' '.join(rec.get('scopes', []))}")
    print(f"access token: {max(0, rec.get('expires_at', 0) - now) // 60} min left")
    print(f"last refresh: {(now - rec.get('refresh_saved_at', now)) // 60} min ago "
          "(the refresh token lives 30 days from then)")


def main():
    parser = argparse.ArgumentParser(prog="siwc_login.py", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("login", help="sign in through the browser and write the credential file")
    p.add_argument("--out", default="openai-credential.json")
    p.add_argument("--port", type=int, default=1455)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--agent-name", default="claude-rotate")
    p.add_argument("--client-id")
    p.add_argument("--host-id")
    p.set_defaults(func=cmd_login)
    p = sub.add_parser("show", help="describe a credential file without printing tokens")
    p.add_argument("file")
    p.set_defaults(func=cmd_show)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
