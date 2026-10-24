# Debian 12 service

This unit targets a small VPS (1–2 GB RAM) with systemd and cgroup v2. It uses one
asyncio/h11 uvicorn worker. Keep the proxy on `127.0.0.1:8484`; publish it to
trusted devices through Tailscale Serve. No public listener or IP allowlist is
needed. DNS, certificate verification, and outbound HTTPS remain available.

The following commands are operator instructions, not an automated installer.
Run them on the destination only after reviewing paths and configuration. Never
copy a running developer checkout, its writable virtualenv, or its secrets into
`/opt` wholesale.

## Fresh installation

As root, install Debian prerequisites and create the non-login service identity:

```sh
apt-get update
apt-get install --no-install-recommends python3 python3-venv ca-certificates openssl adduser
adduser --system --group --no-create-home --home /nonexistent claude-rotate
install -d -o root -g root -m 0755 /opt/claude-rotate/releases
install -d -o root -g claude-rotate -m 0750 /etc/claude-rotate
install -d -o root -g claude-rotate -m 0750 /etc/claude-rotate/tokens
```

Obtain a reviewed source revision in a private operator workspace. Build a new,
uniquely named release (replace `RELEASE` below with that revision or a unique
build ID). Copy only these application files from that reviewed checkout:

```sh
release=/opt/claude-rotate/releases/RELEASE
install -d -o root -g root -m 0755 "$release/deploy"
install -o root -g root -m 0644 rotator.py oai_compat.py requirements.txt requirements.lock "$release/"
install -o root -g root -m 0644 deploy/systemd_runner.py "$release/deploy/"
python3 -m venv "$release/.venv"
"$release/.venv/bin/python" -m pip install -r "$release/requirements.lock"
"$release/.venv/bin/python" -m pip freeze > "$release/installed-requirements.txt"
chown -R root:root "$release"
chmod -R u=rwX,go=rX "$release"
```

`requirements.lock` pins the complete tested Debian 12 / Python 3.11 package
versions. It is a version snapshot, not an artifact-hash or supply-chain attestation.
The general `requirements.txt` retains minimum versions for development. Update
the deployment snapshot deliberately in a fresh environment, scan dependencies,
and rerun the tests before promotion. Build each venv at its final release path;
moving it later can break executable shebangs. The service account must have
read/execute access, and no write access, to the entire code/venv tree.

Create credentials as the operator in a private staging directory using the
existing setup script. This script writes beside itself, regardless of the
current shell directory. Keep it out of the installed release and `/etc`:

```sh
umask 077
stage=$(mktemp -d)
install -m 0700 setup.sh "$stage/setup.sh"
install -m 0600 config.example.json "$stage/config.example.json"
"$stage/setup.sh" init
"$stage/setup.sh" add-account acct-1
"$stage/setup.sh" add-device my-laptop
```

`add-account` asks for a real token obtained with `claude setup-token` in an
account's authenticated session. Keep the device key it prints private. Do not
use `onboard` for staging: it can restart an existing service. Edit the staged
`config.json` to remove unused example accounts, confirm real device keys, set
`bind` to `127.0.0.1`, and review the rotation policy. Relative token paths such
as `tokens/acct-1.token` resolve against `/etc/claude-rotate`, the directory of
the selected config, not the code directory.

Explicitly install only the intended config and token files as root. Repeat the
token command for each configured account; do not install placeholders:

```sh
install -o root -g claude-rotate -m 0640 "$stage/config.json" /etc/claude-rotate/config.json
install -o root -g claude-rotate -m 0640 "$stage/tokens/acct-1.token" /etc/claude-rotate/tokens/acct-1.token
ln -s "$release" /opt/claude-rotate/current
install -o root -g root -m 0644 deploy/claude-rotate.service /etc/systemd/system/claude-rotate.service
systemd-analyze verify /etc/systemd/system/claude-rotate.service
systemctl daemon-reload
systemctl enable --now claude-rotate.service
systemctl status claude-rotate.service
```

The fresh-install `ln -s` assumes `current` does not exist; use the upgrade
procedure when it does. Keep a protected backup of config/tokens, then remove
the private staging copy when no longer needed. Neither secrets nor state
belong in the release tree or source control. No service-side token renewal is
provided; replace expired/revoked tokens through the same staging/install flow
and restart.

## Runtime and operations

systemd creates `/var/lib/claude-rotate` and `/var/log/claude-rotate` owned by the
service account with mode `0700`. The process umask is `0077`. Code, config,
tokens, and home directories are not writable by the service. Writable private
`/tmp` is available. Do not replace `ProtectSystem=strict` with a broad writable
root to work around an installation mistake.

| Setting | Unit default |
| --- | --- |
| Config | `CLAUDE_ROTATE_CONFIG=/etc/claude-rotate/config.json` |
| State | `CLAUDE_ROTATE_STATE=/var/lib/claude-rotate/state.json` |
| Audit | `CLAUDE_ROTATE_AUDIT=/var/log/claude-rotate/audit.jsonl` |
| Request body | `CLAUDE_ROTATE_MAX_REQUEST_BYTES=4194304` (4 MiB) |
| Non-streaming upstream response | `CLAUDE_ROTATE_MAX_RESPONSE_BYTES=8388608` (8 MiB) |
| Active proxy requests | `CLAUDE_ROTATE_MAX_CONCURRENT_REQUESTS=8` |
| Audit cache | `CLAUDE_ROTATE_AUDIT_CACHE_RECORDS=10000`, `CLAUDE_ROTATE_AUDIT_CACHE_BYTES=8388608` |
| Audit rotation | `CLAUDE_ROTATE_AUDIT_MAX_BYTES=8388608`, `CLAUDE_ROTATE_AUDIT_BACKUPS=4` |
| Memory | `MemoryHigh=384M`, `MemoryMax=512M`, `MemorySwapMax=128M` |
| Process/thread and FD limits | `TasksMax=64`, `LimitNOFILE=4096` |

On a VPS with 2 GiB RAM and roughly 1.4 GiB available, keep these conservative
limits initially; they leave room for Tailscale and other services. Existing swap
is sufficient as an emergency buffer, but heavy swapping makes streaming and the
watchdog less reliable. Check RSS and restart counts under your actual workload
before raising limits. The CPU limit is often the tighter constraint on a one-vCPU host.

The eight-request admission limit covers the whole proxy request lifetime,
including body receive, holding, and SSE streaming; admin routes are excluded.
The runner's independent uvicorn `limit_concurrency=32` guard also covers
connections and can reject admin traffic when saturated. These are overload
rejections, not a durable queue. There is no CPU quota. The memory ceiling may
terminate an oversized workload; it cannot guarantee the VPS's total memory
budget alongside other services. Swap allowance only has effect when host swap
is configured. Audit files have bounded rotation and the cache is bounded;
monitor filesystem capacity and journal retention separately. The audit-file limit
must be at least 4096 bytes; invalid configuration fails at startup. A record that
still cannot fit after metadata truncation is skipped with an `audit_write` counter
and journal warning, rather than failing the request. Analytics retain
only bounded recent history from the current file. The panel and stats API warn
when totals are partial; use retained archives for offline accounting.

Non-streaming upstream responses are capped at 8 MiB and return HTTP 502 if
oversized. Streaming responses remain streamed; this cap does not buffer or
limit their total response size.

Readiness is reported only after uvicorn startup has created its listening
socket. A watchdog notification is sent from uvicorn's own event loop every
third of systemd's watchdog interval, honoring `WATCHDOG_PID` when present.
A stalled loop stops notifications and systemd's 45-second watchdog triggers
recovery. The runner supports filesystem and abstract `NOTIFY_SOCKET` paths
without an sd_notify package. This is local loop/listener liveness, not proof of
upstream availability, credential validity, or uninterrupted SSE delivery.

Core dumps are disabled and watchdog recovery uses SIGKILL to avoid writing
in-memory credentials to a crash dump. Namespace creation and privilege gain
are denied. OOM recovery terminates the service's remaining processes together.

`Restart=always` with a 10-second delay and disabled start-rate limiting retries
crashes, watchdog failures, OOM exits, and startup errors. An explicit operator
`systemctl stop` stays stopped. Bad config can therefore cause repeated startup
failures; inspect the journal rather than waiting indefinitely. Graceful stop
reports `STOPPING`, allows uvicorn 30 seconds to drain, then systemd force-kills
any remaining processes by 40 seconds. Streams/held requests can be cut off.
No zero-downtime or exactly-once guarantee is provided.

```sh
journalctl -u claude-rotate.service -n 100 --no-pager
journalctl -u claude-rotate.service -f
systemctl show claude-rotate.service -p ActiveState -p SubState -p NRestarts -p MemoryCurrent
```

Access logging is disabled because admin URLs may carry device credentials.
Runtime warnings/errors go to the journal. Usage/audit records go to the private
JSONL path above, with rotated backups alongside it. Treat both journal and
audit output as sensitive. Readiness and a clean journal do not replace a real
authenticated status check and a low-volume streamed request after installation
or upgrade. Avoid putting device keys in shell history or shared URL logs.

If state or audit writes fail, the application retains in-memory state and
continues serving. Storage-error counters appear in `/rotate/status`, with
rate-limited diagnostics on stderr (the journal under this unit). Audit is best
effort during storage failure; events can be lost, and in-memory state can be
lost on restart. Monitor those counters, journal messages, disk space, and
permissions; process readiness does not establish durable storage health.

Use `systemctl edit claude-rotate.service` for local resource/limit overrides,
for example this drop-in on a larger host:

```ini
[Service]
MemoryHigh=512M
MemoryMax=768M
MemorySwapMax=128M
Environment=CLAUDE_ROTATE_MAX_REQUEST_BYTES=8388608
Environment=CLAUDE_ROTATE_MAX_CONCURRENT_REQUESTS=12
```

After saving, run `systemctl daemon-reload` and
`systemctl restart claude-rotate.service`. Keep memory, concurrency, and body
size in balance; raising request concurrency does not change the runner's
32-connection/task guard. For alternate state/log paths, provision ownership and
explicit sandbox write allowances in the drop-in; changing an environment path
alone does not grant write permission. Do not put tokens in unit environment
variables or drop-ins.

## Tailnet access

Install/enroll Tailscale separately using its supported Debian instructions.
With the proxy bound to loopback, expose only through tailnet Serve:

```sh
tailscale serve --bg http://127.0.0.1:8484
tailscale serve status
```

See the [Tailscale Serve command reference](https://tailscale.com/docs/reference/tailscale-cli/serve)
for background serving and route management.

Use the HTTPS URL reported by Serve as clients' `ANTHROPIC_BASE_URL` and keep the
proxy's device authentication enabled. Set `public_url` in the staged config to
that URL before installing it if setup's printed instructions should use it.
Review tailnet access policy and existing Serve routes before changing them.
Do not enable Funnel for this service. Serve must pass SSE without buffering;
verify a streamed request over the actual tailnet path. Starting Serve is a
separate operator action; it is not performed by this unit.

## Migration from a home-directory service

1. Inventory the old unit/drop-ins (`systemctl cat claude-rotate.service`) and
   back up its config, tokens, state, and audit logs privately. Do not run setup
   against live files. Build a fresh root-owned release/venv as above.
2. Copy the old config/tokens into private staging, normalize relative token
   paths to `tokens/NAME.token` (or install referenced absolute paths explicitly),
   confirm the loopback bind and remove placeholders. Install to `/etc` with the
   ownership/modes above. A previous token mode of `0600` owned by the operator
   must become `0640 root:claude-rotate` for this account to read it.
3. Plan a short outage. Stop the old service before copying mutable state/logs
   or starting the replacement on the same port. Preserve the old install for
   rollback. Do not leave an old launch mechanism running in parallel.
4. Before starting the new unit, provision private state/log directories with
   `install -d -o claude-rotate -g claude-rotate -m 0700 /var/lib/claude-rotate /var/log/claude-rotate`.
   Optionally install the stopped old `state.json` to
   `/var/lib/claude-rotate/state.json` and `logs/audit.jsonl` to
   `/var/log/claude-rotate/audit.jsonl`, both `0600 claude-rotate:claude-rotate`.
   State and analytics may also be discarded deliberately; config/tokens may not.
   Archive oversized historical audit logs privately instead of importing all
   history into the active file. Preserve any existing rotated backups separately.
5. Install/verify the new unit, inspect old drop-ins for conflicting `ExecStart`,
   user, environment, and directory settings, then daemon-reload and start.
   Confirm status, journal, private file permissions, authenticated status, and
   a streamed request through the intended client path. Update backup/monitoring
   paths and retire only the old launcher after verification.

## Immutable upgrades and rollback

Build each new release and venv at a new final path, record resolved dependency
versions, and test it before switching. Never `git pull` or `pip install` into
the active release. Update config separately with a protected backup; restart
is required for configuration/token changes.

For a reviewed upgrade, as root, stop the service, then replace the root-owned
`current` symlink atomically on the same filesystem:

```sh
systemctl stop claude-rotate.service
ln -s /opt/claude-rotate/releases/NEW_RELEASE /opt/claude-rotate/current.next
mv -Tf /opt/claude-rotate/current.next /opt/claude-rotate/current
# If the reviewed unit changed, install it and run systemctl daemon-reload here.
systemctl start claude-rotate.service
```

Choose an unused temporary symlink name if `current.next` already exists; inspect
it first. Keep the previous release and unit until verification passes. Roll
back by stopping, switching `current` to the previous release, restoring the
compatible unit/config if needed, daemon-reloading after unit changes, and
starting again. State/config format compatibility must be checked for each
release. These procedures interrupt connections; clients must tolerate reconnects.
