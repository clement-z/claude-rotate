"""claude-rotate-admin: root-only administration of an installed claude-rotate service.

Standard library only. Shipped inside every release as deploy/claude_rotate_admin.py
and run through /usr/local/sbin/claude-rotate-admin from any working directory.
It edits /etc/claude-rotate (config and tokens), rewrites the saved rotation state
only while the service is stopped, and drives systemd. Tokens are read from a hidden
prompt, stdin or a file, never from the command line, and are never printed.

CLAUDE_ROTATE_PREFIX prefixes every fixed path; it exists for tests and must be
empty on a real host.
"""

import argparse
import fcntl
import getpass
import grp
import json
import os
import pwd
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PREFIX = os.environ.get("CLAUDE_ROTATE_PREFIX", "")


def P(path: str) -> Path:
    return Path(PREFIX + path) if PREFIX else Path(path)


ETC = P("/etc/claude-rotate")
CONFIG = ETC / "config.json"
TOKENS = ETC / "tokens"
STATE_DIR = P("/var/lib/claude-rotate")
STATE = STATE_DIR / "state.json"
OPT = P("/opt/claude-rotate")
CURRENT = OPT / "current"
RELEASES = OPT / "releases"
UNIT = P("/etc/systemd/system/claude-rotate.service")
UNIT_NAME = "claude-rotate.service"
# One lock serializes install.sh, uninstall.sh and every admin command.
LOCK = P("/run/claude-rotate.lock")
SERVICE_USER = "claude-rotate"
SERVICE_GROUP = "claude-rotate"
EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.json"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
TOKEN_PREFIX = "sk-ant-oat01-"
PLACEHOLDER = "REPLACE-WITH"
EXIT_OK, EXIT_REFUSED, EXIT_NOT_LIVE, EXIT_SETUP_NEEDED = 0, 1, 2, 3


class Refused(Exception):
    """Nothing was changed; the message explains why."""


def out(*parts):
    print(*parts, flush=True)


def err(*parts):
    print(*parts, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- identity, paths


def require_root():
    if os.geteuid() != 0 and not PREFIX:
        raise Refused("claude-rotate-admin must run as root: sudo claude-rotate-admin ...")


def service_ids():
    """(uid, gid) of the service identity; falls back to the caller under the test prefix."""
    try:
        return pwd.getpwnam(SERVICE_USER).pw_uid, grp.getgrnam(SERVICE_GROUP).gr_gid
    except KeyError:
        if PREFIX and os.geteuid() != 0:
            return os.getuid(), os.getgid()
        raise Refused(f"service identity '{SERVICE_USER}' is missing; run install.sh first")


def owner_uid():
    return 0 if os.geteuid() == 0 else os.getuid()


def refuse_symlink(path: Path, what: str):
    if path.is_symlink():
        raise Refused(f"{what} {path} is a symbolic link; refusing to operate through it")


def check_fixed_tree():
    """Every managed path and its ancestors inside the prefix must be real directories."""
    for path in (ETC, TOKENS, STATE_DIR, OPT, RELEASES, UNIT.parent, LOCK.parent):
        probe = path
        while True:
            if probe.is_symlink():
                raise Refused(f"{probe} is a symbolic link; refusing to operate through it")
            if str(probe) == PREFIX or probe == probe.parent or (PREFIX and not str(probe).startswith(PREFIX + "/")):
                break
            probe = probe.parent
    refuse_symlink(CONFIG, "config")
    refuse_symlink(STATE, "state file")
    refuse_symlink(CURRENT.parent, "release root")


class AdminLock:
    """Blocking exclusive lock shared with install.sh and uninstall.sh (flock on one file).

    install.sh already holds the lock on file descriptor CLAUDE_ROTATE_LOCK_FD when it
    calls the admin; that descriptor is validated (same inode as the lock file, lock
    obtainable on its open file description) and reused instead of a second flock,
    which would deadlock against the parent. It is never unlocked here, because an
    unlock would release the parent's lock too.
    """

    def __init__(self):
        self.fd = None
        self.inherited = False

    def __enter__(self):
        LOCK.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        refuse_symlink(LOCK, "lock file")
        inherited = os.environ.get("CLAUDE_ROTATE_LOCK_FD")
        if inherited:
            try:
                fd = int(inherited)
                held = os.fstat(fd)
                target = os.stat(LOCK)
            except (ValueError, OSError):
                raise Refused("CLAUDE_ROTATE_LOCK_FD does not refer to an open lock file")
            if (held.st_dev, held.st_ino) != (target.st_dev, target.st_ino):
                raise Refused(f"CLAUDE_ROTATE_LOCK_FD is not {LOCK}")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refused("the inherited lock descriptor is not held by the caller; "
                              "another claude-rotate command is running")
            self.fd, self.inherited = fd, True
            return self
        self.fd = os.open(LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        if not self.inherited:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
        self.fd = None


# ---------------------------------------------------------------- systemd


def systemctl_available():
    return shutil.which("systemctl") is not None


def systemctl(*args, timeout=90):
    return subprocess.run(["systemctl", *args], text=True, capture_output=True, timeout=timeout)


def is_active():
    return systemctl("is-active", "--quiet", UNIT_NAME).returncode == 0


QUIESCENT = {"inactive", "failed"}


def active_state():
    """systemd ActiveState, or 'unknown'. Only inactive/failed mean no process can start."""
    return unit_properties().get("ActiveState") or "unknown"


def unit_installed():
    return UNIT.is_file()


def unit_properties():
    props = {}
    if not systemctl_available():
        return props
    result = systemctl("show", UNIT_NAME, "-p", "ActiveState", "-p", "SubState", "-p", "NRestarts",
                       "-p", "UnitFileState", "-p", "MainPID")
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            props[key] = value
    return props


def journal_hint():
    return f"inspect: journalctl -u {UNIT_NAME} -n 50 --no-pager"


# ---------------------------------------------------------------- config


def validate_name(name: str, what="name"):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise Refused(f"invalid {what} {name!r}: use 1-64 ASCII letters, digits, '_' or '-', "
                      "starting with a letter or digit")


def token_path(acct: dict) -> Path:
    # Same resolution as rotator.load_config: relative to the config directory.
    return CONFIG.parent / str(acct.get("token_file", ""))


def inside_tokens_dir(path: Path) -> bool:
    try:
        resolved_parent = path.parent.resolve(strict=True)
    except OSError:
        return False
    try:
        tokens = TOKENS.resolve(strict=True)
    except OSError:
        return False
    return resolved_parent == tokens and not path.is_symlink()


def load_config() -> dict:
    refuse_symlink(CONFIG, "config")
    if not CONFIG.is_file():
        raise Refused(f"{CONFIG} does not exist; run install.sh (or claude-rotate-admin init)")
    try:
        cfg = json.loads(CONFIG.read_text())
    except (OSError, ValueError) as exc:
        raise Refused(f"{CONFIG} is not valid JSON ({exc}); fix it before using the admin")
    if not isinstance(cfg, dict):
        raise Refused(f"{CONFIG} must contain a JSON object")
    accounts = cfg.setdefault("accounts", [])
    devices = cfg.setdefault("devices", {})
    if not isinstance(accounts, list) or not all(isinstance(a, dict) and isinstance(a.get("name"), str)
                                                  and isinstance(a.get("token_file"), str) for a in accounts):
        raise Refused("config 'accounts' must be a list of objects with string 'name' and 'token_file'")
    if not isinstance(devices, dict):
        raise Refused("config 'devices' must be an object mapping device names to keys")
    names = [a["name"] for a in accounts]
    if len(set(names)) != len(names):
        raise Refused("config 'accounts' has duplicate names")
    return cfg


def write_config(cfg: dict):
    _, gid = service_ids()
    fd, tmp = tempfile.mkstemp(dir=ETC, prefix=".config.json.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(cfg, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(tmp, owner_uid(), gid)
        os.chmod(tmp, 0o640)
        os.replace(tmp, CONFIG)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    fsync_dir(ETC)


def fsync_dir(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_private_file(path: Path, data: str, mode: int, uid: int, gid: int):
    """Atomic replace inside path.parent with ownership/mode set before the rename."""
    refuse_symlink(path, "target")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(tmp, uid, gid)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    fsync_dir(path.parent)


def token_status(acct: dict) -> dict:
    path = token_path(acct)
    info = {"token_file": acct.get("token_file"), "present": False}
    try:
        st = os.lstat(path)
    except OSError:
        return info
    info["present"] = stat.S_ISREG(st.st_mode)
    info["symlink"] = stat.S_ISLNK(st.st_mode)
    info["bytes"] = st.st_size
    info["mode"] = oct(st.st_mode & 0o777)
    return info


def readiness(cfg: dict):
    """Can the service start and serve: at least one enabled account with a non-empty
    token file, and only valid device keys (mirrors rotator.load_config, stricter on
    disabled/empty tokens)."""
    problems, warnings = [], []
    usable = []
    for acct in cfg["accounts"]:
        status = token_status(acct)
        if not status["present"]:
            warnings.append(f"account '{acct['name']}' has no token file ({acct.get('token_file')}); "
                            "it is skipped at startup")
        elif status.get("bytes") == 0:
            warnings.append(f"token file for account '{acct['name']}' is empty")
        elif acct.get("disabled"):
            warnings.append(f"account '{acct['name']}' is disabled")
        else:
            usable.append(acct["name"])
    if not usable:
        problems.append("no enabled account with a non-empty token file: sudo claude-rotate-admin add-token NAME")
    devices = cfg["devices"]
    if not devices:
        problems.append("no device key: sudo claude-rotate-admin add-user NAME")
    bad = [k for k, v in devices.items() if not isinstance(v, str) or not v.strip()]
    if bad:
        problems.append(f"device(s) with an empty key: {', '.join(bad)}")
    placeholders = [k for k, v in devices.items() if isinstance(v, str) and PLACEHOLDER in v]
    if placeholders:
        problems.append(f"placeholder device key(s) from the example config: {', '.join(placeholders)}; "
                        "remove them (remove-user) and use add-user")
    keys = [v for v in devices.values() if isinstance(v, str)]
    if len(set(keys)) != len(keys):
        problems.append("device keys are not unique")
    return not problems, problems, warnings


def public_url(cfg: dict) -> str:
    url = cfg.get("public_url")
    return url if isinstance(url, str) and url else "http://<this-host>:8484"


def bind_address(cfg: dict):
    host = cfg.get("bind", "127.0.0.1")
    if not isinstance(host, str) or host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    port = cfg.get("port", 8484)
    return host, port if isinstance(port, int) else 8484


# ---------------------------------------------------------------- state (rotation)


def read_state():
    refuse_symlink(STATE, "state file")
    if not STATE.exists():
        return None
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError) as exc:
        raise Refused(f"{STATE} is not valid JSON ({exc}); the service cannot start either. "
                      "Stop the service and move the file aside to reset rotation state")
    if not isinstance(state, dict):
        raise Refused(f"{STATE} must contain a JSON object")
    return state


def default_state(cfg: dict) -> dict:
    # Same shape as rotator.load_state when no file exists.
    first = cfg["accounts"][0]["name"] if cfg["accounts"] else None
    return {"active_account": first, "accounts": {a["name"]: {} for a in cfg["accounts"]}, "events": []}


def write_state(state: dict):
    uid, gid = service_ids()
    if not STATE_DIR.exists():
        STATE_DIR.mkdir(mode=0o700)
        os.chown(STATE_DIR, uid, gid)
    refuse_symlink(STATE_DIR, "state directory")
    write_private_file(STATE, json.dumps(state, indent=2), 0o600, uid, gid)


def effective_utilization(info: dict, now: float):
    """(util_5h, util_7d) with a passed reset counting as zero, like rotator."""
    util_5h = info.get("util_5h", 0.0) or 0.0
    util_7d = info.get("util_7d", 0.0) or 0.0
    if info.get("reset_5h") and now >= info["reset_5h"]:
        util_5h = 0.0
    if info.get("reset_7d") and now >= info["reset_7d"]:
        util_7d = 0.0
    return util_5h, util_7d


def account_eligibility(cfg: dict, state, name: str):
    """Why an account cannot be made active, or [] when it can."""
    by_name = {a["name"]: a for a in cfg["accounts"]}
    acct = by_name.get(name)
    if acct is None:
        return [f"account '{name}' is not in the configuration"]
    reasons = []
    if acct.get("disabled"):
        reasons.append("disabled in config")
    status = token_status(acct)
    if not status["present"]:
        reasons.append(f"token file {acct.get('token_file')} is missing")
    elif status.get("bytes") == 0:
        reasons.append("token file is empty")
    info = ((state or {}).get("accounts") or {}).get(name) or {}
    if isinstance(info, dict):
        util_5h, util_7d = effective_utilization(info, time.time())
        threshold = cfg.get("switch_threshold", 0.8)
        threshold_7d = cfg.get("switch_threshold_7d", 0.98)
        if util_5h >= threshold:
            reasons.append(f"5h window at {util_5h:.0%} (threshold {threshold:.0%})")
        if util_7d >= threshold_7d:
            reasons.append(f"7d window at {util_7d:.0%} (threshold {threshold_7d:.0%})")
    return reasons


# ---------------------------------------------------------------- activation


def activate(cfg: dict, *, restart: bool = True, what: str = "change") -> int:
    """Bring the unit in line with the configuration. Returns the exit code."""
    ready, problems, warnings = readiness(cfg)
    for warning in warnings:
        out(f"warning: {warning}")
    if not restart:
        out(f"{what} stored; NOT live until: sudo systemctl restart {UNIT_NAME}")
        return EXIT_OK
    if not systemctl_available() or not unit_installed():
        out(f"{what} stored; the systemd unit is not installed, so nothing is live. Run install.sh.")
        return EXIT_NOT_LIVE
    if not ready:
        active = is_active()
        result = systemctl("disable", "--now", UNIT_NAME)
        if result.returncode == 0:
            state = "stopped and disabled" if active else "left disabled"
            out(f"{what} stored; service {state}: setup needed")
        else:
            out(f"{what} stored; could not disable the service: {result.stderr.strip()}")
        for problem in problems:
            out(f"  - {problem}")
        return EXIT_SETUP_NEEDED
    if is_active():
        result = systemctl("restart", UNIT_NAME)
        verb = "restarted"
    else:
        result = systemctl("enable", "--now", UNIT_NAME)
        verb = "enabled and started"
    if result.returncode != 0 or not is_active():
        detail = (result.stderr or result.stdout).strip()
        out(f"{what} stored but NOT live: service {verb.split()[0]} FAILED"
            + (f": {detail}" if detail else "") + f"; {journal_hint()}")
        return EXIT_NOT_LIVE
    out(f"service {verb}: {what} is live")
    return EXIT_OK


# ---------------------------------------------------------------- commands


def ensure_tree(cfg_required=False):
    """Create or normalize /etc/claude-rotate with service-readable modes."""
    uid, gid = owner_uid(), service_ids()[1]
    for directory in (ETC, TOKENS):
        refuse_symlink(directory, "directory")
        if not directory.exists():
            directory.mkdir(mode=0o750)
        os.chown(directory, uid, gid)
        os.chmod(directory, 0o750)
    if CONFIG.exists():
        refuse_symlink(CONFIG, "config")
        os.chown(CONFIG, uid, gid)
        os.chmod(CONFIG, 0o640)
    for entry in TOKENS.iterdir():
        st = os.lstat(entry)
        if stat.S_ISLNK(st.st_mode):
            out(f"warning: {entry} is a symbolic link; left unchanged")
        elif stat.S_ISREG(st.st_mode):
            os.chown(entry, uid, gid)
            os.chmod(entry, 0o640)


def cmd_init(args) -> int:
    with AdminLock():
        check_fixed_tree()
        ensure_tree()
        if CONFIG.exists():
            cfg = load_config()
            out(f"adopted existing {CONFIG}")
        else:
            if not EXAMPLE.is_file():
                raise Refused(f"{EXAMPLE} is missing; cannot create a default configuration")
            cfg = json.loads(EXAMPLE.read_text())
            cfg["accounts"] = []
            cfg["devices"] = {}
            cfg.setdefault("bind", "127.0.0.1")
            cfg.setdefault("public_url", "")
            write_config(cfg)
            out(f"created {CONFIG} with no accounts or devices")
        ready, problems, warnings = readiness(cfg)
        for warning in warnings:
            out(f"warning: {warning}")
        if ready:
            out("configuration complete")
            return EXIT_OK
        out("setup needed:")
        for problem in problems:
            out(f"  - {problem}")
        return EXIT_SETUP_NEEDED


def cmd_check(args) -> int:
    cfg = load_config()
    ready, problems, warnings = readiness(cfg)
    for warning in warnings:
        out(f"warning: {warning}")
    if ready:
        out("configuration complete")
        return EXIT_OK
    out("setup needed:")
    for problem in problems:
        out(f"  - {problem}")
    return EXIT_SETUP_NEEDED


def cmd_activate(args) -> int:
    with AdminLock():
        check_fixed_tree()
        cfg = load_config()
        return activate(cfg, what="configuration")


def read_token(args) -> str:
    if args.token_file:
        source = Path(args.token_file)
        if not source.is_file():
            raise Refused(f"token file {source} is not a regular file")
        data = source.read_text()
    elif sys.stdin.isatty():
        out("Run `claude setup-token` in a browser session logged into that account.")
        data = getpass.getpass("Paste the sk-ant-oat01-... token here (input hidden): ")
    else:
        data = sys.stdin.read()
    token = data.strip()
    if not token:
        raise Refused("no token given (stdin was empty)")
    if not token.startswith(TOKEN_PREFIX) or len(token) <= len(TOKEN_PREFIX):
        raise Refused(f"token should start with {TOKEN_PREFIX}; nothing stored")
    if any(ch.isspace() or not ch.isprintable() for ch in token):
        raise Refused("token contains whitespace or non-printable characters; nothing stored")
    return token


def cmd_add_token(args) -> int:
    validate_name(args.name, "account name")
    token = read_token(args)
    uid, gid = owner_uid(), service_ids()[1]
    with AdminLock():
        check_fixed_tree()
        cfg = load_config()
        if not TOKENS.is_dir():
            raise Refused(f"{TOKENS} is missing; run install.sh (or claude-rotate-admin init)")
        existing = next((a for a in cfg["accounts"] if a["name"] == args.name), None)
        if existing is not None:
            path = token_path(existing)
            if not inside_tokens_dir(path):
                raise Refused(f"account '{args.name}' uses token_file {existing['token_file']!r}, "
                              f"which is not a regular file inside {TOKENS}; fix the config first")
            write_private_file(path, token, 0o640, uid, gid)
            out(f"token for account '{args.name}' replaced ({path})")
            what = f"renewed token for '{args.name}'"
        else:
            path = TOKENS / f"{args.name}.token"
            if path.exists() or path.is_symlink():
                raise Refused(f"{path} already exists but no account uses it; remove it first")
            write_private_file(path, token, 0o640, uid, gid)
            cfg["accounts"].append({"name": args.name, "token_file": f"tokens/{args.name}.token"})
            write_config(cfg)
            out(f"account '{args.name}' added ({path})")
            what = f"account '{args.name}'"
        return activate(cfg, restart=not args.no_restart, what=what)


def print_device_block(cfg: dict, name: str, key: str):
    url = public_url(cfg)
    out(f"device '{name}' registered. On that machine set:")
    out(f"  export ANTHROPIC_BASE_URL={shlex.quote(url)}")
    out(f"  export CLAUDE_CODE_OAUTH_TOKEN={shlex.quote(key)}")
    out(f"Dashboard: {url}/rotate/panel?key={key}")
    if not cfg.get("public_url"):
        out("(set the real URL once with: sudo claude-rotate-admin set-url https://HOST)")
    out("The key is a bearer credential for this device only; do not share or commit it.")


def cmd_add_user(args) -> int:
    validate_name(args.name, "device name")
    with AdminLock():
        check_fixed_tree()
        cfg = load_config()
        if args.name in cfg["devices"]:
            raise Refused(f"device '{args.name}' already exists; nothing changed. "
                          "To issue a new key: remove-user then add-user")
        removed = [k for k, v in cfg["devices"].items() if isinstance(v, str) and PLACEHOLDER in v]
        for name in removed:
            del cfg["devices"][name]
        key = f"dev-{args.name}-{secrets.token_hex(8)}"
        while key in cfg["devices"].values():
            key = f"dev-{args.name}-{secrets.token_hex(8)}"
        cfg["devices"][args.name] = key
        write_config(cfg)
        for name in removed:
            out(f"removed placeholder device '{name}' from the example config")
        print_device_block(cfg, args.name, key)
        return activate(cfg, restart=not args.no_restart, what=f"key for '{args.name}'")


def cmd_remove_user(args) -> int:
    validate_name(args.name, "device name")
    with AdminLock():
        check_fixed_tree()
        cfg = load_config()
        if args.name not in cfg["devices"]:
            raise Refused(f"device '{args.name}' does not exist; nothing changed")
        del cfg["devices"][args.name]
        write_config(cfg)
        out(f"device '{args.name}' removed from the configuration")
        return activate(cfg, restart=not args.no_restart, what=f"revocation of '{args.name}'")


def cmd_remove_token(args) -> int:
    validate_name(args.name, "account name")
    with AdminLock():
        check_fixed_tree()
        cfg = load_config()
        acct = next((a for a in cfg["accounts"] if a["name"] == args.name), None)
        if acct is None:
            raise Refused(f"account '{args.name}' does not exist; nothing changed")
        cfg["accounts"] = [a for a in cfg["accounts"] if a["name"] != args.name]
        write_config(cfg)
        path = token_path(acct)
        if inside_tokens_dir(path) and path.is_file():
            path.unlink()
            out(f"account '{args.name}' removed; deleted {path}")
        else:
            out(f"account '{args.name}' removed; token file {acct.get('token_file')!r} left in place "
                f"(not a regular file inside {TOKENS})")
        return activate(cfg, restart=not args.no_restart, what=f"removal of '{args.name}'")


def cmd_set_url(args) -> int:
    url = args.url.strip()
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or any(ch.isspace() for ch in url):
        raise Refused("URL must look like https://host[:port] (http or https); nothing changed")
    with AdminLock():
        check_fixed_tree()
        cfg = load_config()
        cfg["public_url"] = url.rstrip("/")
        write_config(cfg)
    out(f"public_url set to {cfg['public_url']} (used in the add-user instructions; no restart needed)")
    return EXIT_OK


def choose_rotate_target(cfg: dict, state, requested):
    """Validate an explicit target or pick the next eligible account after the current one."""
    names = [a["name"] for a in cfg["accounts"]]
    if not names:
        raise Refused("no accounts configured; nothing changed")
    current = (state or {}).get("active_account")
    if current not in names:
        current = default_state(cfg)["active_account"]
    if requested:
        validate_name(requested, "account name")
        if requested not in names:
            raise Refused(f"account '{requested}' is not in the configuration; nothing changed")
        if requested == current:
            return current, None
        reasons = account_eligibility(cfg, state, requested)
        if reasons:
            raise Refused(f"account '{requested}' cannot be made active: {'; '.join(reasons)}; nothing changed")
        return current, requested
    start = names.index(current) + 1 if current in names else 0
    report = []
    for name in names[start:] + names[:start]:
        if name == current:
            continue
        reasons = account_eligibility(cfg, state, name)
        if not reasons:
            return current, name
        report.append(f"{name}: {'; '.join(reasons)}")
    raise Refused("no other eligible account; nothing changed" + (": " + " | ".join(report) if report else ""))


def cmd_rotate(args) -> int:
    with AdminLock():
        check_fixed_tree()
        cfg = load_config()
        # Refusals are decided before the service is touched.
        current, target = choose_rotate_target(cfg, read_state(), args.account)
        if target is None:
            out(f"account '{current}' is already the active account; nothing changed")
            return EXIT_OK
        managed = systemctl_available() and unit_installed()
        stopped_by_us = False
        if managed:
            before = active_state()
            if before not in QUIESCENT:
                # Any state but inactive/failed (including activating/auto-restart) can
                # start a process that would overwrite the file we are about to write.
                stopped = systemctl("stop", UNIT_NAME, timeout=120)
                stopped_by_us = True
                after = active_state()
                if stopped.returncode != 0 or after not in QUIESCENT:
                    detail = (stopped.stderr or stopped.stdout).strip()
                    raise Refused(f"could not bring {UNIT_NAME} to a stopped state (was {before}, now {after}"
                                  + (f": {detail}" if detail else "") + "); rotation state unchanged")

        def restore():
            if not stopped_by_us:
                return "the service was not running"
            started = systemctl("start", UNIT_NAME, timeout=120)
            if started.returncode == 0 and active_state() == "active":
                return "service started again"
            return f"service start FAILED; {journal_hint()}"

        try:
            # The shutdown drain may have saved newer state: decide again from the file.
            state = read_state()
            reasons = account_eligibility(cfg, state, target)
            if reasons:
                raise Refused(f"account '{target}' is no longer eligible after the service drained: "
                              f"{'; '.join(reasons)}; rotation state unchanged")
            if state is None:
                state = default_state(cfg)
            if not isinstance(state.get("accounts"), dict):
                state["accounts"] = {}
            if not isinstance(state.get("events"), list):
                state["events"] = []
            previous = state.get("active_account")
            now = time.time()
            state["active_account"] = target
            state["last_switch_ts"] = now
            util = effective_utilization(state["accounts"].get(previous) or {}, now)[0] if previous else 0.0
            state["events"].append({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "type": "switch",
                                    "from": previous, "to": target, "reason": "manual", "util": util})
            state["events"] = state["events"][-200:]
            write_state(state)
        except Refused as exc:
            err(f"error: {exc} ({restore()})")
            return EXIT_REFUSED
        except OSError as exc:
            out(f"could not write {STATE} ({exc}); active account unchanged ({restore()})")
            return EXIT_NOT_LIVE
        except BaseException:
            err(f"error: unexpected failure before the state was written ({restore()})")
            raise
        if not stopped_by_us:
            out(f"active account set to '{target}' (was '{previous}'); the service is not running, "
                "so it applies at the next start")
            return EXIT_OK
        note = restore()
        if note != "service started again":
            out(f"state now names '{target}' (was '{previous}') but the {note}")
            return EXIT_NOT_LIVE
        out(f"active account is now '{target}' (was '{previous}'); service restarted. "
            f"The switch cooldown ({cfg.get('switch_cooldown_s', 300)} s) applies; quota exhaustion "
            "or a disabled account can still move traffic immediately.")
        return EXIT_OK


# ---------------------------------------------------------------- status


def live_status(cfg: dict):
    keys = [v for v in cfg["devices"].values() if isinstance(v, str) and v.strip() and PLACEHOLDER not in v]
    if not keys:
        return None, "no device key available for an authenticated query"
    host, port = bind_address(cfg)
    url = f"http://{host}:{port}/rotate/status"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {keys[0]}"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 loopback only
            return json.loads(response.read().decode("utf-8")), None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}"


def release_info():
    info = {"current": None, "release": None}
    if CURRENT.is_symlink():
        target = os.readlink(CURRENT)
        info["current"] = target
        marker = Path(target if os.path.isabs(target) else CURRENT.parent / target) / ".claude-rotate-release"
        if marker.is_file():
            try:
                info["release"] = json.loads(marker.read_text())
            except ValueError:
                info["release"] = {"error": "unreadable marker"}
    return info


def collect_status():
    report = {"unit_installed": unit_installed(), "unit": unit_properties(), **release_info()}
    try:
        cfg = load_config()
    except Refused as exc:
        report.update(config_error=str(exc), ready=False)
        return report
    ready, problems, warnings = readiness(cfg)
    report.update(ready=ready, problems=problems, warnings=warnings,
                  public_url=cfg.get("public_url") or None, bind=list(bind_address(cfg)),
                  strategy=cfg.get("strategy", "consume-first"),
                  switch_threshold=cfg.get("switch_threshold", 0.8),
                  switch_threshold_7d=cfg.get("switch_threshold_7d", 0.98))
    try:
        state = read_state()
    except Refused as exc:
        state, report["state_error"] = None, str(exc)
    now = time.time()
    report["active_account"] = (state or {}).get("active_account") or default_state(cfg)["active_account"]
    report["active_account_source"] = "state file" if state else "default (first account)"
    report["last_switch_ts"] = (state or {}).get("last_switch_ts")
    accounts = []
    for acct in cfg["accounts"]:
        status = token_status(acct)
        info = ((state or {}).get("accounts") or {}).get(acct["name"]) or {}
        util_5h, util_7d = effective_utilization(info, now) if isinstance(info, dict) else (0.0, 0.0)
        accounts.append({
            "name": acct["name"], "disabled": bool(acct.get("disabled")), "priority": acct.get("priority", 100),
            "token_file": acct.get("token_file"), "token_file_present": status["present"],
            "token_file_bytes": status.get("bytes"), "token_file_mode": status.get("mode"),
            "util_5h": util_5h, "util_7d": util_7d,
            "reset_5h": info.get("reset_5h") if isinstance(info, dict) else None,
            "reset_7d": info.get("reset_7d") if isinstance(info, dict) else None,
            "last_seen": info.get("last_seen") if isinstance(info, dict) else None,
            "upstream_status": info.get("status") if isinstance(info, dict) else None,
            "eligible_for_rotate": not account_eligibility(cfg, state, acct["name"]),
        })
    report["accounts"] = accounts
    report["devices"] = [{"name": k, "key_suffix": (v[-4:] if isinstance(v, str) and len(v) >= 8 else None),
                          "placeholder": isinstance(v, str) and PLACEHOLDER in v}
                         for k, v in cfg["devices"].items()]
    report["recent_events"] = ((state or {}).get("events") or [])[-5:]
    report["live"] = None
    if report["unit"].get("ActiveState") == "active":
        live, problem = live_status(cfg)
        if live is not None:
            report["live"] = {"active_account": live.get("active_account"),
                              "storage_errors": live.get("storage_errors"),
                              "accounts": {n: {k: a.get(k) for k in ("effective_util_5h", "effective_util_7d",
                                                                    "last_seen", "status")}
                                           for n, a in (live.get("accounts") or {}).items()}}
        else:
            report["live_error"] = problem
    return report


def fmt_time(ts):
    if not ts:
        return "never"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except (ValueError, OverflowError, OSError):
        return str(ts)


def cmd_status(args) -> int:
    report = collect_status()
    if args.json:
        out(json.dumps(report, indent=2, default=str))
        return EXIT_OK
    unit = report.get("unit", {})
    out(f"service: {'unit installed' if report['unit_installed'] else 'unit NOT installed'}; "
        f"state {unit.get('ActiveState', 'unknown')}/{unit.get('SubState', '?')}; "
        f"enabled: {unit.get('UnitFileState', 'unknown')}; restarts: {unit.get('NRestarts', '?')}")
    out(f"release: {report.get('current') or 'none'}"
        + (f" (installed {report['release'].get('installed_at')})" if report.get("release") else ""))
    if "config_error" in report:
        out(f"config: ERROR {report['config_error']}")
        return EXIT_REFUSED
    out(f"config: {'complete' if report['ready'] else 'setup needed'}; strategy {report['strategy']}; "
        f"public_url {report.get('public_url') or '(unset)'}")
    for problem in report.get("problems", []):
        out(f"  - {problem}")
    for warning in report.get("warnings", []):
        out(f"  warning: {warning}")
    if report.get("state_error"):
        out(f"state: ERROR {report['state_error']}")
    live = report.get("live")
    out(f"active account: {report['active_account']} (from {report['active_account_source']}"
        + (f"; live: {live['active_account']}" if live else "") + ")")
    out("accounts (token validity is only visible through real upstream traffic):")
    for acct in report["accounts"]:
        flags = []
        if acct["disabled"]:
            flags.append("disabled")
        if acct["name"] == report["active_account"]:
            flags.append("active")
        if acct["eligible_for_rotate"] and acct["name"] != report["active_account"]:
            flags.append("eligible for rotate")
        token = ("token file present" + (f", {acct['token_file_bytes']} bytes, mode {acct['token_file_mode']}"
                                         if acct.get("token_file_bytes") is not None else "")
                 if acct["token_file_present"] else "token file MISSING")
        out(f"  {acct['name']}: {token}; 5h {acct['util_5h']:.0%}, 7d {acct['util_7d']:.0%}; "
            f"last seen {fmt_time(acct['last_seen'])}; upstream status {acct['upstream_status'] or 'n/a'}"
            + (f" [{', '.join(flags)}]" if flags else ""))
    out("devices:")
    for device in report["devices"]:
        out(f"  {device['name']}: key ...{device['key_suffix'] or '????'}"
            + (" PLACEHOLDER" if device["placeholder"] else ""))
    if live:
        errors = live.get("storage_errors") or {}
        out("live /rotate/status: reachable; storage_errors: " + (json.dumps(errors) if errors else "none"))
    elif report.get("live_error"):
        out(f"live /rotate/status: unavailable ({report['live_error']})")
    events = report.get("recent_events") or []
    if events:
        out("recent events:")
        for event in events:
            out(f"  {event.get('ts')} {event.get('type')} {event.get('from')} -> {event.get('to')} ({event.get('reason')})")
    return EXIT_OK


# ---------------------------------------------------------------- entry point


class Parser(argparse.ArgumentParser):
    """Usage errors exit 1 like every other refusal; exit 2 is reserved for stored-but-not-live."""

    def error(self, message):
        self.print_usage(sys.stderr)
        err(f"error: {message}")
        sys.exit(EXIT_REFUSED)


def build_parser():
    parser = Parser(prog="claude-rotate-admin",
                    description="Administer the installed claude-rotate service (root only).")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name, func, help_text, aliases=()):
        p = sub.add_parser(name, help=help_text, aliases=list(aliases))
        p.set_defaults(func=func)
        return p

    add("status", cmd_status, "show service, configuration and rotation state").add_argument(
        "--json", action="store_true", help="machine-readable output")
    p = add("add-token", cmd_add_token, "store an account token (hidden prompt, stdin or --token-file); "
            "an existing account gets its token replaced", aliases=("add-account",))
    p.add_argument("name", metavar="NAME")
    p.add_argument("--token-file", help="read the token from this file instead of the prompt/stdin")
    p.add_argument("--no-restart", action="store_true", help="store only; do not touch the service")
    p = add("add-user", cmd_add_user, "mint a device key and print the client settings", aliases=("add-device",))
    p.add_argument("name", metavar="NAME")
    p.add_argument("--no-restart", action="store_true")
    p = add("remove-user", cmd_remove_user, "revoke a device key", aliases=("remove-device",))
    p.add_argument("name", metavar="NAME")
    p.add_argument("--no-restart", action="store_true")
    p = add("remove-token", cmd_remove_token, "remove an account and delete its token file", aliases=("remove-account",))
    p.add_argument("name", metavar="NAME")
    p.add_argument("--no-restart", action="store_true")
    add("set-url", cmd_set_url, "set public_url used in the add-user instructions").add_argument("url", metavar="URL")
    add("rotate", cmd_rotate, "make another account active (next eligible, or ACCOUNT); restarts the service").add_argument(
        "account", nargs="?", metavar="ACCOUNT")
    add("activate", cmd_activate, "enable/restart the service if the configuration is complete, else stop and disable it")
    add("check", cmd_check, "exit 0 if the configuration is complete, 3 if setup is needed, 1 if invalid")
    add("init", cmd_init, "create or adopt /etc/claude-rotate (run by install.sh)")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_REFUSED
    try:
        require_root()
        return args.func(args)
    except Refused as exc:
        err(f"error: {exc}")
        return EXIT_REFUSED
    except KeyboardInterrupt:
        err("interrupted")
        return EXIT_REFUSED
    except PermissionError as exc:
        err(f"error: permission denied: {exc}")
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
