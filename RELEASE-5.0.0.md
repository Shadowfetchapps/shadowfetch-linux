# Shadowfetch Linux 5.0.0 — ShadowCode

Edition: **ShadowCode**. Subtitle: **"One Harness. All Models."** Codename:
Umbra; the APT suite stays `umbra` (keeping it means 4.1 systems receive 5.0
from the suite they already track).
Signing fingerprint unchanged:
`8F13 CE15 35EE 1F4A 2916 A1F7 3C5C 900B 7BE8 0CA1`.

Status: **Released 2026-09-30.** The release image below is built, gated and
accepted (see [Acceptance](#acceptance)). The release owner chose to ship this
image and deliver the remaining fixes in the 5.0.1 update (see
[Known issues](#known-issues)). Published 2026-09-30: the ISO, its checksum and signature are served from
`https://www.shadowfetch.com/linux/download/`, and the SBOM, manifests and
evidence bundle are attached to the v5.0.0 GitHub release.

- Version: 5.0.0
- Codename / repository suite: `umbra`
- Architecture: amd64; desktop KDE Plasma 6.7.4 (Frameworks 6.30, Qt 6.10.2); installer Calamares
- Base: Debian testing snapshot `20260929T000000Z`; kernel Linux 7.2.6; systemd 262; Mesa 26.1.6
- Source branch: `release/5.0.0` (the checkout path is named for 4.0.0 and is
  not the version)
- Source commit / tree the image is built from: `9587a7ca348e817870baffa9d390754acf331266` / `2986e02421fc6af1353ce7e7b7695a390c724acf`
- ISO: `shadowfetch-5.0.0-amd64.iso`, 4085778432 bytes, SHA-256 `2d8a72e044e8061bd616b2b4668425cc4d4ec0480a98975f961c0e58cba95e21`,
  detached signature `shadowfetch-5.0.0-amd64.iso.asc` verifying against
  `8F13CE1535EE1F4A2916A1F73C5C900B7BE80CA1`
- Image contents: squashfs 3830738944 bytes; 3359 packages, 19 of
  them Shadowfetch packages
- Publication date: 2026-09-30 (stable)
- ShadowCode: 1.0.0, tag `v1.0.0`, commit
  `e0ab26553cec4243dfb1aeabb66b206bbc6c0474`; `ShadowCode_1.0.0_amd64.deb`,
  28,862,468 bytes, SHA-256
  `6439c6307478dabe0a439fb400549fd5328a3bc527a71cf2561d141260e9d61a`

## Why 5.0.0 and not 4.2.0

5.0 changes what a working 4.1 setup sees and depends on. The desktop is
rebuilt around a preinstalled coding agent, the two looks become one, the
Fire/Ice switch becomes a separate network setting, three commands are
removed, and Welcome offers a different set of agents. The breaking changes are
first in this document, as they were for 4.1.

---

# READ THIS FIRST: what changes for a 4.1 system

## Platform

The Debian testing base moves from snapshot `20260726T000000Z` to
`20260929T000000Z`. Every image package was resolved against both snapshots
in a clean container before the change.

| Component | 4.1.0 | 5.0.0 |
|---|---|---|
| Linux kernel | 7.1.3 | 7.2.6 |
| KDE Plasma (plasma-workspace / KWin) | 6.6.5 | 6.7.4 |
| KDE Frameworks | 6.26 | 6.30 |
| Qt 6 | 6.10.2 | 6.10.2 |
| Mesa | 26.1.5 | 26.1.6 |
| systemd | 261.1 | 262 |
| PipeWire / WirePlumber | 1.6.8 / 0.5.15 | 1.6.9 / 0.5.17 |
| glibc / GCC | 2.42 / 15.2 | 2.43 / 16.1 |
| Python | 3.14.6 | 3.14.7 |
| Node.js / npm | 24.18 / 11.16 | 24.21 / 12.0 |
| bubblewrap (ShadowCode's sandbox) | 0.11.2 | 0.13.0 |
| ShadowCode | not included | 1.0.0 |
| Grok Bot (optional, pinned) | 0.43.0 | 0.61.0 |

The NVIDIA open driver still comes from NVIDIA's signed repository through
`nvidia-driver-assistant`, which now offers 615.71.09; it builds against the
7.2 kernel headers but has not yet been tested on RTX hardware for this
release. DrKonqi moves to 6.7.4; the Shadowfetch coredump-pickup fix is still
required and was re-verified against it. `network-manager-gnome`, a
transitional package now gone from testing, is replaced by
`network-manager-applet` and `nm-connection-editor` (same contents).

## 1. Fire and Ice are gone; the agent network replaces the switch

`shadowfetch-element` is removed, with `element-boot.sh`,
`element-session.sh`, `shadowfetch-element-boot.service` and the
`shadowfetch-element-session` autostart entry. There is one look, ShadowCode.

The Fire/Ice switch did two things: it changed the desktop's colours, and in
Ice it started agent sandboxes without network and paused cloud agents. The
second half survives as its own setting:

```
shadowfetch-agent-network                          # prints online or offline
shadowfetch-agent-network status                   # value and where it came from
shadowfetch-agent-network set online|offline       # your setting
sudo shadowfetch-agent-network --system set online|offline   # system default
```

`offline` means Firebreak sandboxes default to `--net none` and Grok Bot,
Hermes and OpenClaw setup, update and launch are paused. `online` is the
default. Firebreak, Grok Bot, Hermes, OpenClaw, Workbench, Mission Control,
the Control Center and Welcome all read the same setting. Resolution order:
`$SHADOWFETCH_AGENT_NETWORK`, then `~/.config/shadowfetch/agent-network`, then
`/etc/shadowfetch/agent-network`, then online.

**An upgraded Ice machine stays offline.** On upgrade from before 5.0.0,
shadowfetch-defaults' postinst writes `/etc/shadowfetch/agent-network` from
`/etc/shadowfetch/element` (ice becomes offline, fire becomes online), removes
the element file and purges the old boot unit's enable link. A per-user
`~/.config/shadowfetch/element` is not rewritten, but every reader still treats
it as the legacy value at that level (ice = offline) until you run
`shadowfetch-agent-network set`, which replaces it. `$SHADOWFETCH_ELEMENT=ice`
is still honoured as offline.

**What you do:** nothing, if you want the old network behaviour. If you used
Ice only for its look, run `shadowfetch-agent-network set online`.

Also changed:

- The kernel option is now `sf.agent-network=online|offline`. `sf.element=` is
  no longer read. The live boot menu has an "agents offline" entry, and the
  installer carries the choice made there or in Welcome into the installed
  system.
- Machine-readable output renamed its fields. `shadowfetch-grok-bot status
  --json` reports `agent_network` and `blocked_by_offline` instead of
  `element` and `blocked_by_ice`. `shadowfetch-workbench` plans report
  `agent_network` / `recommended_agent_network` instead of `element` /
  `recommended_element`. Update any script that parsed the old names.

## 2. One look: the Ice theme and the Fire/Ice wallpapers are removed

Removed: the `ShadowfetchIce` colour scheme, the `ShadowfetchGlacier` Konsole
scheme, the `org.shadowfetch.ice` look-and-feel, and the wallpapers UmbraFire,
UmbraIce, UmbraFrost, UmbraDrift and UmbraGold with
`/usr/share/backgrounds/shadowfetch/umbra-4k.jpg` and `umbra-ice-4k.jpg`.
`ShadowfetchDark`, `ShadowfetchUmbra`, `org.shadowfetch.dark` and the `umbra`
SDDM theme keep their ids and are restyled to ShadowCode gold (#F2B33D) and
steel (#BCC0C6) on near-black. UmbraContour, UmbraDusk, UmbraEmber and
UmbraGraphite remain as extra wallpapers.

At your next login, `/usr/lib/shadowfetch/look-migrate.sh` runs once. It
repoints only settings that name a removed asset (colour scheme, Konsole
scheme, look-and-feel, wallpaper, lock-screen image, or one of the two retired
accent colours) at the ShadowCode equivalent. A setting that names anything
still installed, such as your own wallpaper or another colour scheme, is left
alone. If Plasma was not ready, it retries at the next login.

Welcome's accent and wallpaper page is removed with the element page.

## 3. `shadowfetch-codex` and `shadowfetch-code-agent` are removed

ShadowCode connects the vendor command-line tools itself, so the 4.1 helpers
that installed Codex, Claude Code, Grok Build and Cursor Agent, and their
`CODEX.md` and `CODING-AGENTS.md`, are gone. Welcome no longer offers those
installers.

- CLIs those helpers already installed in `~/.local/bin` are **left in
  place**. ShadowCode and Mission Control can keep using them; update them
  with each vendor's own updater.
- Menu launchers those helpers wrote (`codex-cli`, `claude-code`,
  `grok-build`, `cursor-agent`) are removed at login, but only if they still
  run one of the removed helpers.
- On a new install, install a vendor CLI from the vendor, then use
  **Connect** in ShadowCode's **Settings › Accounts**.

## 4. Buzz is removed completely

`/usr/libexec/shadowfetch-retire-buzz` and its autostart entry are removed,
and the postinst no longer runs the relay retirement. 4.1 already retired
Buzz's integration; if that retirement had not yet completed for a user, 5.0
does not retry it.

---

# What is new

## ShadowCode, preinstalled

ShadowCode 1.0.0 is the desktop's coding agent. `shadowfetch-desktop` depends
on `shadow-code (>= 1.0.0)`. Open a project, pick a model, describe the
change, approve actions, then review, keep or undo each change. Models come
from:

- **Subscriptions**: Codex, Claude Code, Cursor, Antigravity or Grok, through
  each vendor's CLI and its own login. ShadowCode never reads or stores vendor
  credentials, strips provider API-key variables from vendor CLIs, and never
  buys credits.
- **API keys**: OpenRouter, billed per token.
- **This computer**: a bundled local runtime (Vulkan GPU or CPU) under
  `/usr/lib/shadowcode/`, with free models to download on request.

What ShadowCode 1.0 adds for a first-time user and for real work:

- **Easy to start.** Until a subscription is connected, the model picker lists
  the free models on this computer first. A task that fails for a common
  reason (key refused, credits or allowance used up, conversation too long,
  provider busy, local model not running, offline) says so in plain words and
  offers the next step. Help (`?`) explains words such as worktree,
  checkpoint and rewind.
- **Approvals you can read.** Every approval card says in one sentence what
  the action does, how much it can affect (*Read-only* to *Needs admin*) and
  whether Rewind can undo it. **Always allow here** covers exact test, build
  and lint commands per project, never anything that deletes, installs, uses
  the network or leaves the project.
- **Secret checks** before every commit, push and pull request made from
  ShadowCode; project Git hooks are asked about once per project; new
  packages are looked up on npm, PyPI and crates.io; small Git-ignored files
  such as `.env` are saved before each step and restored by Rewind.
- **Keys in the system keyring**, on request: **Settings › Accounts › Where
  your keys are kept** moves API keys into the Secret Service keyring. A login
  keyring is created and unlocked when you log in, so saving a key does not
  ask for a keyring password.
- **Real work.** One rulebook (rules, skills, commands) reaches every agent;
  **second opinions** review staged changes with another model; **roles**
  pick a model per step (plan, implement, review); **spending limits** for
  paid models ($1 a task, $10 a day by default) with a price estimate;
  **stuck detection**; `/compact` and `/pin`; a code index kept between runs
  for projects of up to 250,000 files; review grouped by risk with **Explain
  this change**; worktree tasks from another branch with setup commands.
- **Your data.** **Settings › Your data** and `shadowcode backup`, `restore`,
  `doctor --repair` and `reset` back up, restore, repair and start over. A
  0.34 profile upgrades in place; an older ShadowCode never opens a newer
  profile.

Welcome's last page, "Connect your services in ShadowCode", opens it. See
`/usr/share/doc/shadowfetch/SHADOWCODE.md`.

**How it is shipped.** ShadowCode is the one package in the image that this
tree does not build. It is republished exactly as upstream CI built and signed
it:

- The pin lives in one place, `tools/release/shadowcode.toml`, and is moved
  only by `tools/bump_shadowcode.py`, which verifies the Ed25519 signature
  over `RELEASE-AUTH` against the vendored trust policy and refuses unsigned,
  tampered, out-of-policy, downgraded or same-version-republished releases.
- `tools/fetch_shadowcode.py` (run by `make packages`) downloads it,
  re-verifies it with upstream's own verifier, and checks the `.deb`'s control
  fields against the pin.
- The package and ISO gates re-verify the signature and bytes, require the
  runtime's files to live only under `/usr/lib/shadowcode/`, and fail on any
  lintian error not in the reviewed `vendor/shadowcode/<version>/lintian-accepted`
  list.
- `make repo` publishes the `.deb`'s source beside the repository under
  `pool/third-party-source/shadow-code/1.0.0/`: reproducible `git archive`s
  of ShadowCode and of the runtime and SPIRV-Headers commits the signed
  manifest names, the AppImage runtime sources tarball, and the signed
  metadata. It is not in `main/source`, because no source package here builds
  it.

ShadowCode updates arrive as system updates through the Shadowfetch
repository; `/etc/shadowcode/policy.yaml` turns off ShadowCode's own GitHub
update check so it does not announce a release this system cannot install yet.

`shadowfetch-doctor` has a new `shadowcode` check: it fails when
`/usr/bin/shadowcode` is missing and warns when a user-level ShadowCode in
`~/.local` takes precedence over the system one (which then stops receiving
system updates).

## Welcome: the agent network and three optional agents

After apps, Welcome asks one question, **Agent network: Online (recommended)
or Offline**, and offers exactly three optional agents, each downloaded only if
you pick it:

- **Grok Bot**, the official native desktop app (same integration as 4.1:
  administrator authentication, vendor update source; the pin moves from
  0.43.0 to 0.61.0).
- **Hermes Agent 0.21.5** (Nous Research, MIT), via `shadowfetch-hermes`.
- **OpenClaw 2026.9.6** (OpenClaw Foundation, MIT), via `shadowfetch-openclaw`.

The "Agent workspace" profile preselects all three. Then Welcome hands you to
ShadowCode.

## Hermes and OpenClaw: optional, user-home installers

Both helpers install into your home folder, as you, with no root, and are
never preinstalled; the source and package gates fail if either name appears
in a package list, a chroot include or any package relationship. Each has
`setup`, `update` (resolve the latest upstream release, verify it, show old →
new, install only after consent), `status --json`, `doctor`, `open`, `info`
and `uninstall [--purge-data]`, and each pauses while the agent network is
offline. The Control Center has a **Hermes & OpenClaw** page driven by each
helper's own status.

- **Hermes**: setup downloads the official installer that shipped with the
  pinned release (checked by SHA-256, byte count and git blob) and installs the
  release commit into `~/.hermes/hermes-agent`. About 2 GB. See `HERMES.md`.
- **OpenClaw**: setup runs `npm ci` with Debian's `nodejs`/`npm` from a
  Shadowfetch-generated `package-lock.json` shipped in
  `/usr/share/shadowfetch/openclaw/2026.9.6/`, verifying the tarball's
  SHA-512 integrity and npm registry signatures before any package script
  runs. `open` runs OpenClaw's local chat inside a Firebreak sandbox whose only
  writable folder is `~/Workspaces/openclaw`. Its Gateway service is never
  installed by setup; `shadowfetch-openclaw gateway enable` installs it only
  after confirmation and pins it to 127.0.0.1. See `OPENCLAW.md`.

## Smaller changes

- Mission engine: read-only queries retry a transient "database is locked"
  (six attempts, 0.05–0.5 s backoff) instead of failing the call.
- Missions: provenance names the provider that actually ran, receipts record
  the model, and an interrupted mission cleans up its partial work and can be
  undone.
- Login keyring: a login keyring is created and unlocked at login, so saving
  service keys (such as ShadowCode's API keys) no longer prompts for a
  keyring password.
- The live session no longer locks its screen after idle and does not show
  the KWallet setup prompt.
- "Element Workbench" is now Workbench; profiles recommend an agent network
  instead of an element.
- Plymouth, SDDM, GRUB, Calamares and the default wallpaper are restyled from
  the new emblem; the wordmark is two-tone.
- Shipped docs: new `SHADOWCODE.md`, `HERMES.md` and `OPENCLAW.md`;
  `LICENSES.md` and `SOURCES.md` list ShadowCode's licence and source.
- Licence: Shadowfetch's own packages move from MIT to GPL-3.0-or-later,
  matching the repository LICENSE. The DrKonqi pickup helper stays
  GPL-3.0-only and `shadowfetch-themes` LGPL-2.1-or-later.
- Guide (the System Passport) opens without crashing, both from Mission
  Control's sidebar and from Welcome's **Check this computer**. `make test`
  now checks every shipped Python file for undefined names.
- The **Shadowfetch Welcome** menu entry reopens setup at any time, also after
  setup is finished (it runs `shadowfetch-welcome --force` and no longer
  shares the login autostart entry).
- The Firebreak launcher stays open: it shows `shadowfetch-firebreak --help`
  and leaves a shell ready, instead of closing at once.
- Missions: the retry budget counts runs that actually happened, so
  cancelling and retrying a mission that never ran no longer uses it up. Real
  runs still count.
- `shadowfetch-control --help` lists every page, including `shadowcode` and
  `optional-agents`.

---

# Known issues

The first two are fixed by the 5.0.1 update, which arrives through the
Shadowfetch APT repository like any other update (`sudo apt update`, then
`fireproof update`). The third affects only the live USB, which an update
cannot change: 5.0.1 turns it off on the live USB, and a 5.0.0 USB stick keeps
it, so use the workaround.

- **ShadowCode 1.0.0's window grows each time it opens (Wayland).** Each
  launch restores a slightly larger window, which can end up extending under
  the panel. On a 1366x768 screen even the first window is larger than the
  screen, so the message box and status bar sit under the panel.
  **Workaround:** maximize the window (its maximize button, or Meta+PgUp);
  ShadowCode then does not save the size. Fixed in ShadowCode 1.0.1, part of
  5.0.1.
- **"database is busy" from Mission Control under very heavy disk load.**
  While a mission is finishing a step on a disk that is heavily loaded,
  Mission Control or a `shadowfetch-missions` command can briefly report
  "database is busy". **Workaround:** retry after a few seconds; the retry
  works. Fixed in 5.0.1.
- **Live USB: a package-list refresh about five minutes after login.** In the
  live session, KDE's update notifier refreshes the package lists about five
  minutes after you log in: a download of about 200 MB that also takes about
  350 MB of RAM, because the live session keeps its changes in memory.
  Installed systems are not affected. **Workaround,** if the computer is low
  on memory or the connection is metered: stay offline in the live session,
  or stop the notifier soon after logging in with
  `systemctl --user stop app-org.kde.discover.notifier@autostart.service`.
  5.0.1 turns the refresh off on the live USB.
- **Security advisory: earlier ISOs shipped a shared DKMS module-signing key.**
  Building the ISO ran DKMS (for `v4l2loopback-dkms`), which generated
  `/var/lib/dkms/mok.key` and `mok.pub`, and the private key shipped in the
  image. Every install from the same ISO has the same key, and anyone with that
  ISO can extract it. Confirmed in 4.1.0. 4.0.0 and earlier releases were built
  the same way, so treat them as affected too. The key only matters if you
  enrolled its certificate in Secure Boot. `shadowfetch-gpu` offers that
  enrolment on Secure Boot machines, and you may also have run
  `mokutil --import /var/lib/dkms/mok.pub` yourself. In that case, anyone with
  the ISO could sign a kernel module that your machine would trust. 5.0.0 images
  no longer contain the key: each machine generates its own the first time
  DKMS builds a module. Existing installs keep the old key until you replace it.
  If `sudo mokutil --test-key /var/lib/dkms/mok.pub` says the key is already
  enrolled:

  ```bash
  sudo mokutil --delete /var/lib/dkms/mok.pub   # choose a one-time password
  sudo rm /var/lib/dkms/mok.key /var/lib/dkms/mok.pub
  dkms status                                   # for each <module>/<version>:
  sudo dkms build --force <module>/<version> && sudo dkms install --force <module>/<version>
  sudo mokutil --import /var/lib/dkms/mok.pub   # the new per-machine key
  ```

  Reboot. In MOK Manager, choose **Delete MOK** and then **Enroll MOK**. If the
  key is not enrolled, you do not need to take any action. Deleting the two
  files is still a good idea. On a system upgraded from 4.x,
  `shadowfetch-doctor` reports the shared key as a `sec.dkms_mok` failure and
  prints these steps, and a one-time desktop notice at login points to them.
  Earlier ISOs also shipped a shared
  `ssl-cert-snakeoil` TLS key; 5.0.0 installs generate their own on first boot.
  If you pointed a service at the snakeoil key, run
  `sudo make-ssl-cert generate-default-snakeoil --force-overwrite`.
- **OpenClaw's security record.** OpenClaw has a long security-advisory record
  (700+ GitHub advisories since early 2026, including critical ones, new ones
  monthly). Shadowfetch's containment (pinned lockfile, Firebreak, no Gateway
  by default) narrows exposure; it does not make OpenClaw safe. A pinned
  release ages fast: run `shadowfetch-openclaw update` often. `update` and
  `setup --latest` use a lockfile npm generates at that moment, whose
  dependencies Shadowfetch has not reviewed. An enabled Gateway runs outside
  the sandbox with your file access.
- **Hermes is not sandboxed.** It runs commands and edits files as you.
- **The agent network does not set ShadowCode's own network mode.** In this
  tree, `shadowfetch-agent-network offline` (including an upgraded Ice
  machine) governs Firebreak sandboxes and the three optional agents only.
  ShadowCode starts Online by default; set **Settings › Permissions &
  network › Offline** in ShadowCode if you want it to run only local models.
  Vendor agents started by ShadowCode use their own sandboxes, not Firebreak.
- **Six acceptance cases are waived, not passed.** MISSION-01's code
  missions, GROK-01, GROK-VISUAL-01, UPGRADE-01's recovery leg,
  SHADOWCODE-01's soak memory check and STRESS-01's mission and container
  loops; see [Acceptance](#acceptance) for what each waiver does and does not
  cover.
- **The APT suite is still named `umbra`.** It is carried from 4.x, so 4.x
  sources lists keep working unchanged.
- ShadowCode needs glibc 2.39 or newer and Vulkan for GPU inference; without a
  Vulkan GPU its local runtime falls back to the CPU.
- **ShadowCode's first run can open light on the dark desktop** and saves
  "follow the system" as its appearance; pick **Dark** in ShadowCode's
  settings. Unchanged in 1.0.0; reported upstream.
- On a machine with no GPU render node (many VMs), ShadowCode's window process
  would idle at high CPU. Shadowfetch sets `WEBKIT_DISABLE_DMABUF_RENDERER=1`
  for the session only on such machines
  (`/usr/lib/systemd/user-environment-generators/60-shadowfetch-webkit-software-rendering`);
  ShadowCode 1.0.0 does not handle this itself yet.
- `drift_gate` reports 0 DRIFT and 4 BLOCKED, all pre-existing detected
  duplications with named remedies (an unnamed palette role, the Workbench
  page's own program constant at two sites, and the two NetworkManager
  connectivity implementations).

---

# Upgrading from 4.1

The supported in-place path is the signed Shadowfetch APT repository:

```bash
sudo apt update
fireproof update          # shadowfetch-update still works and means this
```

This pulls in `shadow-code` as a new dependency of `shadowfetch-desktop`.
Afterwards:

1. Log out and back in once, so the look migration runs.
2. Check `shadowfetch-agent-network status`. An Ice machine reports offline;
   change it if you used Ice only for its look.
3. Update any script that called `shadowfetch-element`, `shadowfetch-codex` or
   `shadowfetch-code-agent`, passed `sf.element=`, or parsed `element` /
   `blocked_by_ice` / `recommended_element` from JSON output.
4. Open ShadowCode and connect your services in **Settings › Accounts**.

---

# Verify the download

The same method as 4.1. Use the exact accepted filename; these commands
download and verify files, they do not write a USB device.

```sh
ISO='shadowfetch-5.0.0-amd64.iso'
ARTIFACT_BASE='https://www.shadowfetch.com/linux/download'
curl --fail --location --remote-name "$ARTIFACT_BASE/$ISO"
curl --fail --location --remote-name "$ARTIFACT_BASE/$ISO.sha256"
curl --fail --location --remote-name "$ARTIFACT_BASE/$ISO.asc"
curl --fail --location --remote-name https://www.shadowfetch.com/linux/shadowfetch.gpg.asc
gpg --show-keys --with-fingerprint shadowfetch.gpg.asc
# The fingerprint must be 8F13 CE15 35EE 1F4A 2916 A1F7 3C5C 900B 7BE8 0CA1.
gpg --import shadowfetch.gpg.asc
gpg --verify "$ISO.asc" "$ISO"
sha256sum --check "$ISO.sha256"
```

Continue only if both the signature and the checksum verify. The expected
SHA-256 is `2d8a72e044e8061bd616b2b4668425cc4d4ec0480a98975f961c0e58cba95e21`.
A GPG warning about personal key trust is not a failed signature. The SBOM
(`sbom-5.0.0.cdx.json`), package manifest (`packages-5.0.0.manifest`) and QA
evidence bundle (`evidence-bundle-5.0.0.tar.gz`) are on the download server and
attached to the v5.0.0 GitHub release: https://github.com/Shadowfetchapps/shadowfetch-linux/releases/tag/v5.0.0
(SHA-256: SBOM `552ce02cf0198244e643b62941c38b1ba2416e547fbfdd8d0c7a6a0cd71f22d9`, package manifest `4ff041494df0538dd041335fc823e0983c483223b33f94633b611c3154c1988b`,
evidence bundle `f7a95161fc787fe9ef3680c9550850b42e174f8860de51e818c8a54f3b13f260`).

---

# Release state

Measured on the release image's own source
(`9587a7ca348e817870baffa9d390754acf331266`) and on the image itself
(SHA-256 `2d8a72e044e8061bd616b2b4668425cc4d4ec0480a98975f961c0e58cba95e21`),
not copied forward from an earlier candidate:

- `make test`: PASS, 2578 tests, including the adversarial
  suites.
- `source_gate`: PASS (SRC-01).
- `package_gate`: PASS, including the ShadowCode `.deb` signature, pin and
  bytes and lintian against the reviewed list.
- `iso_gate`: PASS; `shadow-code` 1.0.0 in the image is byte-identical to the
  signed archive.
- `drift_gate`: 0 DRIFT, 4 BLOCKED (the pre-existing findings under Known
  issues).
- Acceptance (`qa/5.0.0/acceptance.json`): 12 cases pass, including
  EVIDENCE-01 (the evidence bundle), and 6 are waived with the approver
  recorded; PUB-01 is proven after publication (below).

Earlier candidates (ISO `c8ea7ef0…` with ShadowCode 0.34.2, and the 1.0.0
candidates `c64c3493…`, `2abd1f6f…` and `a83d7d8a…`) are superseded; nothing
here is claimed from them.

ShadowCode 1.0.0 pin, measured before the rebuild: signature verified against
the vendored trust policy (commit `e0ab2655`, key maximum 1.0.0), host smoke
7/7, lintian 0 errors.

---

# Acceptance

Acceptance is recorded in `qa/5.0.0/acceptance.json` against this exact ISO
(SHA-256 `2d8a72e044e8061bd616b2b4668425cc4d4ec0480a98975f961c0e58cba95e21`).
Of the 18 prepublication cases, 12 pass and 6 are waived by the release
owner. `PUB-01` is proven after publication, against what is public.

**Pass:** SRC-01, PKG-01, ISO-01, FIRE-01 (live desktop and Mission Control),
ICE-01 (offline agent network), INSTALL-01 (fresh BIOS and UEFI installs boot
from disk), SCOPE-01, DURABLE-01, RECOVERY-01, VISUAL-01 (screenshots at
1920x1080 and 1366x768), RESOURCE-01 (mission admission limits and desktop
responsiveness while missions and media exports run) and EVIDENCE-01 (the
checksum, signature, SBOM, package manifest and QA evidence bundle for this
ISO).

**Waived, approved by the release owner.** Four waivers are for an account or
harness limit. Two, SHADOWCODE-01 and STRESS-01, are for checks that did not
pass. The release owner chose to ship this ISO; 5.0.1 fixes the known issues
behind them (ShadowCode's window, "database is busy" and the live-USB refresh).
Each waiver states what was and was not proven:

- **MISSION-01**, code sub-part: a real code mission needs a paid vendor
  account, which the QA environment does not hold. The media mission and the
  cited-report mission pass with validated artifacts.
- **GROK-01** and **GROK-VISUAL-01**: signing in to Grok Bot needs an X/Grok
  account. Package integrity, installation, launch to the sign-in screen and
  the URL-handler (callback) registration are proven; a signed-in session and
  its screenshot are not.
- **UPGRADE-01**, recovery leg: the VM harness has no recovery leg for
  upgrades. The 4.1 → 5.0 upgrade itself passed, with every migration check:
  the gold accent applied, the `shadowfetch-doctor` shared-MOK check, the
  one-time notice, the agent-network migration, the retired launchers
  removed and user data preserved. Rollback after an upgrade was not
  exercised.
- **SHADOWCODE-01**, soak memory check: the `shadowcode` case passes (the
  pinned 1.0.0 launches, runs and exits cleanly). In the 24-cycle open/close
  soak every window opened and closed cleanly, with no crash, no leftover
  process and idle CPU of 1.6–6.0%, but available memory fell by 680 MiB
  against a 256 MiB limit. A diagnostic rerun traced the drop to the one-time
  package-list refresh that KDE's update notifier starts about five minutes
  after login, written to the live session's RAM; ShadowCode's own memory
  stayed flat through that drop (see Known issues). Separately, ShadowCode
  1.0.0's window grows on each launch under Wayland (see Known issues). Not
  proven: a passing memory check on this ISO.
- **STRESS-01**, mission and container loops: in two full 45-minute runs of
  combined CPU, memory, disk, container, mission and desktop stress on an
  installed system, with ShadowCode open throughout, the system stayed
  healthy: no crashes, out-of-memory kills, failed units, thermal events or
  swap, and every CLI latency probe answered within its limit. Two workload
  loops stopped early: mission CLI calls returned "database is busy" while the
  worker finished a media step under disk stress (see Known issues), and in
  both runs the container loop stopped at its third cycle, when a
  `podman run --rm` client produced the correct result but did not exit
  within 120 s. Not proven: a complete mission and container loop under 45
  minutes of stress on this ISO. The "database is busy" fix is in 5.0.1 (see
  Known issues).
