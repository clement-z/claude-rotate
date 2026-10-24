#!/usr/bin/env bash
# File-based setup for claude-rotate. No database, no cloud — everything lives
# in config.json + tokens/ next to this script.
#
#   ./setup.sh                       first-time init (creates config.json, dirs)
#   ./setup.sh add-device <name>     mint a device key and register it
#   ./setup.sh add-account <name>    store a `claude setup-token` for an account
#   ./setup.sh onboard <user> <dev>  team flow: mint <user>-<dev> key, restart the
#                                    service, print a paste-ready welcome block
#                                    (uses config.json "public_url" when set)
set -euo pipefail
cd "$(dirname "$0")"

validate_name() {
    if [[ ! "$1" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$ ]]; then
        echo "error: names must be 1-64 ASCII letters, digits, underscores or hyphens, starting with a letter or digit" >&2
        exit 1
    fi
}

# Validate all caller-provided labels before creating or changing any files.
case "${1:-init}" in
init) ;;
add-device|add-account)
    name="${2:?usage: ./setup.sh $1 <name>}"
    validate_name "$name"
    ;;
onboard)
    user="${2:?usage: ./setup.sh onboard <username> <device>}"
    dev="${3:?usage: ./setup.sh onboard <username> <device>}"
    validate_name "$user"
    validate_name "$dev"
    name="${user}-${dev}"
    validate_name "$name"
    ;;
*) echo "unknown subcommand: $1" >&2; exit 1 ;;
esac

umask 077
mkdir -p tokens logs
chmod 700 tokens

if [ ! -f config.json ]; then
    cp config.example.json config.json
    echo "created config.json from config.example.json — edit accounts/devices or use the subcommands below"
fi
# Also protect configurations copied or restored with broader permissions.
chmod 600 config.json

case "${1:-init}" in
init)
    echo "ready. next steps:"
    echo "  ./setup.sh add-account acct-1   # paste a token from: claude setup-token"
    echo "  ./setup.sh add-device my-laptop"
    echo "  python3 rotator.py"
    ;;
add-device)
    key="dev-${name}-$(openssl rand -hex 8)"
    python3 - "$name" "$key" <<'EOF'
import json, sys
cfg = json.load(open("config.json"))
cfg.setdefault("devices", {})
cfg["devices"] = {k: v for k, v in cfg["devices"].items() if "REPLACE-WITH" not in v}
cfg["devices"][sys.argv[1]] = sys.argv[2]
json.dump(cfg, open("config.json", "w"), indent=2)
EOF
    url=$(python3 -c "import json; print(json.load(open('config.json')).get('public_url') or 'http://<this-host>:8484')")
    echo "device '$name' registered. On that machine set:"
    printf '  export ANTHROPIC_BASE_URL=%q\n' "$url"
    printf '  export CLAUDE_CODE_OAUTH_TOKEN=%q\n' "$key"
    ;;
onboard)
    # Team flow: a member sends you their username + device name; you run
    #   ./setup.sh onboard <username> <device>
    # and paste the printed block back to them. Mints the key as
    # <username>-<device>, restarts the service so it's live immediately.
    if python3 -c "import json,sys; sys.exit(0 if sys.argv[1] in json.load(open('config.json')).get('devices', {}) else 1)" "$name"; then
        echo "error: device '$name' already exists — revoke it first (delete from config.json) or pick another name" >&2
        exit 1
    fi
    "$0" add-device "$name" > /dev/null
    key=$(python3 -c "import json,sys; print(json.load(open('config.json'))['devices'][sys.argv[1]])" "$name")
    url=$(python3 -c "import json; print(json.load(open('config.json')).get('public_url') or 'http://<this-host>:8484')")
    if command -v systemctl > /dev/null && systemctl is-active --quiet claude-rotate 2> /dev/null; then
        # non-root operators need sudo for the restart; -n so we fail into the
        # hint instead of hanging on a password prompt (set -e must not kill
        # the block before the key is printed)
        if systemctl restart claude-rotate 2> /dev/null \
                || sudo -n systemctl restart claude-rotate 2> /dev/null; then
            echo "(service restarted — key is live)"
        else
            echo "(key stored but NOT live — run: sudo systemctl restart claude-rotate)"
        fi
    else
        echo "(restart rotator.py to activate the key)"
    fi
    echo
    echo "──── paste this back to $user ────────────────────────────────"
    echo "Hi $user — your Claude access for '$dev' is ready."
    echo
    echo "Add these two lines to your shell profile (~/.zshrc or ~/.bashrc):"
    echo
    printf '  export ANTHROPIC_BASE_URL=%q\n' "$url"
    printf '  export CLAUDE_CODE_OAUTH_TOKEN=%q\n' "$key"
    echo
    echo "Open a new terminal and run 'claude' as usual — no login needed."
    echo "Usage dashboard: $url/rotate/panel?key=$key"
    echo "The key is personal to this device — don't share or commit it."
    echo "──────────────────────────────────────────────────────────────"
    ;;
add-account)
    echo "Run 'claude setup-token' in a browser session logged into that account,"
    read -r -s -p "then paste the sk-ant-oat01-... token here (input hidden): " token
    echo
    case "$token" in sk-ant-oat01-*) ;; *) echo "error: token should start with sk-ant-oat01-" >&2; exit 1;; esac
    if [ -e "tokens/${name}.token" ]; then chmod 600 "tokens/${name}.token"; fi
    printf '%s' "$token" > "tokens/${name}.token"
    chmod 600 "tokens/${name}.token"
    python3 - "$name" <<'EOF'
import json, sys
cfg = json.load(open("config.json"))
if not any(a["name"] == sys.argv[1] for a in cfg["accounts"]):
    cfg["accounts"].append({"name": sys.argv[1], "token_file": f"tokens/{sys.argv[1]}.token"})
json.dump(cfg, open("config.json", "w"), indent=2)
EOF
    echo "account '$name' stored. Restart rotator.py to load it."
    ;;
*)
    echo "unknown subcommand: $1" >&2; exit 1 ;;
esac
