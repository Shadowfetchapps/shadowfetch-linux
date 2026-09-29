# OpenClaw on Shadowfetch Linux

OpenClaw is an open-source personal AI agent from the OpenClaw Foundation (MIT
licence). It is optional: it is not in the ISO, and nothing is downloaded
unless you choose it in Welcome, on the Control Center's **Hermes & OpenClaw**
page, or with `shadowfetch-openclaw setup`.

## Security record: read this first

OpenClaw has a long security-advisory record: more than 700 GitHub advisories
since early 2026, including critical ones, with new ones every month. A pinned
release ages fast. Review the advisories before you install it, and keep it
updated:

    https://github.com/openclaw/openclaw/security/advisories

Because of that record, Shadowfetch installs and runs it with these limits:

- **No root.** Setup runs as you, with Debian's own `nodejs` and `npm`. No
  `sudo`, no third-party Node repository.
- **Pinned bytes.** Setup runs `npm ci` from a package-lock.json that
  Shadowfetch generated and ships in `/usr/share/shadowfetch/openclaw/<version>/`,
  so every package is pinned by SHA-512. The OpenClaw tarball's integrity, npm
  registry signatures and the installed tree are checked before any package
  script runs, and then only OpenClaw's own install script runs.
- **Sandboxed launch.** `shadowfetch-openclaw open` runs OpenClaw's local chat
  mode inside a Firebreak sandbox, with its own workspace
  (`~/Workspaces/openclaw`) as the only writable folder. The sandbox has
  network, because OpenClaw talks to your model provider.
- **No listening service.** The OpenClaw Gateway is never installed by setup.
  `shadowfetch-openclaw gateway enable` installs it only after you confirm, and
  binds it to 127.0.0.1. The Gateway runs **outside** the sandbox, as you, with
  your access to your files. `gateway disable` removes it.

## What setup does

Shadowfetch 5.0 pins OpenClaw **2026.9.6**. Setup downloads it (about 311 MB
unpacked) and roughly 370 npm dependencies from registry.npmjs.org, and writes
about 1 GB:

- `~/.local/share/shadowfetch/openclaw/<version>` and the
  `~/.local/bin/openclaw` link;
- `~/.local/state/shadowfetch/openclaw` (install receipt and npm cache);
- `~/Workspaces/openclaw`, whose `.openclaw` folder holds OpenClaw's config,
  sessions and the API keys you enter.

OpenClaw is cloud-connected: it needs your own model provider account or API
key, entered during its onboarding on first open. Provider charges and data
handling apply. Shadowfetch does not store your keys.

While the agent network is offline (`shadowfetch-agent-network`), setup,
update, launch and enabling the Gateway are paused.

## Commands

    shadowfetch-openclaw setup          # review and install the pinned release
    shadowfetch-openclaw open           # local chat in a Firebreak sandbox
    shadowfetch-openclaw status --json
    shadowfetch-openclaw doctor
    shadowfetch-openclaw update         # show npm's latest release, install after verification and consent
    shadowfetch-openclaw gateway status|enable|disable
    shadowfetch-openclaw uninstall      # add --purge-data to also remove config, sessions and keys
    shadowfetch-openclaw info

`update` and `setup --latest` install a release newer than the pin with a
lockfile npm generates at that moment. Its dependencies were not reviewed by
Shadowfetch; the consent text says so.

Docs: https://docs.openclaw.ai
