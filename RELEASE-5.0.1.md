# Shadowfetch Linux 5.0.1 — ShadowCode

Edition: **ShadowCode**. Subtitle: **"One Harness. All Models."** Codename:
Umbra; the APT suite stays `umbra`, so 4.1 and 5.0 systems receive 5.0.1 from
the suite they already track. Signing fingerprint unchanged:
`8F13 CE15 35EE 1F4A 2916 A1F7 3C5C 900B 7BE8 0CA1`.

Status: **Not released.** This is the point release for the known issues
5.0.0 shipped with. The fixes and the version bump are in the
`release/5.0.1` tree. No 5.0.1 image has been built, gated or accepted yet.
Every case in `qa/5.0.1/acceptance.json` is pending, and nothing below is
claimed from a 5.0.1 image.

- Version: 5.0.1, a point release of 5.0.0. Nothing a working 5.0.0 setup
  depends on is removed or renamed, and no package is added or retired.
- Previous release: 5.0.0, ISO `shadowfetch-5.0.0-amd64.iso`, SHA-256
  `2d8a72e044e8061bd616b2b4668425cc4d4ec0480a98975f961c0e58cba95e21`
  (published 2026-09-30)
- Codename / repository suite: `umbra`
- Source branch: `release/5.0.1`
- Packages: every Shadowfetch package moves to `5.0.1-1`. `grub-btrfs` keeps
  its own `4.14-2`.
- ShadowCode: **still 1.0.0** in this tree. ShadowCode 1.0.1 is pending; see
  [ShadowCode 1.0.1](#shadowcode-101-the-window-size-fix-pending).

---

# What 5.0.1 fixes

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

**This only changes 5.0.1 USB sticks.** An update cannot change a USB stick
you already wrote. On a 5.0.0 stick, keep using the 5.0.0 workaround: stay
offline in the live session, or run
`systemctl --user stop app-org.kde.discover.notifier@autostart.service` soon
after logging in.

## ShadowCode 1.0.1, the window-size fix: pending

**5.0.0 known issue.** Under Wayland, ShadowCode 1.0.0 opens a slightly larger
window each time, and on a 1366x768 screen even the first window is larger than
the screen.

ShadowCode 1.0.1 fixes this. It is **not yet in 5.0.1.**
`tools/release/shadowcode.toml` still pins 1.0.0, and
`shadowfetch-desktop` still requires `shadow-code (>= 1.0.0)`. The pin moves to
1.0.1 only after ShadowCode's owner publishes the signed 1.0.1 release. Then
`tools/bump_shadowcode.py` authenticates it and moves the pin, the vendored
metadata and the desktop's dependency floor together. The vendored trust
policy authorises its key only up to 1.0.0, so that bump first needs a
reviewed trust refresh (`--refresh-trust`) from a published ShadowCode commit.

Until then, the 5.0.0 workaround applies: maximize the ShadowCode window (its
maximize button, or Meta+PgUp). ShadowCode then does not save the size.

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
| `shadowfetch-meta` | Rebuild; still requires `shadow-code (>= 1.0.0)` |
| `shadowfetch-ember`, `-fireproof`, `-firewatchd`, `-hwscan`, `-menus`, `-phoenix`, `-welcome` | Rebuild only: `shadowfetch-desktop` requires every Shadowfetch package at the same version |

Each package's `debian/changelog` has the details.

---

# Upgrading

## From 5.0.0

The supported path is the signed Shadowfetch APT repository:

```bash
sudo apt update
fireproof update          # shadowfetch-update still works and means this
```

Then log out and back in once. The update does not restart the Mission
Control worker already running in your session, so until you do, that worker
is still the 5.0.0 engine. To restart only the worker instead:

```bash
systemctl --user restart shadowfetch-missions.service
```

Nothing else changes for a 5.0.0 setup: no setting, command or file format.

## From 4.1

The same two commands bring a 4.1 system straight to 5.0.1, because the
`umbra` suite serves the newest release. Everything in
[RELEASE-5.0.0.md](RELEASE-5.0.0.md#upgrading-from-41) applies: log out and
back in once, check `shadowfetch-agent-network status`, update scripts that
used the removed commands, and connect your services in ShadowCode.

## A new install

Use the 5.0.1 ISO once it is published. Until then, install from the 5.0.0
ISO and update as above.

---

# Known issues

- **ShadowCode 1.0.0's window still grows (Wayland)** until the pin moves to
  1.0.1; see above. **Workaround:** maximize the window.
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
- **Not yet proven on a 5.0.1 image.** The fixes above are proven by host-side
  tests, and the live-USB change by a VM of the 5.0.0 image with the drop-in
  added. The 5.0.1 ISO still has to be built, gated and accepted. STRESS-01
  needs a 45-minute run on an idle host.
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
- `source_gate`, `package_gate`, `iso_gate` and acceptance: not run. No 5.0.1
  packages or image exist yet.

Still to do before 5.0.1 is published:

1. ShadowCode's owner publishes the signed 1.0.1 release; refresh the trust
   policy and run `tools/bump_shadowcode.py 1.0.1`.
2. Build the packages, repository and ISO, and pass `source_gate`,
   `package_gate` and `iso_gate`.
3. Run and record the acceptance cases in `qa/5.0.1/acceptance.json`
   against that exact ISO, with STRESS-01 on an idle host.
