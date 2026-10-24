# Debian 12 service

This unit targets a small VPS (1–2 GB RAM) with systemd and cgroup v2. It uses one
asyncio/h11 uvicorn worker. Keep the proxy on `127.0.0.1:8484`; publish it to
trusted devices through Tailscale Serve. No public listener or IP allowlist is
needed. DNS, certificate verification, and outbound HTTPS remain available.

## Installer

`install.sh` performs the installation below from a reviewed checkout, as root,
and is safe to rerun:

```sh
sudo ./install.sh
sudo claude-rotate-admin add-token acct-1       # paste the output of: claude setup-token
sudo claude-rotate-admin add-user alice-laptop  # prints the device's two export lines
sudo claude-rotate-admin status
```

What it does, in order: installs missing Debian prerequisites (`python3`,
`python3-venv`, `openssl`, `adduser`, `ca-certificates`; `--no-apt` fails
instead), reuses or creates the `claude-rotate` system user, builds a root-owned
release at `/opt/claude-rotate/releases/<id>` (`<id>` is derived from the source
files, so identical source reuses its complete release and an interrupted build
is rebuilt), creates `/etc/claude-rotate/config.json` with no accounts or devices
or adopts an existing one (ownership and modes normalized, JSON validated),
installs the unit and `/usr/local/sbin/claude-rotate-admin`, switches `current`
atomically, verifies the unit, and finally runs `claude-rotate-admin activate`.
When no enabled account token and device key exist yet, the unit is left
disabled and stopped and the script prints `installed: setup needed`; otherwise
the service is enabled and started, or restarted into the new release.

The release marker `.claude-rotate-release` identifies directories the installer
built; `.claude-rotate-building` marks a build in progress. The active release is
never modified or deleted. Install, uninstall and the admin command share the
lock `/run/claude-rotate.lock`. The installer refuses to proceed when
`claude-rotate.service`, the admin wrapper or `current` exist but were not
created by this project, or when any managed path is a symbolic link.

`claude-rotate-admin` is a standard-library Python program shipped inside every
release (`deploy/claude_rotate_admin.py`) and run through the wrapper with the
release's own interpreter. Its commands are documented in
[OPERATIONS.md](../OPERATIONS.md#accounts-and-devices). Exit codes: 0 done,
1 refused with nothing changed, 2 stored but the service failed to (re)start,
3 stored but the configuration is still incomplete (service left disabled).

## Removal

```sh
sudo ./uninstall.sh                     # keep /etc/claude-rotate, state, logs and the service identity
sudo ./uninstall.sh --purge [--yes]     # also delete them and the claude-rotate user/group
sudo ./uninstall-legacy.sh [--purge]    # also remove the manual releases 89ec74a and 89ec74a-recovery.*
```

Before changing anything, the script takes the shared lock and checks every
target: the unit must contain this project's `ExecStart` line, the wrapper its
marker line, `current` must be a symlink to a direct child of `releases/`, and no
managed directory or ancestor may be a symlink. Any failure aborts with nothing
removed. It then runs `systemctl disable --now` and deletes files only after
`systemctl is-active` reports `inactive` or `failed`. Removed: the unit, the
wrapper, the `current`/`current.next` links and release directories carrying an
installer marker. Kept and reported: unknown directories under `releases/`,
drop-ins, and by default `/etc/claude-rotate`, `/var/lib/claude-rotate`,
`/var/log/claude-rotate` and the identity, so a later `install.sh` adopts them.

`--legacy` additionally removes release directories named exactly `89ec74a`,
`89ec74a7c7e22501df50f417a3d23da8d27ae428` or either with a
`-recovery.<suffix>` part, and only when their contents are the expected runtime
files (an interrupted virtualenv counts). Hash-looking names or a stray
`rotator.py` are not enough. `--purge` asks for confirmation unless `--yes` is
given, refuses symlinked data roots, and deletes the identity only when it is a
system account with home `/nonexistent`, a nologin shell, primary group
`claude-rotate` and no running process. Home checkouts, staging directories such
as `~/claude-rotate-install.*`, Tailscale Serve routes and other services are
never touched. Repeated runs exit 0.

## Layout and permissions

`install.sh` is the only supported provisioning path; there is no separate manual
recipe, because the admin command it installs is part of the layout. What it
produces, and what any hand-built release must match:

| Path | Owner / mode | Notes |
| --- | --- | --- |
| `/opt/claude-rotate/releases/<id>/` | root, dirs `0755`, files `0644` | `rotator.py`, `oai_compat.py`, `requirements*.txt/.lock`, `config.example.json`, `deploy/systemd_runner.py`, `deploy/claude_rotate_admin.py`, `.venv/`, `installed-requirements.txt`, `.claude-rotate-release` |
| `/opt/claude-rotate/current` | root symlink | switched atomically to a complete release only |
| `/usr/local/sbin/claude-rotate-admin` | root `0755` | wrapper; runs the admin module from `current` with the release interpreter |
| `/etc/systemd/system/claude-rotate.service` | root `0644` | copy of `deploy/claude-rotate.service` |
| `/etc/claude-rotate/`, `tokens/` | `root:claude-rotate 0750` | `config.json` and `tokens/*.token` are `0640 root:claude-rotate` |
| `/var/lib/claude-rotate`, `/var/log/claude-rotate` | `claude-rotate 0700` | created by systemd (`StateDirectory`/`LogsDirectory`); files `0600` |
| `claude-rotate` user/group | system account, home `/nonexistent`, no login | created once; an existing account is reused |

Never copy a running developer checkout, its writable virtualenv, or its secrets
into `/opt` wholesale. The service account must have read/execute access, and no
write access, to the entire code/venv tree; it reads config and tokens through
the group and writes only its state and log directories.

`requirements.lock` pins the complete tested Debian 12 / Python 3.11 package
versions. It is a version snapshot, not an artifact-hash or supply-chain attestation.
The general `requirements.txt` retains minimum versions for development. Update
the deployment snapshot deliberately in a fresh environment, scan dependencies,
and rerun the tests before promotion. The installer builds each venv at its final
release path; moving a venv later can break executable shebangs.

Credentials are created in place with `claude-rotate-admin`, which writes
`/etc/claude-rotate/config.json` and `tokens/NAME.token` with the ownership and
modes above and restarts the service. Do not run `setup.sh` inside
`/etc/claude-rotate`: its `0600`/`0700` modes make the files unreadable to the
service account. Relative token paths such as `tokens/acct-1.token` resolve
against `/etc/claude-rotate`, the directory of the selected config, not the code
directory. Keep a protected backup of `/etc/claude-rotate`. Neither secrets nor
state belong in the release tree or source control. Renew an expired or revoked
token with `claude-rotate-admin add-token NAME` for the existing account; it
replaces the token file and restarts the service.

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
proxy's device authentication enabled. Run
`sudo claude-rotate-admin set-url https://<your-tailnet-host>` so the admin's
client instructions use that URL.
Review tailnet access policy and existing Serve routes before changing them.
Do not enable Funnel for this service. Serve must pass SSE without buffering;
verify a streamed request over the actual tailnet path. Starting Serve is a
separate operator action; it is not performed by this unit.

## Migration from a manual or home-directory service

### Manual installation in this layout

A manual installation that already uses this layout (`/opt/claude-rotate/releases/*`,
`current`, `/etc/claude-rotate`, the unit from this repository, including the
`89ec74a` and `89ec74a-recovery.*` releases) is migrated in this order:

```sh
sudo ./uninstall-legacy.sh      # stops the manual service; removes its unit, current link and releases; keeps /etc, state, logs, identity
sudo ./install.sh               # adopts /etc/claude-rotate and starts the service when accounts and devices exist
sudo claude-rotate-admin status
```

The uninstall step keeps `/etc/claude-rotate`, `/var/lib/claude-rotate`,
`/var/log/claude-rotate` and the service account, so the reinstall needs no
reconfiguration. Expect a short outage between the two commands.

Running `install.sh` directly also works: it adopts the configuration, builds a
new release, switches `current` and restarts into it. The old manual release
directories then stay under `/opt/claude-rotate/releases/` unused. Do **not**
run `uninstall-legacy.sh` or `uninstall.sh` after that to tidy them: both
scripts remove the whole installation (unit, admin command, `current` and the
new releases too), not only the old directories. To remove the old
directories after a direct install, confirm `readlink /opt/claude-rotate/current`
points elsewhere and delete them by hand.

### Home-directory service (outside the automated path)

A service that runs from a home checkout with its own `claude-rotate.service`
has a unit `install.sh` does not recognize, and the installer refuses to
overwrite it. The installer does not migrate this layout automatically; do the
following by hand, planning a short outage:

1. Inventory the old unit/drop-ins (`systemctl cat claude-rotate.service`) and
   back up its config, tokens, state, and audit logs privately. Do not run setup
   against live files.
2. Stop and disable the old service, then move its unit and drop-ins aside and
   reload systemd, for example:
   `systemctl disable --now claude-rotate.service`,
   `mv /etc/systemd/system/claude-rotate.service /root/claude-rotate.service.old`,
   `systemctl daemon-reload`. Keep the old checkout for rollback; do not leave an
   old launch mechanism running in parallel.
3. Copy the old `config.json` and token files into `/etc/claude-rotate` and
   `/etc/claude-rotate/tokens` as root, normalize relative token paths to
   `tokens/NAME.token`, confirm the loopback bind and remove placeholder
   devices. `install.sh` normalizes ownership and modes to
   `0640`/`0750 root:claude-rotate`; a previous operator-owned `0600` token is
   unreadable to the service account until then.
4. Run `sudo ./install.sh`. It adopts the copied configuration and starts the
   service when an enabled account token and a device key exist. Confirm
   `claude-rotate-admin status`, the journal, an authenticated status request and
   a streamed request through the intended client path. Update backup/monitoring
   paths and retire the old checkout only after verification.

## Immutable upgrades and rollback

`sudo ./install.sh` from a newer reviewed checkout is the upgrade: it builds the
new release and venv at a new final path, records resolved dependency versions
in `installed-requirements.txt`, switches `current` atomically and restarts the
service. Never `git pull` or `pip install` into the active release. Update
config separately with the admin command; restart is required for
configuration/token changes.

For a manual promotion or a rollback to a kept release, as root, stop the
service, then replace the root-owned `current` symlink atomically on the same
filesystem:

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
