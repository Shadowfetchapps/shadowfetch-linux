# FINAL_OPERATIONS_CHECKLIST

Everything an operator has to do, and the specific ways each step has gone
wrong before. The traps are here because each one cost real time or real damage;
none of them is hypothetical.

## Before anything: where the work lives

Branch `release/5.0.0` in `~/projects/shadowfetch-4.0.0` on the Linux publisher
(the directory name is historical, not the version). There is one authorized
publishing tree and `publish_release_4_0_0.py` refuses to run anywhere else
(`sys.platform != "linux" or ROOT != PUBLISHER or os.geteuid() == 0`, where
`PUBLISHER` is `~/projects/shadowfetch-4.0.0` of the running account). The `_4_0_0` in its name is a legacy label: it publishes the
release named by the sole non-historical `tools/release/versions/<v>.toml`,
which is 5.0.0.

## The 5.0.0 release run, in order

Each step names what must be true before the next one starts. Do not edit the
tree while any step from 3 onward runs (see the trap under *Running the
tests*).

1. **Settle the inputs.** The platform refresh (Debian snapshot, kernel,
   Plasma) has landed and the `TODO(platform)` placeholders in `README.md` and
   `RELEASE-5.0.0.md` are filled. The APT suite decision in
   `tools/release/versions/5.0.0.toml` (`codename = "umbra"`, marked
   PROVISIONAL) is made and the comment updated. Every `debian/changelog` top
   entry has its final date.
2. **ShadowCode pin.** Check whether upstream has published a newer release
   (`gh release view -R Shadowfetchapps/ShadowCode`). To move the pin, follow
   `vendor/shadowcode/README.md`: `--refresh-trust` only from a published,
   reviewed upstream commit, then

       python3 tools/bump_shadowcode.py <version> --dry-run
       python3 tools/bump_shadowcode.py <version>

   which rewrites `tools/release/shadowcode.toml`, `vendor/shadowcode/<v>/`
   and the `shadow-code (>= <v>)` floor in `packages/shadowfetch-meta/debian/control`.
   The pin is 1.0.0 (`v1.0.0`, `e0ab2655`; trust policy from that commit,
   key maximum 1.0.0). To keep it, re-running `bump_shadowcode.py 1.0.0` is a
   verified no-op. A move after the ISO is built means rebuilding the ISO and
   re-running acceptance: nothing measured on an image with another
   ShadowCode carries over.
   Add `vendor/shadowcode/<v>/lintian-accepted` only after reviewing each
   entry. Update the version in `README.md`, `RELEASE-5.0.0.md` and the
   meta changelog if it moved.
3. **Fetch and smoke ShadowCode.**

       make shadowcode          # tools/fetch_shadowcode.py: download, verify signature and pin
       make shadowcode-smoke    # tools/shadowcode_smoke.py: extract, ldd, --version, runtime --version

   The smoke must report 7/7.
4. **Tests and source gate.** `make test`, then `make source-gate`. The
   source gate fails if Hermes or OpenClaw is named outside their allowlist,
   or if a shipped Shadowfetch payload names a retired runtime. That scan
   includes the docs under `packages/shadowfetch-defaults/data/`, so
   ShadowCode's docs there describe its "local model runtime" generically.
5. **Packages and repository.**

       make packages repo
       make package-gate

   `make repo` also stages ShadowCode's source under
   `repo/pool/third-party-source/shadow-code/<v>/`. The package gate re-checks
   the signature, pin and bytes, the `shadowfetch-desktop` floor, the runtime's
   confinement to `/usr/lib/shadowcode/`, the reviewed lintian list, the
   published sources, and that nothing carries or depends on Hermes/OpenClaw.
6. **Drift gate.** `python3 tools/drift_gate.py` must print 0 DRIFT. Read the
   BLOCKED list; do not trust the exit code alone.
7. **Image.** `make iso` (sudo): builds, checksums, signs (`make sign`) and
   runs `make iso-gate`, which requires `shadow-code` at exactly the pinned
   version with launcher, desktop entry and runtime byte-identical to the
   signed archive. Record the ISO's size, SHA-256, source commit and tree.
8. **VM acceptance.** Drive every required case in `qa/5.0.0/acceptance.json`
   through the harness with `--record` (via `VM_ACCEPTANCE_ARGS`), for example

       make vm-acceptance VM_CASE=live-boot VM_ACCEPTANCE_ARGS=--record
       make vm-acceptance VM_CASE=shadowcode VM_ACCEPTANCE_ARGS=--record
       make vm-acceptance VM_CASE=shadowcode-soak VM_ACCEPTANCE_ARGS=--record

   `shadowcode-soak` records only after a `shadowcode` run of the same
   artifact has passed; together they prove `SHADOWCODE-01`. `UPGRADE-01`
   needs `--upgrade-base-image` pointing at an installed, APT-updated 4.1.0
   image; the 3.5.0 base the harness names no longer exists. Then
   `make vm-acceptance-verify` and `make acceptance-gate`, which refuses
   unless every required case is pass or waived with a named approver.
9. **Fill the documents.** `README.md` and `RELEASE-5.0.0.md` (and the
   website's `releases/5.0.0.json` and `src/data/linux-screenshots.ts`) carry
   the final wording with the image facts as tokens. Replace each with the
   value measured on the final ISO in step 7: `@@ISO_SHA256@@`,
   `@@ISO_SIZE@@` (bytes), `@@SRC_COMMIT@@`, `@@SRC_TREE@@` (full hashes),
   `@@SQUASHFS_SIZE@@` (bytes), `@@PKG_COUNT@@`, `@@TEST_COUNT@@` (the last
   `make test` on that source), and on the website also `@@ISO_SIZE_LABEL@@`
   (for example `4.09 GB (3.81 GiB)`). In the website's JSON,
   `"@@ISO_SIZE@@"` becomes a bare integer, quotes included. If acceptance
   ended differently from the recorded outcome (12 pass including
   `EVIDENCE-01`, 6 waived, `PUB-01` after publication), correct the
   Acceptance and Known issues sections in both documents first. Then
   `grep -rn 'TODO(iso)\|TODO(qa)\|TODO(platform)\|@@[A-Z0-9_]*@@'
   README.md RELEASE-5.0.0.md docs` must print nothing (`TODO(publish)` is
   filled in step 10). Re-run `python3 tools/drift_gate.py`.
10. **Publish.** `make publish` (runs `pre-release-check`, then
    `publish_release_4_0_0.py --apply`; see *Publishing* below). Then the
    public byte verification, the v5.0.0 GitHub release with the checksum,
    signature, SBOM, package manifest and evidence bundle, and the website,
    per `RELEASE-5.0.0.md`. `PUB-01` is proven last, against what is public.

## Running the tests

```sh
make test            # every package suite, then `make attacks`
make attacks         # the six adversarial suites on their own
```

`make test` takes about ten minutes and RUNS THE ATTACKS. The attacks are not a
separate quality tier: they assert what the system REFUSES, which a test written
against an implementation structurally cannot notice.

**Trap: do not edit the tree while a verification runs.** This was done twice in
this program and both results were garbage — `TEST=2` with attack failures that
did not reproduce on the settled tree. If you started a run and then changed a
file, throw the result away. There is no partial credit.

**Trap: background a long run with `setsid`.** A plain `&` over ssh dies with
the session:

```sh
setsid nohup make test </dev/null >/tmp/test.log 2>&1 &
```

**Trap: `pkill -f "make test"` from an ssh command line matches the ssh command
itself** and kills your own session (exit 255). Match on something narrower.

## The gates

```sh
python3 tools/release/source_gate.py     # runs make test, plus source checks
python3 tools/release/package_gate.py    # builds and inspects the debs
python3 tools/release/iso_gate.py        # builds and boots the image
python3 tools/drift_gate.py              # one authority per fact
make acceptance-gate                     # tools/release/acceptance.py, VERSION=5.0.0
```

`drift_gate` exits non-zero on DRIFT (a copy disagrees with its source) and
prints BLOCKED separately (a real duplication whose remedy is outside one
stage's territory). **A BLOCKED finding is an OBSERVATION with a named remedy,
not an enforced control** — read the printed report, never the exit code alone.

**Trap: `package_gate` fails on files git still tracks that the tree deleted.**
It reads payload from the index, so a rename or deletion that is unstaged reads
as "a payload file no package ships". Stage the deletion.

## Provider policy

Adding or changing a provider manifest requires re-sealing the pin:

```sh
python3 tools/providers/seal_policy.py          # dry run: prints the privilege diff
python3 tools/providers/seal_policy.py --yes    # writes it
```

It is deliberately NOT called by make, by a gate or by CI. A tool that silently
re-digests whatever manifests happen to be present turns the pin into
decoration. Read the printed diff: each line is a privilege a provider will be
permitted to request.

**Trap: the `approved_note` is not decoration either.** Write what was actually
reviewed and what was accepted as a known cost. The two notes added this phase
name the credential that reaches the sandbox and the single egress destination a
real run was observed to contact.

## Credentials

Per-provider environment files live in `~/.config/shadowfetch/missions/*.env`,
mode 0600. The worker reads the DIRECTORY and takes only the identities the
provider registry declares, so a stray file cannot inject `PATH` or
`LD_PRELOAD`; a value already exported wins over a file.

**Trap, now fixed, worth knowing:** the systemd unit used to name ONE file
(`codex.env`), so a second provider's key reached nothing at all while its
readiness reported it present. If you add a provider, you do not edit the unit.

## Publishing

```sh
python3 tools/publish_release_4_0_0.py            # plan only
python3 tools/publish_release_4_0_0.py --apply    # uploads
```

Order is a control: the signed APT `InRelease` is the last of the objects, the
ISO's bytes are then streamed back and compared, and only after that is
`releases/CURRENT.json` written. Nothing that DIRECTS a reader is written before
the thing it directs them to is present and proven. `--published` defaults to
the ISO's mtime so re-running rewrites nothing.

## Packages-only point update (`--apt-only`)

A point release that ships no ISO (5.0.1 is the first). Installed systems take
it with `sudo apt update; fireproof update`; the previous ISO stays the
download, and the website keeps naming it. The publisher has a mode for exactly
that and nothing more.

**Selecting it.** Either `delivery = "apt-only"` under `[release]` in
`tools/release/versions/<v>.toml` (the reviewed way: the decision is recorded
with the release), or `--apt-only` on the command line. It is never inferred
from the tree: an ISO release whose image is missing is refused, not quietly
published as packages only. Any `delivery` other than `iso` or `apt-only` is
refused, flag or no flag.

```toml
[release]
delivery = "apt-only"

[apt_only]
# Optional. The release whose ISO this update is applied on top of. Default:
# the newest earlier release whose data does not say apt-only (for 5.0.1, 5.0.0).
base_release = "5.0.0"
# Optional. Cases required IN ADDITION to SRC-01, PKG-01 and UPGRADE-01.
acceptance = ["DURABLE-01"]
```

**What it writes, in order:** the repository key (`shadowfetch.gpg.asc`,
immutable: byte-identical to what is published or the run refuses), every file
in `repo/pool/`, the index files under `apt/dists/<codename>/`, and then
`Release.gpg`, `Release` and `InRelease` last. Each object, uploaded now or
already present, is streamed back and hashed before the next one is written,
so no index names a package, and `InRelease` names no index, that the bucket
was not shown to hold. **Never written:** the ISO, its `.asc` and `.sha256`,
the evidence files, `releases/CURRENT.json`. Nothing is deleted. The index
files under `apt/dists/` are the only objects it may replace; a different
immutable object anywhere in the plan refuses the whole run before the first
upload.

**Preconditions, each a refusal:**

1. **Acceptance subset** of `qa/<v>/acceptance.json`. `SRC-01`, `PKG-01` and
   `UPGRADE-01`, plus any `[apt_only].acceptance`, are present, still
   `required`, `prepublish`, and `pass` with evidence or `waived` with an
   approver and a reason. Image cases (`ISO-01`, `INSTALL-01`, `VISUAL-01`,
   `EVIDENCE-01`, ...) may stay `pending`. No case may be recorded `fail`. The
   manifest's `artifact` block says what the subset was run against:
   * `iso_path` / `iso_sha256` name the **base image**, which for 5.0.1 is
     5.0.0's `2d8a72e0...` exactly as `qa/5.0.0/acceptance.json` records it.
     It is the image `UPGRADE-01` starts from. Evidence must be bound to it,
     and `acceptance.py record` stamps whatever `iso_sha256` says, so set it
     before recording anything.
   * `apt_packages_sha256` / `apt_sources_sha256` are the SHA-256 of
     `repo/dists/<codename>/main/binary-amd64/Packages` and
     `main/source/Sources`, which pin every `.deb` and source file. **Set them
     before recording anything, too.** `acceptance.py record` stamps both onto
     every evidence entry and every waiver it writes, and the publisher
     requires each entry and waiver of the subset to carry the digests of the
     indices being published. The base image digest cannot do that job on its
     own: every 5.0.0 receipt is bound to `2d8a72e0...` as well. Setting the
     manifest-level digests afterwards binds nothing recorded before them. A
     rebuilt repository changes both, and the publisher refuses until the
     subset is re-run against it, these two are updated, and the cases are
     recorded again.

   The base release's acceptance is never this update's: an evidence file
   that `qa/<base>/acceptance.json` records, a file in the base release's
   evidence directory, an `evidence_root` pointing there, and a waiver
   whose reason is the base release's reason for that case, word for word,
   are each refused. A waiver is argued again for this release.

   `make acceptance-gate` still refuses such a manifest, and that is correct.
   It is the gate for an ISO release and is unchanged.
2. `tools/pre_release_check.sh` passes, with a 7-day minimum on `Valid-Until`
   and complete corresponding source, including `pool/third-party-source/`.
   The publisher points it at `repo/` and the release's codename itself, so
   `REPO_DIR` or `CODENAME` exported in your shell are ignored. It reads
   `Valid-Until` only from the signed text, and refuses an `InRelease` that
   is anything other than exactly one clearsigned message.
3. `repo/shadowfetch.gpg.asc` is the release key. Both `InRelease` and
   `Release.gpg` verify under it. `InRelease` begins with the clearsigned
   header and ends with the signature, with nothing outside them: gpgv
   accepts unsigned text there, but apt does not. In the text gpgv says is
   signed, `Date` is not in the future and `Valid-Until` is at least 7 days
   away. That text lists every index file exactly, with matching bytes, and
   `Release` is that same text. Every compressed index (`.gz`, `.xz`, `.bz2`)
   decompresses to the plain index beside it, which is the file every check
   reads (apt downloads the compressed one). A compression nothing here can
   read (`.zst`, `.lz4`) is refused. Nothing exists under `repo/dists/`
   outside `dists/<codename>/`. Everything under `repo/dists/` is uploaded,
   and only that directory is signed.
4. The binary index names exactly the release data's packages at exactly its
   versions, and the source index names exactly its sources. Every pool file is
   one the indices name, with those bytes, and every `.deb` in the pool is
   byte-identical to the one in `build/`. `pool/third-party-source/` is the
   one exception, and each of its files is still checked. It must sit at
   `<package>/<version>/<file>` for a package and version the binary index
   lists. `tools/fetch_shadowcode.py` removes other versions when it stages a
   new pin. It must also be one of these:
   * named in that directory's `SOURCE-SHA256SUMS` with those bytes;
   * for ShadowCode, a signed asset of the pin (the `.deb` or the
     runtime-sources tarball) that passes the upstream verifier;
   * a signed metadata file (`RELEASE-AUTH`, `RELEASE-AUTH.sig`, `SHA256SUMS`,
     `RELEASE-MANIFEST.json`) byte-identical to `vendor/shadowcode/<version>/`;
   * the `README` the fetch tool writes.

   A partial download, a second version directory or anything else is
   refused, because it would be uploaded as a permanent object.

**Running it:**

```sh
make repo                                                     # after make packages
python3 tools/publish_release_4_0_0.py --apt-only             # plan: every check, then the object list
python3 tools/publish_release_4_0_0.py --apt-only --apply     # uploads
```

Do not run `make publish` for this, because that target depends on `iso-gate`.
The publisher runs `pre_release_check.sh` itself. `--published` is refused,
because there is no pointer to date. `--apply` has the same guard and the same
credential handling as an ISO release: it runs only from the authorized tree,
on Linux, as a non-root user, with `SHADOWFETCH_R2_ENDPOINT`,
`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` taken from the process
environment and never written down. A worktree anywhere else can plan but
cannot apply. Bring the release branch, `build/`, `repo/` and the QA evidence
to the authorized tree first.

**Afterwards:** check that the public `/linux/apt/dists/<codename>/InRelease`
is byte-identical to `repo/dists/<codename>/InRelease`, and that
`/linux/releases.json` still names the previous ISO. Then, on an installed
system of the base release, confirm that `sudo apt update; fireproof update`
takes the update, and record `PUB-01` against what is public.

## The compliance trap, which is not this project's but shares a machine

`shadowfetch-ios-apps.pages.dev` hosts the App Store privacy and support URLs
for ~530 live apps. **Cloudflare Pages deploys REPLACE the whole project
directory.** Deploying one app's `Website/` folder there deletes every other
app's compliance page; it has happened twice, and the second time all 530 URLs
were 404 at origin for about 42 hours behind edge cache. Never run
`wrangler pages deploy … --project-name shadowfetch-ios-apps` from anything but
the repair job:

```sh
cd ~/shadowfetchcrew/compliance-watch && ./repair.sh
```

That verifies all managed URLs and redeploys the COMPLETE set. A LaunchAgent
runs it every six hours; `compliance-watch/last-run.json` is the last result.
The Mac's `wrangler`/`npx` are intercepted and refuse a partial Pages deploy
from any other directory.

## When something refuses

The vocabulary is deliberate and each word means a different thing. A refusal
that says `not_enforced` is telling you a control does not exist, not that it
failed. A `partial` is a control with a stated residual. `observed` means a fact
was recorded, not verified. `not_representable` means the system has no way to
express the thing at all. If a message uses one of these words, the answer is in
`docs/SECURITY_CLAIMS_MATRIX.md` under the matching row.
