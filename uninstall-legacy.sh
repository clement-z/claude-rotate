#!/usr/bin/env bash
# Remove the earlier manual claude-rotate install (releases 89ec74a and
# 89ec74a-recovery.*) together with anything install.sh created.
exec "$(dirname "$0")/uninstall.sh" --legacy "$@"
