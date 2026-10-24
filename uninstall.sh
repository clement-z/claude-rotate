#!/usr/bin/env bash
# Remove a claude-rotate install made by install.sh (or, with --legacy, the
# earlier manual install). Private data and the service identity are kept
# unless --purge is given. Every target is checked before anything is changed:
# an unrecognized unit, wrapper or current link aborts the whole run.
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: sudo ./uninstall.sh [--legacy] [--purge] [--yes]
       sudo ./uninstall-legacy.sh [--purge] [--yes]

Stops claude-rotate.service and removes its unit, the admin wrapper, the
current symlinks and managed release directories under
/opt/claude-rotate/releases.

  --legacy  also remove the manual-install releases 89ec74a and
            89ec74a-recovery.* when they contain only the expected runtime files
  --purge   also remove /etc/claude-rotate, /var/lib/claude-rotate,
            /var/log/claude-rotate, the claude-rotate.service.d drop-in
            directory and the claude-rotate system user/group
  --yes     do not ask for confirmation before --purge

Home checkouts, staging directories, Tailscale configuration and other
services are never touched.
EOF
}

LEGACY=0
PURGE=0
YES=0
for arg in "$@"; do
    case "$arg" in
        --legacy) LEGACY=1 ;;
        --purge) PURGE=1 ;;
        --yes) YES=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "uninstall.sh: unknown argument: $arg" >&2; usage >&2; exit 2 ;;
    esac
done

# CLAUDE_ROTATE_PREFIX relocates every path for tests; production leaves it empty.
P="${CLAUDE_ROTATE_PREFIX:-}"
if [ -n "$P" ]; then
    while [ "${P%/}" != "$P" ]; do P="${P%/}"; done
    if [ -z "$P" ] || [ "${P#/}" = "$P" ] || [ ! -d "$P" ]; then
        echo "uninstall.sh: CLAUDE_ROTATE_PREFIX must be an existing absolute directory other than /" >&2
        exit 2
    fi
elif [ "$EUID" -ne 0 ]; then
    echo "uninstall.sh: must be run as root (sudo $0)" >&2
    exit 1
fi

UNIT="$P/etc/systemd/system/claude-rotate.service"
DROPIN="$P/etc/systemd/system/claude-rotate.service.d"
WRAPPER="$P/usr/local/sbin/claude-rotate-admin"
OPT="$P/opt/claude-rotate"
RELEASES="$OPT/releases"
CURRENT="$OPT/current"
NEXT="$OPT/current.next"
ETC="$P/etc/claude-rotate"
LIB="$P/var/lib/claude-rotate"
LOG="$P/var/log/claude-rotate"
RUN="$P/run"
LOCK="$RUN/claude-rotate.lock"
SERVICE_USER=claude-rotate

RELEASE_MARKER=.claude-rotate-release
BUILDING_MARKER=.claude-rotate-building
WRAPPER_LINE='# claude-rotate-managed: installed by claude-rotate install.sh'
UNIT_LINE='ExecStart=/opt/claude-rotate/current/.venv/bin/python /opt/claude-rotate/current/deploy/systemd_runner.py'
LEGACY_NAME_RE='^(89ec74a|89ec74a7c7e22501df50f417a3d23da8d27ae428)(-recovery\.[A-Za-z0-9_-]+)?$'
LEGACY_ALLOWED=" rotator.py oai_compat.py requirements.txt requirements.lock config.example.json installed-requirements.txt deploy .venv __pycache__ "

ANCESTORS=("$P/etc" "$P/etc/systemd" "$P/etc/systemd/system" "$P/usr" "$P/usr/local"
           "$P/usr/local/sbin" "$P/opt" "$OPT" "$RELEASES" "$P/var" "$P/var/lib"
           "$P/var/log" "$RUN")
PURGE_ROOTS=("$P/etc" "$P/var" "$P/var/lib" "$P/var/log" "$ETC" "$LIB" "$LOG")

REMOVED=()
KEPT=()
SKIPPED=()
STATUS=0

removed() { REMOVED+=("$1"); echo "removed: $1"; }
kept() { KEPT+=("$1"); echo "kept: $1 ($2)"; }
skipped() { SKIPPED+=("$1"); echo "skipped: $1 ($2)"; }
warn() { echo "warning: $*" >&2; }
die() { echo "uninstall.sh: $*" >&2; exit 1; }
exists() { [ -e "$1" ] || [ -L "$1" ]; }
real_dir() { [ -d "$1" ] && [ ! -L "$1" ]; }
real_file() { [ -f "$1" ] && [ ! -L "$1" ]; }
have_systemctl() { command -v systemctl >/dev/null 2>&1; }

remove_file() {
    rm -f -- "$1" || die "could not remove $1"
    removed "$1"
}

remove_tree() {
    # rm -rf does not follow symlinks inside the tree; --one-file-system stops
    # it from descending into anything mounted below a checked directory.
    rm -rf --one-file-system -- "$1" || die "could not remove $1"
    removed "$1"
}

dir_is_empty() {
    local entries
    entries=$(find "$1" -mindepth 1 -maxdepth 1 -print -quit) || return 1
    [ -z "$entries" ]
}

# First symlink among the given paths, or nothing.
first_symlink() {
    local path
    for path in "$@"; do
        if [ -L "$path" ]; then
            printf '%s\n' "$path"
            return 0
        fi
    done
    return 0
}

# Test seams, honored only with a prefix: they replace the passwd/group lookups
# so the identity checks can run without a real claude-rotate account.
passwd_line() {
    if [ -n "$P" ] && [ -n "${CLAUDE_ROTATE_TEST_PASSWD_LINE+x}" ]; then
        printf '%s' "$CLAUDE_ROTATE_TEST_PASSWD_LINE"
    else
        getent passwd "$1" || true
    fi
}

group_line() {
    if [ -n "$P" ] && [ -n "${CLAUDE_ROTATE_TEST_GROUP_LINE+x}" ]; then
        printf '%s' "$CLAUDE_ROTATE_TEST_GROUP_LINE"
    else
        getent group "$1" || true
    fi
}

purge_command() {
    local cmd="sudo $0"
    [ "$LEGACY" -eq 1 ] && cmd="$cmd --legacy"
    printf '%s --purge\n' "$cmd"
}

unit_is_ours() { real_file "$UNIT" && grep -qxF -- "$UNIT_LINE" "$UNIT"; }
wrapper_is_ours() { real_file "$WRAPPER" && grep -qxF -- "$WRAPPER_LINE" "$WRAPPER"; }

# A current/current.next link is ours when it points at a direct child of
# releases/, written relative to the link or as the absolute release path.
link_target_ok() {
    local target name
    target=$(readlink -- "$1") || return 1
    case "$target" in
        releases/*) name="${target#releases/}" ;;
        "$RELEASES"/*) name="${target#"$RELEASES"/}" ;;
        /opt/claude-rotate/releases/*) name="${target#/opt/claude-rotate/releases/}" ;;
        *) return 1 ;;
    esac
    case "$name" in
        ""|.|..|*/*) return 1 ;;
    esac
    return 0
}

is_legacy_release() {
    local dir="$1" name entry base
    name=$(basename -- "$dir")
    [[ "$name" =~ $LEGACY_NAME_RE ]] || return 1
    local has_rotator=0 has_venv=0 count=0
    shopt -s nullglob dotglob
    for entry in "$dir"/*; do
        base=$(basename -- "$entry")
        case "$LEGACY_ALLOWED" in
            *" $base "*) ;;
            *) shopt -u nullglob dotglob; return 1 ;;
        esac
        count=$((count + 1))
        [ "$base" = rotator.py ] && real_file "$entry" && has_rotator=1
        [ "$base" = .venv ] && real_dir "$entry" && has_venv=1
    done
    shopt -u nullglob dotglob
    # A complete copy has rotator.py; an interrupted build may only have a
    # partial .venv or nothing at all yet.
    [ "$has_rotator" -eq 1 ] || [ "$has_venv" -eq 1 ] || [ "$count" -eq 0 ]
}

# Shared with install.sh and claude-rotate-admin; held until exit.
if [ -L "$RUN" ] || { exists "$RUN" && [ ! -d "$RUN" ]; }; then
    die "$RUN is not a real directory; nothing removed"
fi
install -d -m 0755 -- "$RUN" || die "could not create $RUN"
[ ! -L "$LOCK" ] || die "$LOCK is a symlink; nothing removed"
exec 9>>"$LOCK" || die "could not open $LOCK"
flock -w 120 9 || die "another claude-rotate install/uninstall/admin command is running"

# Preflight: decide everything before the first change. Any failure here
# exits 1 with the system untouched.
PROBLEMS=()
for path in "${ANCESTORS[@]}"; do
    if [ -L "$path" ]; then
        PROBLEMS+=("$path is a symlink")
    elif exists "$path" && [ ! -d "$path" ]; then
        PROBLEMS+=("$path is not a directory")
    fi
done
if exists "$UNIT" && ! unit_is_ours; then
    PROBLEMS+=("unrecognized claude-rotate.service; refusing to remove any claude-rotate files; inspect: systemctl cat claude-rotate.service")
fi
if exists "$WRAPPER" && ! wrapper_is_ours; then
    PROBLEMS+=("$WRAPPER has no claude-rotate-managed marker; unknown deployment")
fi
for link in "$CURRENT" "$NEXT"; do
    if [ -L "$link" ]; then
        link_target_ok "$link" || PROBLEMS+=("$link points outside $RELEASES: $(readlink -- "$link" || true)")
    elif exists "$link"; then
        PROBLEMS+=("$link is not a symlink")
    fi
done
if [ "$PURGE" -eq 1 ]; then
    link=$(first_symlink "${PURGE_ROOTS[@]}" "$DROPIN")
    [ -z "$link" ] || PROBLEMS+=("refusing --purge: $link is a symlink")
fi
if [ "${#PROBLEMS[@]}" -gt 0 ]; then
    printf 'uninstall.sh: %s\n' "${PROBLEMS[@]}" >&2
    die "preflight failed; nothing removed"
fi

# Release plan. Unknown entries are kept, never an abort.
REMOVE_RELEASES=()
KEEP_RELEASES=()
KEEP_REASONS=()
if real_dir "$RELEASES"; then
    shopt -s nullglob dotglob
    entries=("$RELEASES"/*)
    shopt -u nullglob dotglob
    for entry in ${entries[@]+"${entries[@]}"}; do
        reason=""
        if ! real_dir "$entry"; then
            reason="not a real directory"
        elif real_file "$entry/$RELEASE_MARKER" || real_file "$entry/$BUILDING_MARKER"; then
            :
        elif is_legacy_release "$entry"; then
            [ "$LEGACY" -eq 1 ] || reason="manual-install release; remove with uninstall-legacy.sh"
        else
            reason="not a claude-rotate release"
        fi
        if [ -z "$reason" ]; then
            REMOVE_RELEASES+=("$entry")
        else
            KEEP_RELEASES+=("$entry")
            KEEP_REASONS+=("$reason")
        fi
    done
fi

if [ "$PURGE" -eq 1 ] && [ "$YES" -eq 0 ]; then
    if [ ! -t 0 ]; then
        die "--purge needs confirmation; rerun on a terminal or add --yes"
    fi
    printf 'This deletes %s, %s, %s and the %s identity.\ntype "purge" to confirm: ' \
        "$ETC" "$LIB" "$LOG" "$SERVICE_USER" >&2
    answer=""
    read -r answer || true
    [ "$answer" = purge ] || die "purge not confirmed; nothing removed"
fi

# Stop the service. Nothing is deleted unless systemd confirms it is down.
if unit_is_ours; then
    if have_systemctl; then
        disable_rc=0
        systemctl disable --now claude-rotate.service || disable_rc=$?
        active_rc=0
        state=$(systemctl is-active claude-rotate.service 2>/dev/null) || active_rc=$?
        # A unit that is not loaded also reports "inactive".
        case "$active_rc:$state" in
            0:*) stopped=0 ;;
            *:inactive|*:failed) stopped=1 ;;
            *) stopped=0 ;;
        esac
        if [ "$stopped" -ne 1 ]; then
            die "claude-rotate.service did not stop (disable exit $disable_rc, is-active exit $active_rc, state '$state'); nothing removed"
        fi
        echo "stopped and disabled: claude-rotate.service ($state)"
    else
        echo "systemctl not found; no service to stop"
    fi
fi

# Unit.
if exists "$UNIT"; then
    remove_file "$UNIT"
    if have_systemctl; then
        systemctl daemon-reload || warn "systemctl daemon-reload failed"
        systemctl reset-failed claude-rotate.service >/dev/null 2>&1 || true
    fi
else
    skipped "$UNIT" "not present"
fi
if exists "$DROPIN" && [ "$PURGE" -eq 0 ]; then
    kept "$DROPIN" "drop-in directory; removed only with --purge"
fi

# Admin wrapper.
if exists "$WRAPPER"; then
    remove_file "$WRAPPER"
else
    skipped "$WRAPPER" "not present"
fi

# current / current.next symlinks.
for link in "$CURRENT" "$NEXT"; do
    if [ -L "$link" ]; then
        remove_file "$link"
    fi
done

# Release directories.
for entry in ${REMOVE_RELEASES[@]+"${REMOVE_RELEASES[@]}"}; do
    remove_tree "$entry"
done
for i in ${KEEP_RELEASES[@]+"${!KEEP_RELEASES[@]}"}; do
    kept "${KEEP_RELEASES[$i]}" "${KEEP_REASONS[$i]}"
done
if real_dir "$RELEASES"; then
    if dir_is_empty "$RELEASES"; then
        rmdir -- "$RELEASES" || die "could not remove $RELEASES"
        removed "$RELEASES"
    else
        kept "$RELEASES" "not empty"
    fi
fi
if real_dir "$OPT"; then
    if dir_is_empty "$OPT"; then
        rmdir -- "$OPT" || die "could not remove $OPT"
        removed "$OPT"
    else
        kept "$OPT" "not empty"
    fi
elif ! exists "$OPT"; then
    skipped "$OPT" "not present"
fi

# Private data and identity.
identity_retained() { kept "$SERVICE_USER identity" "$1"; }

remove_identity() {
    local line name uid gid home shell group_name rc
    line=$(passwd_line "$SERVICE_USER")
    if [ -z "$line" ]; then
        skipped "$SERVICE_USER identity" "user not present"
        return 0
    fi
    IFS=: read -r name _ uid gid _ home shell <<<"$line"
    if [ "$name" != "$SERVICE_USER" ]; then
        identity_retained "passwd entry name is '$name'"; return 0
    fi
    if ! [[ "$uid" =~ ^[0-9]+$ ]] || [ "$uid" -eq 0 ] || [ "$uid" -ge 1000 ]; then
        identity_retained "uid $uid is not a system uid"; return 0
    fi
    if [ "$home" != /nonexistent ]; then
        identity_retained "home is $home, not /nonexistent"; return 0
    fi
    case "$shell" in
        /usr/sbin/nologin|/sbin/nologin|/bin/false) ;;
        *) identity_retained "shell is $shell"; return 0 ;;
    esac
    group_name=$(group_line "$gid" | cut -d: -f1)
    if [ "$group_name" != "$SERVICE_USER" ]; then
        identity_retained "primary group is '${group_name:-gid $gid}'"; return 0
    fi
    rc=0
    pgrep -u "$uid" >/dev/null 2>&1 || rc=$?
    if [ "$rc" -eq 0 ]; then
        identity_retained "processes are running as uid $uid"; return 0
    elif [ "$rc" -ne 1 ]; then
        identity_retained "could not check processes (pgrep exit $rc)"; return 0
    fi
    if ! command -v deluser >/dev/null 2>&1; then
        identity_retained "deluser not found"; return 0
    fi
    if ! deluser --system "$SERVICE_USER"; then
        identity_retained "deluser failed"
        STATUS=1
        return 0
    fi
    removed "$SERVICE_USER user"
    if [ -n "$(group_line "$SERVICE_USER")" ]; then
        if command -v delgroup >/dev/null 2>&1 && delgroup --system "$SERVICE_USER"; then
            removed "$SERVICE_USER group"
        else
            warn "group $SERVICE_USER remains"
            KEPT+=("$SERVICE_USER group")
        fi
    fi
}

if [ "$PURGE" -eq 0 ]; then
    for dir in "$ETC" "$LIB" "$LOG"; do
        if exists "$dir"; then
            kept "$dir" "private data; removed only with --purge"
        fi
    done
    if [ -n "$(passwd_line "$SERVICE_USER")" ]; then
        kept "$SERVICE_USER identity" "removed only with --purge"
    fi
else
    # Re-check: the tree may have changed since preflight.
    link=$(first_symlink "${PURGE_ROOTS[@]}" "$DROPIN")
    [ -z "$link" ] || die "refusing --purge: $link is a symlink"
    for dir in "$ETC" "$LIB" "$LOG"; do
        if real_dir "$dir"; then
            remove_tree "$dir"
        elif exists "$dir"; then
            kept "$dir" "not a directory"
        fi
    done
    if real_dir "$DROPIN"; then
        remove_tree "$DROPIN"
        if have_systemctl; then
            systemctl daemon-reload || warn "systemctl daemon-reload failed"
        fi
    elif exists "$DROPIN"; then
        kept "$DROPIN" "not a directory"
    fi
    remove_identity
fi

# Summary.
echo
if [ "${#REMOVED[@]}" -eq 0 ]; then
    echo "nothing to do: no removable claude-rotate files found"
else
    echo "removed:"
    printf '  %s\n' "${REMOVED[@]}"
fi
if [ "${#KEPT[@]}" -gt 0 ]; then
    echo "kept:"
    printf '  %s\n' "${KEPT[@]}"
fi
if [ "${#SKIPPED[@]}" -gt 0 ]; then
    echo "skipped:"
    printf '  %s\n' "${SKIPPED[@]}"
fi
if [ "$PURGE" -eq 0 ]; then
    echo "to also delete private config, tokens, state, logs and the service identity:"
    echo "  $(purge_command)"
fi
echo 'not touched: home checkouts and staging directories (such as'
echo '  ~/claude-rotate and ~/claude-rotate-install.*), Tailscale Serve routes'
echo '  and other services.'
exit "$STATUS"
