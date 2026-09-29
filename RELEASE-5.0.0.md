# Shadowfetch Linux 5.0.0 — ShadowCode

Edition: **ShadowCode**. Subtitle: **"One Harness. All Models."** Codename:
Umbra; the APT suite stays `umbra` (provisional in
`tools/release/versions/5.0.0.toml` until the first 5.0 package is published;
keeping it means 4.1 systems receive 5.0 from the suite they already track).
Signing fingerprint unchanged:
`8F13 CE15 35EE 1F4A 2916 A1F7 3C5C 900B 7BE8 0CA1`.

Status: **NOT RELEASED.** No 5.0.0 ISO exists yet. Every artifact fact below
marked `TODO(iso)` is filled in after the build.

- Version: 5.0.0
- Codename / repository suite: `umbra`
- Architecture: amd64; desktop KDE Plasma 6.7.4 (Frameworks 6.30, Qt 6.10.2); installer Calamares
- Base: Debian testing snapshot `20260929T000000Z`; kernel Linux 7.2.6; systemd 262; Mesa 26.1.6
- Source branch: `release/5.0.0` (the checkout path is named for 4.0.0 and is
  not the version)
- Source commit / tree the image is built from: `TODO(iso)` / `TODO(iso)`
- ISO: `shadowfetch-5.0.0-amd64.iso`, TODO(iso) bytes, SHA-256 `TODO(iso)`,
  detached signature `shadowfetch-5.0.0-amd64.iso.asc` verifying against
  `8F13CE1535EE1F4A2916A1F73C5C900B7BE80CA1`
- Publication date: TODO(iso)
- ShadowCode: 0.34.2, tag `v0.34.2`, commit
  `3f81044e1fe3d8f24cc1293e8efde79db5533213`; `ShadowCode_0.34.2_amd64.deb`,
  27,549,024 bytes, SHA-256
  `203593292de40a97c10e110d2bcec8c378aa2347480208fe1771967ab96cec44`

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
| ShadowCode | not included | 0.34.2 |
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

ShadowCode 0.34.2 is the desktop's coding agent. `shadowfetch-desktop` depends
on `shadow-code (>= 0.34.2)`. Open a project, pick a model, describe the
change, approve actions, then review, keep or undo each change. Models come
from:

- **Subscriptions**: Codex, Claude Code, Cursor, Antigravity or Grok, through
  each vendor's CLI and its own login. ShadowCode never reads or stores vendor
  credentials, strips provider API-key variables from vendor CLIs, and never
  buys credits.
- **API keys**: OpenRouter, billed per token.
- **This computer**: a bundled local runtime (Vulkan GPU or CPU) under
  `/usr/lib/shadowcode/`, with free models to download on request.

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
  `pool/third-party-source/shadow-code/0.34.2/`: reproducible `git archive`s
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
- "Element Workbench" is now Workbench; profiles recommend an agent network
  instead of an element.
- Plymouth, SDDM, GRUB, Calamares and the default wallpaper are restyled from
  the new emblem; the wordmark is two-tone.
- Shipped docs: new `SHADOWCODE.md`, `HERMES.md` and `OPENCLAW.md`;
  `LICENSES.md` and `SOURCES.md` list ShadowCode's licence and source.

---

# Known issues

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
- **VM acceptance has not run.** TODO(iso): all 19 required cases in
  `qa/5.0.0/acceptance.json`, including the new `SHADOWCODE-01` (with the
  `shadowcode` and `shadowcode-soak` VM cases), are `pending`. Record here which
  pass, which are waived and by whom.
- **The upgrade path is unproven.** TODO(iso): `UPGRADE-01` needs an installed,
  APT-updated 4.1.0 image; the harness still names the 3.5.0 base image, which
  no longer exists on the build host.
- **The APT suite is provisional.** `umbra` is carried from 4.x; it must be
  confirmed before the first 5.0 package is published.
- ShadowCode needs glibc 2.39 or newer and Vulkan for GPU inference; without a
  Vulkan GPU its local runtime falls back to the CPU.
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
ARTIFACT_BASE='https://www.shadowfetch.com/linux/download'   # TODO(iso): confirm once published
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
SHA-256 is `TODO(iso)`. A GPG warning about personal key trust is not a failed
signature. The SBOM, package manifest and QA evidence bundle will be attached
to the v5.0.0 GitHub release: TODO(iso).

---

# Release state

TODO(iso): record the gates as measured on the final candidate — `make test`,
`source_gate`, `package_gate` (including lintian on the ShadowCode `.deb`),
`iso_gate`, `drift_gate`, `acceptance --version 5.0.0` — with dates, exit
codes and test counts, measured rather than copied forward.

Measured on the source tree before the ShadowCode 0.34.2 pin, per commit
`1d4cca8`: `make test` PASS (2,402 tests), `drift_gate` 0 DRIFT / 4 BLOCKED,
ShadowCode host smoke 7/7, `fetch_shadowcode.py` PASS including source
archives. Per commit `fa82767`, the 0.34.2 signature, fetch, source archives
and 7-check host smoke pass and lintian reports 0 errors.
