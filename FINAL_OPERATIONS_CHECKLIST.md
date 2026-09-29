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
   To keep 0.34.2, re-running `bump_shadowcode.py 0.34.2` is a verified no-op.
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
9. **Fill the documents.** Replace every `TODO(iso)` in `README.md` and
   `RELEASE-5.0.0.md` with measured values (size, SHA-256, commit/tree, date,
   acceptance results, gate verdicts). `grep -rn 'TODO(iso)\|TODO(platform)'
   README.md RELEASE-5.0.0.md` must print nothing. Re-run
   `python3 tools/drift_gate.py`.
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
