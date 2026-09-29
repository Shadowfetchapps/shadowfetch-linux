# ShadowCode on Shadowfetch Linux

ShadowCode is the coding agent built into Shadowfetch Linux 5.0. Open a
project, pick a model, describe the change, watch the agent work, then review
the diff. It is preinstalled as the `shadow-code` package: open it from the
application menu or run `shadowcode`. `man shadowcode` has the command-line
reference.

## Connecting your models

Nothing is connected until you sign in. Open **Settings > Accounts** in
ShadowCode (Welcome's last page opens ShadowCode for you). The model can come
from:

- **A subscription you already have**: Codex, Claude Code, Cursor, Antigravity
  or Grok. ShadowCode drives each vendor's own command-line tool (`codex`,
  `claude`, `cursor-agent`, `grok`; Antigravity uses Google's agent server,
  which **Install** on its Accounts card downloads). Install the CLI from the
  vendor first; **Connect** then runs the vendor's own login command.
  ShadowCode does not read or store vendor credentials, and it starts vendor
  CLIs with provider API-key variables removed so a subscription turn is not
  silently billed per token.
- **An OpenRouter API key** if you have no subscription. Every token is billed
  to your OpenRouter account.
- **A model on this computer.** ShadowCode ships its own local runtime under
  `/usr/lib/shadowcode/` (Vulkan GPU or CPU) and offers free models to
  download, with one recommended for your hardware. Nothing downloads until
  you press **Download**.

ShadowCode never buys credits or turns on overages. Usage figures come from
the vendor; where a vendor reports none, ShadowCode says so.

Shadowfetch 4.1 installed vendor CLIs with `shadowfetch-codex` and
`shadowfetch-code-agent`. Those helpers are removed in 5.0. CLIs they already
installed in `~/.local/bin` stay where they are and ShadowCode can use them;
update them with each vendor's own updater. Welcome opens ShadowCode with your
session `PATH` so it finds them.

## Network, approvals and sandboxing

- ShadowCode has its own network setting under **Settings > Permissions &
  network**: Online, Web tools off, or Offline (only models on this computer
  run).
- `shadowfetch-agent-network` is a separate Shadowfetch setting. It decides
  whether Firebreak sandboxes start with network, and pauses Grok Bot, Hermes
  and OpenClaw when offline. It does not change ShadowCode's own network
  setting; set that in ShadowCode.
- ShadowCode's own agent asks before actions by default. Its shell sandbox
  uses `bubblewrap`. Vendor agents follow their own execution rules and
  sandbox; they do not inherit ShadowCode's.

## Updates and provenance

Shadowfetch republishes the ShadowCode `.deb` unmodified, after checking the
publisher's Ed25519 signature, the pinned version and the file's SHA-256. It
arrives and updates through the Shadowfetch APT repository with the rest of
the system (`fireproof update`). ShadowCode's own GitHub update check is turned
off by `/etc/shadowcode/policy.yaml`, because a new upstream release reaches
this system only after Shadowfetch has verified and published it. An
administrator's edit to that file wins.

If you also installed ShadowCode's AppImage for your user, that copy in
`~/.local` takes precedence over the system one and does not receive system
updates. `shadowfetch-doctor` warns when it finds one.

## Licence and source

ShadowCode is licensed under the Apache License 2.0; its bundled components
keep their own licences. See `LICENSES.md` and `SOURCES.md`.

Upstream: https://github.com/Shadowfetchapps/ShadowCode
