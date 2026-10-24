"""claude-rotate-admin under a temporary prefix with a fake systemctl and dummy secrets.

  python3 tests/test_admin.py

Nothing here touches /etc, /opt, /var or a real service: CLAUDE_ROTATE_PREFIX points
every managed path into a temporary directory and a recording systemctl stub is
first on PATH.
"""
import http.server
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADMIN = ROOT / "deploy" / "claude_rotate_admin.py"
UNIT_TEXT = (ROOT / "deploy" / "claude-rotate.service").read_text()
TOKEN = "sk-ant-oat01-dummy-admin-test-token-0001"

FAKE_SYSTEMCTL = r"""#!/bin/sh
# Recording systemd stub for tests. State lives in $FAKE_SYSTEMD_DIR.
d="$FAKE_SYSTEMD_DIR"
echo "$*" >> "$d/calls.log"
[ -e "$d/fail-all" ] && exit 1
state() { cat "$d/active" 2>/dev/null || echo inactive; }
enabled() { cat "$d/enabled" 2>/dev/null || echo disabled; }
case "$1" in
  is-active) [ "$(state)" = active ] ;;
  is-enabled) enabled ;;
  enable|start|restart)
    if [ -e "$d/fail-start" ]; then echo inactive > "$d/active"; echo "Job for claude-rotate.service failed." >&2; exit 1; fi
    [ "$1" = enable ] && echo enabled > "$d/enabled"
    echo active > "$d/active" ;;
  stop)
    [ -e "$d/fail-stop" ] && exit 1
    [ -x "$d/on-stop" ] && "$d/on-stop"
    echo inactive > "$d/active" ;;
  disable) echo disabled > "$d/enabled"; case "$*" in *--now*) echo inactive > "$d/active" ;; esac ;;
  show) printf 'ActiveState=%s\nSubState=running\nNRestarts=0\nUnitFileState=%s\nMainPID=0\n' "$(state)" "$(enabled)" ;;
  daemon-reload|reset-failed) ;;
  *) echo "fake systemctl: unexpected $*" >&2; exit 9 ;;
esac
"""


class Fixture:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-rotate-admin-test-"))
        self.prefix = self.tmp / "prefix"
        self.prefix.mkdir()
        (self.prefix / "etc/systemd/system").mkdir(parents=True)
        (self.prefix / "etc/systemd/system/claude-rotate.service").write_text(UNIT_TEXT)
        self.fake = self.tmp / "fake-systemd"
        self.fake.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.bin / "systemctl").write_text(FAKE_SYSTEMCTL)
        (self.bin / "systemctl").chmod(0o700)
        self.env = dict(os.environ, CLAUDE_ROTATE_PREFIX=str(self.prefix), FAKE_SYSTEMD_DIR=str(self.fake),
                        PATH=str(self.bin) + os.pathsep + os.environ["PATH"])
        self.etc = self.prefix / "etc/claude-rotate"
        self.config = self.etc / "config.json"
        self.tokens = self.etc / "tokens"
        self.state_dir = self.prefix / "var/lib/claude-rotate"
        self.state = self.state_dir / "state.json"

    def run(self, *args, input=None, env=None):
        return subprocess.run([sys.executable, "-I", str(ADMIN), *args], env=env or self.env, input=input,
                              text=True, capture_output=True, timeout=60)

    def calls(self):
        log = self.fake / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def reset_calls(self):
        (self.fake / "calls.log").unlink(missing_ok=True)

    def set_active(self, active):
        (self.fake / "active").write_text("active" if active else "inactive")

    def cfg(self):
        return json.loads(self.config.read_text())

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_init_creates_empty_config_and_modes(f):
    r = f.run("init")
    assert r.returncode == 3, r
    assert "setup needed" in r.stdout and "add-token" in r.stdout and "add-user" in r.stdout
    cfg = f.cfg()
    assert cfg["accounts"] == [] and cfg["devices"] == {}
    assert "REPLACE-WITH" not in f.config.read_text()
    assert mode(f.config) == 0o640 and mode(f.etc) == 0o750 and mode(f.tokens) == 0o750
    assert f.run("check").returncode == 3
    # Adoption normalizes modes and keeps content.
    f.config.chmod(0o600)
    f.tokens.chmod(0o700)
    r = f.run("init")
    assert r.returncode == 3 and "adopted" in r.stdout
    assert mode(f.config) == 0o640 and mode(f.tokens) == 0o750
    # Fresh unconfigured install: activate must leave the unit disabled, never loop.
    f.reset_calls()
    r = f.run("activate")
    assert r.returncode == 3 and "setup needed" in r.stdout, r
    assert any(c.startswith("disable --now") for c in f.calls())
    assert not any(c.startswith(("start", "restart", "enable")) for c in f.calls())


def test_invalid_names_do_nothing(f):
    before = f.config.read_bytes()
    for bad in ("two words", "quote'name", "../outside", "a" * 65, "-leading", ""):
        for cmd in (("add-user", bad), ("add-token", bad), ("remove-user", bad), ("remove-token", bad), ("rotate", bad)):
            r = f.run(*cmd, input=TOKEN)
            assert r.returncode == 1, (cmd, r)
            assert not bad or bad not in r.stdout
    assert f.config.read_bytes() == before
    assert list(f.tokens.iterdir()) == []


def test_add_token_and_renewal(f):
    f.set_active(False)
    f.reset_calls()
    r = f.run("add-token", "acct-1", input=TOKEN + "\n")
    assert r.returncode == 3, r  # stored, but no device yet: setup needed, service disabled
    assert TOKEN not in r.stdout + r.stderr
    token_file = f.tokens / "acct-1.token"
    assert token_file.read_text() == TOKEN and mode(token_file) == 0o640
    assert f.cfg()["accounts"] == [{"name": "acct-1", "token_file": "tokens/acct-1.token"}]
    assert not any(c.startswith(("start", "restart", "enable")) for c in f.calls())
    # Bad tokens are refused without touching anything.
    before = token_file.read_bytes()
    for bad in ("", "sk-ant-oat01-", "not-a-token", "sk-ant-oat01-with space", "sk-ant-oat01-ok\nextra line"):
        r = f.run("add-token", "acct-1", input=bad)
        assert r.returncode == 1, (bad, r)
        assert token_file.read_bytes() == before
    # Renewal keeps account settings and replaces only the token.
    cfg = f.cfg()
    cfg["accounts"][0]["disabled"] = True
    cfg["accounts"][0]["priority"] = 5
    f.config.write_text(json.dumps(cfg))
    renewed = TOKEN.replace("0001", "0002")
    r = f.run("add-token", "acct-1", "--no-restart", input=renewed)
    assert r.returncode == 0 and "replaced" in r.stdout, r
    assert token_file.read_text() == renewed
    assert f.cfg()["accounts"][0] == {"name": "acct-1", "token_file": "tokens/acct-1.token", "disabled": True, "priority": 5}
    cfg["accounts"][0].pop("disabled")
    f.config.write_text(json.dumps(cfg))
    # --token-file and the add-account alias.
    source = f.tmp / "second.token"
    source.write_text(TOKEN.replace("0001", "0003") + "\n")
    r = f.run("add-account", "acct-2", "--token-file", str(source), "--no-restart")
    assert r.returncode == 0, r
    assert (f.tokens / "acct-2.token").read_text() == TOKEN.replace("0001", "0003")
    assert [a["name"] for a in f.cfg()["accounts"]] == ["acct-1", "acct-2"]


def test_add_user_activation_and_duplicates(f):
    f.set_active(False)
    f.reset_calls()
    r = f.run("add-user", "alice-laptop")
    assert r.returncode == 0, r
    cfg = f.cfg()
    key = cfg["devices"]["alice-laptop"]
    assert key.startswith("dev-alice-laptop-") and len(key) == len("dev-alice-laptop-") + 16
    assert f"export CLAUDE_CODE_OAUTH_TOKEN={key}" in r.stdout
    assert "export ANTHROPIC_BASE_URL=" in r.stdout
    assert "is live" in r.stdout
    assert any(c.startswith("enable --now") for c in f.calls())
    # Duplicate: refused, nothing changes, no service action.
    before = f.config.read_bytes()
    f.reset_calls()
    r = f.run("add-user", "alice-laptop")
    assert r.returncode == 1 and f.config.read_bytes() == before, r
    assert "already exists" in r.stderr and f.calls() == []
    # Active service: a second device restarts and is live; alias accepted.
    f.set_active(True)
    f.reset_calls()
    r = f.run("add-device", "bob-desktop")
    assert r.returncode == 0 and "restarted" in r.stdout and "is live" in r.stdout, r
    assert any(c.startswith("restart") for c in f.calls())
    assert set(f.cfg()["devices"]) == {"alice-laptop", "bob-desktop"}
    assert f.cfg()["devices"]["alice-laptop"] == key


def test_readiness_needs_enabled_nonempty_token(f):
    cfg = f.cfg()
    saved = json.dumps(cfg)
    for acct in cfg["accounts"]:
        acct["disabled"] = True
    f.config.write_text(json.dumps(cfg))
    r = f.run("check")
    assert r.returncode == 3 and "no enabled account" in r.stdout, r
    f.config.write_text(saved)
    tokens = {a["name"]: (f.tokens / f"{a['name']}.token").read_bytes() for a in cfg["accounts"]}
    (f.tokens / "acct-2.token").write_bytes(b"")
    r = f.run("check")
    assert r.returncode == 0 and "empty" in r.stdout, r  # acct-1 still carries the service
    (f.tokens / "acct-1.token").write_bytes(b"")
    r = f.run("check")
    assert r.returncode == 3, r
    for name, data in tokens.items():
        (f.tokens / f"{name}.token").write_bytes(data)
    assert f.run("check").returncode == 0


def test_inherited_lock_descriptor(f):
    lock = f.prefix / "run/claude-rotate.lock"
    script = (
        "import fcntl, os, subprocess, sys\n"
        f"fd = os.open({str(lock)!r}, os.O_RDWR | os.O_CREAT, 0o600)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX)\n"
        "env = dict(os.environ, CLAUDE_ROTATE_LOCK_FD=str(fd))\n"
        f"r = subprocess.run([sys.executable, '-I', {str(ADMIN)!r}, 'check'], env=env, pass_fds=[fd], timeout=20)\n"
        "sys.exit(r.returncode)\n")
    r = subprocess.run([sys.executable, "-c", script], env=f.env, text=True, capture_output=True, timeout=60)
    assert r.returncode == 0, r  # no deadlock: the child reused the parent's held lock
    # A descriptor for another file, or for a lock held by a third party, is refused.
    other = f.tmp / "other.lock"
    other.write_text("")
    script = (
        "import fcntl, os, subprocess, sys\n"
        f"fd = os.open({str(other)!r}, os.O_RDWR)\n"
        "env = dict(os.environ, CLAUDE_ROTATE_LOCK_FD=str(fd))\n"
        f"r = subprocess.run([sys.executable, '-I', {str(ADMIN)!r}, 'add-user', 'nope'], env=env, pass_fds=[fd], timeout=20, capture_output=True, text=True)\n"
        "print(r.stderr); sys.exit(r.returncode)\n")
    r = subprocess.run([sys.executable, "-c", script], env=f.env, text=True, capture_output=True, timeout=60)
    assert r.returncode == 1 and "is not" in r.stdout, r
    assert "nope" not in f.cfg()["devices"]


def test_restart_failure_is_reported(f):
    (f.fake / "fail-start").write_text("")
    try:
        f.set_active(True)
        r = f.run("add-user", "carol-phone")
        assert r.returncode == 2, r
        assert "NOT live" in r.stdout and "FAILED" in r.stdout and "journalctl" in r.stdout
        assert "is live" not in r.stdout
        assert "carol-phone" in f.cfg()["devices"]  # stored, honestly reported as not live
    finally:
        (f.fake / "fail-start").unlink()
    r = f.run("remove-user", "carol-phone", "--no-restart")
    assert r.returncode == 0 and "NOT live until" in r.stdout
    # A missing unit cannot be live either.
    unit = f.prefix / "etc/systemd/system/claude-rotate.service"
    unit.rename(unit.with_suffix(".bak"))
    try:
        r = f.run("add-user", "dave-tablet")
        assert r.returncode == 2 and "unit is not installed" in r.stdout, r
    finally:
        unit.with_suffix(".bak").rename(unit)
    f.run("remove-user", "dave-tablet", "--no-restart")


def test_concurrent_add_users_are_serialized(f):
    f.set_active(True)
    names = [f"worker-{i}" for i in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda n: f.run("add-user", n, "--no-restart"), names))
    assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
    devices = f.cfg()["devices"]
    assert all(n in devices for n in names)
    assert len(set(devices.values())) == len(devices)
    for n in names:
        assert f.run("remove-user", n, "--no-restart").returncode == 0
    assert not any(n in f.cfg()["devices"] for n in names)


def test_status_has_no_secrets(f):
    f.set_active(False)
    r = f.run("status", "--json")
    assert r.returncode == 0, r
    report = json.loads(r.stdout)
    text = r.stdout
    assert TOKEN not in text and TOKEN.replace("0001", "0002") not in text
    for key in f.cfg()["devices"].values():
        assert key not in text
        assert key[-4:] in text
    assert report["ready"] is True
    acct = report["accounts"][0]
    assert acct["token_file_present"] is True and "valid" not in json.dumps(acct)
    assert report["active_account"] == "acct-1"
    human = f.run("status")
    assert human.returncode == 0 and "token file present" in human.stdout and TOKEN not in human.stdout
    assert "token validity is only visible through real upstream traffic" in human.stdout


def test_status_live_query_uses_header(f):
    seen = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen["path"] = self.path
            seen["auth"] = self.headers.get("Authorization")
            body = json.dumps({"active_account": "acct-2", "storage_errors": {"state_write": {"count": 3}},
                               "accounts": {"acct-1": {"effective_util_5h": 0.1}}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cfg = f.cfg()
        cfg["port"] = server.server_address[1]
        f.config.write_text(json.dumps(cfg))
        f.set_active(True)
        r = f.run("status", "--json")
        assert r.returncode == 0, r
        report = json.loads(r.stdout)
        assert seen["path"] == "/rotate/status" and seen["auth"].startswith("Bearer dev-")
        assert report["live"]["active_account"] == "acct-2"
        assert report["live"]["storage_errors"] == {"state_write": {"count": 3}}
        human = f.run("status")
        assert "storage_errors" in human.stdout and seen["auth"][7:] not in human.stdout
    finally:
        server.shutdown()
        server.server_close()


def write_state(f, state):
    f.state_dir.mkdir(parents=True, exist_ok=True)
    f.state.write_text(json.dumps(state))


def test_rotate_rejections_do_not_mutate(f):
    cfg = f.cfg()
    cfg["accounts"] = [{"name": "acct-1", "token_file": "tokens/acct-1.token"},
                       {"name": "acct-2", "token_file": "tokens/acct-2.token"},
                       {"name": "acct-3", "token_file": "tokens/acct-3.token", "disabled": True},
                       {"name": "acct-4", "token_file": "tokens/acct-4.token"}]
    f.config.write_text(json.dumps(cfg))
    (f.tokens / "acct-4.token").unlink(missing_ok=True)
    write_state(f, {"active_account": "acct-1", "accounts": {"acct-2": {"util_5h": 0.95, "reset_5h": time.time() + 3600}},
                    "events": [], "custom": "keep"})
    before_state, before_cfg = f.state.read_bytes(), f.config.read_bytes()
    f.set_active(True)
    f.reset_calls()
    for target, reason in (("nonexistent", "not in the configuration"), ("acct-3", "disabled"),
                           ("acct-4", "missing"), ("acct-2", "5h window")):
        r = f.run("rotate", target)
        assert r.returncode == 1 and reason in r.stderr, (target, r)
    r = f.run("rotate")  # every other account is ineligible
    assert r.returncode == 1 and "no other eligible" in r.stderr, r
    r = f.run("rotate", "acct-1")
    assert r.returncode == 0 and "already the active" in r.stdout
    assert f.state.read_bytes() == before_state and f.config.read_bytes() == before_cfg
    assert f.calls() == []  # never stopped the service for a refusal


def test_rotate_switches_after_stop_and_preserves_state(f):
    hook = f.fake / "on-stop"
    hook.write_text("#!/bin/sh\n" + f"{sys.executable} - <<'EOF'\n"
                    "import json\n"
                    f"p = {str(f.state)!r}\n"
                    "s = json.load(open(p))\n"
                    "s['accounts']['acct-1'] = {'util_5h': 0.42, 'last_seen': 1.0}\n"
                    "s['drained'] = True\n"
                    "json.dump(s, open(p, 'w'))\nEOF\n")
    hook.chmod(0o700)
    write_state(f, {"active_account": "acct-1", "accounts": {"acct-2": {"util_5h": 0.1}}, "events": [], "custom": "keep"})
    f.set_active(True)
    f.reset_calls()
    r = f.run("rotate")
    assert r.returncode == 0, r
    assert "active account is now 'acct-2'" in r.stdout and "cooldown" in r.stdout
    calls = f.calls()
    assert calls.index("stop claude-rotate.service") < calls.index("start claude-rotate.service")
    state = json.loads(f.state.read_text())
    assert state["active_account"] == "acct-2" and state["custom"] == "keep"
    assert state["drained"] is True and state["accounts"]["acct-1"]["util_5h"] == 0.42  # written during stop, kept
    assert state["accounts"]["acct-2"] == {"util_5h": 0.1}
    assert abs(state["last_switch_ts"] - time.time()) < 60
    assert state["events"][-1]["reason"] == "manual" and state["events"][-1]["to"] == "acct-2"
    assert mode(f.state) == 0o600
    hook.unlink()
    # Explicit target and wrap-around while stopped: no systemctl start/stop.
    f.set_active(False)
    f.reset_calls()
    r = f.run("rotate", "acct-1")
    assert r.returncode == 0 and "applies at the next start" in r.stdout, r
    assert json.loads(f.state.read_text())["active_account"] == "acct-1"
    assert not any(c.startswith(("stop", "start")) for c in f.calls())
    # Start failure after the switch is reported with the previous account.
    f.set_active(True)
    (f.fake / "fail-start").write_text("")
    try:
        r = f.run("rotate", "acct-2")
        assert r.returncode == 2 and "FAILED" in r.stdout and "'acct-1'" in r.stdout and "journalctl" in r.stdout, r
        assert json.loads(f.state.read_text())["active_account"] == "acct-2"
    finally:
        (f.fake / "fail-start").unlink()
    # Stop failure: nothing changes.
    (f.fake / "fail-stop").write_text("")
    try:
        f.set_active(True)
        before = f.state.read_bytes()
        r = f.run("rotate", "acct-1")
        assert r.returncode == 1 and "stopped state" in r.stderr and f.state.read_bytes() == before, r
    finally:
        (f.fake / "fail-stop").unlink()
    # A unit that is activating (auto-restart) is stopped before the file is written.
    (f.fake / "active").write_text("activating")
    f.reset_calls()
    r = f.run("rotate", "acct-1")
    assert r.returncode == 0, r
    calls = f.calls()
    assert calls.index("stop claude-rotate.service") < calls.index("start claude-rotate.service")
    assert json.loads(f.state.read_text())["active_account"] == "acct-1"
    # Eligibility is re-checked against the drained state; the service is started again.
    hook = f.fake / "on-stop"
    hook.write_text("#!/bin/sh\n" + f"{sys.executable} - <<'EOF'\n"
                    "import json, time\n"
                    f"p = {str(f.state)!r}\n"
                    "s = json.load(open(p))\n"
                    "s['accounts']['acct-2'] = {'util_5h': 0.99, 'reset_5h': time.time() + 3600}\n"
                    "json.dump(s, open(p, 'w'))\nEOF\n")
    hook.chmod(0o700)
    f.set_active(True)
    f.reset_calls()
    r = f.run("rotate", "acct-2")
    assert r.returncode == 1 and "no longer eligible" in r.stderr and "service started again" in r.stderr, r
    assert json.loads(f.state.read_text())["active_account"] == "acct-1"
    assert "start claude-rotate.service" in f.calls()
    hook.unlink()
    # No state file yet: the default shape is created.
    f.state.unlink()
    f.set_active(False)
    r = f.run("rotate", "acct-2")
    assert r.returncode == 0, r
    assert json.loads(f.state.read_text())["accounts"].keys() >= {"acct-1", "acct-2"}


def test_remove_token_and_setup_needed(f):
    cfg = f.cfg()
    cfg["accounts"] = [a for a in cfg["accounts"] if a["name"] in ("acct-1", "acct-2")]
    f.config.write_text(json.dumps(cfg))
    # A symlinked token file is never deleted.
    outside = f.tmp / "outside.token"
    outside.write_text("keep")
    link = f.tokens / "acct-2.token"
    link.unlink()
    link.symlink_to(outside)
    r = f.run("remove-token", "acct-2", "--no-restart")
    assert r.returncode == 0 and "left in place" in r.stdout, r
    assert outside.exists() and link.is_symlink()
    link.unlink()
    assert [a["name"] for a in f.cfg()["accounts"]] == ["acct-1"]
    r = f.run("remove-token", "acct-2")
    assert r.returncode == 1 and "does not exist" in r.stderr
    # Removing the last account: service stopped and disabled, setup needed, exit 3.
    f.set_active(True)
    f.reset_calls()
    r = f.run("remove-account", "acct-1")
    assert r.returncode == 3 and "setup needed" in r.stdout and "stopped and disabled" in r.stdout, r
    assert not (f.tokens / "acct-1.token").exists() and f.cfg()["accounts"] == []
    assert any(c.startswith("disable --now") for c in f.calls())
    r = f.run("remove-user", "nobody")
    assert r.returncode == 1 and "does not exist" in r.stderr


def test_set_url_and_symlinked_config(f):
    for bad in ("ftp://x", "host.example", "https://", "https://h ost"):
        assert f.run("set-url", bad).returncode == 1, bad
    r = f.run("set-url", "https://proxy.tail1234.ts.net/")
    assert r.returncode == 0 and f.cfg()["public_url"] == "https://proxy.tail1234.ts.net"
    r = f.run("add-user", "eve-laptop", "--no-restart")
    assert "export ANTHROPIC_BASE_URL=https://proxy.tail1234.ts.net" in r.stdout
    assert "/rotate/panel?key=" in r.stdout
    real = f.config.read_bytes()
    f.config.unlink()
    (f.tmp / "elsewhere.json").write_bytes(real)
    f.config.symlink_to(f.tmp / "elsewhere.json")
    try:
        r = f.run("add-user", "mallory")
        assert r.returncode == 1 and "symbolic link" in r.stderr, r
        assert "mallory" not in (f.tmp / "elsewhere.json").read_text()
    finally:
        f.config.unlink()
        f.config.write_bytes(real)


def main():
    f = Fixture()
    order = ["test_init_creates_empty_config_and_modes", "test_invalid_names_do_nothing",
             "test_add_token_and_renewal", "test_add_user_activation_and_duplicates",
             "test_readiness_needs_enabled_nonempty_token", "test_inherited_lock_descriptor",
             "test_restart_failure_is_reported", "test_concurrent_add_users_are_serialized",
             "test_status_has_no_secrets", "test_status_live_query_uses_header",
             "test_rotate_rejections_do_not_mutate", "test_rotate_switches_after_stop_and_preserves_state",
             "test_remove_token_and_setup_needed", "test_set_url_and_symlinked_config"]
    tests = [(n, globals()[n]) for n in order]  # the fixture is built up step by step
    assert sorted(order) == sorted(n for n, g in globals().items() if n.startswith("test_") and callable(g))
    try:
        for name, fn in tests:
            fn(f)
            print(f"ok   {name}")
    finally:
        f.cleanup()
    print(f"{len(tests)} admin checks passed")


if __name__ == "__main__":
    main()
