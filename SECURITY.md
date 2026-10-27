# Security and deployment

This proxy holds account credentials and grants every registered device access
to the shared account pool. Treat device keys as secrets and give them only to
trusted clients. All device keys can view the account and device usage dashboard;
there is no separate administrator role or per-device quota enforcement.

## Tailscale deployment

Keep `bind` at `127.0.0.1` and use Tailscale Serve:

```sh
tailscale serve --bg http://127.0.0.1:8484
```

Use the resulting HTTPS URL as `public_url` and `ANTHROPIC_BASE_URL`, and restrict
tailnet access to the users/devices that need the service. Tailscale Serve is
available within the tailnet; Tailscale Funnel exposes a service to the internet.
See the [official Serve documentation](https://tailscale.com/docs/features/tailscale-serve).
Binding directly to the server's Tailscale IP is also supported. Avoid `0.0.0.0`
unless a separately configured firewall restricts every other interface.

`panel_tailnet: true` opens the read-only dashboard and its JSON endpoints to any
request carrying `Tailscale-User-Login`. Serve sets that header for tailnet users
(not tagged devices, not Funnel) and strips client-supplied copies, so with the
required loopback bind only Serve can add it — or any process on the server
itself, which can then read the dashboard without a key. The dashboard shows
account and device names, the last 4 characters of device keys and per-device
usage; never tokens or message content. Proxy endpoints still require a device key.

Run the service as a dedicated unprivileged user. Keep the installation directory
and backups private. `setup.sh` enforces mode 0600 for configuration and tokens,
and 0700 for the token directory. A token file or configuration manually edited
outside setup must receive equivalent protection. Do not use `phase0_proxy.py`
as a shared service: it is an unauthenticated, loopback-only diagnostic harness.

For Debian 12, use [the hardened systemd unit and installation guide](deploy/README.md).
Its config/tokens are `0640 root:claude-rotate` inside private `0750` directories,
readable only by root and the dedicated service group. The service can write its
private state/log directories, but cannot modify installed code or credentials.

## Credentials and requests

- Use the `Authorization: Bearer ...` header for API calls. Query-string keys
  remain supported for initial dashboard links, but can appear in the first
  request's access logs and in copied links. Disable or redact query logging in
  any front-end proxy. The dashboard removes its key from the address bar and
  uses headers for polling; reopening the original link is required after a
  manual reload. Private responses use `no-store` and `no-referrer`.
- The transparent proxy removes local query keys, browser cookies/referrers,
  and Tailscale/forwarding identity headers before contacting Anthropic. It
  still forwards ordinary API headers and permits all upstream paths/methods.
  Treat a device key as permission to use the shared upstream identity.
- Empty, non-string, duplicate, and public placeholder device keys fail startup.
  Revoke a device by removing its entry from `config.json` and restarting.
  Configuration is not hot-reloaded.
- A disabled or removed active account is replaced before the next request is
  sent. If every account is disabled, requests return 503 without using a token.
  Already-running requests are not revoked by editing configuration.
- `max_request_bytes` defaults to 33554432 (32 MiB). Both API front doors enforce
  the actual streamed byte count and return 413 above the limit, including for
  chunked requests. Set a positive integer in `config.json` to change it.
- Buffered upstream responses also have a byte limit (`max_response_bytes`,
  default 32 MiB); larger responses return 502 and close the upstream response.
  SSE is relayed incrementally. The Debian unit overrides request/response limits
  to 4/8 MiB and permits eight concurrent proxy requests, including their streams
  and quota waits. Overload returns 503 with `Retry-After`; this is a global
  admission limit, not per-device fairness or a durable queue.
- Quota holds and burst pacing poll for client disconnects at one-second intervals
  while waiting, and before retrying. A network operation already in progress
  can continue until its own completion or timeout.

## Remaining limits

This is intended for a trusted private deployment. It does not provide tenant
isolation, per-device rate/concurrency quotas, or a filesystem quota. Audit files
now rotate automatically and analytics retention is bounded, with explicit partial
history warnings. Rotated files, journals and unrelated data still need disk
monitoring. State/audit writes are best effort if storage fails: errors appear in
stderr and authenticated status, while requests continue with in-memory state.
A compromised authorized client can still consume all admitted capacity.
SSE content and provider response metadata still trust Anthropic. Keep dependencies updated in an isolated environment
and scan the versions actually installed. `requirements.lock` pins the tested
Debian 12/Python 3.11 deployment graph; `requirements.txt` retains minimum versions
for other development environments. Version pins are not package-integrity proofs.

The 2026-10-24 source audit found no explicit malicious payload, hidden downloader,
or unexpected runtime upstream in baseline `19529c6`. It did find and remediate
input handling, dashboard rendering, credential forwarding, account-disable,
configuration, and resource-handling defects. This is a bounded source review,
not a guarantee about future commits, installed packages, the host, or vendor
account compatibility. Tests use dummy credentials and a mocked Anthropic API;
the disconnect regression additionally exercises a real local TCP/Uvicorn server.
