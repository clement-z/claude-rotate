# Team onboarding

How a team member gets Claude Code access through claude-rotate.

## For team members

Send the admin two things:

1. **your username** (short, lowercase — e.g. `maria`)
2. **a device name** for the machine you'll use (e.g. `laptop`, `desktop`,
   `gpu-box` — one request per machine)

Names must start with an ASCII letter or digit and contain only ASCII letters,
digits, underscores, or hyphens. Each name, and the combined
`<username>-<device>` label, must be at most 64 characters. The same rule applies
to names passed to `add-device` and `add-account`.

You'll get back two `export` lines. Put them in your shell profile
(`~/.zshrc` / `~/.bashrc`), open a new terminal, run `claude` — no Anthropic
login needed. Your usage dashboard link comes with the reply.

Rules of the road:

- The key is **personal to that device**. Don't share it, don't commit it,
  don't reuse it on a second machine — ask for another key instead.
- If a machine is lost or the key leaks, tell the admin — revocation is
  instant and only affects that device.

## For the admin

On the Debian service (installed with `install.sh`), from any directory:

```bash
sudo claude-rotate-admin add-user maria-laptop
```

This mints the key `maria-laptop`, restarts the service so it is live, and
prints the two `export` lines plus the dashboard link for the requester (the
URL comes from `public_url`; set it once with
`sudo claude-rotate-admin set-url https://YOUR-TAILSCALE-HOST`). The command
prints `is live` only after systemd reports the service active. Paste the block
back over a reasonably private channel — the key is a bearer credential.

**Revoke**: `sudo claude-rotate-admin remove-user maria-laptop`. It restarts the
service, so revocation is immediate and affects only that device. The panel's
device roster shows every key (suffix only) with last-seen times, so stale
devices are easy to spot. `sudo claude-rotate-admin status` lists them too.

On a standalone checkout (no installer), the equivalent is
`./setup.sh onboard maria laptop` next to `config.json`.

**Naming convention** `<username>-<device>` is what makes the analytics
readable: the panel's per-device tables become per-person tables for free.
