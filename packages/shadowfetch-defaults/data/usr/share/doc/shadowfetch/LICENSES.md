# Shadowfetch Linux — Licensing

Shadowfetch Linux is a Debian derivative. It is composed almost entirely of
free/open-source software from Debian, KDE, and other upstreams, each licensed
under its own terms (GPL-2.0+, GPL-3.0+, LGPL-2.1+, MIT, BSD, Apache-2.0, and
others). Those licenses and the corresponding source are available from Debian
(https://www.debian.org/distrib/packages) and each project upstream.

## Shadowfetch's own components

Each Shadowfetch package states its license in
`/usr/share/doc/<package>/copyright`, and that file is authoritative. Most
Shadowfetch packages are published under the MIT License. The exceptions:
shadowfetch-fireline and grub-btrfs are GPL-3.0-or-later; the DrKonqi pickup
helper is GPL-3.0-only (its compiled KDE source keeps GPL-3.0-only OR
LicenseRef-KDE-Accepted-GPL); and the theme assets derived from KDE's Breeze
(shadowfetch-themes: colour schemes, SDDM theme, look-and-feel) remain under
Breeze's LGPL-2.1+ terms.

## ShadowCode

ShadowCode, the preinstalled coding agent (package `shadow-code`), is
Copyright 2026 Shadowfetch and licensed under the Apache License 2.0. Its
licence and NOTICE file are installed as
`/usr/share/doc/shadowcode/notices/ShadowCode-LICENSE` and
`/usr/share/doc/shadowcode/notices/ShadowCode-NOTICE`; the Apache licence
requires that NOTICE to travel with any copy you redistribute, and it does not
grant permission to use the name "ShadowCode" for other products. The
third-party components bundled inside ShadowCode, including its local model
runtime, keep their own licences; their notices are under
`/usr/share/doc/shadowcode/notices/` and `/usr/lib/shadowcode/NOTICES/`.
Shadowfetch republishes the `.deb` exactly as the upstream publisher built and
signed it. See `SHADOWCODE.md`.

ShadowCode's subscriptions and API keys connect to third-party services
(Codex, Claude Code, Cursor, Antigravity, Grok, OpenRouter). Those vendors'
command-line tools are not part of the image, and their terms govern your
account. Models you download in ShadowCode keep their own licences.

## Optional agents, downloaded only on request

None of these is embedded in the ISO. Each is downloaded only after you choose
it, and Shadowfetch's licences grant no rights to them.

- **Hermes Agent** (Nous Research, MIT) is installed into your home folder by
  `shadowfetch-hermes`. See `HERMES.md`.
- **OpenClaw** (OpenClaw Foundation, MIT) is installed into your home folder
  by `shadowfetch-openclaw`. See `OPENCLAW.md`.
- **Grok Bot**, the native desktop application, is downloaded from its official
  vendor after administrator authentication. Its binary is proprietary and is
  not redistributed by Shadowfetch; Grok Bot and Cursor service terms apply.
  `GROK-BOT.md` explains its native package, cloud account, and vendor update
  source. This integration does not imply vendor endorsement.

## Written offer for source

The complete corresponding source for the Shadowfetch packages is:
  * published at https://github.com/Shadowfetchapps/shadowfetch-linux
  * listed in the signed APT source index,
      https://www.shadowfetch.com/linux/apt/dists/umbra/main/source/Sources
  * for ShadowCode, published beside the repository under
      https://www.shadowfetch.com/linux/apt/pool/third-party-source/shadow-code/
    (see `SOURCES.md`)

For source of any upstream Debian/KDE component, contact
signing@shadowfetch.com and we will direct you to, or provide, the exact
corresponding source for the version shipped.
