"""Exercise uninstall.sh against fixture trees under a temporary prefix.

Every run sets CLAUDE_ROTATE_PREFIX and puts fake systemctl, deluser, delgroup,
pgrep and systemd-analyze first on PATH, so no host path or service is touched.
"""
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNINSTALL = ROOT / "uninstall.sh"
UNINSTALL_LEGACY = ROOT / "uninstall-legacy.sh"
WRAPPER_LINE = "# claude-rotate-managed: installed by claude-rotate install.sh"
UNIT_LINE = ("ExecStart=/opt/claude-rotate/current/.venv/bin/python "
             "/opt/claude-rotate/current/deploy/systemd_runner.py")
FAKES = ("systemctl", "deluser", "delgroup", "pgrep", "systemd-analyze")
GOOD_PASSWD = "claude-rotate:x:998:998::/nonexistent:/usr/sbin/nologin"
GOOD_GROUP = "claude-rotate:x:998:"


class Env:
    def __init__(self, scratch):
        self.scratch = Path(scratch)
        self.prefix = self.scratch / "prefix"
        self.prefix.mkdir()
        self.outside = self.scratch / "outside"
        self.outside.mkdir()
        self.log = self.scratch / "calls.log"
        bin_dir = self.scratch / "bin"
        bin_dir.mkdir()
        for name in FAKES:
            rc_var = "FAKE_%s_RC" % name.upper().replace("-", "_")
            default = 1 if name == "pgrep" else 0
            fake = bin_dir / name
            body = '#!/bin/sh\necho "%s $*" >> "$CALL_LOG"\n' % name
            if name == "systemctl":
                # FAKE_SYSTEMCTL_RC fails every call, as in the lead's probe;
                # is-active otherwise reports a stopped unit unless overridden.
                body += ('[ -n "${FAKE_SYSTEMCTL_RC:-}" ] && exit "$FAKE_SYSTEMCTL_RC"\n'
                         'if [ "$1" = is-active ]; then\n'
                         '  printf "%s\\n" "${FAKE_ISACTIVE_OUT-inactive}"\n'
                         '  exit "${FAKE_ISACTIVE_RC:-3}"\nfi\n'
                         'exit "${FAKE_DISABLE_RC:-0}"\n')
            else:
                body += 'exit "${%s:-%d}"\n' % (rc_var, default)
            fake.write_text(body)
            fake.chmod(0o755)
        self.env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"],
                        CLAUDE_ROTATE_PREFIX=str(self.prefix), CALL_LOG=str(self.log),
                        CLAUDE_ROTATE_TEST_PASSWD_LINE="")
        self.env.pop("CLAUDE_ROTATE_TEST_GROUP_LINE", None)

    def p(self, rel):
        return self.prefix / rel.lstrip("/")

    def run(self, *args, script=UNINSTALL, stdin=subprocess.DEVNULL, **extra):
        if self.log.exists():
            self.log.unlink()
        env = dict(self.env, **extra)
        return subprocess.run([str(script), *args], env=env, stdin=stdin,
                              text=True, capture_output=True)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []


def write(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def runtime_files(release, with_rotator=True, venv="full"):
    if with_rotator:
        write(release / "rotator.py", "# rotator\n")
        write(release / "oai_compat.py")
        write(release / "requirements.lock")
        write(release / "deploy/systemd_runner.py")
    if venv == "full":
        write(release / ".venv/pyvenv.cfg", "home = /usr/bin\n")
        write(release / ".venv/bin/python")
    elif venv == "partial":
        write(release / ".venv/lib/python3.11/site-packages/pip-partial")


def build_full(e):
    """A tree that has both the install.sh layout and the manual-install leftovers."""
    e.p("etc/systemd/system").mkdir(parents=True)
    shutil.copy(ROOT / "deploy/claude-rotate.service", e.p("etc/systemd/system/claude-rotate.service"))
    write(e.p("etc/systemd/system/claude-rotate.service.d/override.conf"), "[Service]\n")
    write(e.p("usr/local/sbin/claude-rotate-admin"),
          "#!/bin/sh\n%s\nexec /opt/claude-rotate/current/.venv/bin/python -I x \"$@\"\n" % WRAPPER_LINE)
    rel = e.p("opt/claude-rotate/releases")
    managed = rel / "0123456789ab"
    runtime_files(managed)
    write(managed / ".claude-rotate-release", "{}\n")
    # A symlink inside a managed release must be removed, not followed.
    write(e.outside / "keep-me.txt", "outside\n")
    (managed / "link-out").symlink_to(e.outside)
    building = rel / "ba9876543210"
    runtime_files(building, venv="partial")
    write(building / ".claude-rotate-building")
    runtime_files(rel / "89ec74a")
    write(rel / "89ec74a/config.example.json", "{}\n")
    runtime_files(rel / "89ec74a-recovery.AbC123", venv="partial")
    runtime_files(rel / "deadbeef1234")
    write(rel / "other-service/app.py")
    runtime_files(rel / "89ec74a7c7e22501df50f417a3d23da8d27ae428-recovery.Zz9")
    write(rel / "89ec74a7c7e22501df50f417a3d23da8d27ae428-recovery.Zz9/notes.txt", "mine\n")
    write(e.outside / "marked/.claude-rotate-release", "{}\n")
    (rel / "symlinked-release").symlink_to(e.outside / "marked")
    (rel / "89ec74a-recovery.Link").symlink_to(e.outside / "marked")
    # A symlinked marker is not proof; neither is a legacy name over unrelated content.
    write(rel / "0a0a0a0a0a0a/app.py")
    (rel / "0a0a0a0a0a0a/.claude-rotate-release").symlink_to(e.outside / "marked/.claude-rotate-release")
    write(rel / "89ec74a-recovery.Docs/requirements.txt")
    (e.p("opt/claude-rotate/current")).symlink_to("releases/0123456789ab")
    (e.p("opt/claude-rotate/current.next")).symlink_to("releases/ba9876543210")
    write(e.p("etc/claude-rotate/config.json"), "{}\n")
    write(e.p("etc/claude-rotate/tokens/acct.token"), "dummy-token\n")
    write(e.p("var/lib/claude-rotate/state.json"), "{}\n")
    write(e.p("var/log/claude-rotate/audit.jsonl"), "")


KEPT_RELEASES = ("deadbeef1234", "other-service",
                 "89ec74a7c7e22501df50f417a3d23da8d27ae428-recovery.Zz9",
                 "symlinked-release", "89ec74a-recovery.Link", "0a0a0a0a0a0a",
                 "89ec74a-recovery.Docs")
LEGACY_RELEASES = ("89ec74a", "89ec74a-recovery.AbC123")


def exists(path):
    return path.exists() or path.is_symlink()


def check_default_and_legacy():
    with tempfile.TemporaryDirectory() as scratch:
        e = Env(scratch)
        build_full(e)
        rel = e.p("opt/claude-rotate/releases")
        r = e.run()
        assert r.returncode == 0, r.stdout + r.stderr
        assert not exists(e.p("etc/systemd/system/claude-rotate.service"))
        assert not exists(e.p("usr/local/sbin/claude-rotate-admin"))
        assert not exists(e.p("opt/claude-rotate/current"))
        assert not exists(e.p("opt/claude-rotate/current.next"))
        assert not exists(rel / "0123456789ab")
        assert not exists(rel / "ba9876543210")
        for name in KEPT_RELEASES + LEGACY_RELEASES:
            assert exists(rel / name), name
        assert (e.outside / "keep-me.txt").read_text() == "outside\n"
        assert (e.outside / "marked/.claude-rotate-release").exists()
        assert e.p("etc/systemd/system/claude-rotate.service.d/override.conf").exists()
        assert e.p("etc/claude-rotate/tokens/acct.token").exists()
        assert e.p("var/lib/claude-rotate/state.json").exists()
        assert e.p("var/log/claude-rotate/audit.jsonl").exists()
        calls = e.calls()
        assert "systemctl disable --now claude-rotate.service" in calls, calls
        assert "systemctl daemon-reload" in calls, calls
        assert calls.index("systemctl disable --now claude-rotate.service") < calls.index("systemctl daemon-reload")
        assert not any(c.startswith(("deluser", "delgroup")) for c in calls), calls
        assert "--purge" in r.stdout
        assert "manual-install release" in r.stdout

        r = e.run(script=UNINSTALL_LEGACY)
        assert r.returncode == 0, r.stdout + r.stderr
        for name in LEGACY_RELEASES:
            assert not exists(rel / name), name
        for name in KEPT_RELEASES:
            assert exists(rel / name), name
        assert e.p("var/lib/claude-rotate/state.json").exists()
        # Unit already gone: no service calls on the second pass.
        assert e.calls() == [], e.calls()

        r = e.run("--legacy")
        assert r.returncode == 0, r.stdout + r.stderr
        assert "nothing to do" in r.stdout
        assert sorted(p.name for p in rel.iterdir()) == sorted(KEPT_RELEASES)


def check_clean_and_args():
    with tempfile.TemporaryDirectory() as scratch:
        e = Env(scratch)
        for _ in range(2):
            r = e.run()
            assert r.returncode == 0, r.stdout + r.stderr
            assert "nothing to do" in r.stdout
        assert e.calls() == []
        assert e.run("--bogus").returncode == 2
        assert e.run("--help").returncode == 0
        assert e.run(CLAUDE_ROTATE_PREFIX=str(e.scratch / "missing")).returncode == 2
        assert e.run(CLAUDE_ROTATE_PREFIX="relative/dir").returncode == 2
        if os.geteuid() != 0:
            # "/" is rejected before any path is used.
            assert e.run(CLAUDE_ROTATE_PREFIX="//").returncode == 2

        # Full install removed, second run on the result is a no-op.
        build_full(e)
        assert e.run("--legacy").returncode == 0
        r = e.run("--legacy")
        assert r.returncode == 0 and "nothing to do" in r.stdout, r.stdout


def snapshot(root):
    """Every path under root with its type and content, for untouched checks."""
    out = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            out[rel] = ("link", os.readlink(path))
        elif path.is_dir():
            out[rel] = ("dir",)
        else:
            out[rel] = ("file", path.read_bytes())
    return out


def assert_untouched(e, before, r):
    """Preflight refusals exit 1 and change nothing but the lock file."""
    assert r.returncode == 1, r.stdout + r.stderr
    after = snapshot(e.prefix)
    for created in ("run/claude-rotate.lock", "run"):
        if created not in before:
            after.pop(created, None)
    assert after == before, sorted(set(before.items()) ^ set(after.items()))
    assert "nothing removed" in r.stderr, r.stderr


def probe_tree(e, case):
    """Same layout as lead-acceptance/uninstall_boundary_probe.py, plus a marked wrapper."""
    unit = e.p("etc/systemd/system/claude-rotate.service")
    unit.parent.mkdir(parents=True)
    unit.write_text("ExecStart=/unrelated/application\n" if case == "foreign-unit" else UNIT_LINE + "\n")
    if case == "symlink-parent":
        (e.prefix / "opt").symlink_to(e.outside, target_is_directory=True)
    release = e.p("opt/claude-rotate/releases/aabbccddeeff")
    release.mkdir(parents=True)
    (release / ".claude-rotate-release").write_text("{}")
    (release / "important").write_text("retain on refusal")
    current = e.p("opt/claude-rotate/current")
    current.symlink_to(release, target_is_directory=True)
    write(e.p("usr/local/sbin/claude-rotate-admin"), "#!/bin/sh\n%s\n" % WRAPPER_LINE)
    return unit, current, release


def check_probe_cases():
    for case, extra in (("foreign-unit", {}), ("stop-failure", {"FAKE_SYSTEMCTL_RC": "1"}),
                        ("symlink-parent", {})):
        for args in ((), ("--purge", "--yes"), ("--legacy",)):
            with tempfile.TemporaryDirectory() as scratch:
                e = Env(scratch)
                unit, current, release = probe_tree(e, case)
                before = snapshot(e.prefix)
                outside_before = snapshot(e.outside)
                r = e.run(*args, **extra)
                assert_untouched(e, before, r)
                assert unit.exists() and current.is_symlink(), case
                assert (release / "important").read_text() == "retain on refusal", case
                assert e.p("usr/local/sbin/claude-rotate-admin").exists(), case
                assert snapshot(e.outside) == outside_before, case
                calls = e.calls()
                if case == "stop-failure":
                    assert calls[:2] == ["systemctl disable --now claude-rotate.service",
                                         "systemctl is-active claude-rotate.service"], calls
                    assert "did not stop" in r.stderr, r.stderr
                else:
                    assert calls == [], (case, calls)
                if case == "foreign-unit":
                    assert ("unrecognized claude-rotate.service; refusing to remove any "
                            "claude-rotate files; inspect: systemctl cat claude-rotate.service") in r.stderr


def check_service_states():
    # Anything but a non-zero is-active with "inactive"/"failed" aborts before deletion.
    for extra in ({"FAKE_ISACTIVE_OUT": "active", "FAKE_ISACTIVE_RC": "0"},
                  {"FAKE_ISACTIVE_OUT": "deactivating", "FAKE_ISACTIVE_RC": "3"},
                  {"FAKE_ISACTIVE_OUT": "", "FAKE_ISACTIVE_RC": "3"},
                  {"FAKE_ISACTIVE_OUT": "inactive", "FAKE_ISACTIVE_RC": "0"},
                  {"FAKE_ISACTIVE_OUT": "active", "FAKE_ISACTIVE_RC": "0", "FAKE_DISABLE_RC": "1"}):
        with tempfile.TemporaryDirectory() as scratch:
            e = Env(scratch)
            build_full(e)
            before = snapshot(e.prefix)
            r = e.run(**extra)
            assert_untouched(e, before, r)
            assert "did not stop" in r.stderr, (extra, r.stderr)
    # disable fails because the unit is not loaded, but systemd reports it stopped.
    for out in ("inactive", "failed"):
        with tempfile.TemporaryDirectory() as scratch:
            e = Env(scratch)
            build_full(e)
            r = e.run(FAKE_DISABLE_RC="1", FAKE_ISACTIVE_OUT=out)
            assert r.returncode == 0, r.stdout + r.stderr
            assert not exists(e.p("etc/systemd/system/claude-rotate.service"))


def check_foreign_targets():
    cases = {}

    def foreign_unit(e):
        write(e.p("etc/systemd/system/claude-rotate.service"), "[Service]\nExecStart=/usr/bin/other-proxy\n")
    cases["foreign unit"] = foreign_unit

    def symlinked_unit(e):
        unit = e.p("etc/systemd/system/claude-rotate.service")
        unit.unlink()
        write(e.outside / "unit", (ROOT / "deploy/claude-rotate.service").read_text())
        unit.symlink_to(e.outside / "unit")
    cases["symlinked unit"] = symlinked_unit

    def unmarked_wrapper(e):
        write(e.p("usr/local/sbin/claude-rotate-admin"), "#!/bin/sh\necho mine\n")
    cases["unmarked wrapper"] = unmarked_wrapper

    def current_dir(e):
        current = e.p("opt/claude-rotate/current")
        current.unlink()
        write(current / "rotator.py")
    cases["current is a directory"] = current_dir

    for name, target in (("outside", None), ("parent", "../elsewhere"),
                         ("nested", "releases/0123456789ab/deploy"), ("dotdot", "releases/.."),
                         ("bare", "0123456789ab")):
        def relink(e, target=target, link="current"):
            path = e.p("opt/claude-rotate/" + link)
            path.unlink()
            path.symlink_to(target or e.outside)
        cases["current -> " + name] = relink
        cases["current.next -> " + name] = lambda e, f=relink: f(e, link="current.next")

    for name, mutate in cases.items():
        for args in ((), ("--legacy",), ("--purge", "--yes")):
            with tempfile.TemporaryDirectory() as scratch:
                e = Env(scratch)
                build_full(e)
                mutate(e)
                before = snapshot(e.prefix)
                r = e.run(*args)
                assert_untouched(e, before, r)
                assert e.calls() == [], (name, e.calls())

    # Absolute links to the release path are accepted, inside and outside the prefix form.
    for target in ("{rel}/0123456789ab", "/opt/claude-rotate/releases/0123456789ab"):
        with tempfile.TemporaryDirectory() as scratch:
            e = Env(scratch)
            build_full(e)
            current = e.p("opt/claude-rotate/current")
            current.unlink()
            current.symlink_to(target.format(rel=e.p("opt/claude-rotate/releases")))
            r = e.run()
            assert r.returncode == 0, r.stdout + r.stderr
            assert not exists(current)


def check_symlinked_parents():
    for parent in ("opt", "opt/claude-rotate", "opt/claude-rotate/releases", "etc/systemd",
                   "etc/systemd/system", "usr/local/sbin", "var/log", "run"):
        with tempfile.TemporaryDirectory() as scratch:
            e = Env(scratch)
            build_full(e)
            path = e.p(parent)
            if not path.exists():
                path.mkdir(parents=True)
            target = e.outside / "moved"
            shutil.move(str(path), str(target))
            path.symlink_to(target)
            before = snapshot(e.prefix)
            outside_before = snapshot(e.outside)
            r = e.run("--legacy")
            assert r.returncode == 1, (parent, r.stdout + r.stderr)
            assert snapshot(e.outside) == outside_before, parent
            assert_untouched(e, before, r)
            assert e.calls() == [], (parent, e.calls())

    # A plain file where a directory belongs is refused too.
    with tempfile.TemporaryDirectory() as scratch:
        e = Env(scratch)
        build_full(e)
        shutil.rmtree(e.p("var/lib"))
        write(e.p("var/lib"), "not a dir\n")
        before = snapshot(e.prefix)
        assert_untouched(e, before, e.run())


def check_lock():
    with tempfile.TemporaryDirectory() as scratch:
        e = Env(scratch)
        build_full(e)
        lock = e.p("run/claude-rotate.lock")
        lock.parent.mkdir()
        holder = subprocess.Popen(["flock", str(lock), "sleep", "2"])
        try:
            deadline = time.monotonic() + 5
            while subprocess.run(["flock", "-n", str(lock), "true"]).returncode == 0:
                assert time.monotonic() < deadline, "lock holder did not start"
                time.sleep(0.05)
            started = time.monotonic()
            r = e.run()
            waited = time.monotonic() - started
        finally:
            holder.wait()
        assert r.returncode == 0, r.stdout + r.stderr
        assert waited >= 1.0, waited
        assert not exists(e.p("etc/systemd/system/claude-rotate.service"))

    with tempfile.TemporaryDirectory() as scratch:
        e = Env(scratch)
        build_full(e)
        e.p("run").mkdir()
        write(e.outside / "lock-target", "keep\n")
        e.p("run/claude-rotate.lock").symlink_to(e.outside / "lock-target")
        before = snapshot(e.prefix)
        r = e.run()
        assert_untouched(e, before, r)
        assert (e.outside / "lock-target").read_text() == "keep\n"
        assert e.calls() == []


def check_purge():
    with tempfile.TemporaryDirectory() as scratch:
        e = Env(scratch)
        build_full(e)
        r = e.run("--purge", "--yes")
        assert r.returncode == 0, r.stdout + r.stderr
        for rel in ("etc/claude-rotate", "var/lib/claude-rotate", "var/log/claude-rotate",
                    "etc/systemd/system/claude-rotate.service.d",
                    "etc/systemd/system/claude-rotate.service"):
            assert not exists(e.p(rel)), rel
        assert "user not present" in r.stdout
        assert not any(c.startswith("deluser") for c in e.calls())
        assert e.p("etc").is_dir() and e.p("var/lib").is_dir()
        r = e.run("--purge", "--yes")
        assert r.returncode == 0 and "nothing to do" in r.stdout, r.stdout

    # Symlinked data roots refuse the whole run before anything is removed.
    for victim in ("var/lib/claude-rotate", "var/lib", "etc/claude-rotate"):
        with tempfile.TemporaryDirectory() as scratch:
            e = Env(scratch)
            build_full(e)
            path = e.p(victim)
            target = e.outside / "data"
            shutil.move(str(path), str(target))
            path.symlink_to(target)
            before = sorted(str(p) for p in target.rglob("*"))
            r = e.run("--purge", "--yes")
            assert r.returncode == 1, r.stdout + r.stderr
            assert "is a symlink" in r.stderr
            assert sorted(str(p) for p in target.rglob("*")) == before
            assert e.p("etc/systemd/system/claude-rotate.service").exists()
            assert e.p("opt/claude-rotate/releases/0123456789ab").exists()
            assert e.calls() == []


def check_purge_confirmation():
    with tempfile.TemporaryDirectory() as scratch:
        e = Env(scratch)
        build_full(e)
        r = e.run("--purge")
        assert r.returncode == 1, r.stdout + r.stderr
        assert "--yes" in r.stderr
        assert e.p("etc/systemd/system/claude-rotate.service").exists()
        assert e.p("var/lib/claude-rotate/state.json").exists()
        assert e.calls() == []

        def tty_run(answer):
            master, slave = os.openpty()
            try:
                os.write(master, answer.encode())
                return e.run("--purge", stdin=slave)
            finally:
                os.close(slave)
                os.close(master)

        r = tty_run("nope\n")
        assert r.returncode == 1 and "not confirmed" in r.stderr, r.stdout + r.stderr
        assert 'type "purge" to confirm' in r.stderr
        assert e.p("var/lib/claude-rotate/state.json").exists()
        r = tty_run("purge\n")
        assert r.returncode == 0, r.stdout + r.stderr
        assert not exists(e.p("var/lib/claude-rotate"))


def check_identity():
    def purge_with(passwd, group=GOOD_GROUP, pgrep_rc="1"):
        with tempfile.TemporaryDirectory() as scratch:
            e = Env(scratch)
            r = e.run("--purge", "--yes", CLAUDE_ROTATE_TEST_PASSWD_LINE=passwd,
                      CLAUDE_ROTATE_TEST_GROUP_LINE=group, FAKE_PGREP_RC=pgrep_rc)
            return r, e.calls()

    r, calls = purge_with(GOOD_PASSWD)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "pgrep -u 998" in calls, calls
    assert "deluser --system claude-rotate" in calls, calls
    assert "delgroup --system claude-rotate" in calls, calls

    retained = {
        "uid": ("claude-rotate:x:1001:998::/nonexistent:/usr/sbin/nologin", GOOD_GROUP, "1"),
        "root": ("claude-rotate:x:0:0::/nonexistent:/usr/sbin/nologin", GOOD_GROUP, "1"),
        "home": ("claude-rotate:x:998:998::/home/claude-rotate:/usr/sbin/nologin", GOOD_GROUP, "1"),
        "shell": ("claude-rotate:x:998:998::/nonexistent:/bin/bash", GOOD_GROUP, "1"),
        "name": ("claude-rotatex:x:998:998::/nonexistent:/usr/sbin/nologin", GOOD_GROUP, "1"),
        "group": (GOOD_PASSWD, "users:x:998:", "1"),
        "running": (GOOD_PASSWD, GOOD_GROUP, "0"),
        "pgrep-error": (GOOD_PASSWD, GOOD_GROUP, "2"),
    }
    for case, (passwd, group, rc) in retained.items():
        r, calls = purge_with(passwd, group, rc)
        assert r.returncode == 0, (case, r.stdout + r.stderr)
        assert "identity" in r.stdout and "kept" in r.stdout, (case, r.stdout)
        assert not any(c.startswith(("deluser", "delgroup")) for c in calls), (case, calls)

    for shell in ("/sbin/nologin", "/bin/false"):
        r, calls = purge_with("claude-rotate:x:998:998::/nonexistent:" + shell)
        assert "deluser --system claude-rotate" in calls, (shell, calls)

    # Without --purge the identity is reported, never removed.
    with tempfile.TemporaryDirectory() as scratch:
        e = Env(scratch)
        r = e.run(CLAUDE_ROTATE_TEST_PASSWD_LINE=GOOD_PASSWD, CLAUDE_ROTATE_TEST_GROUP_LINE=GOOD_GROUP)
        assert r.returncode == 0 and "claude-rotate identity" in r.stdout, r.stdout
        assert e.calls() == []


def check_scripts_parse():
    for script in (UNINSTALL, UNINSTALL_LEGACY):
        subprocess.run(["bash", "-n", str(script)], check=True)
    if shutil.which("shellcheck"):
        subprocess.run(["shellcheck", str(UNINSTALL), str(UNINSTALL_LEGACY)], check=True)


def main():
    check_scripts_parse()
    check_default_and_legacy()
    check_clean_and_args()
    check_probe_cases()
    check_service_states()
    check_foreign_targets()
    check_symlinked_parents()
    check_lock()
    check_purge()
    check_purge_confirmation()
    check_identity()
    print("uninstall checks passed")


if __name__ == "__main__":
    main()
