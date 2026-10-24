"""Exercise setup with copied files, dummy secrets and no real service access."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        for name in ("setup.sh", "config.example.json"):
            shutil.copy(ROOT / name, root / name)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        # Never restart an operator's installed service from this test.
        (bin_dir / "systemctl").write_text("#!/bin/sh\nexit 1\n")
        (bin_dir / "systemctl").chmod(0o700)
        env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ["PATH"])

        def run(*args, input=None):
            return subprocess.run([str(root / "setup.sh"), *args], env=env,
                                  input=input, text=True, capture_output=True)

        for invalid in ("two words", "quote'name", "../outside", "a" * 65, "-leading"):
            for args in (("add-device", invalid), ("add-account", invalid),
                         ("onboard", invalid, "laptop"), ("onboard", "user", invalid)):
                assert run(*args).returncode != 0, args
                assert not (root / "config.json").exists(), args
                assert not (root / "tokens").exists(), args
        assert run("init").returncode == 0
        cfg_path = root / "config.json"
        cfg_path.chmod(0o644)
        assert run("add-device", "Alice_01-laptop").returncode == 0
        assert cfg_path.stat().st_mode & 0o777 == 0o600
        assert (root / "tokens").stat().st_mode & 0o777 == 0o700
        assert run("add-account", "audit-account", input="sk-ant-oat01-dummy-test\n").returncode == 0
        assert (root / "tokens/audit-account.token").stat().st_mode & 0o777 == 0o600

        # A configured URL containing shell metacharacters must stay literal.
        cfg = json.loads(cfg_path.read_text())
        cfg["public_url"] = "https://proxy.invalid/a b?x=1&y='two'"
        cfg_path.write_text(json.dumps(cfg))
        added = run("onboard", "alice", "desktop")
        assert added.returncode == 0, added.stderr
        exports = "\n".join(line for line in added.stdout.splitlines() if line.startswith("  export "))
        decoded = subprocess.run(["bash", "-c", exports + '\nprintf "%s\\n%s" "$ANTHROPIC_BASE_URL" "$CLAUDE_CODE_OAUTH_TOKEN"'],
                                 text=True, capture_output=True, check=True).stdout.splitlines()
        assert decoded[0] == cfg["public_url"]
        assert decoded[1] == json.loads(cfg_path.read_text())["devices"]["alice-desktop"]
        before = cfg_path.read_bytes()
        assert run("onboard", "alice", "desktop").returncode != 0
        assert cfg_path.read_bytes() == before
        assert not (root / "outside.token").exists()
        print("setup security checks passed")


if __name__ == "__main__":
    main()
