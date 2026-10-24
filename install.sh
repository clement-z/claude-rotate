#!/usr/bin/env bash
# Install or upgrade claude-rotate as a hardened Debian 12 systemd service.
#
#   sudo ./install.sh [--no-apt]
#
# Builds a root-owned immutable release under /opt/claude-rotate/releases/<id>,
# installs the unit and the claude-rotate-admin command, adopts or creates
# /etc/claude-rotate, and starts the service only when an account token and a
# device key exist. Repeatable: an identical source reuses its complete release,
# an interrupted build is rebuilt, and the active release is never modified.
# CLAUDE_ROTATE_PREFIX relocates every path for tests; production leaves it empty.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: sudo ./install.sh [--no-apt]

  --no-apt   do not run apt-get for missing prerequisites; fail instead

After installation:
  sudo claude-rotate-admin add-token acct-1     # paste the output of: claude setup-token
  sudo claude-rotate-admin add-user alice-laptop
  sudo claude-rotate-admin status
EOF
}

NO_APT=0
for arg in "$@"; do
    case "$arg" in
        --no-apt) NO_APT=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install.sh: unknown argument: $arg" >&2; usage >&2; exit 1 ;;
    esac
done

die() { echo "install.sh: $*" >&2; exit 1; }
say() { echo "install.sh: $*"; }

SRC=$(cd "$(dirname "$0")" && pwd -P)
cd "$SRC"
for f in rotator.py oai_compat.py requirements.txt requirements.lock config.example.json \
         deploy/systemd_runner.py deploy/claude_rotate_admin.py deploy/claude-rotate.service; do
    [ -f "$SRC/$f" ] || die "missing $SRC/$f; run from a complete checkout"
done

P="${CLAUDE_ROTATE_PREFIX:-}"
if [ -n "$P" ]; then
    while [ "${P%/}" != "$P" ]; do P="${P%/}"; done
    if [ -z "$P" ] || [ "${P#/}" = "$P" ] || [ ! -d "$P" ]; then
        die "CLAUDE_ROTATE_PREFIX must be an existing absolute directory other than /"
    fi
elif [ "$EUID" -ne 0 ]; then
    die "must be run as root: sudo $0"
fi

SERVICE_USER=claude-rotate
OPT="$P/opt/claude-rotate"
RELEASES="$OPT/releases"
CURRENT="$OPT/current"
ETC="$P/etc/claude-rotate"
UNIT="$P/etc/systemd/system/claude-rotate.service"
WRAPPER_DIR="$P/usr/local/sbin"
WRAPPER="$WRAPPER_DIR/claude-rotate-admin"
LOCK="$P/run/claude-rotate.lock"
UNIT_LINE='ExecStart=/opt/claude-rotate/current/.venv/bin/python /opt/claude-rotate/current/deploy/systemd_runner.py'
WRAPPER_LINE='# claude-rotate-managed: installed by claude-rotate install.sh'
RELEASE_MARKER=.claude-rotate-release
BUILDING_MARKER=.claude-rotate-building

# --- preflight: every managed path must be a real directory, nothing symlinked ----
for path in "$P/etc" "$P/etc/systemd" "$P/etc/systemd/system" "$P/usr" "$P/usr/local" "$WRAPPER_DIR" \
            "$P/opt" "$OPT" "$RELEASES" "$ETC" "$ETC/tokens" "$P/var" "$P/var/lib" "$P/var/log" "$P/run"; do
    if [ -L "$path" ]; then
        die "$path is a symbolic link; refusing to install through it"
    elif [ -e "$path" ] && [ ! -d "$path" ]; then
        die "$path exists and is not a directory"
    fi
done
# Dangling symlinks fail -e, so test -L first.
if [ -L "$UNIT" ] || { [ -e "$UNIT" ] && ! { [ -f "$UNIT" ] && grep -qxF -- "$UNIT_LINE" "$UNIT"; }; }; then
    die "$UNIT exists but is not the claude-rotate unit from this project; inspect it (systemctl cat claude-rotate.service) before installing"
fi
if [ -L "$WRAPPER" ] || { [ -e "$WRAPPER" ] && ! { [ -f "$WRAPPER" ] && grep -qxF -- "$WRAPPER_LINE" "$WRAPPER"; }; }; then
    die "$WRAPPER exists but was not installed by this script; move it aside first"
fi
# current must be a symlink to a direct child of releases/ (canonical paths); anything
# else is another deployment and is never overwritten or deleted.
current_target=""
if [ -L "$CURRENT" ]; then
    raw_target=$(readlink -- "$CURRENT")
    case "$raw_target" in /*) target_path="$raw_target" ;; *) target_path="$OPT/$raw_target" ;; esac
    [ -L "$target_path" ] && die "$CURRENT points at a symbolic link ($raw_target); not a layout install.sh manages"
    canon_target=$(realpath -m -- "$target_path")
    canon_releases=$(realpath -m -- "$RELEASES")
    case "$canon_target" in
        "$canon_releases"/*) target_name="${canon_target#"$canon_releases"/}" ;;
        *) die "$CURRENT points outside $RELEASES ($raw_target); not a layout install.sh manages" ;;
    esac
    case "$target_name" in
        ""|.|..|*/*) die "$CURRENT must point at a direct child of $RELEASES (found $raw_target)" ;;
    esac
    current_target="$canon_target"
elif [ -e "$CURRENT" ]; then
    die "$CURRENT exists and is not a symbolic link; this is not a layout install.sh manages"
fi

# --- prerequisites ------------------------------------------------------------------
need_pkgs=()
have_python() { command -v python3 >/dev/null 2>&1 && python3 -c 'import sys, venv, ensurepip; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; }
have_python || need_pkgs+=(python3 python3-venv)
command -v openssl >/dev/null 2>&1 || need_pkgs+=(openssl)
command -v adduser >/dev/null 2>&1 || need_pkgs+=(adduser)
[ -f /etc/ssl/certs/ca-certificates.crt ] || need_pkgs+=(ca-certificates)
if [ "${#need_pkgs[@]}" -gt 0 ]; then
    if [ "$NO_APT" -eq 1 ] || ! command -v apt-get >/dev/null 2>&1; then
        die "missing prerequisites: ${need_pkgs[*]} (install them, e.g. apt-get install --no-install-recommends ${need_pkgs[*]})"
    fi
    say "installing prerequisites: ${need_pkgs[*]}"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends "${need_pkgs[@]}"
    have_python || die "python3 with venv/ensurepip (>= 3.10) is still unavailable"
fi
command -v systemctl >/dev/null 2>&1 || die "systemctl not found; this installer targets systemd hosts"
PYTHON=$(command -v python3)

# --- lock shared with uninstall.sh and claude-rotate-admin -----------------------------
install -d -m 0755 -- "$P/run"
[ -L "$LOCK" ] && die "$LOCK is a symbolic link"
exec 9>>"$LOCK"
flock -w 120 9 || die "another claude-rotate install/uninstall/admin command is running"
# The admin reuses this held descriptor instead of taking a second lock on the same file.
export CLAUDE_ROTATE_LOCK_FD=9

# --- service identity (reuse an existing one) ------------------------------------------
if ! getent passwd "$SERVICE_USER" >/dev/null; then
    if getent group "$SERVICE_USER" >/dev/null; then
        adduser --system --no-create-home --home /nonexistent --ingroup "$SERVICE_USER" "$SERVICE_USER"
    else
        adduser --system --group --no-create-home --home /nonexistent "$SERVICE_USER"
    fi
    say "created system user $SERVICE_USER"
else
    say "using existing system user $SERVICE_USER"
fi
getent group "$SERVICE_USER" >/dev/null || die "group $SERVICE_USER is missing"

# --- directories -----------------------------------------------------------------------
install -d -o root -g root -m 0755 -- "$OPT" "$RELEASES"
install -d -o root -g "$SERVICE_USER" -m 0750 -- "$ETC" "$ETC/tokens"

# --- release build (content-addressed) ---------------------------------------------------
SHIPPED=(rotator.py oai_compat.py requirements.txt requirements.lock config.example.json
         deploy/systemd_runner.py deploy/claude_rotate_admin.py)
fingerprint=$(sha256sum "${SHIPPED[@]}" | sha256sum | cut -c1-12)
release="$(realpath -m -- "$RELEASES")/$fingerprint"

if [ -d "$release" ] && [ ! -L "$release" ] && [ -f "$release/$RELEASE_MARKER" ]; then
    say "release $fingerprint is already built; reusing it"
else
    if [ -e "$release" ] || [ -L "$release" ]; then
        if [ "$current_target" = "$release" ]; then
            # Never touch the active release: build beside it instead.
            release="$RELEASES/$fingerprint.$(date +%Y%m%d%H%M%S)"
            say "active release $fingerprint is incomplete; building $release instead"
        elif [ -d "$release" ] && [ ! -L "$release" ] && [ -f "$release/$BUILDING_MARKER" ]; then
            say "removing interrupted build $release"
            rm -rf --one-file-system -- "${release:?}"
        else
            die "$release exists but is neither a complete nor an interrupted install.sh release; remove it manually"
        fi
    fi
    say "building release $release"
    install -d -o root -g root -m 0755 -- "$release" "$release/deploy"
    printf '%s\n' "pid=$$ started=$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$release/$BUILDING_MARKER"
    for f in rotator.py oai_compat.py requirements.txt requirements.lock config.example.json; do
        install -o root -g root -m 0644 -- "$SRC/$f" "$release/$f"
    done
    install -o root -g root -m 0644 -- "$SRC/deploy/systemd_runner.py" "$SRC/deploy/claude_rotate_admin.py" "$release/deploy/"
    "$PYTHON" -m venv --clear "$release/.venv"
    "$release/.venv/bin/python" -m pip install --no-cache-dir --disable-pip-version-check -r "$release/requirements.lock"
    "$release/.venv/bin/python" -m pip freeze --disable-pip-version-check > "$release/installed-requirements.txt"
    chown -R root:root -- "$release"
    chmod -R u=rwX,go=rX -- "$release"
    printf '{"id": "%s", "source_fingerprint": "%s", "installed_at": "%s", "source": "%s"}\n' \
        "$(basename -- "$release")" "$fingerprint" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$SRC" > "$release/$RELEASE_MARKER"
    chmod 0644 -- "$release/$RELEASE_MARKER"
    rm -f -- "${release:?}/${BUILDING_MARKER:?}"
    say "release built: $release"
fi

# --- configuration: adopt or create /etc/claude-rotate ---------------------------------------
set +e
CLAUDE_ROTATE_PREFIX="$P" "$release/.venv/bin/python" -I "$release/deploy/claude_rotate_admin.py" init
init_rc=$?
set -e
case "$init_rc" in
    0|3) ;;
    *) die "configuration check failed (exit $init_rc); fix $ETC/config.json and rerun" ;;
esac

# --- unit, admin command, current symlink -------------------------------------------------------
if [ -f "$UNIT" ] && cmp -s -- "$SRC/deploy/claude-rotate.service" "$UNIT"; then
    say "unit already installed"
else
    install -o root -g root -m 0644 -- "$SRC/deploy/claude-rotate.service" "$UNIT"
    say "installed $UNIT"
fi
install -d -o root -g root -m 0755 -- "$WRAPPER_DIR"
wrapper_tmp=$(mktemp -- "$WRAPPER_DIR/.claude-rotate-admin.XXXXXX")
cat > "$wrapper_tmp" <<EOF
#!/bin/sh
$WRAPPER_LINE
# Runs the admin module of the active release with the release's own interpreter.
exec $CURRENT/.venv/bin/python -I $CURRENT/deploy/claude_rotate_admin.py "\$@"
EOF
chown root:root -- "$wrapper_tmp"
chmod 0755 -- "$wrapper_tmp"
mv -f -- "$wrapper_tmp" "$WRAPPER"

# A temporary link left by an interrupted earlier run is a dangling symlink of ours.
shopt -s nullglob
for stale in "$OPT"/.current.*; do
    [ -L "$stale" ] && rm -f -- "${stale:?}"
done
shopt -u nullglob
if [ "$current_target" != "$release" ]; then
    link_tmp=$(mktemp -u -- "$OPT/.current.XXXXXX")
    ln -s -- "$release" "$link_tmp"
    mv -Tf -- "$link_tmp" "$CURRENT"
    say "current -> $release"
fi
# Verify only once the ExecStart path exists through the current symlink.
if [ -z "$P" ] && command -v systemd-analyze >/dev/null 2>&1; then
    systemd-analyze verify "$UNIT"
fi
systemctl daemon-reload

# --- activation -------------------------------------------------------------------------------
set +e
CLAUDE_ROTATE_PREFIX="$P" "$WRAPPER" activate
activate_rc=$?
set -e
case "$activate_rc" in
    0)
        say "installed and running. Check with: sudo claude-rotate-admin status"
        ;;
    3)
        say "installed: setup needed. Next:"
        echo "  sudo claude-rotate-admin add-token acct-1     # paste the output of: claude setup-token"
        echo "  sudo claude-rotate-admin add-user alice-laptop"
        ;;
    *)
        die "installed, but the service did not start (exit $activate_rc); see journalctl -u claude-rotate -n 50 --no-pager"
        ;;
esac
