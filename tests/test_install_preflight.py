"""install.sh preflight under a temporary prefix: unknown deployments are refused untouched.

  python3 tests/test_install_preflight.py

Only the checks that run before any system change are exercised; the build itself
needs root, apt and network and is covered by the container lifecycle run.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UNIT_LINE = "ExecStart=/opt/claude-rotate/current/.venv/bin/python /opt/claude-rotate/current/deploy/systemd_runner.py\n"
MARKER = "# claude-rotate-managed: installed by claude-rotate install.sh\n"


def snapshot(prefix: Path):
    seen = {}
    for path in sorted(prefix.rglob("*")):
        st = os.lstat(path)
        seen[str(path.relative_to(prefix))] = (st.st_mode, os.readlink(path) if path.is_symlink() else None)
    return seen


def run(prefix: Path, fakebin: Path):
    env = dict(os.environ, CLAUDE_ROTATE_PREFIX=str(prefix), PATH=f"{fakebin}{os.pathsep}{os.environ['PATH']}")
    return subprocess.run(["bash", str(ROOT / "install.sh"), "--no-apt"], env=env, text=True,
                          capture_output=True, timeout=60)


def case(name, setup, expect_refusal, needle):
    with tempfile.TemporaryDirectory(prefix="claude-rotate-install-preflight-") as tmp:
        t = Path(tmp)
        prefix = t / "prefix"
        fakebin = t / "bin"
        for d in (prefix / "opt/claude-rotate/releases", prefix / "etc/systemd/system", prefix / "usr/local/sbin", fakebin):
            d.mkdir(parents=True)
        # An identity lookup that fails stops the script right after preflight on this host,
        # which is enough to show that preflight itself passed.
        for tool in ("systemctl", "systemd-analyze", "adduser", "apt-get"):
            (fakebin / tool).write_text("#!/bin/sh\nexit 0\n")
            (fakebin / tool).chmod(0o700)
        setup(prefix, t)
        before = snapshot(prefix)
        r = run(prefix, fakebin)
        after = snapshot(prefix)
        assert r.returncode == 1, (name, r)
        assert needle in r.stderr, (name, r.stderr)
        if expect_refusal:
            assert before == after, (name, "preflight refusal changed the tree")
        print(f"ok   {name}")


def link_current(target):
    def setup(prefix, t):
        (prefix / "opt/claude-rotate/current").symlink_to(target)
    return setup


def main():
    outside = Path(tempfile.mkdtemp(prefix="claude-rotate-unrelated-"))
    try:
        (outside / "sentinel").write_text("do not touch")
        case("current-to-unrelated-absolute", link_current(str(outside)), True, "points outside")
        case("current-nested", link_current("releases/abc/deploy"), True, "direct child")
        case("current-parent-escape", link_current("releases/../elsewhere"), True, "points outside")
        case("current-dotdot-relative", link_current("../elsewhere"), True, "points outside")
        case("current-releases-itself", link_current("releases"), True, "points outside")

        def symlinked_target(prefix, t):
            (prefix / "opt/claude-rotate/releases/real").mkdir()
            (prefix / "opt/claude-rotate/releases/alias").symlink_to(prefix / "opt/claude-rotate/releases/real")
            (prefix / "opt/claude-rotate/current").symlink_to("releases/alias")
        case("current-to-symlinked-release", symlinked_target, True, "symbolic link")

        def real_dir_current(prefix, t):
            (prefix / "opt/claude-rotate/current").mkdir()
        case("current-real-directory", real_dir_current, True, "not a symbolic link")

        def dangling_unit(prefix, t):
            (prefix / "etc/systemd/system/claude-rotate.service").symlink_to(t / "missing.service")
        case("dangling-unit-symlink", dangling_unit, True, "not the claude-rotate unit")

        def foreign_unit(prefix, t):
            (prefix / "etc/systemd/system/claude-rotate.service").write_text("ExecStart=/unrelated/app\n")
        case("foreign-unit", foreign_unit, True, "not the claude-rotate unit")

        def dangling_wrapper(prefix, t):
            (prefix / "usr/local/sbin/claude-rotate-admin").symlink_to(t / "missing-admin")
        case("dangling-wrapper-symlink", dangling_wrapper, True, "not installed by this script")

        def symlinked_opt(prefix, t):
            import shutil
            shutil.rmtree(prefix / "opt")
            (t / "elsewhere").mkdir()
            (prefix / "opt").symlink_to(t / "elsewhere")
        case("symlinked-opt", symlinked_opt, True, "symbolic link")

        def well_formed(prefix, t):
            (prefix / "etc/systemd/system/claude-rotate.service").write_text(UNIT_LINE)
            (prefix / "usr/local/sbin/claude-rotate-admin").write_text("#!/bin/sh\n" + MARKER)
            (prefix / "opt/claude-rotate/current").symlink_to(str(prefix / "opt/claude-rotate/releases/aaaabbbbcccc"))
        # Preflight passes; the run stops later because this host has no claude-rotate identity.
        case("well-formed-layout-passes-preflight", well_formed, False, "group claude-rotate is missing")
        assert (outside / "sentinel").read_text() == "do not touch"
    finally:
        import shutil
        shutil.rmtree(outside, ignore_errors=True)
    print("install preflight checks passed")


if __name__ == "__main__":
    main()
