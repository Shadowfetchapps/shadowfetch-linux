# ShadowCode: vendored trust and signed release metadata

ShadowCode (`Shadowfetchapps/ShadowCode`) is preinstalled in Shadowfetch Linux
5.0. It is the one package in the image that is **not built from this tree**:
Shadowfetch republishes the `.deb` exactly as upstream CI built and signed it.
Everything here exists so that "these are the bytes the upstream publisher
signed, for the version we pinned" is checked, not assumed, at every step.

## Layout

| path | what it is |
| --- | --- |
| `trust/policy` | upstream `release/trust/policy`: repository identity, version floor, and each signing key's authorised version interval |
| `trust/<key-id>.pem` | the Ed25519 public key (SPKI PEM) named by the policy |
| `upstream/verify-native-release.sh`, `upstream/native-release-auth-lib.sh` | upstream's offline verifier, byte for byte. Checks the Ed25519 signature over `RELEASE-AUTH` (`openssl pkeyutl -verify -rawin`), the key interval, the manifest and checksum digests, and an asset's size and SHA-256 |
| `<version>/RELEASE-AUTH`, `RELEASE-AUTH.sig`, `SHA256SUMS`, `RELEASE-MANIFEST.json` | the four signed metadata files of that release |

Reviewed trust commit: `e923e5e2758ef6189738f944c86653ca0e87e93f` (tag `v1.0.1`)

`trust/` and `upstream/` are byte-for-byte copies of `release/trust/` and
`scripts/` at that commit. Against the first reviewed commit, `e15c4480`
(`v0.33.1`), the only change is the key's authorised maximum, `0.33.1` ->
`0.34.2` (at `3f81044e`, `v0.34.2`) -> `1.0.0` (at `e0ab2655`, `v1.0.0`) -> `1.0.1`; the key, the version floor
and both verifier scripts are unchanged. **The policy authorises key
`f0c60ff8…3b7f` for ShadowCode 0.33.0 through 1.0.1.** A later release needs
step 1 below again with a published commit whose policy authorises it.

The version pin itself is **not** here. It is `tools/release/shadowcode.toml`,
the one place the shipped version, commit and per-asset size/SHA-256 live.

## Who checks what

| step | tool | what it re-verifies |
| --- | --- | --- |
| `make packages` | `tools/fetch_shadowcode.py` | downloads into `build/cache/shadowcode/<v>/`; published metadata byte-identical to `vendor/shadowcode/<v>/`; upstream verifier on the `.deb` **and** the runtime-sources tarball; signed `RELEASE-AUTH` == pin; `dpkg-deb` control fields == pin; stages `build/shadow-code_<v>_amd64.deb` |
| `make repo` | same tool, `--offline --stage-sources` | includes the `.deb` (as `Section: devel`, which its control file lacks); publishes the `.deb`'s source archives, the runtime-sources tarball and the signed metadata under `repo/pool/third-party-source/shadow-code/<v>/` |
| `make package-gate` | `tools/release/package_gate.py` | signature + pin + bytes again, from scratch; `shadowfetch-desktop` floor == pin; llama.cpp files only under `/usr/lib/shadowcode/`; pooled `.deb` and published sources match the pin; container install + `shadowcode --version` |
| `make iso-gate` | `tools/release/iso_gate.py` | `shadow-code` installed at exactly the pinned version; launcher, desktop entry and `llama-server` byte-identical to the signed archive; llama.cpp/ggml files nowhere but `/usr/lib/shadowcode/` |
| no VM | `tools/shadowcode_smoke.py` | extracts the verified `.deb`, `ldd`s every ELF, runs `shadowcode --version` headless and the bundled `llama-server`/`llama-cli --version` |
| VM | `tools/acceptance/vm_acceptance.py run --case shadowcode` / `shadowcode-soak` | launches it in the live session; see `tools/acceptance/README.md` |

Nothing trusts an earlier step: there is no "verified" marker file to forge.

## Bumping to a new ShadowCode release

Only after the tag is **published** (`gh release view v<version> -R
Shadowfetchapps/ShadowCode`).

1. A version beyond the vendored policy's maximum first needs the widened
   policy from a **published, reviewed** upstream commit (review
   `git diff e15c4480 <sha> -- release/trust scripts/verify-native-release.sh scripts/native-release-auth-lib.sh`):

       git -C <ShadowCode clone> fetch origin --tags
       python3 tools/bump_shadowcode.py --refresh-trust \
           --trust-commit <published commit whose policy authorises the new version> \
           --shadowcode-checkout <ShadowCode clone>

   It refuses a commit on no remote branch or tag, and refuses any key this
   tree has not already trusted (a new key is a separate reviewed change).
   1.0.2 and later need the same step again with a policy that authorises them.

2. Bump (verifies the signature with the currently pinned release as
   `--previous-dir`, so downgrades and same-version republications refuse):

       python3 tools/bump_shadowcode.py <version> --dry-run
       python3 tools/bump_shadowcode.py <version>

   This writes `vendor/shadowcode/<version>/`, rewrites
   `tools/release/shadowcode.toml`, and sets `shadow-code (>= <version>)` in
   `packages/shadowfetch-meta/debian/control`. Re-running it is a verified
   no-op.

3. Build, gate, image, accept:

       python3 tools/fetch_shadowcode.py && python3 tools/shadowcode_smoke.py
       make packages repo
       make package-gate
       make iso                  # sudo; runs sign + iso-gate
       make vm-acceptance VM_CASE=shadowcode
       make vm-acceptance VM_CASE=shadowcode-soak VM_ACCEPTANCE_ARGS=--record

   Since 0.34.0 ShadowCode Recommends `bubblewrap`; `--apt-recommends true` in
   `live-build/auto/config` installs it in the image.

## Source distribution

Makefile's `repo` target promises complete corresponding source in
`main/source`. ShadowCode cannot be put there honestly: `main/source` is a
Sources index of `.dsc` packages, and a `.dsc` claims "this builds that binary".
Nothing here builds the ShadowCode `.deb` -- a fabricated source package would
be a false statement in a signed index.

What `make repo` publishes instead, beside the index and uploaded with the rest
of `pool/`, is `pool/third-party-source/shadow-code/<version>/`: the release's
`ShadowCode_<v>_appimage-runtime-sources.tar.gz` and its four signed metadata
files. Be precise about what that tarball is: it is the corresponding source of
the **AppImage runtime** (musl, squashfuse, fuse3, zstd … under `sources/`),
not of the `.deb`, which does not contain that runtime.

The `.deb`'s own source is published in the same folder: one `git archive` per
input the signed release names -- ShadowCode at the commit in `RELEASE-AUTH`,
and the llama.cpp and SPIRV-Headers commits in `RELEASE-MANIFEST.json`
(`runtime_pin`) -- as `<name>-<commit>.tar.gz`, listed in `SOURCE-SHA256SUMS`.
Each is fetched by commit id and made with `git archive | gzip -n`, so it is
reproducible, and `gzip -dc <file> | git get-tar-commit-id` prints the commit
it holds; the package gate checks both. Rust and npm dependencies are not
vendored: the ShadowCode archive carries `Cargo.lock` and the UI lockfile, whose
digests the signed manifest records. Once upstream attaches a signed source
tarball to its releases, mirror that instead.
