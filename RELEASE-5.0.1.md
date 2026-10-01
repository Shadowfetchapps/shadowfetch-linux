# Shadowfetch Linux 5.0.1 — ShadowCode

Edition: **ShadowCode**. Subtitle: **"One Harness. All Models."** Codename:
Umbra; the APT suite stays `umbra`, so 4.1 and 5.0 systems receive 5.0.1 from
the suite they already track. Signing fingerprint unchanged:
`8F13 CE15 35EE 1F4A 2916 A1F7 3C5C 900B 7BE8 0CA1`.

Status: **Not released.** This is the point release for the known issues
5.0.0 shipped with. The fixes and the version bump are in the
`release/5.0.1` tree. 5.0.1 is an **APT-only update**: it is published as
packages in the signed repository, with no new ISO, and installs on top of the
5.0.0 image (`delivery = "apt-only"`, `base_release = "5.0.0"` in
`tools/release/versions/5.0.1.toml`). Every case in
`qa/5.0.1/acceptance.json` is pending.

- Version: 5.0.1, a point release of 5.0.0. Nothing a working 5.0.0 setup
  depends on is removed or renamed, and no package is added or retired.
- Previous release: 5.0.0, ISO `shadowfetch-5.0.0-amd64.iso`, SHA-256
  `2d8a72e044e8061bd616b2b4668425cc4d4ec0480a98975f961c0e58cba95e21`
  (published 2026-09-30)
- Codename / repository suite: `umbra`
- Source branch: `release/5.0.1`
- Packages: every Shadowfetch package moves to `5.0.1-1`. `grub-btrfs` keeps
  its own `4.14-2`.
- ShadowCode: **1.0.1** (tag `v1.0.1`, commit
  `e923e5e2758ef6189738f944c86653ca0e87e93f`), the signed upstream release; see
  [ShadowCode 1.0.1](#shadowcode-101-the-window-size-fix).

---

# What 5.0.1 fixes

## `fireproof update` can update Fireproof itself

**Found in 5.0.1 QA; present since 2.1.4.** `fireproof update` (and the
Fireproof page) could not install an update that included
`shadowfetch-fireproof`. The new package's install script stopped Fireproof's
daemon, `fireproofd`. That daemon is the process running apt and dpkg, and
systemd stopped everything inside its service, so dpkg was killed halfway
through the update. The system was left with dozens of packages unpacked but
not configured. needrestart then tried to restart `fireproofd` from inside
the same update, and that could hang for up to an hour while holding the
package lock and blocking reboot. Retrying `fireproof update` failed the same
way.

**Fix** (`shadowfetch-fireproof`):

- No install script of the package stops, starts or restarts `fireproofd`
  any more.
- Stopping `fireproofd` signals the daemon only, not dpkg
  (`KillMode=mixed`). The daemon already waits for dpkg to finish before it
  exits.
- needrestart never restarts `fireproofd`
  (`/etc/needrestart/conf.d/50-shadowfetch-fireproof.conf`).
- The new daemon still takes over once the update is done. An idle daemon is
  restarted right away. When `fireproofd` is the one running the update, the
  restart waits until that update has finished. `fireproofd` also exits after
  every successful update, so the next request starts the version now
  installed.
- **An interrupted update is reported, not built on.** When an earlier dpkg
  run was interrupted (packages unpacked but not configured, or anything
  `dpkg --audit` reports), `fireproof check`, `fireproof update` and the
  Fireproof page offer no update. They list the unfinished packages and show
  the command that repairs it:

  ```bash
  sudo dpkg --configure -a && sudo apt -f install
  ```

- The verify battery's "Mirror still resolves" check now looks up the
  mirror's host name. A mirror URL with a port (`http://host:3142/`) was
  looked up as `host:3142` and reported as not resolving.

## Fireproof never removes packages as a side effect of an update

**Found in 5.0.1 QA.** While Debian testing is in the middle of a library
transition, some upgrades can only be installed by removing packages that
still use the old library. In QA, on a system that came from 4.1, Fireproof
planned 14 to 16 removals during the libavcodec/mlt transition, among them
`shadowfetch-desktop`, `shadowfetch-creative-base`, Krita and Kdenlive. On
the same system `apt full-upgrade` held 12 packages back and removed nothing.

**Cause.** Fireproof planned the update with python3-apt's dist-upgrade.
That is libapt's classic resolver, which removes packages to install
upgrades. `apt` 3 uses a different solver, which holds those upgrades back.

**Fix** (`shadowfetch-fireproof`):

- When the full update would remove a package, Fireproof keeps that package
  and holds back only the upgrades that needed it removed. Nothing is
  removed. The rest of the update goes ahead.
- `fireproof check`, `fireproof update` and the Fireproof page list the
  held-back packages, the packages that would have been removed, and why:

  > Debian testing is in the middle of a library transition. Installing 12
  > updates now would remove 16 installed packages (...), so Fireproof holds
  > them back and removes nothing. They will update once the transition is
  > complete in Debian testing; you do not need to do anything.

  Held-back updates are not counted on the update badge.
- The only removals an update can make are the ones Shadowfetch itself asks
  for: a package that an incoming `shadowfetch-*` package `Conflicts` with
  or `Breaks`, and that was installed automatically. `shadowfetch-desktop`,
  `shadowfetch-creative-base` and any package you installed yourself are
  never removed by an update.
- The commit re-checks the same plan under the package lock, after analyze,
  and refuses a plan that would remove a protected package. Analyze still
  changes nothing, and the update is still wrapped in a Phoenix Point.

## "database is busy" from Mission Control under heavy disk load

**5.0.0 known issue.** While a mission finished a step on a heavily loaded
disk, Mission Control and `shadowfetch-missions show`, `list` and `cancel` could
answer "database is busy" after waiting 10 seconds.

**Cause.** The mission engine opens and closes a database connection for every
call, so nearly every close was the last open connection. SQLite's last close
copies the write-ahead log back into the database and deletes it, and it holds
an exclusive lock while it waits for two disk syncs. Every other process waited
behind that lock. On the 5.0.0 stress guest one sync took 15 to 34 seconds,
so a single read could wait past its whole budget.

**Fix** (`shadowfetch-missions`):

- No connection copies the log back on close any more. SQLite's normal
  checkpoint after a commit still keeps the log small, and it blocks no one.
  Short commands also skip that checkpoint, so no sync lands on them after
  they commit. Data is as safe as before.
- **Reads no longer wait on a writer.** `show`, `list`, `events` and the
  desktop's views answer while the worker is writing.
- The engine now needs Python 3.12 or newer
  (`Depends: python3 (>= 3.12)`). The 5.0 image already ships a newer one.

Proven host-side: a worker-shaped process with every disk sync delayed by
12 seconds, while `show` polls and Stop is pressed. These tests fail on the
5.0.0 engine and pass on 5.0.1. The 45-minute stress run (STRESS-01) has not
yet been repeated on a 5.0.1 image; see [Known issues](#known-issues).

## Stop works while the database is busy

A Stop has to write to the database. A worker commit still holds the write
lock while its own sync runs, so in 5.0.0 Stop could also time out.

- **Stop is saved first.** `cancel` saves the request as a small file in
  Mission Control's private state directory. It then waits at most 2 seconds
  for the database.
- **If the database is busy,** `cancel` answers at once: "Stop requested.
  Mission Control's database is busy, so the request was saved and is recorded
  in the mission's history as soon as the database is free." Mission Control
  shows **Stop requested: saved, waiting to be recorded** until the worker
  records it.
- **The worker records it** as the ordinary stop, in the mission's history,
  at its next check. A queued mission whose stop is not yet recorded does not
  start.
- **A late Stop is never lost.** If the Stop arrives while the mission is
  already finishing, the history says the stop was asked for and not applied.
- **Nothing waits on the disk once the Stop is saved.** In a host test with
  each disk sync delayed by 3 seconds, Stop answered in 0.2 seconds.
- **If the request cannot be saved** (a full disk, for example), `cancel`
  writes the stop directly, as 5.0.0 did.

## Missions created together run in the order you created them

5.0.0 broke a tie between missions created in the same second on their random
mission id, so four media missions created within half a second ran 1, 4, 3, 2.
Missions now run, and list, in the order they were created.

## A waiting mission says what it is waiting for

In 5.0.0, once one mission waited for your review, the other missions for the
same project stayed **Queued** with nothing running and no reason given. The
engine holds them until you review the first one, and now says so:

- `show` and `list` report a `hold` for such a mission, naming the mission
  you need to review.
- Mission Control shows the reason on the queue row (the tooltip has the full
  message) and under **Waiting** in the mission's Overview.

The rule itself is unchanged, and the reason shown is the rule the worker
applies.

## Live USB: no package-list refresh after login

**5.0.0 known issue.** About five minutes after login, KDE's update notifier
refreshed the package lists in the live session: a download of about 200 MB
that also used about 350 MB of RAM, because a live session keeps its changes
in memory.

5.0.1 does not start the update notifier on the live USB
(`shadowfetch-defaults`). Installed and upgraded systems keep update
notifications. This was checked against the shipped 5.0.0 image with the 5.0.1
drop-in added:

- **Live session:** the notifier was skipped at login, and PackageKit ran no
  refresh, or any other transaction, in the 8.6 minutes after login that were
  watched.
- **Installed system:** the notifier still started and still refreshed the
  package lists.

**This does not change any USB stick yet.** An update cannot change a USB
stick you already wrote, and 5.0.1 ships no new ISO, so the fix reaches the
live USB with the next image. On a 5.0.0 stick, keep using the 5.0.0
workaround: stay offline in the live session, or run
`systemctl --user stop app-org.kde.discover.notifier@autostart.service` soon
after logging in.

## ShadowCode 1.0.1, the window-size fix

**5.0.0 known issue.** Under Wayland, ShadowCode 1.0.0 opens a slightly larger
window each time, and on a 1366x768 screen even the first window is larger than
the screen.

5.0.1 ships ShadowCode 1.0.1, which fixes this: the window no longer saves its
size (position and maximized state are still restored), and the first window is
fitted to the screen it opens on. `shadowfetch-desktop` now requires
`shadow-code (>= 1.0.1)`. The pin was moved with `tools/bump_shadowcode.py`
after a reviewed trust refresh from the published commit `e923e5e`, whose only
trust change widens the same key's authorised range to 1.0.1. 1.0.1's build
also moves its AppImage runtime build container to Alpine's openssl 3.3.7-r2
security update; the runtime it builds is byte-identical.

---

# QA harness fixes

These are in `tools/`, which ships in no package. They change how 5.0.1 is
qualified, not what is installed.

- **STRESS-01 mission loop.** The helper now treats "database is busy" as an
  answer to ask again, with backoff and a budget, instead of a failed
  command. It never replays an undo or a create that may already have
  happened. Stopping the private worker can no longer crash the helper and
  lose its result.
- **STRESS-01 container loop.** In 5.0.0 the container gave the correct result
  and then `podman run --rm` spent 97 to 138 seconds removing it on the
  saturated disk. One 120-second limit covered both, so a correct, removed
  container was reported as failed at cycle 3. The result is now judged when
  it arrives, and the removal is timed separately. Cycles run one at a time,
  as in 4.x. A cycle over the 4.x 120-second bound makes the run
  **PASS_WITH_OBSERVATIONS**, never a plain PASS, and so does any "database is
  busy" answer.
- **STRESS-01 host runner.** It refuses to start on a busy host (another VM,
  or load above a limit) and records host load. It takes the release under
  test instead of assuming 5.0.0.
- **SHADOWCODE-01 soak.** Before measuring, the soak stops the idle PackageKit
  daemon and every armed timer. In the 5.0.0 soak both fired inside the cycles
  and moved the memory readings. The memory slope is fitted after the first
  two warm-up closes and is judged only from 18 closes; a shorter soak runs
  on, up to 60 minutes, to reach them. ShadowCode's window size is also read
  from KWin at each launch, and a window that gets larger at two launches in a
  row fails the soak. ShadowCode 1.0.0 fails that check, as it should.

---

# Package changes

| Package | 5.0.1-1 |
| --- | --- |
| `shadowfetch-missions` | Reads never wait on a writer; Stop is saved when the database is busy; same-second missions run in creation order; queued missions report a review-gate `hold`; `Depends: python3 (>= 3.12)`; reports 5.0.1 |
| `shadowfetch-control-center` | Mission Control shows why a mission is held and a Stop that is saved but not yet recorded |
| `shadowfetch-defaults` | No update notifier on the live USB; version strings 5.0.1 |
| `shadowfetch-branding` | os-release and `/usr/share/shadowfetch/version` report 5.0.1 |
| `shadowfetch-fireline` | Firebreak and the MCP server report 5.0.1; no functional change |
| `shadowfetch-themes` | SDDM theme metadata reports 5.0.1; no visual change |
| `shadowfetch-drkonqi-pickup` | CMake project version 5.0.1; no functional change |
| `shadowfetch-meta` | Rebuild; `shadowfetch-desktop` requires `shadow-code (>= 1.0.1)` (ShadowCode 1.0.1 is pinned) |
| `shadowfetch-fireproof` | Updates itself without killing dpkg (no stop from its install scripts, `KillMode=mixed`, needrestart leaves `fireproofd` alone, restart after the update); refuses to offer an update on top of an interrupted one and shows the repair command; never removes packages as a side effect of an update (holds back the upgrades that would, and says why); mirror check ignores the port |
| `shadowfetch-ember`, `-firewatchd`, `-hwscan`, `-menus`, `-phoenix`, `-welcome` | Rebuild only: `shadowfetch-desktop` requires every Shadowfetch package at the same version |

Each package's `debian/changelog` has the details.

---

# Upgrading

## From 5.0.0 or 4.1: use apt for this one update

```bash
sudo apt update && sudo apt full-upgrade
```

Use these two commands, not `fireproof update` or Control Center's Update
button, to install 5.0.1. If apt lists packages as "kept back", that is
expected while Debian testing is in the middle of a transition: apt holds
them and removes nothing. Then log out and back in once (or restart).

From 5.0.1 on, `fireproof update` is the update command again, including for
Fireproof's own updates, and it never removes packages: while Debian testing
is in a transition, it holds back the upgrades that would remove packages,
lists them, and installs the rest.

**Why apt this once.** 5.0.1 fixes `fireproof update` updating Fireproof (see
above), but on a 5.0.0 or 4.1 system this one update would still be run by
the 5.0.0 or 4.1 Fireproof daemon that is already running. It is not the old
package's install scripts. Neither 4.1 nor 5.0.0 ships a pre-removal script,
and their post-removal script acts only when the package is removed. The
script that stopped Fireproof was the *incoming* package's, and 5.0.1's no
longer does. What the old version still controls is the update itself:

- The old daemon runs dpkg inside its own service, under the old service
  settings, until systemd reloads them partway through the update. A stop
  that reaches it before then, such as a shutdown or a service restart, still
  kills dpkg with it.
- Its analyze does not notice a system that an earlier attempt left half
  configured. It offers a normal update on top.
- It keeps running its old code until the update has finished and the
  deferred restart replaces it.
- It plans the update with the resolver that removes packages during a
  Debian testing transition (see
  [above](#fireproof-never-removes-packages-as-a-side-effect-of-an-update)).
  In QA, 4.1's `fireproof update` removed `shadowfetch-desktop`,
  `shadowfetch-creative-base`, Krita and Kdenlive on the way to 5.0.0.

With apt, dpkg runs in your terminal, not inside `fireproofd`, so none of
this applies. 5.0.1's install script then restarts the idle daemon on the new
version. (In QA, a fixed package also installed cleanly from 5.0.0 with
`fireproof update`. That was a diagnostic run, and apt is still the
recommended path for this update.)

**If an earlier update was interrupted,** repair it first. Signs of this are
`sudo dpkg --audit` printing anything, or apt asking you to run
`dpkg --configure -a`. An earlier `fireproof update` that included
`shadowfetch-fireproof`, such as 4.1 to 5.0.0, went through the same stop
and was most likely interrupted:

```bash
sudo dpkg --configure -a
sudo apt full-upgrade
sudo apt install shadowfetch-desktop shadowfetch-creative-base
```

The last command puts back the desktop metapackages (and with them Krita and
Kdenlive) if the interrupted update removed them. If they are still
installed, it does nothing. Then restart.

If apt says the package lock is held by `fireproofd`, the old daemon is
still waiting out its one-hour stop timeout, and a normal restart is refused
while it waits. Free the lock with
`sudo systemctl kill --signal=KILL fireproofd.service` (or restart with
`sudo systemctl reboot -i`), then run the repair. This sequence was tested
in QA on 4.1 systems that `fireproof update` had left interrupted on the way
to 5.0.0.

## After updating from 5.0.0

Log out and back in once. The update does not restart the Mission
Control worker already running in your session, so until you do, that worker
is still the 5.0.0 engine. To restart only the worker instead:

```bash
systemctl --user restart shadowfetch-missions.service
```

Nothing else changes for a 5.0.0 setup: no setting, command or file format.

## After updating from 4.1

The same two commands bring a 4.1 system straight to 5.0.1, because the
`umbra` suite serves the newest release. Everything in
[RELEASE-5.0.0.md](RELEASE-5.0.0.md#upgrading-from-41) applies: log out and
back in once, check `shadowfetch-agent-network status`, update scripts that
used the removed commands, and connect your services in ShadowCode.

## A new install

5.0.1 ships no new ISO. Install from the 5.0.0 ISO and update as above.

---

# Known issues

- **5.0.0 USB sticks keep the package-list refresh.** Only a 5.0.1 stick has
  the fix; see above for the workaround.
- **A `create` that answers "database is busy" may already have queued the
  mission.** After it commits, the engine reads the database once more to
  write the mission's entry to the system journal. In rare cases under very
  heavy disk load, that read can be the one that answers busy. **Workaround:**
  check `shadowfetch-missions list` before creating it again. Not fixed in
  5.0.1.
- **Stop can still be slow on a saturated disk in one case:** right after
  SQLite has emptied its log, the next write restarts the log with two disk
  syncs, and on the 5.0.0 stress guest one sync took up to 34 seconds. The
  Stop is saved before that write, so the worker still records it.
- **Not yet proven on an upgraded system with the final packages.** The
  fixes above are proven by host-side tests, the live-USB change by a VM of the
  5.0.0 image with the drop-in added, and the Fireproof self-update by a
  diagnostic VM run with a prototype of the fix. The APT-only acceptance cases
  (SRC-01, PKG-01, UPGRADE-01, DURABLE-01, MISSION-01) still have to be run and
  recorded against the repository that is published, and UPGRADE-01 re-run
  with `fireproof update` on the final packages. The Fireproof no-removals
  fix is proven on the 4.1 base against the final repository (see
  [Release state](#release-state)).
- **Package retirements and manually installed packages.** Fireproof only
  removes a package that an incoming `shadowfetch-*` package `Conflicts`
  with or `Breaks` if it was installed automatically. Most Shadowfetch
  packages on an installed system are marked as manually installed (11 of
  17 on the 4.1 system QA used), so a future release that retires one of them
  by `Conflicts` would see Fireproof hold that update back. Retire packages
  with a transitional package, or ask users to update with apt for that
  release.
- **Carried from 5.0.0, unchanged:** the security advisory about the shared
  DKMS module-signing key on systems installed from 4.x ISOs
  (`shadowfetch-doctor` still flags it), OpenClaw's security record, Hermes
  running unsandboxed, the agent network not setting ShadowCode's own network
  mode, ShadowCode's first run opening light on the dark desktop, and
  ShadowCode's glibc 2.39 and Vulkan requirements. See
  [RELEASE-5.0.0.md](RELEASE-5.0.0.md#known-issues).
- `drift_gate` reports 0 DRIFT and 4 BLOCKED, the same pre-existing detected
  duplications as 5.0.0.

---

# Release state

Measured on this tree, not on an image:

- `tools/stamp_version.py 5.0.1`: STAMP COMPLETE, 18 anchored sites in 14
  files. The changelogs and the Calamares slideshow were done by hand.
- `drift_gate`: 0 DRIFT, 4 BLOCKED (the pre-existing findings under Known
  issues).
- `make test`: 2,739 unittest cases in 13 suites, plus the Fireline script
  tests, the Firebreak shell test and the DrKonqi pickup checks. The
  adversarial suites all pass. Four tests depend on the build host. Each
  failed in one of two runs and passed in the other, and none is caused by
  this change:
  - the stamper's file-mode check expects 0755, and a checkout made with
    umask 002 has 0775;
  - Control Center's system-summary check reads the real root filesystem,
    which crossed 90% used between the runs;
  - two ShadowCode source-archive tests failed when run from a scratch clone,
    where git could not read the archive's commit id.
- After the `shadowfetch-fireproof` self-upgrade fix (2026-10-01, umask 022):
  `make test` passes (2,873 unittest cases in 13 suites, plus the script,
  shell and adversarial suites). `make packages && make repo && make
  package-gate` gives PACKAGE_GATE_PASSED: 19 binary and 16 source packages,
  signed index valid until 2027-03-30, and the built `shadowfetch-fireproof`
  checked to never stop `fireproofd` from its own upgrade. Index digests
  recorded in `qa/5.0.1/acceptance.json`: Packages
  `b4216844b3c958c4ef91293753bc40d931e1495c8b50ba92fb25d5c7732459d8`,
  Sources `6351de1fe4b69ae361e56bdc30dc0832b310ae6f1b101a0c96594432474189f6`.
- After the Fireproof no-removals fix (2026-10-01, umask 022): the
  `shadowfetch-fireproof` suite passes (189 tests, 23 of them new in
  `tests/test_no_side_effect_removals.py`). `make test` passes every suite
  except `tools/tests`, where two `test_shadowcode` linkage tests fail
  because `tools/release/shadowcode.toml` `ships_in` names only 5.0.1 since
  the ShadowCode 1.0.1 bump (7cfed12) while the tests expect 5.0.0; that is
  unrelated to this fix and still open. The adversarial suites pass. `make
  packages && make repo && make package-gate` gives PACKAGE_GATE_PASSED;
  only `shadowfetch-fireproof_5.0.1-1_all.deb` changed (`60b42b31...`), the
  other 18 packages are byte-identical. Signed index valid until
  2027-03-30. Index digests, recorded in `qa/5.0.1/acceptance.json`:
  Packages `9953f138b9dd55a29b5297e426a7666fb39cb097d123fdbc134620fd69e6ac1d`,
  Sources `ccea3f0dbfe6040996528a6d8291bed0ceef7041d9d0266140f8e9d6de576ed6`,
  InRelease `70590e1e8d60f39c72ad3ad0340281ebd8c39eea68eb8499d09bf24d76fb89c8`.
  These replace every earlier 5.0.1 digest.
- Proven in a VM on the 4.1 upgrade base against that served, signed
  repository: `sudo apt update && sudo apt full-upgrade` to 5.0.1 (36
  upgraded, 1 new, 0 removed, 12 kept back), reboot, then `fireproof check`
  and `fireproof update` as the desktop user against the live Debian
  testing archive. Fireproof proposed 0 removals and listed the same 12
  held-back packages as apt, with the message; exit 0. With a QA-only dummy
  update added, `fireproof update` installed it through the full protocol
  (Phoenix Point labelled, verify battery all OK), held back the dummy that
  needs the transition, removed nothing, exit 0. `dpkg --audit` clean,
  `shadowfetch-desktop`, `shadowfetch-creative-base`, Krita and Kdenlive
  still installed, 0 failed units after a reboot. libapt's classic
  dist-upgrade (what the 4.1 and 5.0.0 Fireproof plan with) still proposes
  16 removals on the same system.
- Final qualification in the publishing tree (2026-10-01, umask 022):
  - `make test` passes: 2,898 unittest cases in 13 suites, all OK (7
    skipped), including the two `test_shadowcode` linkage tests, which now
    take the shipping release from the pin's `ships_in`.
  - `make source-gate`: SOURCE_GATE_PASSED.
  - `make packages && make repo && make package-gate`: PACKAGE_GATE_PASSED,
    19 binary packages (17 `shadowfetch-*`, `shadow-code` 1.0.1,
    `grub-btrfs` 4.14-2) and 16 source packages, signed index valid until
    2027-03-30. `packages/` is unchanged since the no-removals fix, but this
    tree's source files carry different mtimes, so every `shadowfetch-*`
    .deb differs in bytes from the earlier build (same files and contents).
    Index digests, recorded in `qa/5.0.1/acceptance.json`, replace every
    earlier 5.0.1 digest: Packages
    `fd0b98c288b63dcbfaf6751adc2abbd51fb0bd1abd7954f1cb215db6e7ce10cf`,
    Sources `db12ba1a15a59a1c8de8032c656831e0b2047c9693ffc0eb009d62f9cf9e578d`,
    InRelease `2be292bd8008d3459481845a6433fc9d81ebca3001a94bab71f0b900f07fc279`.
  - Upgrade proof on that repository, from the shipped 5.0.0 install:
    `sudo apt update && sudo apt full-upgrade` (signature checking on),
    reboot, all 19 checks pass: 16 `shadowfetch-*` at 5.0.1-1 (every
    published one that was installed; `shadowfetch-nvidia` is not installed
    on that machine), ShadowCode 1.0.1, nothing removed, no failed units,
    user files, ShadowCode settings and 5.0.0 missions kept, the Mission
    Control fixes live. ShadowCode 1.0.1 opened at the same size three times.
    `fireproof check` and `fireproof update` as the desktop user exit 0 and
    remove nothing.
  - Acceptance recorded: SRC-01, PKG-01, UPGRADE-01 and DURABLE-01 pass;
    MISSION-01 is waived for the code mission only (media and cited-report
    missions pass on the 5.0.1 engine; a code mission needs a paid account).
    The publisher's `--apt-only` plan passes. There is no `iso_gate`: 5.0.1
    ships no image.

Still to do before 5.0.1 is published:

1. Done: ShadowCode 1.0.1 is published and pinned.
2. Done: packages and the signed repository built with umask 022;
   `source_gate` and `package_gate` pass.
3. Done: the APT-only acceptance subset is recorded in
   `qa/5.0.1/acceptance.json` against that repository. Rebuilding the
   repository changes both index digests, and every case would then have to
   be recorded again.
4. Publish with `python3 tools/publish_release_4_0_0.py --apt-only --apply`
   from this tree.
