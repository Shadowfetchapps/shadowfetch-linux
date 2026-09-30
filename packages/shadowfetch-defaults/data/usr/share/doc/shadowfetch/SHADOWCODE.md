# ShadowCode on Shadowfetch Linux

ShadowCode is the coding agent built into Shadowfetch Linux 5.0. Open a
project, pick a model, describe the change, watch the agent work, then review
the diff. It is preinstalled as the `shadow-code` package (ShadowCode 1.0):
open it from the application menu, press Meta+Shift+C, or run `shadowcode`.
`man shadowcode` has the command-line reference, and **?** inside the app
explains words such as worktree, checkpoint and rewind.

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
  download, with one recommended for your hardware. Until a subscription is
  connected, the model picker lists these free models first. Nothing
  downloads until you press **Download**.

ShadowCode never buys credits or turns on overages. Usage figures come from
the vendor; where a vendor reports none, ShadowCode says so. For paid models
it asks before a task passes $1 or a day passes $10 (change the limits in
Settings) and shows a price estimate before sending. When a task fails for a
common reason -- a refused key, used-up credits, a busy provider, a local
model that is not running, no network -- it says so in plain words and offers
the next step, such as **Try again** or **Continue on another model…**.

API keys are kept in a private file in your profile by default. **Settings >
Accounts > Where your keys are kept** can move them into the system keyring
instead; on this desktop that is KWallet.

## Working on real projects

- **Review.** Changed files are grouped by risk (config and CI, dependencies,
  source, tests, generated, docs); **Explain this change** describes one
  file's change in plain words. **Second opinions** have another model review
  your staged changes before you commit; reviewers never edit files.
- **Roles and one rulebook.** A different model or subscription can plan,
  implement and review (**Settings > Roles**). Your rules, skills and commands
  (**Settings > Rules & skills**) reach ShadowCode's own agent and every
  subscription CLI.
- **Staying on track.** A task that fails the same way three times, or edits a
  file back and forth, pauses and asks how to go on. **Only change these**
  keeps a task to the files you @-mentioned, and a heads-up tells you when a
  task skipped or deleted tests or changed CI. `/compact` and `/pin` manage a
  long conversation.
- **Large projects and worktrees.** The code index is kept between runs and
  covers up to 250,000 files. Worktree tasks can start from another branch
  and run setup commands.

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
- ShadowCode's own agent asks before actions by default. Every approval card
  says what the action does, how much it can affect (*Read-only* to *Needs
  admin*) and whether Rewind can undo it. **Always allow here** can remember
  an exact test, build or lint command for one project; it is never offered
  for anything that deletes, installs, uses the network or leaves the project.
  Its shell sandbox uses `bubblewrap`. Vendor agents follow their own
  execution rules and sandbox; they do not inherit ShadowCode's.
- Before a commit, push or pull request made from ShadowCode, it checks the
  changes for keys, passwords and `.env` files. It asks once per project
  before running the project's own Git hooks. When a task wants to add a
  package, the approval card says whether it exists on npm, PyPI or
  crates.io, how new it is and whether its name is a typo of a popular one.
  Small Git-ignored files such as `.env` are saved before each step so Rewind
  can restore them.

## Your data

**Settings > Your data**, or `shadowcode backup`, `shadowcode restore`,
`shadowcode doctor --repair` and `shadowcode reset`, back up, restore, repair
and start over. A backup can include your API keys, including those kept in
the keyring. ShadowCode upgrades an older profile in place, and an older
ShadowCode never opens a profile a newer one wrote.

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
