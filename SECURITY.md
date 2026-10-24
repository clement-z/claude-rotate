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

Run the service as a dedicated unprivileged user. Keep the installation directory
and backups private. `setup.sh` enforces mode 0600 for configuration and tokens,
and 0700 for the token directory. A token file or configuration manually edited
outside setup must receive equivalent protection. Do not use `phase0_proxy.py`
as a shared service: it is an unauthenticated, loopback-only diagnostic harness.

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
- Quota holds and burst pacing poll for client disconnects at one-second intervals
  while waiting, and before retrying. A network operation already in progress
  can continue until its own completion or timeout.

## Remaining limits

This is intended for a trusted private deployment. It does not provide tenant
isolation, per-device rate/concurrency quotas, a disk quota, or automatic audit
log rotation. Rotate `logs/audit.jsonl` and monitor disk/memory use. A compromised
authorized client can still consume shared capacity. The response and SSE paths
trust the upstream provider. Keep dependencies updated in an isolated environment
and scan the versions actually installed; `requirements.txt` has minimum versions
and does not lock the entire dependency graph.

The 2026-10-24 source audit found no explicit malicious payload, hidden downloader,
or unexpected runtime upstream in baseline `19529c6`. It did find and remediate
input handling, dashboard rendering, credential forwarding, account-disable,
configuration, and resource-handling defects. This is a bounded source review,
not a guarantee about future commits, installed packages, the host, or vendor
account compatibility. Tests use dummy credentials and a mocked Anthropic API;
the disconnect regression additionally exercises a real local TCP/Uvicorn server.
