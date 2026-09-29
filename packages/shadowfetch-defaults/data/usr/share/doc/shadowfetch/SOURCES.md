# Shadowfetch Linux — Corresponding Source

Shadowfetch Linux is built from Debian and KDE. The corresponding source for
every upstream package is available from Debian
(https://www.debian.org/distrib/packages and its snapshot archive) and from
each project upstream, under that package's own license.

## Shadowfetch's own components

Each Shadowfetch package is built from this project's source tree, and its
license is stated in `/usr/share/doc/<package>/copyright` (see `LICENSES.md`).
Their source packages are in the signed APT source index below.

## ShadowCode

ShadowCode (package `shadow-code`) is the one package in the image that
Shadowfetch does not build. Shadowfetch republishes the `.deb` exactly as the
ShadowCode publisher built it, after checking the publisher's Ed25519
signature over the release metadata, the pinned version, and the file's size
and SHA-256. Upstream: https://github.com/Shadowfetchapps/ShadowCode .

Because nothing in this project builds that `.deb`, it has no entry in the
Debian source index; a source package there would claim to build it. Its
source is published beside the repository instead, one folder per version:

    https://www.shadowfetch.com/linux/apt/pool/third-party-source/shadow-code/<version>/

That folder holds:

- one `git archive` per input the signed release names: ShadowCode at the
  release commit, and the local model runtime and SPIRV-Headers at the
  commits its signed `RELEASE-MANIFEST.json` records. They are listed with
  their SHA-256 in `SOURCE-SHA256SUMS`, and
  `gzip -dc <file> | git get-tar-commit-id` prints the commit each holds.
  Rust and npm dependencies are pinned by the `Cargo.lock` and UI lockfile
  inside the ShadowCode archive.
- the release's AppImage runtime sources tarball. This is the corresponding
  source of the AppImage runtime the publisher also ships; it is not the
  source of the `.deb`.
- the four signed release metadata files (`RELEASE-AUTH`, `RELEASE-AUTH.sig`,
  `SHA256SUMS`, `RELEASE-MANIFEST.json`) and a `README`.

## Optional agents

None of these is embedded in the ISO; each is downloaded only after you choose
it, and installed for your user.

- **Hermes Agent** (MIT). `shadowfetch-hermes` downloads the official installer
  that shipped with the pinned release, checks its SHA-256, and installs the
  release commit. Source: https://github.com/NousResearch/hermes-agent . See
  `HERMES.md`.
- **OpenClaw** (MIT). `shadowfetch-openclaw` installs the pinned release from
  the npm registry with `npm ci` and the package-lock.json in
  `/usr/share/shadowfetch/openclaw/<version>/`, which pins every package by
  SHA-512. Source: https://github.com/openclaw/openclaw . See `OPENCLAW.md`.
- **Grok Bot** is a proprietary native cloud-agent desktop application. The
  Shadowfetch setup helper is MIT licensed; the vendor application is not
  included in this source tree or the ISO. After selection and administrator
  authentication, the helper downloads the exact official Debian package and
  checks its SHA-256, byte count, package name, version and architecture
  before installing it through APT. The vendor package adds its normal signed
  update source. See `GROK-BOT.md` and
  `/usr/share/shadowfetch/grok-bot/release.json` for provenance and vendor terms.

### Written offer for corresponding source

The complete corresponding source for this release is published in the signed
APT source index, alongside the matching binary packages:

    https://www.shadowfetch.com/linux/apt/dists/umbra/main/source/Sources

The signed InRelease authenticates the index and its SHA-256 references each
source archive and Debian source control file. ShadowCode's source is in the
`third-party-source` folder described above. The project's public home and
issue tracker are at https://github.com/Shadowfetchapps/shadowfetch-linux .
For the corresponding source of any upstream Debian/KDE component shipped in
this image, email signing@shadowfetch.com and we will provide the exact source
for the version shipped, at no more than the cost of distribution.
