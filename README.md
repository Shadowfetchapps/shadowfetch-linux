# Shadowfetch Linux 5.0 — ShadowCode

**One Harness. All Models.**

Shadowfetch Linux is an independent Debian testing derivative with KDE Plasma 6, a creative desktop, reviewed updates and recovery tools. Version 5.0 is built around **ShadowCode**, a coding agent that comes preinstalled: open a project, pick a model, describe the change, watch the agent work, then review the diff.

**What that means on this desktop.** ShadowCode runs the models you already have access to, from one window:

- **Subscriptions** — Codex, Claude Code, Cursor, Antigravity or Grok, through each vendor's own command-line tool and sign-in. You install the vendor's CLI; ShadowCode's **Connect** runs the vendor's login. It does not read or store vendor credentials, and it never buys credits.
- **API keys** — an OpenRouter key if you have no subscription, billed per token to your OpenRouter account.
- **On this computer** — a bundled local model runtime (Vulkan GPU or CPU) and a short list of free models to download, one recommended for your hardware. Nothing downloads until you choose it.

Nothing is connected until you sign in. Welcome's last step opens ShadowCode so you can connect what you have. 5.0 also **changes behaviour that 4.1 setups depend on**: Fire and Ice are gone, several commands are removed, and the Fire/Ice network switch is now a separate setting. Read the [release notes](RELEASE-5.0.0.md) before upgrading.

<!-- TODO(iso): add 5.0 screenshots (ShadowCode, Welcome, Mission Control) once VISUAL-01 has captured them from the release ISO. -->

[Download](https://www.shadowfetchlinux.org/download) · [Screenshots](https://www.shadowfetchlinux.org/screenshots) · [Release notes](RELEASE-5.0.0.md) · [ShadowCode](https://github.com/Shadowfetchapps/ShadowCode)

## Current release

| Fact | Value |
| --- | --- |
| Version / codename | 5.0.0 / Umbra |
| Edition | ShadowCode — "One Harness. All Models." |
| Publication date / channel | TODO(iso) / stable |
| ISO | shadowfetch-5.0.0-amd64.iso |
| Size | TODO(iso) bytes |
| SHA-256 | `TODO(iso)` |
| ISO product source commit / tree | `TODO(iso)` / `TODO(iso)` |
| Base / desktop | Debian testing snapshot 20260929T000000Z / KDE Plasma 6.7.4 |
| Kernel | Linux 7.2.6 |
| ShadowCode | 0.34.2 (`shadow-code`, republished unmodified from the signed upstream `.deb`) |
| Architecture / APT suite | amd64 / `umbra` |
| Final boot acceptance | TODO(iso): VM acceptance not yet run; all 19 cases in `qa/5.0.0/acceptance.json` are pending |

Signing-key fingerprint: `8F13 CE15 35EE 1F4A 2916 A1F7 3C5C 900B 7BE8 0CA1`.

## What is on the desktop

| Feature | What you can do |
| --- | --- |
| **ShadowCode** | Preinstalled coding agent. Connect subscriptions, an OpenRouter key or a local model in **Settings › Accounts**; approve actions, review and undo changes. See `/usr/share/doc/shadowfetch/SHADOWCODE.md`. |
| **Agent network** | `shadowfetch-agent-network online\|offline` decides whether Firebreak agent sandboxes start with network. Offline also pauses Grok Bot, Hermes and OpenClaw. Choose it in Welcome, at the boot menu, or later from a terminal. |
| **Optional agents** | Welcome offers exactly three, each downloaded only if you pick it: **Grok Bot** (official native app), **Hermes Agent** 0.21.5 and **OpenClaw** 2026.9.6. Hermes and OpenClaw install into your home folder without root; OpenClaw opens only inside a Firebreak sandbox, with its Gateway off. |
| **Mission Control** | Create work in a Workbench project, watch the persistent queue, and review files, receipts and changes before accepting them. Code and report missions use a provider you name (`codex`, `claude` or `localmodel`); media exports run offline. |
| **Review and recovery** | Accept, cancel, retry or restore a mission's local checkpoint. Fireproof simulates and rechecks updates; supported Btrfs layouts give Phoenix snapshot recovery. |
| **Workbench, Guide, Ember, Firewatch** | Unchanged from 4.1 apart from the agent network replacing Fire/Ice; "Element Workbench" is now just Workbench. |

There is one look, ShadowCode: gold and steel on black. `shadowfetch-doctor` checks that ShadowCode is installed and warns when a user-level copy in `~/.local` shadows the system one.

## Scope, connections and data

ShadowCode has its own network setting (**Settings › Permissions & network**: Online, Web tools off, Offline). `shadowfetch-agent-network` is a separate Shadowfetch setting for Firebreak sandboxes and the optional agents; it does not change ShadowCode's. Vendor CLIs that ShadowCode drives follow their own execution rules and sandboxes.

Hermes runs commands and edits files as you and is not sandboxed. OpenClaw has a long security-advisory record (700+ GitHub advisories since early 2026); Shadowfetch discloses it before setup, pins its dependencies, runs it in Firebreak, never starts its Gateway unless you enable it (loopback only), and recommends updating it often.

No Shadowfetch account is required. No API keys, account sessions or model weights are bundled. Optional vendor applications and services keep their own network behaviour, terms and account requirements. Receipts, prompts and source material can be private; review files before sharing them. Local restoration cannot reverse external effects of an approved network action.

## Verify and install

The [download page](https://www.shadowfetchlinux.org/download) links the ISO, checksum and detached signature. TODO(iso): the SBOM, package manifest and QA evidence bundle will be attached to the v5.0.0 GitHub release. Use the exact accepted filename below. These commands download and verify files; they do not write a USB device.

```sh
ISO='shadowfetch-5.0.0-amd64.iso'
ARTIFACT_BASE='https://www.shadowfetch.com/linux/download'   # TODO(iso): confirm once published
curl --fail --location --remote-name "$ARTIFACT_BASE/$ISO"
curl --fail --location --remote-name "$ARTIFACT_BASE/$ISO.sha256"
curl --fail --location --remote-name "$ARTIFACT_BASE/$ISO.asc"
curl --fail --location --remote-name https://www.shadowfetch.com/linux/shadowfetch.gpg.asc
gpg --show-keys --with-fingerprint shadowfetch.gpg.asc
# Compare the fingerprint with the value above before importing.
gpg --import shadowfetch.gpg.asc
gpg --verify "$ISO.asc" "$ISO"
sha256sum --check "$ISO.sha256"
```

Continue only after the signature and checksum both verify. A GPG warning about personal key trust differs from a failed signature. Write the verified ISO with a USB image writer, then follow the [installation guide](https://www.shadowfetchlinux.org/install).

The boot menu offers a normal entry and an "agents offline" entry; the installer carries that choice to the installed system. The live session uses `shadow` / `shadow` with passwordless sudo. The installer creates the chosen user and removes the live account. See the [verification guide](https://www.shadowfetchlinux.org/verify), [Secure Boot guide](https://www.shadowfetchlinux.org/secure-boot) and [known issues](https://www.shadowfetchlinux.org/known-issues).

**Upgrading from 4.1:** `sudo apt update` then `fireproof update`. Read the [release notes](RELEASE-5.0.0.md) first; the upgrade removes `shadowfetch-element`, `shadowfetch-codex` and `shadowfetch-code-agent`, and moves an Ice machine to the offline agent network.

## Hardware and limits

Use a 64-bit Intel/AMD computer. Plan for 8 GB RAM and 100 GB disk space for a comfortable desktop; local models and demanding creative projects need more memory and storage. GPU inference in ShadowCode needs Vulkan. These planning figures are not a physical-hardware certification.

Secure Boot has no Microsoft-trusted shim. Intel/AMD use Mesa; NVIDIA setup is an explicit, simulate-first workflow. VM rendering tests do not establish physical GPU acceleration performance, and hybrid laptops need their own validation. Phoenix Points require a supported Btrfs root; ext4 does not provide the same snapshot recovery. Debian testing can change faster than Debian stable.

TODO(iso): acceptance summary. At the time of writing every case in `qa/5.0.0/acceptance.json`, including the new `SHADOWCODE-01`, is pending; the release notes will record which pass, which are waived and why.

## Build from source

The project uses Debian live-build, Debian source packages and a signed reprepro repository. Build on a Debian host; privileged build steps use sudo. Package builds and source tests do not require production publishing credentials.

```sh
make deps          # install build dependencies
make test          # focused behavior checks
make source-gate   # tests, parsers, linters and secret scans
make shadowcode    # fetch and verify the pinned ShadowCode .deb
make packages      # build the Debian packages into build/
```

The release build uses the configured signing key:

```sh
make repo          # signed local APT repository, plus ShadowCode's published source
make package-gate  # package, repository and clean-install checks
make iso           # privileged image assembly, signature and ISO gate
make qemu          # launch the resulting image for a smoke test
```

`make iso` produces `shadowfetch-5.0.0-amd64.iso` in the repository root. `VERSION ?= 5.0.0` and `CODENAME ?= umbra` live in the Makefile. The ShadowCode version lives only in `tools/release/shadowcode.toml`, moved by `tools/bump_shadowcode.py`; see `vendor/shadowcode/README.md`. Signing and publishing require the maintainer's private key and authorized publisher credentials, which are not in this repository. Consult `make help`, the [release notes](RELEASE-5.0.0.md) and `FINAL_OPERATIONS_CHECKLIST.md` before release operations; `.github/CI-SECRETS.md` records that the CI pipeline holds no secrets and why.

Source map: `packages/shadowfetch-missions/` contains the queue and execution engine; `packages/shadowfetch-control-center/` contains the native Qt UI; `packages/shadowfetch-welcome/` contains first boot; `packages/shadowfetch-defaults/` supplies integration helpers (agent network, Hermes and OpenClaw installers, doctor); `packages/shadowfetch-meta/` declares the desktop, including its ShadowCode dependency; `packages/shadowfetch-drkonqi-pickup/` contains the pinned KDE pickup source, correction and behavior checks. `vendor/shadowcode/` holds ShadowCode's vendored trust policy and signed release metadata. `live-build/` assembles the desktop, `tools/` holds gates and release tooling, and `qa/5.0.0/` indexes acceptance evidence.

## Support and contributing

Use [GitHub Issues](https://github.com/Shadowfetchapps/shadowfetch-linux/issues) for bugs, installation reports and hardware notes. Include the exact ISO and checksum result, firmware/boot mode, CPU/GPU/RAM, disk layout, the failing step and redacted `shadowfetch-health --json` output. For mission bugs, include the workflow, state and redacted receipt. Report ShadowCode application bugs to [ShadowCode's tracker](https://github.com/Shadowfetchapps/ShadowCode/issues). Report security-sensitive findings through [SECURITY.md](SECURITY.md).

Patches to packages, build tools, tests and documentation are welcome. Run `make source-gate` before submitting. Do not post password exports, private keys, tokens, private source files or unredacted account logs.

## Licensing

The ISO aggregates upstream packages under their respective licenses. Each Shadowfetch package states its license in its `debian/copyright`: most are MIT; `shadowfetch-fireline` and `grub-btrfs` are GPL-3.0-or-later; the Breeze-derived `shadowfetch-themes` is LGPL-2.1-or-later. The DrKonqi pickup helper's own code and packaging use **GPL-3.0-only**; its compiled KDE source retains **GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL**, and its source package includes the upstream archive, signature, release key and downstream patch. This repository's [LICENSE](LICENSE) is GPL-3.0. ShadowCode is **Apache-2.0** and its bundled components keep their own licenses; its corresponding source is published beside the APT repository under `pool/third-party-source/shadow-code/`. Other upstream source retains its original license notices. Shadowfetch and Umbra names, marks and artwork are reserved under [TRADEMARKS.md](TRADEMARKS.md); rebrand derivative distributions. Optional vendor applications retain their own licenses and terms. Shadowfetch Linux is independent and does not imply Debian or vendor endorsement.

[Docs](https://www.shadowfetchlinux.org/docs) · [Security model](https://www.shadowfetchlinux.org/security) · [Release feed](https://www.shadowfetchlinux.org/releases.json) · [Previous 4.1 release](RELEASE-4.1.0.md)
