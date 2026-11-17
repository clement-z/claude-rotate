"""claude-rotate-admin's provider side: install-credential, add-user --providers, the
readiness of object device entries and the providers in status. Each group runs the
real entry point under a temporary CLAUDE_ROTATE_PREFIX with the recording systemctl
stub from test_admin.py; every secret is a dummy.

  python3 tests/test_providers_admin.py
"""
import atexit
import http.server
import json
import os
import shlex
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_admin import FAKE_SYSTEMCTL, TOKEN, Fixture, mode  # noqa: E402

PASS = 0
FIXTURES = []
UNIT = "claude-rotate.service"
LAPTOP_KEY = "dev-laptop-0123456789abcdef"
DROP = object()  # credential(): leave the key out
PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")

# The stub with a hook on start as well as on stop, to see the credential at both moments.
START_CASE = "  enable|start|restart)\n"
assert START_CASE in FAKE_SYSTEMCTL
SYSTEMCTL = FAKE_SYSTEMCTL.replace(START_CASE, START_CASE + '    [ -x "$d/on-start" ] && "$d/on-start"\n')


def check(name, cond, detail=None):
    global PASS
    print(("ok  " if cond else "FAIL") + " " + name)
    if not cond:
        if detail is not None:
            print(f"     {detail!r}")
        sys.exit(1)
    PASS += 1


@atexit.register
def cleanup():
    for f in FIXTURES:
        f.cleanup()


def make():
    f = Fixture()
    FIXTURES.append(f)
    (f.bin / "systemctl").write_text(SYSTEMCTL)
    for var in PROXY_VARS:
        f.env.pop(var, None)
    assert f.run("init").returncode == 3
    (f.prefix / "var/lib").mkdir(parents=True)  # exists on any host; the admin creates only claude-rotate/
    (f.tokens / "acct-1.token").write_text(TOKEN)
    cfg = f.cfg()
    cfg.update(accounts=[{"name": "acct-1", "token_file": "tokens/acct-1.token"}],
               devices={"laptop": LAPTOP_KEY},
               providers={"openai": {"kind": "openai-siwc", "prefix": "/openai"},
                          "openai-team": {"kind": "openai-siwc", "prefix": "/team"},
                          "glm": {"kind": "static-bearer", "prefix": "/glm", "upstream": "https://glm.invalid/v1",
                                  "token_file": "tokens/glm.token"}})
    f.config.write_text(json.dumps(cfg))
    f.creds = f.state_dir / "credentials"
    f.live = f.creds / "openai.json"
    for event in ("stop", "start"):
        snap, hook = shlex.quote(str(f.fake / f"at-{event}")), f.fake / f"on-{event}"
        hook.write_text(f"#!/bin/sh\ncat {shlex.quote(str(f.live))} > {snap} 2>/dev/null || echo missing > {snap}\n")
        hook.chmod(0o700)
    return f


def reset(f):
    f.reset_calls()
    for event in ("stop", "start"):
        (f.fake / f"at-{event}").unlink(missing_ok=True)


def seen_at(f, event):
    path = f.fake / f"at-{event}"
    return path.read_text() if path.exists() else None


def stop_then_start(calls):
    stop = f"stop {UNIT}"
    if stop not in calls:
        return False
    return any(c.split()[0] in ("start", "enable", "restart") for c in calls[calls.index(stop) + 1:])


def credential(n, **changes):
    rec = {"email": "fake@example.com", "client_id": "oaiapp_fake", "ext_agent_host_id": "urn:uuid:fake-host",
           "access_token": f"acc-FAKE-{n}-xxxxxxxxxxxx", "refresh_token": f"ref-FAKE-{n}-yyyyyyyyyyyy",
           "expires_at": int(time.time()) + 3600, "scopes": ["openid"]}
    rec.update(changes)
    return {k: v for k, v in rec.items() if v is not DROP}



def source(f, name, text):
    directory = f.tmp / "upload"
    directory.mkdir(exist_ok=True)
    path = directory / name
    path.write_bytes(text if isinstance(text, bytes) else text.encode())
    path.chmod(0o600)
    return path


def install(f, src):
    return f.run("install-credential", "openai", "--file", str(src))


def test_install_refusals():
    f = make()
    good = source(f, "good.json", json.dumps(credential(1)))
    f.set_active(True)
    for label, provider in (("an unknown provider", "nope"), ("a static-bearer provider", "glm")):
        reset(f)
        r = f.run("install-credential", provider, "--file", str(good))
        check(f"install-credential refuses {label}",
              r.returncode == 1 and "not an openai-siwc provider" in r.stderr
              and f"stop {UNIT}" not in f.calls() and not f.state_dir.exists(), r)
    bad = {"invalid JSON": "{not json", "a JSON list": "[]",
           "a missing access_token": credential(1, access_token=DROP), "an empty access_token": credential(1, access_token=""),
           "a blank access_token": credential(1, access_token="  "),
           "a missing refresh_token": credential(1, refresh_token=DROP), "an empty refresh_token": credential(1, refresh_token=""),
           "a missing client_id": credential(1, client_id=DROP), "an empty client_id": credential(1, client_id=""),
           "a string expires_at": credential(1, expires_at="1900000000"), "a boolean expires_at": credential(1, expires_at=True),
           "a missing expires_at": credential(1, expires_at=DROP),
           "a NaN expires_at": json.dumps(credential(1, expires_at=0)).replace('"expires_at": 0', '"expires_at": NaN'),
           "an Infinity expires_at": json.dumps(credential(1, expires_at=0)).replace('"expires_at": 0', '"expires_at": Infinity'),
           "a file over 64 KiB": " " * (64 * 1024) + json.dumps(credential(1)), "non-UTF-8 bytes": b"\xff\xfe{}"}
    for label, content in bad.items():
        src = source(f, "bad.json", content if isinstance(content, (str, bytes)) else json.dumps(content))
        reset(f)
        r = install(f, src)
        check(f"install-credential refuses {label} before the unit is touched",
              r.returncode == 1 and "nothing changed" in r.stderr
              and f"stop {UNIT}" not in f.calls() and not f.state_dir.exists(), (r, f.calls()))


def test_install_success_and_prev():
    f = make()
    first = json.dumps(credential(1))  # no trailing newline: the admin adds one
    src = source(f, "first.json", first)
    src_before = (src.read_bytes(), mode(src), src.stat().st_mtime_ns)
    f.set_active(True)
    reset(f)
    r = install(f, src)
    calls = f.calls()
    check("install-credential succeeds and the service is live again", r.returncode == 0 and "is live" in r.stdout, r)
    check("the credential lands at <state dir>/credentials/openai.json", f.live.read_text() == first + "\n")
    # Not root: as_service forks without changing uid, so the owner is the caller.
    # The root path (setgid/setuid to the service user) is not exercised here.
    st = os.lstat(f.live)
    check("the credential has mode 600 and the current uid as owner", mode(f.live) == 0o600 and st.st_uid == os.getuid(),
          (oct(mode(f.live)), st.st_uid))
    check("the credentials and state directories are not group or world accessible",
          mode(f.creds) & 0o077 == 0 and mode(f.state_dir) & 0o077 == 0, (oct(mode(f.creds)), oct(mode(f.state_dir))))
    check("the unit is stopped before the write and started after",
          stop_then_start(calls) and seen_at(f, "stop") == "missing\n" and seen_at(f, "start") == first + "\n",
          (calls, seen_at(f, "stop"), seen_at(f, "start")))
    check("the source file is left as it was",
          src.exists() and (src.read_bytes(), mode(src), src.stat().st_mtime_ns) == src_before)
    secrets = [credential(1)["access_token"], credential(1)["refresh_token"]]
    check("the output prints no token", not any(s in r.stdout + r.stderr for s in secrets), r)

    second = json.dumps(credential(2)) + "\n"
    f.set_active(True)
    reset(f)
    r = f.run("install-credential", "openai", input=second)  # from stdin this time
    prev = f.creds / "openai.json.prev"
    check("a second install (from stdin) keeps the first as .prev",
          r.returncode == 0 and f.live.read_text() == second and prev.read_text() == first + "\n"
          and mode(prev) == 0o600, r)
    check("the second install stopped the unit while it held the first credential",
          stop_then_start(f.calls()) and seen_at(f, "stop") == first + "\n" and seen_at(f, "start") == second)
    check("no temporary file is left in the credentials directory",
          sorted(p.name for p in f.creds.iterdir()) == ["openai.json", "openai.json.prev"], list(f.creds.iterdir()))
    check("the second output prints no token",
          not any(s in r.stdout + r.stderr for s in (credential(2)["access_token"], credential(2)["refresh_token"])))


def test_install_unit_states_and_failures():
    f = make()
    one, two, three = (json.dumps(credential(n)) + "\n" for n in (1, 2, 3))
    f.set_active(False)
    reset(f)
    r = install(f, source(f, "one.json", one))
    check("an inactive unit is not stopped, and is started after the install",
          r.returncode == 0 and f"stop {UNIT}" not in f.calls() and seen_at(f, "start") == one, (r, f.calls()))

    (f.fake / "active").write_text("activating")
    reset(f)
    r = install(f, source(f, "two.json", two))
    check("an activating unit is stopped before the install",
          r.returncode == 0 and stop_then_start(f.calls()) and seen_at(f, "stop") == one and f.live.read_text() == two,
          (r, f.calls()))

    f.set_active(True)
    (f.fake / "fail-stop").write_text("")
    reset(f)
    before = (f.live.read_bytes(), (f.creds / "openai.json.prev").read_bytes())
    r = install(f, source(f, "three.json", three))
    (f.fake / "fail-stop").unlink()
    check("a stop that fails refuses the install with nothing written",
          r.returncode == 1 and "stopped state" in r.stderr and "not installed" in r.stderr
          and (f.live.read_bytes(), (f.creds / "openai.json.prev").read_bytes()) == before
          and sorted(p.name for p in f.creds.iterdir()) == ["openai.json", "openai.json.prev"]
          and not any(c.split()[0] in ("start", "enable", "restart") for c in f.calls()), (r, f.calls()))

    aside = f.tmp / "credentials-aside"
    f.creds.rename(aside)
    f.creds.write_text("a regular file\n")
    f.set_active(True)
    reset(f)
    r = install(f, source(f, "three.json", three))
    check("an install that fails after the stop (credentials is a regular file) starts the service again",
          r.returncode == 1 and "credential NOT installed" in r.stderr and "service started again" in r.stderr
          and stop_then_start(f.calls()) and f.creds.read_text() == "a regular file\n", (r, f.calls()))

    (f.fake / "fail-start").write_text("")
    f.set_active(True)
    reset(f)
    r = install(f, source(f, "three.json", three))
    (f.fake / "fail-start").unlink()
    check("an install failure followed by a failed restart reports both",
          r.returncode == 1 and "credential NOT installed" in r.stderr and "service start FAILED" in r.stderr
          and "journalctl" in r.stderr and stop_then_start(f.calls()), r)
    f.creds.unlink()

    elsewhere = f.tmp / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    f.creds.symlink_to(elsewhere)
    f.set_active(True)
    reset(f)
    r = install(f, source(f, "three.json", three))
    check("a symlinked credentials directory fails after the stop, its target is not written and the service is restarted",
          r.returncode == 1 and "credential NOT installed" in r.stderr and "service started again" in r.stderr
          and list(elsewhere.iterdir()) == [] and stop_then_start(f.calls()), (r, f.calls()))
    f.creds.unlink()
    aside.rename(f.creds)

    real_state = f.tmp / "state-aside"
    f.state_dir.rename(real_state)
    f.state_dir.symlink_to(real_state)
    f.set_active(True)
    reset(f)
    r = install(f, source(f, "three.json", three))
    check("a symlinked state directory is refused before the unit is touched",
          r.returncode == 1 and "symbolic link" in r.stderr and f"stop {UNIT}" not in f.calls()
          and (real_state / "credentials/openai.json").read_text() == two, (r, f.calls()))
    f.state_dir.unlink()
    real_state.rename(f.state_dir)

    # systemctl stop reports an error, yet the unit ends up stopped.
    real = f.bin / "systemctl.real"
    (f.bin / "systemctl").rename(real)
    (f.bin / "systemctl").write_text(f'#!/bin/sh\n{shlex.quote(str(real))} "$@"; rc=$?\n[ "$1" = stop ] && exit 1\nexit $rc\n')
    (f.bin / "systemctl").chmod(0o755)
    f.set_active(True)
    reset(f)
    r = install(f, source(f, "three.json", three))
    real.replace(f.bin / "systemctl")
    check("a stop that errs but leaves the unit stopped installs and starts the service again",
          r.returncode == 0 and seen_at(f, "stop") == two and f.live.read_text() == three
          and seen_at(f, "start") == three and stop_then_start(f.calls()), (r, f.calls()))


def test_add_user_providers():
    f = make()
    f.set_active(True)
    reset(f)
    r = f.run("add-user", "codex-box", "--providers", "openai")
    entry = f.cfg()["devices"].get("codex-box")
    key = entry.get("key") if isinstance(entry, dict) else None
    check("add-user --providers openai writes {key, providers: [openai]}",
          r.returncode == 0 and entry == {"key": key, "providers": ["openai"]}
          and isinstance(key, str) and key.startswith("dev-codex-box-"), (r, entry))
    check("the generated key is printed once, on a device key line",
          [line.strip() for line in r.stdout.splitlines() if "device key:" in line] == [f"device key: {key}"]
          and r.stdout.count(key) == 1 and "CLAUDE_CODE_OAUTH_TOKEN" not in r.stdout
          and "/rotate/panel" not in r.stdout, r.stdout)
    check("the provider's base URL is named and the object entry passes readiness",
          "openai: base URL http://<this-host>:8484/openai/v1" in r.stdout and "is live" in r.stdout, r.stdout)
    for n, value in enumerate(("", ",", "openai,", " , openai", "openai,openai", "nope", "openai,nope")):
        before = f.config.read_bytes()
        reset(f)
        r = f.run("add-user", f"refused-{n}", "--providers", value)
        check(f"add-user --providers {value!r} is refused with the config unchanged",
              r.returncode == 1 and "--providers must list" in r.stderr and f.config.read_bytes() == before
              and f.calls() == [], r)
    r = f.run("add-user", "plain-box", "--no-restart")
    entry = f.cfg()["devices"].get("plain-box")
    check("without --providers the entry stays a plain string (Claude accounts only)",
          r.returncode == 0 and isinstance(entry, str) and f"export CLAUDE_CODE_OAUTH_TOKEN={entry}" in r.stdout, r)
    r = f.run("add-user", "named-box", "--providers", "anthropic", "--no-restart")
    check("--providers anthropic alone is also stored as a plain string",
          r.returncode == 0 and isinstance(f.cfg()["devices"].get("named-box"), str), r)
    r = f.run("add-user", "both-box", "--providers", "anthropic, openai", "--no-restart")
    entry = f.cfg()["devices"].get("both-box")
    check("--providers anthropic,openai stores both and prints the Claude and provider settings",
          r.returncode == 0 and isinstance(entry, dict) and entry["providers"] == ["anthropic", "openai"]
          and f"export CLAUDE_CODE_OAUTH_TOKEN={entry['key']}" in r.stdout and "openai: base URL" in r.stdout, r)


def test_status_reports_providers():
    f = make()
    leaked = ("acc-LIVE-should-not-print", "ref-LIVE-should-not-print")
    live = {"active_account": "acct-1", "storage_errors": {}, "accounts": {"acct-1": {"effective_util_5h": 0.2}},
            "providers": {
                "openai": {"kind": "openai-siwc", "prefix": "/openai", "state": "ok", "reason": None,
                           "client_id": "oaiapp_fakeA", "host_id": "urn:uuid:fake-host-a", "access_token_min": 42,
                           "refresh_token_days": 29.5, "access_token": leaked[0], "refresh_token": leaked[1]},
                "openai-team": {"kind": "openai-siwc", "prefix": "/team", "state": "down",
                                "reason": "the refresh token was rejected", "client_id": "oaiapp_fakeB",
                                "host_id": "urn:uuid:fake-host-b", "access_token_min": None},
                "glm": {"kind": "static-bearer", "prefix": "/glm", "state": "ok", "reason": None}}}
    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["path"], seen["auth"] = self.path, self.headers.get("Authorization")
            body = json.dumps(live).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    codex_key = "dev-codex-box-fedcba9876543210"
    try:
        cfg = f.cfg()
        cfg["port"] = server.server_address[1]
        cfg["devices"] = {"codex-box": {"key": codex_key, "providers": ["openai"]}, "laptop": LAPTOP_KEY}
        f.config.write_text(json.dumps(cfg))
        f.set_active(True)
        r = f.run("status", "--json")
        report = json.loads(r.stdout) if r.returncode == 0 else {}
        human = f.run("status")
    finally:
        server.shutdown()
        server.server_close()
    check("status queries the live endpoint with a key that has the Claude grant",
          seen.get("path") == "/rotate/status" and seen.get("auth") == f"Bearer {LAPTOP_KEY}", seen)
    providers = (report.get("live") or {}).get("providers") or {}
    check("status --json carries each provider's state, client_id and host id",
          r.returncode == 0 and report.get("ready") is True
          and all(providers.get(n, {}).get(k) == live["providers"][n].get(k)
                  for n in live["providers"] for k in ("kind", "prefix", "state", "reason", "client_id", "host_id")),
          providers)
    lines = human.stdout.splitlines()
    check("status text carries each provider's state, client_id and host id",
          human.returncode == 0
          and any("provider openai (openai-siwc, /openai): ok" in x and "client_id oaiapp_fakeA, host id urn:uuid:fake-host-a"
                  in x and "access token 42 min" in x for x in lines)
          and any("provider openai-team (openai-siwc, /team): down: the refresh token was rejected" in x
                  and "client_id oaiapp_fakeB, host id urn:uuid:fake-host-b" in x for x in lines)
          and any("provider glm (static-bearer, /glm): ok" in x for x in lines)
          and any(f"codex-box: key ...{codex_key[-4:]}; providers openai" in x for x in lines), human.stdout)
    secrets = (*leaked, TOKEN, LAPTOP_KEY, codex_key)
    check("neither status output contains a token or a device key",
          not any(s in r.stdout + r.stderr + human.stdout + human.stderr for s in secrets))


def test_readiness_of_object_devices():
    f = make()
    obj_key = "dev-obj-0011223344556677"
    bad = {"no providers": {"key": obj_key}, "an empty providers list": {"key": obj_key, "providers": []},
           "an extra key": {"key": obj_key, "providers": ["openai"], "note": "x"},
           "an unknown provider": {"key": obj_key, "providers": ["nope"]},
           "providers as a string": {"key": obj_key, "providers": "openai"}}
    cfg = f.cfg()
    for label, entry in bad.items():
        cfg["devices"] = {"laptop": LAPTOP_KEY, "obj": entry}
        f.config.write_text(json.dumps(cfg))
        r = f.run("check")
        check(f"check reports an object device entry with {label}",
              r.returncode == 3 and "device 'obj': an object needs" in r.stdout, r)
    r = f.run("status", "--json")
    check("status --json lists the same problem",
          r.returncode == 0 and any("device 'obj'" in p for p in json.loads(r.stdout)["problems"]), r)
    for providers in (["openai"], ["anthropic", "openai"]):
        cfg["devices"] = {"laptop": LAPTOP_KEY, "obj": {"key": obj_key, "providers": providers}}
        f.config.write_text(json.dumps(cfg))
        r = f.run("check")
        check(f"check accepts a valid object entry with providers {providers}",
              r.returncode == 0 and "configuration complete" in r.stdout and "device 'obj'" not in r.stdout, r)


test_install_refusals()
test_install_success_and_prev()
test_install_unit_states_and_failures()
test_add_user_providers()
test_status_reports_providers()
test_readiness_of_object_devices()
print(f"\nall {PASS} checks passed")
