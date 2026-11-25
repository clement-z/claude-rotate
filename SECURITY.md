# Security and deployment

This proxy holds account credentials and grants every registered device access
to the shared account pool. Treat device keys as secrets and give them only to
trusted clients. All device keys can view the account and device usage dashboard
and switch the active account from it; there is no separate administrator role or
per-device quota enforcement.

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

`panel_tailnet: true` opens the dashboard, its JSON endpoints and its account switch to any
request carrying `Tailscale-User-Login`. Serve sets that header for tailnet users
(not tagged devices, not Funnel) and strips client-supplied copies, so with the
required loopback bind only Serve can add it — or any process on the server
itself, which can then read the dashboard without a key. The dashboard shows
account and device names, the last 4 characters of device keys and per-device
usage; never tokens or message content. Proxy endpoints still require a device key.
The account switch (`POST /rotate/switch`) accepts only `application/json` and
refuses a request a browser marks as cross-site (`Sec-Fetch-Site`), and the panel
forbids framing, so another web page open in a tailnet user's browser can neither
send the switch nor trick a click on the panel's buttons.

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
  and Tailscale/forwarding identity headers before contacting Anthropic, and its
  upstream client keeps no cookies between requests. It still forwards ordinary
  API headers and permits all upstream paths/methods, except path segments that
  decode to `.` or `..` or hide a separator, which get 400.
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
  SSE is relayed incrementally; under a provider prefix, so is any answer whose
  length the upstream does not give. The Debian unit overrides request/response limits
  to 4/8 MiB and permits 48 concurrent proxy requests, including their streams
  and quota waits. Overload returns 503 with `Retry-After`; this is a global
  admission limit, not per-device fairness or a durable queue.
- Quota holds and burst pacing poll for client disconnects at one-second intervals
  while waiting, and before retrying. A network operation already in progress
  can continue until its own completion or timeout.

## Other providers

With a `providers` table, this one process holds every provider credential: the
Claude account tokens, each static bearer token and the Sign in with ChatGPT
credential. What that changes:

- A compromise of the service process, its user or the host exposes all of them at
  once, not only the Claude accounts. Keep the existing hardening (dedicated user,
  root-owned code, private directories, loopback bind behind Tailscale Serve) and
  give the host nothing else to do.
- A device key reaches only what its entry grants. The string form keeps meaning
  the Claude accounts alone; a device calling a provider it was not granted gets
  403. Give a team or agent container a key that lists only what it needs. A key
  opens the dashboard and its JSON endpoints, including the account switch, only
  with the `anthropic` grant. With `panel_tailnet` the tailnet identity is a
  separate way in: any tailnet user can open the dashboard whatever key the
  request carries, as before.
- Under a provider prefix the device key (`Authorization`, `x-api-key` and the
  `key` query parameter), browser cookies and forwarding headers are dropped; the
  provider's own bearer is the only credential sent, and only to that provider's
  upstream. Upstream `Set-Cookie` headers are dropped on the way back, and the
  upstream clients keep no cookies, so one device's cookie never reaches another
  device's request. Provider responses get `Content-Security-Policy: default-src
  'none'; sandbox`, so HTML from a provider opened in a browser cannot run script
  on this origin, where device keys and the dashboard live. The upstream URL is
  built from the raw request path; a segment that decodes to `.` or `..` or hides a
  separator is refused, and the result must stay under the configured base URL.
  For a static upstream mounted under a base path (`https://host/api`), segments
  that would become a dot segment or a separator after more decoding, or once a
  server strips `;` parameters, are refused too. That covers the common ways a
  server behind the upstream re-reads a path, not every one: if the provider's
  bearer is also valid for other applications on that origin, prefer an upstream
  at the root of an origin of its own. Other request headers and the body pass
  through unchanged; a stream the upstream breaks off reaches the client as a
  broken connection, not a clean end.
- Static bearer tokens sit with the account tokens under `/etc/claude-rotate/tokens`
  (read-only to the service). The ChatGPT credential sits in
  `/var/lib/claude-rotate/credentials`, owned by the service with mode 0600,
  because each refresh rotates the refresh token and the service must write the
  replacement. `claude-rotate-admin install-credential` writes it as the service
  user, not as root. The previous file is kept as `.prev`; its refresh token is
  dead once the next refresh succeeds. Back up the state directory as a secret.
- The OpenAI origins of `openai-siwc` are fixed in the code, so no configuration
  can send the refresh token elsewhere. Static upstream URLs may not carry user
  info.
- Tokens are never written to the audit log, status, alerts or error bodies. A
  refused refresh shows only the OAuth error code.
- One holder per ChatGPT credential. OpenAI's CI guidance warns against sharing one
  login across machines, and a second holder that refreshes kills the credential
  for both. Delete the copy on the machine where the sign-in ran. OpenAI documents
  the plan route for a user's own open-source or self-hosted tool; it says nothing
  about a gateway serving several of one person's agents. Revoking plan access per
  transferred session is not available yet on OpenAI's side; disconnect the agent
  in ChatGPT's settings to cut it off.
- With `"images": true`, the proxy also holds a Codex ChatGPT login,
  `credentials/<provider>.codex.json`, and sends Codex's two image requests to
  `https://chatgpt.com/backend-api/codex/images/...` with it. This is a private
  endpoint, used here the way the Codex CLI uses it, without any OpenAI programme
  behind it: OpenAI left image generation out of token sharing. OpenAI's Terms of
  Use (effective 2026-01-01) forbid to "circumvent any rate limits or restrictions"
  and to "automatically or programmatically extract data or Output", and this use
  can be read as either. An action against the account would also end the Sign in
  with ChatGPT route for every device. The requests come from httpx on the server's
  address, without Codex's cookies or version header; they do not pass as the Codex
  CLI and do not try to.
- That login is broader than the Sign in with ChatGPT token: it runs the plan
  without the token-sharing limits, reaches Codex cloud tasks and the account's
  connectors. The proxy keeps only its access and refresh tokens and the account id
  (not the id token, not an API key `codex login` may have stored), mode 0600 under
  the service user, and sends it only to the two image URLs, which are fixed in the
  code. Devices reach those two routes and nothing else with it. Deleting the file
  does not end the login: revoke it in ChatGPT's settings or with `codex logout` on
  a copy.
- Codex turns its image tool on for a provider of its own when the provider sends
  the header `x-openai-actor-authorization`. The proxy drops that header before any
  upstream, on every path, so a value meant for the proxy never reaches OpenAI or
  Anthropic as a credential.
- Revoking a device is still a config edit and a restart.

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
