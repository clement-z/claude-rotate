# Service operations — Debian 12

Manage the installed service over SSH using systemd. The web dashboard is
read-only; account and device administration uses the configuration files below.
For installation or migration, see [the deployment guide](deploy/README.md).

## Everyday commands

Run these on the VPS. Each table row is a separate action, not a script to paste
in full.

| Action | Command |
| --- | --- |
| Status | `sudo systemctl status claude-rotate --no-pager` |
| Follow logs (Ctrl-C exits the viewer) | `sudo journalctl -u claude-rotate -f` |
| Last 100 log lines | `sudo journalctl -u claude-rotate -n 100 --no-pager` |
| Restart | `sudo systemctl restart claude-rotate` |
| Stop | `sudo systemctl stop claude-rotate` |
| Start | `sudo systemctl start claude-rotate` |
| Stop and disable startup at boot | `sudo systemctl disable --now claude-rotate` |
| Enable startup at boot and start | `sudo systemctl enable --now claude-rotate` |

The unit retries failures after 10 seconds and has a 45-second event-loop
watchdog. An explicit stop stays stopped until started again, or until reboot
if the service is enabled. Stops/restarts allow a bounded drain but can interrupt
requests. Invalid configuration can cause repeated startup failures; check logs.

## Health and limits

```sh
sudo systemctl show claude-rotate \
  -p ActiveState -p NRestarts -p MemoryCurrent -p MemoryMax
df -h /var/lib/claude-rotate /var/log/claude-rotate
```

Shipped defaults: memory pressure threshold 384 MiB, memory ceiling 512 MiB,
swap allowance 128 MiB, eight active proxy requests, 4 MiB request bodies and
8 MiB buffered responses. SSE responses remain streamed. Overload returns 503;
oversized bodies return 413 and oversized buffered responses return 502.

The audit log rotates at 8 MiB with four backups. The statistics cache retains
at most 10,000 records and 8 MiB of encoded record data; Python object overhead
is additional. Dashboard totals can represent partial history. Journal retention
is separate. Monitor disk space, increasing restart counts and `storage_errors`
in the authenticated `/rotate/status` response. An active process does not prove
upstream token validity; also test a small Claude request through the proxy.

## Installed files

| Purpose | Path |
| --- | --- |
| Accounts, device keys and rotation policy | `/etc/claude-rotate/config.json` |
| Account tokens | `/etc/claude-rotate/tokens/` |
| Saved rotation state | `/var/lib/claude-rotate/state.json` |
| Request audit and rotated backups | `/var/log/claude-rotate/audit.jsonl*` |
| Active release symlink | `/opt/claude-rotate/current` |
| Versioned code and virtual environments | `/opt/claude-rotate/releases/` |
| Installed systemd unit | `/etc/systemd/system/claude-rotate.service` |

Config and token files should be `0640 root:claude-rotate`; their directories
are `0750 root:claude-rotate`. State/log directories are private to the service.
Keep a protected backup of `/etc/claude-rotate`; it contains bearer credentials.
For a consistent backup of mutable state/logs, stop the service before copying.

## Configuration and credentials

Back up the live config before editing. These commands check JSON syntax and
restart only if editing and the syntax check succeed:

```sh
sudoedit /etc/claude-rotate/config.json &&
sudo python3 -m json.tool /etc/claude-rotate/config.json >/dev/null &&
sudo systemctl restart claude-rotate
```

Check status/logs afterward. JSON syntax validation does not validate account
tokens. Config and token changes require a restart; there is no hot reload.

- Revoke a device: remove its entry from `devices`, then restart.
- Disable an account: set `"disabled": true` on that account, then restart.
- Prefer an account: assign a lower numeric `priority` than other accounts.
- Renew an account token: generate it with `claude setup-token`, install it at
  the configured token path with the ownership/mode above, then restart.
- Add accounts/devices: work from a private staging copy of the **current live
  config**, use `setup.sh add-account` / `add-device` there, explicitly install
  the updated config/token files into `/etc/claude-rotate`, then restart.

`setup.sh` edits files beside itself. Running it in an old checkout or staging
directory does **not** update the installed service. Reinstalling an outdated
staging config can undo later additions or revocations. Keep secrets out of Git.

For systemd resource overrides, use `sudo systemctl edit claude-rotate`, then
`sudo systemctl daemon-reload` and `sudo systemctl restart claude-rotate`.
Application JSON edits alone do not need `daemon-reload`. See the deployment
guide before changing resource or writable-directory limits.

## Tailscale and dashboard

```sh
sudo tailscale serve status
```

Use its reported HTTPS URL as clients' `ANTHROPIC_BASE_URL`. Their
`CLAUDE_CODE_OAUTH_TOKEN` is a **device key**. Keep the application bound to
`127.0.0.1:8484`; Tailscale Serve handles tailnet access. Serve is configured
separately from this service. See [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve).

Open the read-only dashboard on a tailnet-connected client:

```text
https://YOUR-TAILSCALE-HOST/rotate/panel?key=YOUR-DEVICE-KEY
```

Treat that URL as a password because it includes the key. Every device key can
view the dashboard; there is no separate administrator role. JSON endpoints
`/rotate/status` and `/rotate/stats` also accept `Authorization: Bearer KEY`,
which avoids putting the key in a URL.

## Updates and rollback

Build a new release and virtual environment in a new final directory, install
from its `requirements.lock`, and validate it before promotion. Do not run
`git pull` or `pip install` inside the active release. Stop the service, switch
`current` to the new release, and start it. Keep the previous release and unit
for rollback; restore compatible config/unit files if needed. Check real client
traffic after switching. Dependency upgrades, especially uvicorn, require the
runner tests and service acceptance checks.

Follow the [upgrade and rollback procedure](deploy/README.md#immutable-upgrades-and-rollback).
The [Debian systemctl reference](https://manpages.debian.org/bookworm/systemd/systemctl.1.en.html)
documents service-control commands.
