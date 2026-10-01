#!/usr/bin/env python3
"""The acceptance cases themselves.

Vocabulary, kept strictly distinct because collapsing it is how an unproven
release gets published:

  OBSERVED   the harness saw a fact and recorded it. No judgement attached.
  PASSED     an observation was compared against a stated expectation and met it.
  BLOCKED    the case could not be executed here. Not a failure of the artifact,
             and emphatically not a pass.
  FAILED     an expectation was stated and the system did not meet it.

A case function may only return by finishing its checks, by raising Blocked, or
by raising. There is no path that produces PASS without at least one check
having been evaluated against the running system.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
import re
import shlex
import statistics
import time
from typing import Any, Callable

from .evidence import EvidenceSet, harness_fingerprint, sha256_file
from .ledger import Ledger, receipt_problems
from .vm import Guest, GuestError


class Blocked(Exception):
    """The case cannot be executed in this environment. Never a pass."""


class Context:
    def __init__(
        self,
        *,
        name: str,
        repo_root: Path,
        run_dir: Path,
        evidence: EvidenceSet,
        artifact: dict[str, Any],
        options: dict[str, Any],
        work_root: Path | None = None,
    ) -> None:
        self.name = name
        self.repo_root = repo_root
        # Where this release's runs live: work/qa-<version>. A case that has to
        # consume an earlier run of this harness (INSTALL-01 needs one install
        # per firmware) reads the ledger from here rather than recomputing the
        # layout the runner already knows.
        self.work_root = work_root if work_root is not None else run_dir.parent.parent
        self.run_dir = run_dir
        self.evidence = evidence
        self.artifact = artifact
        self.options = options
        self.checks: list[dict[str, Any]] = []
        self.observations: dict[str, Any] = {}
        self.transcript: list[str] = []
        self.guests: list[Guest] = []

    # -- recording --

    def log(self, message: str) -> None:
        line = f"{time.strftime('%H:%M:%S', time.gmtime())} {message}"
        self.transcript.append(line)
        print(line, flush=True)

    def observe(self, key: str, value: Any) -> Any:
        """Record a fact. An observation is not a verdict; nothing passes here."""
        self.observations[key] = value
        self.log(f"OBSERVED {key} = {value!r}"[:400])
        return value

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        state = "PASSED" if condition else "FAILED"
        self.checks.append({"name": name, "state": state, "detail": detail})
        self.log(f"{state} {name}" + (f" -- {detail}" if detail else ""))
        return bool(condition)

    def blocked(self, reason: str) -> None:
        raise Blocked(reason)

    # -- guests --

    def guest(self, name: str, **kwargs: Any) -> Guest:
        machine = Guest(self.run_dir / "vm" / name, name, **kwargs)
        self.guests.append(machine)
        return machine

    def collect_machine_evidence(self, machine: Guest, prefix: str) -> None:
        """Serial console and QEMU stderr, always, pass or fail.

        A failed run's evidence is the point: it is what tells the next person
        whether the artifact broke or the harness did.
        """
        for source, kind in ((machine.serial_log, "log"), (machine.qemu_log, "log")):
            if source.is_file() and source.stat().st_size:
                target = self.evidence.path(f"{prefix}-{source.name}")
                target.write_bytes(source.read_bytes())
                self.evidence.try_add(target, kind)

    def snap(self, machine: Guest, name: str, *, required: bool = False) -> dict | None:
        target = self.evidence.path(name)
        try:
            info = machine.screenshot(target)
        except GuestError as error:
            if required:
                raise
            self.log(f"screenshot {name} unavailable: {error}")
            return None
        item = self.evidence.add(target, "screenshot") if required else \
            self.evidence.try_add(target, "screenshot")
        if item is None:
            self.log(f"screenshot {name} did not meet the evidence floor")
        return info


# --- shared guest probes ------------------------------------------------------


def system_report(machine: Guest) -> dict[str, Any]:
    """One structured read of the guest. Guest output is evidence, not proof."""
    def out(command: str) -> str:
        return machine.run(command, timeout=120)["stdout"].strip()

    return {
        "os_release": out("cat /etc/os-release"),
        "version_marker": out("cat /usr/share/shadowfetch/version 2>/dev/null"),
        "cmdline": out("cat /proc/cmdline"),
        "kernel": out("uname -r"),
        "boot_id": out("cat /proc/sys/kernel/random/boot_id"),
        "machine_id": out("cat /etc/machine-id 2>/dev/null"),
        "root_mount": out("findmnt -no SOURCE,FSTYPE,OPTIONS /"),
        "boot_mount": out("findmnt -no SOURCE,FSTYPE /boot 2>/dev/null"),
        "system_state": out("systemctl is-system-running 2>&1"),
        "failed_units": out("systemctl --failed --no-legend --plain 2>&1"),
        "dpkg_audit": out("dpkg --audit 2>&1"),
        "shadowfetch_packages": out(
            "dpkg-query -W -f='${Package}\\t${Version}\\t${db:Status-Abbrev}\\n' "
            "'shadowfetch-*' 2>/dev/null"
        ),
        "modules_present": out(
            "test -d /lib/modules/$(uname -r) && echo yes || echo no"
        ),
        "boot_kernel_present": out(
            "test -f /boot/vmlinuz-$(uname -r) && echo yes || echo no"
        ),
    }


SETTLED_STATES = ("running", "degraded")


def settled_report(ctx: Context, machine: Guest, label: str) -> dict[str, Any]:
    """system_report() taken only after systemd has stopped saying "starting".

    The guest agent answers long before boot finishes, so a state read at
    that moment is the question asked too early, not an answer. Bounded by
    --settle-timeout; a system still "starting" at the bound is reported as
    exactly that, and check_system_state() fails it with the time it was given.
    """
    bound = float(ctx.options.get("settle_timeout", 300))
    started = time.monotonic()
    settled = _settle_system(machine, bound)
    seconds = round(time.monotonic() - started, 1)
    report = system_report(machine)
    report["settled_state"] = settled
    report["settle_seconds"] = seconds
    report["settle_timeout_seconds"] = bound
    # The judged state is the settled one; system_report's own read happened a
    # moment later and is kept as observed, not substituted.
    report["system_state"] = settled or report["system_state"]
    ctx.observe(f"{label}_systemd_settle", {"state": settled, "seconds": seconds,
                                             "bound_seconds": bound})
    return report


def check_system_state(ctx: Context, name: str, report: dict[str, Any]) -> bool:
    """One wording for "systemd came up", shared by every case that judges it.

    "degraded" still passes, as it always has here -- but never silently: the
    failed units are named in the check and recorded as an observation. A
    state that never left "starting" says how long it was waited for.
    """
    state = report["system_state"]
    detail = f"systemctl is-system-running = {state!r}"
    if state == "degraded":
        detail += f"; failed units: {report.get('failed_units') or '(none listed)'}"
        ctx.observe("degraded_failed_units", report.get("failed_units") or "")
    elif report.get("failed_units"):
        detail += f"; failed units: {report['failed_units']}"
    if state not in SETTLED_STATES and "settle_timeout_seconds" in report:
        detail += (f"; still not settled after {report['settle_seconds']}s "
                   f"(bound {report['settle_timeout_seconds']:g}s)")
    return ctx.check(name, state in SETTLED_STATES, detail)


def push_script(machine: Guest, path: str, source: str) -> None:
    """Write a helper into the guest without trusting anything in its PATH."""
    encoded = base64.b64encode(source.encode()).decode()
    machine.run(
        f"printf %s {shlex.quote(encoded)} | /usr/bin/base64 -d > {shlex.quote(path)}",
        check=True,
    )


# --- LIVE-BOOT ----------------------------------------------------------------


def case_live_boot(ctx: Context) -> None:
    """Boot the ISO under test and prove the live system actually came up.

    Cheapest possible run against the real artifact, and the one that proves
    the harness itself is wired to the artifact rather than to a stale disk.
    """
    iso = Path(ctx.artifact["path"])
    machine = ctx.guest("live", firmware=ctx.options.get("firmware", "bios"))
    machine.create_disk(int(ctx.options.get("disk_gib", 32)))
    ctx.log(f"booting {iso.name} ({ctx.artifact['sha256'][:16]}...)")
    machine.boot("live", iso=iso, note="live boot of the artifact under test")
    try:
        waited = machine.wait_agent(float(ctx.options.get("boot_timeout", 900)))
        ctx.observe("guest_agent_seconds", round(waited, 1))
        # Settle FIRST, then read the report, so failed_units describes the
        # same moment as the state being judged. The first 5.0.0 run asked 51s
        # after boot and was told "starting"; systemd reached "running" at ~60s.
        report = settled_report(ctx, machine, "live")
        ctx.evidence.write_json("live-system-report.json", report)

        ctx.check(
            "live session boots from the ISO under test",
            "boot=live" in report["cmdline"],
            report["cmdline"][:200],
        )
        ctx.check(
            "live system reports the release version",
            report["version_marker"] == ctx.options["version"],
            f"version marker {report['version_marker']!r}",
        )
        ctx.check(
            "live os-release matches the release version",
            f'VERSION_ID="{ctx.options["version"]}"' in report["os_release"],
        )
        check_system_state(ctx, "systemd reaches a running state", report)
        # Give the desktop session time to paint before the screenshot: an
        # empty framebuffer is not evidence that a desktop came up.
        settle = float(ctx.options.get("desktop_settle", 90))
        ctx.log(f"waiting {settle:.0f}s for the desktop session")
        time.sleep(settle)
        shot = ctx.snap(machine, "live-desktop.png", required=True)
        ctx.check(
            "live desktop framebuffer is captured at release resolution",
            bool(shot) and shot["width"] >= 1280 and shot["height"] >= 720,
            f"{shot['width']}x{shot['height']}" if shot else "no capture",
        )
    finally:
        try:
            machine.shutdown()
        finally:
            ctx.collect_machine_evidence(machine, "live")


# --- RECOVERY -----------------------------------------------------------------

RECOVERY_MARKER = "/etc/shadowfetch-acceptance-marker"
RECOVERY_WITNESS = "/etc/shadowfetch-acceptance-after-point"


def _recovery_base(ctx: Context) -> Path:
    base = ctx.options.get("base_image")
    if not base:
        ctx.blocked(
            "no installed base image supplied. Pass --base-image with a qcow2 "
            "disk holding an installed system of the release under test, or run "
            "the install case first so this case can consume its result."
        )
    path = Path(base).resolve()
    if not path.is_file():
        ctx.blocked(f"base image does not exist: {path}")
    return path


def _prepare_point(ctx: Context, machine: Guest) -> dict[str, Any]:
    """Bring the guest to a known state and take a Phoenix Point of it."""
    report = system_report(machine)
    ctx.evidence.write_json("recovery-baseline.json", report)
    ctx.check(
        "base system is the release under test",
        report["version_marker"] == ctx.options["version"],
        f"version marker {report['version_marker']!r}",
    )
    ctx.check(
        "base system root is Btrfs, as Phoenix Points require",
        "btrfs" in report["root_mount"],
        report["root_mount"],
    )
    ctx.check(
        "base system booted from disk, not from the live medium",
        "boot=live" not in report["cmdline"],
        report["cmdline"][:200],
    )
    if any(check["state"] == "FAILED" for check in ctx.checks):
        ctx.blocked(
            "the supplied base image is not a usable installed system of this "
            "release; see the failed checks above"
        )

    original = f"phoenix-point-content-{int(time.time())}"
    machine.run(
        f"printf %s {shlex.quote(original)} > {RECOVERY_MARKER}", check=True
    )
    machine.run(f"rm -f {RECOVERY_WITNESS}", check=True)
    machine.run("/usr/bin/sync", check=True)

    created = machine.run(
        "snapper --no-dbus -c root create --print-number --description "
        "'VM acceptance: state to restore'",
        timeout=300,
    )
    number = created["stdout"].strip()
    if created["exitcode"] != 0 or not number.isdecimal():
        ctx.blocked(
            "could not create a Phoenix Point on the base image "
            f"(snapper exit {created['exitcode']}): "
            f"{(created['stdout'] + created['stderr']).strip()[:500]}"
        )
    ctx.observe("phoenix_point", int(number))

    # Diverge from the Point so a restore is observable rather than a no-op.
    mutated = f"mutated-after-point-{int(time.time())}"
    machine.run(f"printf %s {shlex.quote(mutated)} > {RECOVERY_MARKER}", check=True)
    machine.run(f"printf %s witness > {RECOVERY_WITNESS}", check=True)
    machine.run("/usr/bin/sync", check=True)
    ctx.check(
        "system state diverges from the Point before the restore",
        machine.out(f"cat {RECOVERY_MARKER}") == mutated
        and machine.out(f"test -f {RECOVERY_WITNESS} && echo yes || echo no") == "yes",
        "marker mutated and witness file created",
    )
    return {
        "point": int(number),
        "original": original,
        "mutated": mutated,
        "root_device": machine.out("findmnt -no SOURCE / | sed 's/\\[.*//'"),
        "baseline": report,
    }


def _marker_state(machine: Guest, state: dict[str, Any]) -> str:
    """Which generation is the booted root? Never 'probably'."""
    marker = machine.out(f"cat {RECOVERY_MARKER} 2>/dev/null")
    witness = machine.out(f"test -f {RECOVERY_WITNESS} && echo yes || echo no")
    if marker == state["original"] and witness == "no":
        return "restored"
    if marker == state["mutated"] and witness == "yes":
        return "pre-restore"
    return f"mixed(marker={marker!r},witness={witness})"


def case_recovery(ctx: Context) -> None:
    """Restore a Phoenix Point and prove the restored state is what boots."""
    base = _recovery_base(ctx)
    machine = ctx.guest("recovery", firmware=ctx.options.get("firmware", "bios"))
    provenance = machine.clone_disk(base)
    ctx.observe("base_image", provenance["base_image"])
    ctx.evidence.write_json("recovery-base-provenance.json", provenance)
    machine.boot("installed", note="installed system before restore")
    try:
        machine.wait_agent(float(ctx.options.get("boot_timeout", 900)))
        state = _prepare_point(ctx, machine)

        started = time.monotonic()
        restore = machine.run(
            f"/usr/libexec/phoenix-restore {state['point']}", timeout=900
        )
        elapsed = time.monotonic() - started
        ctx.observe("restore_seconds", round(elapsed, 1))
        ctx.evidence.write_text(
            "recovery-phoenix-restore.log",
            f"$ /usr/libexec/phoenix-restore {state['point']}\n"
            f"exit={restore['exitcode']}\n\n{restore['stdout']}\n{restore['stderr']}\n",
        )
        ctx.check(
            "phoenix-restore reports success",
            restore["exitcode"] == 0,
            f"exit {restore['exitcode']}",
        )
        if restore["exitcode"] != 0:
            return

        ctx.check(
            "restore does not take effect before the reboot it asks for",
            _marker_state(machine, state) == "pre-restore",
            "the running root is still the pre-restore generation",
        )
        reboot = machine.reboot(float(ctx.options.get("boot_timeout", 900)))
        ctx.check(
            "the machine really rebooted",
            reboot["boot_id_after"] != reboot["boot_id_before"],
            f"boot_id {reboot['boot_id_before'][:8]} -> {reboot['boot_id_after'][:8]}",
        )

        # "starting" is the question asked too early, not an answer: the
        # agent replies while first-boot units are still running.
        after = system_report(machine)
        after["settled_state"] = _settle_system(machine)
        after["system_state"] = after["settled_state"] or after["system_state"]
        ctx.evidence.write_json("recovery-after-restore.json", after)
        generation = _marker_state(machine, state)
        ctx.check(
            "the restored Point is what boots",
            generation == "restored",
            f"booted generation: {generation}",
        )
        ctx.check(
            "the restored root and /boot are the same generation",
            after["modules_present"] == "yes" and after["boot_kernel_present"] == "yes",
            f"modules for {after['kernel']}: {after['modules_present']}, "
            f"/boot/vmlinuz-{after['kernel']}: {after['boot_kernel_present']}",
        )
        ctx.check(
            "the restored system is the release under test",
            after["version_marker"] == ctx.options["version"],
            f"version marker {after['version_marker']!r}",
        )
        ctx.check(
            "the restored system's package database is consistent",
            after["dpkg_audit"] == "",
            after["dpkg_audit"][:300] or "dpkg --audit is clean",
        )
        ctx.check(
            "the restored system reaches a running state",
            after["system_state"] in ("running", "degraded"),
            f"systemctl is-system-running = {after['system_state']!r}"
            + (f"; failed units: {after['failed_units']}" if after["failed_units"] else ""),
        )
        ctx.snap(machine, "recovery-after-restore.png")
    finally:
        try:
            machine.shutdown()
        finally:
            ctx.collect_machine_evidence(machine, "recovery")


# --- INTERRUPTED RECOVERY (power loss) ----------------------------------------


def _subvolumes(machine: Guest) -> list[str]:
    """The volume's subvolume names, as the guest itself reports them.

    This is the harness's window into what a restore is doing, and it works
    against the implementation that actually shipped. The first version of this
    case watched phoenix-restore's intent journal instead and blocked every
    time: the journal is uncommitted work in the tree, and the 4.0.0 binary in
    the ISO under test (sha256 b730de43..., 254 lines) contains no journalling
    at all. Watching filesystem state rather than a log line also observes the
    restore rather than the restore's account of itself.
    """
    listing = machine.out("btrfs subvolume list / 2>/dev/null")
    return [
        line.split(" path ", 1)[1].strip()
        for line in listing.splitlines()
        if " path " in line
    ]


def _restore_implementation(machine: Guest) -> dict[str, Any]:
    """Identify the restore implementation this artifact actually ships.

    Recorded in the receipt because the expectations below depend on it: a
    binary that does not journal cannot be failed for leaving no journal, and a
    reader needs to see which of the two it was.
    """
    def out(command: str) -> str:
        return machine.run(command, timeout=120)["stdout"].strip()

    return {
        "path": "/usr/libexec/phoenix-restore",
        "sha256": out("sha256sum /usr/libexec/phoenix-restore | cut -d' ' -f1"),
        "lines": out("wc -l < /usr/libexec/phoenix-restore"),
        "package_version": out("dpkg-query -W -f='${Version}' shadowfetch-phoenix"),
        "journals": out("grep -c journal /usr/libexec/phoenix-restore || true"),
        "promises_a_journal": out(
            "grep -qi 'journalled' /usr/libexec/phoenix-restore && echo yes || echo no"
        ),
        "stages_external_boot": out(
            "grep -qc prepare_external_boot /usr/libexec/phoenix-restore && echo yes "
            "|| echo no"
        ),
        "rolls_back_boot_on_interrupt": out(
            "grep -q 'rollback_external_boot' /usr/libexec/phoenix-restore && "
            "grep -qE 'trap .*rollback|ROOT_EXCHANGED' /usr/libexec/phoenix-restore "
            "&& echo yes || echo no"
        ),
    }


def _post_crash_state(machine: Guest, device: str) -> dict[str, Any]:
    """Everything the machine says about itself after the power cut."""
    mount = machine.run(
        "mkdir -p /run/sf-acceptance-top && "
        f"mount -t btrfs -o ro,subvolid=5 {shlex.quote(device)} /run/sf-acceptance-top",
        timeout=120,
    )
    toplevel_entries = ""
    toplevel_journal = ""
    if mount["exitcode"] == 0:
        toplevel_entries = machine.out("ls -1 /run/sf-acceptance-top 2>/dev/null")
        toplevel_journal = machine.out(
            "cat /run/sf-acceptance-top/phoenix-restore.journal 2>/dev/null"
        )
        machine.run("umount /run/sf-acceptance-top", timeout=60)
    return {
        "subvolumes": _subvolumes(machine),
        "toplevel_entries": toplevel_entries,
        "toplevel_journal": toplevel_journal,
        "root_journal": machine.out(
            "cat /var/lib/shadowfetch/phoenix-restore.journal 2>/dev/null"
        ),
        "restore_output": machine.out(
            "cat /var/lib/shadowfetch/phoenix-restore.out 2>/dev/null"
        ),
        "update_grub_flag": machine.out(
            "test -f /var/lib/shadowfetch/phoenix-update-grub && "
            "cat /var/lib/shadowfetch/phoenix-update-grub || echo ABSENT"
        ),
        "root_subvol_option": machine.out("findmnt -no OPTIONS / 2>/dev/null"),
        "boot_backups": machine.out("ls -1d /boot/phoenix-kernel-backup-* 2>/dev/null"),
        "boot_contents": machine.out("ls -1 /boot 2>/dev/null"),
        "grub_next_entry": machine.out(
            "grub-editenv /boot/grub/grubenv list 2>/dev/null"
        ),
    }


def case_recovery_interrupted(ctx: Context) -> None:
    """Cut power in the middle of a restore.

    The claim under test is not "the restore succeeds". It is the far more
    important one: whatever the machine does after losing power mid-restore, it
    must never end up reporting a completed restore it did not complete, and it
    must never boot a root from one generation against a /boot from another.

    The kill is aimed, not timed. The harness watches the volume's subvolume
    list through the guest agent and pulls the plug the moment @new exists --
    the writable copy of the Point is made, the atomic exchange has not
    happened, and the external /boot has already been staged. That is the one
    window in which a half-applied restore is possible. If the restore finishes
    before the harness can cut (it takes about 1.6s on this hardware), the case
    reports BLOCKED: an interruption that did not interrupt anything proves
    nothing, and must not be allowed to look like a pass.
    """
    base = _recovery_base(ctx)
    machine = ctx.guest(
        "recovery-interrupted", firmware=ctx.options.get("firmware", "bios")
    )
    provenance = machine.clone_disk(base)
    ctx.observe("base_image", provenance["base_image"])
    ctx.evidence.write_json("interrupted-base-provenance.json", provenance)
    machine.boot("installed", note="installed system before interrupted restore")
    try:
        machine.wait_agent(float(ctx.options.get("boot_timeout", 900)))
        state = _prepare_point(ctx, machine)
        device = state["root_device"]
        ctx.observe("root_device", device)

        implementation = _restore_implementation(machine)
        ctx.evidence.write_json("interrupted-implementation.json", implementation)
        ctx.observe("restore_binary_sha256", implementation["sha256"])
        journals = implementation["journals"].isdigit() and int(
            implementation["journals"]
        ) > 0
        ctx.observe("restore_journals", journals)

        # Both generations here share a kernel version, so the /boot staging
        # moves no kernel out. Recorded rather than assumed: it bounds what the
        # root-versus-/boot check below can prove on this base image.
        kernel = machine.out("uname -r")
        point_kernels = machine.out(
            f"ls -1 /.snapshots/{state['point']}/snapshot/lib/modules 2>/dev/null"
        )
        ctx.observe("running_kernel", kernel)
        ctx.observe("point_kernels", point_kernels.split())

        machine.run("rm -f /var/lib/shadowfetch/phoenix-restore.out", check=True)
        machine.run("/usr/bin/sync", check=True)
        baseline = set(_subvolumes(machine))
        ctx.observe("subvolumes_before_restore", sorted(baseline))

        deadline_seconds = float(ctx.options.get("interrupt_deadline", 90))
        ctx.log(f"starting phoenix-restore {state['point']} detached")
        guest_pid = machine.agent.execute_detached(
            f"/usr/libexec/phoenix-restore {state['point']} "
            "> /var/lib/shadowfetch/phoenix-restore.out 2>&1"
        )
        ctx.observe("restore_guest_pid", guest_pid)

        started = time.monotonic()
        trigger = None
        observed: list[str] = []
        while time.monotonic() - started < deadline_seconds:
            observed = _subvolumes(machine)
            appeared = set(observed) - baseline
            completed = {name for name in appeared if name.startswith("@_prev_")}
            if "@new" in observed:
                trigger = "pre-exchange-window"
                break
            if completed:
                ctx.observe("subvolumes_at_completion", sorted(observed))
                ctx.blocked(
                    "the restore completed in "
                    f"{time.monotonic() - started:.1f}s -- before the harness could "
                    f"cut power (it observed {sorted(completed)}). Nothing was "
                    "interrupted, so this run proves nothing about power loss "
                    "during a restore and is not a pass. Re-run against a Point "
                    "large enough that the writable copy and the /boot staging "
                    "take longer than one guest-agent round trip."
                )
        elapsed = time.monotonic() - started
        if trigger is None:
            ctx.blocked(
                f"no restore was observed in flight within {deadline_seconds:.0f}s "
                f"(subvolumes: {sorted(observed)}). The power cut was not taken, "
                "because cutting power to a machine that is not restoring anything "
                "would prove nothing."
            )

        killed = machine.kill_hard()
        ctx.observe("interrupt_trigger", trigger)
        ctx.observe("interrupt_after_seconds", round(elapsed, 2))
        ctx.observe("subvolumes_at_interrupt", sorted(observed))
        ctx.log(f"power cut: SIGKILL to QEMU {killed['pid']} at {killed['at_utc']}")
        ctx.evidence.write_json(
            "interrupted-power-cut.json",
            {
                "trigger": trigger,
                "elapsed_seconds": round(elapsed, 2),
                "subvolumes_at_interrupt": sorted(observed),
                "subvolumes_before": sorted(baseline),
                "qemu": killed,
                "implementation": implementation,
            },
        )
        ctx.collect_machine_evidence(machine, "interrupted-first")

        # --- and now: what does the machine do next? ---
        machine.boot("installed", note="boot after the power cut")
        recovered = True
        try:
            machine.wait_agent(float(ctx.options.get("boot_timeout", 900)))
        except GuestError as error:
            recovered = False
            ctx.observe("post_crash_boot_error", str(error))
            ctx.snap(machine, "interrupted-post-crash-console.png")
        ctx.check(
            "the machine still boots after power loss during a restore",
            recovered,
            "the guest agent answered after the power cut"
            if recovered
            else "the guest never came back; see the serial log and the console capture",
        )
        if not recovered:
            return

        after = system_report(machine)
        after["settled_state"] = _settle_system(machine)
        after["system_state"] = after["settled_state"] or after["system_state"]
        crash = _post_crash_state(machine, device)
        ctx.evidence.write_json(
            "interrupted-post-crash.json", {"system": after, "state": crash}
        )

        generation = ctx.observe("post_crash_generation", _marker_state(machine, state))
        # What the machine CLAIMS. On the shipped implementation there are two
        # surfaces: the restore's own output, and the machine-readable flag the
        # next boot acts on. Neither may claim a restore that did not happen.
        claimed_complete = ctx.observe(
            "claims_a_completed_restore",
            "is now the permanent system root" in crash["restore_output"]
            or crash["update_grub_flag"] != "ABSENT"
            or "restore complete" in crash["toplevel_journal"]
            or "restore complete" in crash["root_journal"],
        )

        # 1. One coherent generation. Not a blend of two.
        ctx.check(
            "the booted root is exactly one generation, not a mixture",
            generation in ("restored", "pre-restore"),
            f"booted generation: {generation}",
        )
        # 2. The honesty invariant, and the reason this case exists.
        #    Under-claiming is safe: the cut can land after the exchange but
        #    before anything durable says so, and a restore that quietly worked
        #    harms nobody. Over-claiming is the defect -- a completion reported
        #    for work that did not happen.
        ctx.check(
            "no completed restore is claimed unless the restore completed",
            (not claimed_complete) or generation == "restored",
            f"claims completion = {claimed_complete}, booted generation = "
            f"{generation}",
        )
        # 3. The leftover writable copy must not become the running root. The
        #    implementation parks it on the next restore; what matters here is
        #    that the machine did not boot it by accident.
        ctx.check(
            "a leftover writable copy is not what the machine booted",
            "subvol=/@" in crash["root_subvol_option"]
            and "@new" not in crash["root_subvol_option"],
            f"root mount options: {crash['root_subvol_option']}",
        )
        # 4. Root and /boot must never come from different generations. On this
        #    base image both generations carry the same kernel, so the /boot
        #    staging moves nothing; this check is therefore necessary but not
        #    sufficient, and a differing-kernel base image would test it harder.
        ctx.check(
            "root and /boot are the same generation after the power cut",
            after["modules_present"] == "yes" and after["boot_kernel_present"] == "yes",
            f"running kernel {after['kernel']}: modules "
            f"{after['modules_present']}, /boot image {after['boot_kernel_present']}",
        )
        # 5. Diagnosability, judged against what this implementation promises.
        #    A binary that never journals is not failed for leaving no journal;
        #    one whose own help text promises a journal is.
        if journals or implementation["promises_a_journal"] == "yes":
            ctx.check(
                "the interrupted restore left the durable journal it promises",
                bool(
                    crash["toplevel_journal"].strip() or crash["root_journal"].strip()
                ),
                f"top-level journal {len(crash['toplevel_journal'])} bytes, "
                f"root journal {len(crash['root_journal'])} bytes",
            )
        else:
            ctx.observe(
                "diagnosability",
                "the implementation in this artifact does not journal its restore "
                "steps, so an interrupted restore leaves no intent record; the "
                "only trace is the restore's captured output and the on-disk "
                "subvolume layout",
            )
            ctx.check(
                "the implementation does not promise a journal it does not write",
                implementation["promises_a_journal"] == "no",
                "no journalling in the binary and no promise of one in its help",
            )
        # 6. The system is usable, not merely alive.
        ctx.check(
            "the recovered system reports the release version",
            after["version_marker"] == ctx.options["version"],
            f"version marker {after['version_marker']!r}",
        )
        ctx.check(
            "the recovered system's package database is consistent",
            after["dpkg_audit"] == "",
            after["dpkg_audit"][:300] or "dpkg --audit is clean",
        )
        ctx.check(
            "the recovered system reaches a running state",
            after["system_state"] in ("running", "degraded"),
            f"systemctl is-system-running = {after['system_state']!r}"
            + (f"; failed units: {after['failed_units']}" if after["failed_units"] else ""),
        )
        # 7. And it must still be possible to finish the job: a restore run
        #    after the crash has to reach a coherent result rather than trip
        #    over the wreckage of the first one.
        second = machine.run(
            f"/usr/libexec/phoenix-restore {state['point']}", timeout=900
        )
        ctx.evidence.write_text(
            "interrupted-second-restore.log",
            f"exit={second['exitcode']}\n\n{second['stdout']}\n{second['stderr']}\n",
        )
        ctx.check(
            "a restore attempted after the power cut either succeeds or refuses "
            "out loud",
            second["exitcode"] == 0
            or "phoenix-restore:" in (second["stdout"] + second["stderr"]),
            f"exit {second['exitcode']}: "
            f"{(second['stdout'] + second['stderr']).strip()[:200]}",
        )
        if second["exitcode"] == 0:
            machine.reboot(float(ctx.options.get("boot_timeout", 900)))
            recovered_generation = ctx.observe(
                "generation_after_second_restore", _marker_state(machine, state)
            )
            ctx.check(
                "the restore that follows the power cut lands the Point it names",
                recovered_generation == "restored",
                f"booted generation: {recovered_generation}",
            )
        ctx.snap(machine, "interrupted-post-crash.png")
    finally:
        try:
            machine.shutdown()
        finally:
            ctx.collect_machine_evidence(machine, "interrupted-second")


# --- UPGRADE ------------------------------------------------------------------


def case_upgrade(ctx: Context) -> None:
    """Upgrade an installed previous release to the release under test.

    Requires a previous-release installed image. There is no honest way to
    synthesise one from the artifact under test, so without it this case is
    BLOCKED, never assumed.
    """
    base = ctx.options.get("upgrade_base_image")
    if not base:
        ctx.blocked(
            "no previous-release installed image supplied (--upgrade-base-image). "
            "The 3.5.0 QA base this tree's existing upgrade clones are layered on "
            "(~/projects/shadowfetch-3.5.0/work/qa-3.5.0/vm/bios-fire-2af853b1/"
            "disk.qcow2) no longer exists on this host, so every one of those "
            "clones is unopenable. Rebuild or restore a 3.5.0 installed image "
            "before this case can run."
        )
    base_path = Path(base).resolve()
    if not base_path.is_file():
        ctx.blocked(f"previous-release base image does not exist: {base_path}")

    repo = ctx.options.get("upgrade_repo")
    if not repo:
        ctx.blocked(
            "no package source supplied (--upgrade-repo). The upgrade must "
            "install the packages built from the artifact under test, not "
            "whatever a network mirror happens to serve."
        )

    machine = ctx.guest("upgrade", firmware=ctx.options.get("firmware", "bios"))
    provenance = machine.clone_disk(base_path)
    ctx.evidence.write_json("upgrade-base-provenance.json", provenance)
    machine.boot("installed", note="previous release before upgrade")
    try:
        machine.wait_agent(float(ctx.options.get("boot_timeout", 900)))
        before = system_report(machine)
        ctx.evidence.write_json("upgrade-before.json", before)
        previous = ctx.options.get("upgrade_from_version", "")
        ctx.check(
            "base system is the previous release",
            bool(previous) and before["version_marker"] == previous,
            f"version marker {before['version_marker']!r}, expected {previous!r}",
        )
        if before["version_marker"] == ctx.options["version"]:
            ctx.blocked(
                "the supplied base image is already the release under test; an "
                "upgrade case run on it would prove nothing"
            )

        home = machine.out("getent passwd 1000 | cut -d: -f6") or "/root"
        keepsake = f"{home}/vm-acceptance-user-data.txt"
        content = f"user data that must survive the upgrade {time.time()}"
        machine.run(f"printf %s {shlex.quote(content)} > {shlex.quote(keepsake)}", check=True)
        digest = machine.out(f"sha256sum {shlex.quote(keepsake)} | cut -d' ' -f1")

        # The first 5.0.0 run ran apt the moment the agent answered and every
        # fetch from 10.0.2.2 failed with "Address family for hostname not
        # supported": no IPv4 address yet. A network that never comes up is the
        # environment, not the upgrade, so it is BLOCKED -- and apt never runs.
        _await_network(ctx, machine, "upgrade")
        install = machine.run(
            "DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=180 "
            f"--no-remove -y install {repo}",
            timeout=3600,
        )
        ctx.evidence.write_text(
            "upgrade-apt.log",
            f"exit={install['exitcode']}\n\n{install['stdout']}\n{install['stderr']}\n",
        )
        ctx.check(
            "the upgrade installs without removing packages",
            install["exitcode"] == 0
            and not any(
                line.startswith("Remv ") for line in install["stdout"].splitlines()
            ),
            f"apt-get exit {install['exitcode']}",
        )
        machine.reboot(float(ctx.options.get("boot_timeout", 900)))
        after = settled_report(ctx, machine, "upgrade_after")
        ctx.evidence.write_json("upgrade-after.json", after)
        ctx.check(
            "the upgraded system is the release under test",
            after["version_marker"] == ctx.options["version"],
            f"version marker {after['version_marker']!r}",
        )
        ctx.check(
            "user data survives the upgrade byte for byte",
            machine.out(f"sha256sum {shlex.quote(keepsake)} | cut -d' ' -f1") == digest,
        )
        ctx.check(
            "the machine identity is preserved across the upgrade",
            after["machine_id"] == before["machine_id"],
        )
        ctx.check(
            "the upgraded system's package database is consistent",
            after["dpkg_audit"] == "",
            after["dpkg_audit"][:300] or "dpkg --audit is clean",
        )
        check_system_state(ctx, "the upgraded system reaches a running state", after)
        ctx.snap(machine, "upgrade-after.png")
    finally:
        try:
            machine.shutdown()
        finally:
            ctx.collect_machine_evidence(machine, "upgrade")


# A guest is "online" for apt when network-online.target is active AND it has
# an IPv4 default route: the target alone can be reached by a wait-online that
# gave up, and the route is what a fetch from the host's 10.0.2.2 needs.
NETWORK_ONLINE_PROBE = (
    "systemctl is-active --quiet network-online.target "
    "&& ip -4 route show default | grep -q ."
)


def _await_network(ctx: Context, machine: Guest, label: str) -> float:
    """Wait, bounded by --network-timeout, for the guest network to be online.

    Returns the seconds waited and records them. Running out raises Blocked with
    the guest's own view of its network written to evidence first: the case
    could not be executed, which is not a verdict on the artifact.
    """
    bound = float(ctx.options.get("network_timeout", 180))
    started = time.monotonic()
    deadline = started + bound
    while True:
        # nm-online returns as soon as NetworkManager's startup is complete, or
        # after its own -t; missing on a non-NM image, it simply fails fast.
        machine.run("nm-online -s -q -t 20 2>/dev/null", timeout=60)
        if machine.run(NETWORK_ONLINE_PROBE, timeout=60)["exitcode"] == 0:
            waited = round(time.monotonic() - started, 1)
            ctx.observe(f"{label}_network_online_seconds", waited)
            return waited
        if time.monotonic() >= deadline:
            break
        time.sleep(5)
    state = machine.out(
        "{ systemctl is-active network-online.target; ip -4 addr; "
        "ip -4 route; nmcli -t general status; } 2>&1", timeout=60)
    ctx.evidence.write_text(f"{label}-network-wait.log", state or "(no output)")
    ctx.blocked(
        f"the guest network did not come online within {bound:g}s "
        "(network-online.target active with an IPv4 default route), so the "
        "package source could not be reached; apt was not run"
    )
    return 0.0  # unreachable: blocked() raises


# --- INSTALL ------------------------------------------------------------------
#
# WHY THIS CASE IS DRIVEN THROUGH ACCESSIBILITY, AND WHAT HAD TO BE FIXED FIRST.
#
# The installer is driven through the guest's AT-SPI bus so that every step
# asserts WHICH page it is acting on before it acts on it. Blind keystrokes
# into a wizard prove nothing: they can "succeed" against a dialog that is not
# the one anybody thinks it is.
#
# Two obstacles stood in the way. The second is the one that blocked this case
# through five consecutive runs with "expected exactly one 'calamares'
# application, found 0".
#
# 1. pkexec. The desktop launcher `calamares-install-debian` runs xhost and
#    then pkexec, and pkexec cannot be authorised without a human at the
#    keyboard: through the guest agent it answers "Error executing command as
#    another user: Not authorized" and nothing starts. The harness starts
#    /usr/bin/calamares directly as root instead. Say plainly what that costs:
#    this case covers the INSTALLER, not the polkit path a user takes to reach
#    it.
#
# 2. A root process cannot use the live user's D-Bus session bus AT ALL, and
#    that -- not the toolkit, not the environment -- is why the installer never
#    appeared on the bus. Measured inside the 4.0.0 live session: connecting to
#    unix:path=/run/user/1000/bus as uid 0 is dropped at EXTERNAL
#    authentication ("org.freedesktop.DBus.Error.NoReply: Did not receive a
#    reply"), and the running installer holds exactly two sockets, one to the
#    Wayland compositor and none to any accessibility bus. Qt's AT-SPI bridge
#    (compiled into libQt6Gui here, not a loadable plugin) attaches only after
#    it can read org.a11y.Status from a session bus, so with no session bus
#    there is no bridge, no registration, and no "calamares" on anybody's
#    accessibility bus. Setting QT_ACCESSIBILITY, or switching
#    org.a11y.Status.IsEnabled on for the SESSION USER, cannot help: both fix
#    an obstacle the root process never reaches.
#
#    So the installer is given a session bus of its own uid -- a private
#    dbus-daemon started as root, on which org.a11y.Bus activates a root-owned
#    at-spi bus and registry -- and the driver reads that same bus. Nothing
#    about the installer is faked or bypassed: it is the shipped binary, on the
#    live session's real compositor, doing a real install of the artifact under
#    test to a real disk that is then booted.

ATSPI_PATH = "/tmp/sf-atspi-driver.py"
A11Y_RUNTIME = "/run/sf-acceptance-a11y"

ATSPI_DRIVER = r'''#!/usr/bin/env python3
"""Read and act on one application's accessible controls.

Runs inside the guest, as the uid that owns the accessibility bus it is given.

Every selector must resolve to EXACTLY ONE showing control of the named role.
An ambiguous selector is an error and never a guess: on the summary page
"Install" is both the sidebar step and the button that starts the installation,
and a driver that quietly picked one of them would be asserting nothing about
which one it pressed.
"""
import json
import re
import sys

import dbus

ACC = "org.a11y.atspi.Accessible"
PROP = "org.freedesktop.DBus.Properties"
ACTION = "org.a11y.atspi.Action"
TEXT = "org.a11y.atspi.Text"
COMPONENT = "org.a11y.atspi.Component"

ROLES = {7: "checkbox", 11: "combo", 16: "dialog", 20: "filler", 23: "frame",
         26: "icon", 27: "image", 29: "label", 30: "layered", 31: "list",
         32: "listitem", 33: "menu", 35: "menuitem", 37: "tab", 38: "tablist",
         39: "panel", 40: "password", 42: "progress", 43: "button",
         44: "radio", 46: "rootpane", 48: "scrollbar", 49: "scrollpane",
         51: "slider", 52: "spin", 53: "split", 55: "table", 56: "cell",
         61: "text", 62: "toggle", 65: "tree", 66: "treetable", 68: "viewport",
         69: "window", 75: "application", 79: "entry", 83: "heading",
         84: "page", 85: "section", 91: "treeitem"}
STATES = {1: "active", 4: "checked", 6: "defunct", 7: "editable", 8: "enabled",
          10: "expanded", 11: "focusable", 12: "focused", 16: "modal",
          20: "pressed", 23: "selected", 24: "sensitive", 25: "showing",
          30: "visible", 39: "default", 41: "checkable"}
# A closed combo box still reports its popup list as showing, so walking into
# one would put sixty language names on every page snapshot.
COLLAPSED = (11,)


def fail(message):
    sys.stderr.write(message + "\n")
    raise SystemExit(1)


def normalise(value):
    """Compare what a person reads, not what the toolkit stores.

    Calamares labels its partitioning choices in rich text, so the accessible
    name of the erase-disk option is a paragraph of HTML. Tags become spaces
    rather than vanishing, so that "<b>Erase</b>disk" cannot be made to match
    "Erasedisk".
    """
    value = re.sub(r"<[^>]+>", " ", value).replace("&", "")
    return re.sub(r"\s+", " ", value).strip().rstrip(".").casefold()


def states_of(obj):
    """AT-SPI reports states as a two-word bitfield, not a list of numbers."""
    words = [int(word) for word in obj.GetState(dbus_interface=ACC)]
    bits = words[0] | (words[1] << 32) if len(words) > 1 else words[0]
    return sorted(name for bit, name in STATES.items() if bits & (1 << bit))


def connect():
    session = dbus.SessionBus()
    status = session.get_object("org.a11y.Bus", "/org/a11y/bus")
    return dbus.bus.BusConnection(
        str(status.GetAddress(dbus_interface="org.a11y.Bus")))


def applications(bus):
    registry = bus.get_object("org.a11y.atspi.Registry",
                              "/org/a11y/atspi/accessible/root")
    found = []
    for name, path in registry.GetChildren(dbus_interface=ACC):
        try:
            obj = bus.get_object(name, path)
            label = str(obj.Get(ACC, "Name", dbus_interface=PROP))
        except dbus.DBusException:
            continue
        found.append((label, str(name), str(path)))
    return found


def walk(bus, name, path, depth, out, limit=2000):
    if depth > 30 or len(out) >= limit:
        return
    try:
        obj = bus.get_object(name, path)
        role = int(obj.GetRole(dbus_interface=ACC))
        out.append({
            "depth": depth,
            "role": ROLES.get(role, str(role)),
            "role_id": role,
            "name": str(obj.Get(ACC, "Name", dbus_interface=PROP)),
            "description": str(obj.Get(ACC, "Description", dbus_interface=PROP)),
            "states": states_of(obj),
            "bus": name,
            "path": path,
        })
        if role in COLLAPSED:
            return
        for child, childpath in obj.GetChildren(dbus_interface=ACC):
            walk(bus, str(child), str(childpath), depth + 1, out, limit)
    except dbus.DBusException:
        return


def select(nodes, role, by, value, index, expect):
    key = "description" if by == "desc" else "name"
    matches = [node for node in nodes
               if node["role"] == role
               and "showing" in node["states"]
               and normalise(node[key]).startswith(normalise(value))]
    if len(matches) != expect:
        fail("expected %d showing %s matching %s %r, found %d: %s"
             % (expect, role, by, value, len(matches),
                json.dumps([(m["name"][:60], m["states"]) for m in matches])))
    return matches[index]


bus = connect()
app_name = sys.argv[1]
mode = sys.argv[2]
present = applications(bus)
if mode == "apps":
    print(json.dumps({"applications": sorted(label for label, _, _ in present)},
                     indent=2))
    raise SystemExit(0)

wanted = [entry for entry in present if entry[0] == app_name]
if len(wanted) != 1:
    fail("expected exactly one %r application on the accessibility bus, found "
         "%d among %r" % (app_name, len(wanted),
                          sorted(label for label, _, _ in present)))
nodes = []
walk(bus, wanted[0][1], wanted[0][2], 0, nodes)

if mode == "dump":
    print(json.dumps(nodes, indent=2))
    raise SystemExit(0)
if mode == "page":
    # What a person can see right now. Qt keeps every wizard page in the widget
    # tree and marks only the current one as showing, so this is the page's own
    # account of itself -- not a guess from a screenshot, and not the step the
    # harness believes it asked for.
    print(json.dumps({"showing": [
        {"role": node["role"], "name": node["name"],
         "description": node["description"], "states": node["states"]}
        for node in nodes
        if "showing" in node["states"] and node["depth"] > 0
        and (node["name"] or node["description"]
             or node["role"] in ("progress", "entry", "text", "password"))
    ]}, indent=2))
    raise SystemExit(0)

role, by, value = sys.argv[3], sys.argv[4], sys.argv[5]
index = int(sys.argv[6]) if len(sys.argv) > 6 else 0
expect = int(sys.argv[7]) if len(sys.argv) > 7 else 1
match = select(nodes, role, by, value, index, expect)
obj = bus.get_object(match["bus"], match["path"])

if mode == "control":
    print(json.dumps({"name": match["name"], "description": match["description"],
                      "states": match["states"]}))
elif mode == "click":
    if "sensitive" not in match["states"]:
        fail("refusing to act on an insensitive %s %r: states %s"
             % (role, value, match["states"]))
    result = bool(obj.DoAction(0, dbus_interface=ACTION, timeout=20))
    print(json.dumps({"clicked": match["name"][:120], "result": result,
                      "states_before": match["states"]}))
elif mode == "focus":
    grabbed = bool(obj.GrabFocus(dbus_interface=COMPONENT, timeout=20))
    print(json.dumps({"grabbed": grabbed, "states": states_of(obj)}))
elif mode == "text":
    print(json.dumps({"text": str(obj.GetText(0, -1, dbus_interface=TEXT,
                                              timeout=20)),
                      "states": states_of(obj)}))
else:
    fail("unknown mode %r" % mode)
'''

# The account the installer is told to create. Every character here has to be
# typeable as a QEMU key name, and the installed system is checked for exactly
# this account afterwards: it is the end-to-end proof that what was typed into
# the accessible fields is what the installer wrote to the disk.
INSTALL_ACCOUNT = {
    "fullname": "QA Acceptance",
    "username": "qa",
    "hostname": "sf-acceptance",
    "password": "acceptance4000",
}

# The users page's three line edits carry no accessible name at all, so they
# are told apart by the hint Calamares shows under each one -- the same thing a
# person reads. Matching them by position in the layout would silently survive
# a reordering of the page, which is exactly the kind of "it passed" this
# harness exists to prevent. The two password fields are indistinguishable by
# description because they say the same thing; they take the same value, and
# the count is asserted (exactly two) so a third one appearing is an error.
USER_FIELDS = (
    ("fullname", "text", "Your Full Name", 1, 0),
    ("username", "text", "<small>If more than one person", 1, 0),
    ("hostname", "text", "<small>This name will be used", 1, 0),
    ("password", "password", "<small>Enter the same password twice", 2, 0),
    ("confirm", "password", "<small>Enter the same password twice", 2, 1),
)

QEMU_KEYS = {" ": "spc", "-": "minus", ".": "dot", "_": "shift-minus",
             "/": "slash", "@": "shift-2"}


def _keys_for(text: str) -> list[str]:
    """QEMU key names for a string, or an error. Never a silent substitution."""
    keys = []
    for char in text:
        if char.islower() or char.isdigit():
            keys.append(char)
        elif char.isupper():
            keys.append("shift-" + char.lower())
        elif char in QEMU_KEYS:
            keys.append(QEMU_KEYS[char])
        else:
            raise GuestError(f"no QEMU key name for {char!r}")
    return keys


def _plain(value: str) -> str:
    """The visible text of a rich-text accessible name."""
    import re as _re

    return _re.sub(r"\s+", " ", _re.sub(r"<[^>]+>", " ", value)).strip()


def _live_session(ctx: Context, machine: Guest) -> dict[str, str]:
    """Find the live desktop session to drive. Observed, not assumed."""
    probe = machine.run(
        "for p in $(pgrep -x plasmashell); do "
        "u=$(stat -c %U /proc/$p); i=$(stat -c %u /proc/$p); "
        "d=$(tr '\\0' '\\n' < /proc/$p/environ | sed -n 's/^DISPLAY=//p' | head -1); "
        "w=$(tr '\\0' '\\n' < /proc/$p/environ | sed -n 's/^WAYLAND_DISPLAY=//p' | head -1); "
        "printf '%s\\t%s\\t%s\\t%s\\n' \"$u\" \"$i\" \"$d\" \"$w\"; done",
        timeout=120,
    )
    rows = [line.split("\t") for line in probe["stdout"].strip().splitlines() if line]
    if len(rows) != 1:
        ctx.blocked(
            "expected exactly one live desktop session to drive, found "
            f"{len(rows)}: {probe['stdout'].strip()[:300]!r}"
        )
    user, uid, display, wayland = (rows[0] + ["", "", "", ""])[:4]
    return {
        "user": user,
        "uid": uid,
        "display": display,
        "wayland_display": wayland,
    }


def _installer_bus(ctx: Context, machine: Guest) -> str:
    """Give the root installer a session bus, and an accessibility bus on it.

    See the note at the top of this section: without this the installer runs
    perfectly and is invisible to every accessibility client, which reads
    exactly like "the installer failed to start".
    """
    machine.run(f"rm -rf {A11Y_RUNTIME}", timeout=60)
    machine.run(f"mkdir -p {A11Y_RUNTIME} && chmod 700 {A11Y_RUNTIME}", check=True)
    started = machine.run(
        f"/usr/bin/env XDG_RUNTIME_DIR={A11Y_RUNTIME} HOME=/root "
        "/usr/bin/dbus-daemon --session --fork --print-address "
        f"--address=unix:path={A11Y_RUNTIME}/bus",
        timeout=120,
    )
    address = started["stdout"].strip().splitlines()[-1].strip() if started["stdout"] else ""
    if started["exitcode"] != 0 or not address.startswith("unix:"):
        ctx.blocked(
            "could not start a session bus for the installer (dbus-daemon exit "
            f"{started['exitcode']}): "
            f"{(started['stdout'] + started['stderr']).strip()[:300]}"
        )
    environment = (
        f"DBUS_SESSION_BUS_ADDRESS={shlex.quote(address)} "
        f"XDG_RUNTIME_DIR={A11Y_RUNTIME} HOME=/root "
    )
    # Setting IsEnabled both switches toolkit accessibility on and activates
    # org.a11y.Bus on this private bus, which starts the at-spi bus and registry
    # as root. Qt attaches its bridge at construction, so this has to happen
    # before the installer starts.
    machine.run(
        f"/usr/bin/env {environment}/usr/bin/dbus-send --session --print-reply "
        "--dest=org.a11y.Bus /org/a11y/bus org.freedesktop.DBus.Properties.Set "
        "string:org.a11y.Status string:IsEnabled variant:boolean:true",
        timeout=120,
    )
    state = machine.run(
        f"/usr/bin/env {environment}/usr/bin/dbus-send --session --print-reply "
        "--dest=org.a11y.Bus /org/a11y/bus org.freedesktop.DBus.Properties.Get "
        "string:org.a11y.Status string:IsEnabled; "
        f"/usr/bin/env {environment}/usr/bin/dbus-send --session --print-reply "
        "--dest=org.a11y.Bus /org/a11y/bus org.a11y.Bus.GetAddress",
        timeout=120,
    )
    ctx.evidence.write_text(
        "install-accessibility.log",
        f"$ dbus-daemon --session --address=unix:path={A11Y_RUNTIME}/bus\n"
        f"{address}\n\n$ dbus-send ... org.a11y.Status IsEnabled / GetAddress\n"
        f"exit={state['exitcode']}\n{state['stdout']}\n{state['stderr']}\n\n"
        "processes:\n"
        + machine.out("ps -eo user:10,pid,args | grep -E 'at-spi|dbus-daemon' "
                      "| grep -v grep"),
    )
    ctx.check(
        "toolkit accessibility is switched on for the installer's own session",
        "boolean true" in state["stdout"] and "at-spi" in state["stdout"],
        _plain(state["stdout"])[:200],
    )
    ctx.observe("installer_a11y_bus", address)
    return address


def _driver(
    ctx: Context,
    machine: Guest,
    bus: str,
    *args: str,
    timeout: float = 240,
) -> dict:
    command = (
        f"/usr/bin/env DBUS_SESSION_BUS_ADDRESS={shlex.quote(bus)} "
        f"XDG_RUNTIME_DIR={A11Y_RUNTIME} HOME=/root LC_ALL=C.UTF-8 "
        f"/usr/bin/python3 {ATSPI_PATH} "
        + " ".join(shlex.quote(argument) for argument in args)
    )
    return machine.run(command, timeout=timeout)


def _page(ctx: Context, machine: Guest, bus: str, timeout: float = 240) -> list[dict]:
    result = _driver(ctx, machine, bus, "calamares", "page", timeout=timeout)
    if result["exitcode"] != 0:
        raise GuestError(
            "could not read the installer's accessible page: "
            f"{(result['stdout'] + result['stderr']).strip()[:400]}"
        )
    return json.loads(result["stdout"])["showing"]


def _visible(page: list[dict], role: str, text: str) -> bool:
    needle = text.casefold()
    return any(
        node["role"] == role and needle in _plain(node["name"]).casefold()
        for node in page
    )


def _labels(page: list[dict]) -> str:
    return " | ".join(
        _plain(node["name"]) for node in page
        if node["role"] in ("label", "heading") and node["name"]
    )


def _await_page(
    ctx: Context,
    machine: Guest,
    bus: str,
    name: str,
    markers: tuple[tuple[str, str], ...],
    timeout: float = 90,
) -> list[dict]:
    """Wait for a page that carries every one of its markers, then assert it.

    The check is on the page's own controls, so "the installer advanced" cannot
    be satisfied by a screenshot that happens to look right, by the step the
    harness asked for, or by a page that merely stopped changing.
    """
    deadline = time.monotonic() + timeout
    page: list[dict] = []
    while time.monotonic() < deadline:
        page = _page(ctx, machine, bus)
        if all(_visible(page, role, text) for role, text in markers):
            break
        time.sleep(3)
    ctx.evidence.write_json(f"install-page-{name}.json", page)
    missing = [text for role, text in markers if not _visible(page, role, text)]
    ctx.check(
        f"the installer is on its {name} page",
        not missing,
        f"missing {missing!r}; showing: {_labels(page)[:300]}"
        if missing else _labels(page)[:300],
    )
    if missing:
        ctx.blocked(
            f"the installer never reached its {name} page (missing {missing!r}). "
            "Acting on an unidentified page would prove nothing, so the run "
            f"stops here. What was on screen: {_labels(page)[:400]}"
        )
    return page


def _click(ctx: Context, machine: Guest, bus: str, role: str, name: str) -> dict:
    result = _driver(ctx, machine, bus, "calamares", "click", role, "name", name)
    if result["exitcode"] != 0:
        ctx.blocked(
            f"could not press the {role} named {name!r}: "
            f"{(result['stdout'] + result['stderr']).strip()[:300]}"
        )
    ctx.log(f"pressed {role} {name!r}: {result['stdout'].strip()[:160]}")
    return json.loads(result["stdout"])


def _control(
    ctx: Context, machine: Guest, bus: str, role: str, name: str
) -> list[str]:
    result = _driver(ctx, machine, bus, "calamares", "control", role, "name", name)
    if result["exitcode"] != 0:
        return []
    return list(json.loads(result["stdout"])["states"])


def _type_into(
    ctx: Context, machine: Guest, bus: str, field: tuple, text: str
) -> dict:
    """Focus a field through accessibility, type real keys, read it back.

    Setting the text through AT-SPI's EditableText interface DOES land the
    characters, and Calamares ignores them. Measured on 4.0.0: all five fields
    set that way, all five reading back correctly, and the Next button still
    insensitive -- the users page listens for QLineEdit::textEdited, the signal
    that means a person typed. One real keystroke enabled it.

    So the keys are real (QEMU's own input device, the path a physical keyboard
    takes) while every assertion stays on the accessibility bus: focus is
    verified before typing, and the content is read back after. A password
    field reads back as bullets, so for those the character count is what can
    honestly be compared.
    """
    name, role, hint, expect, index = field
    for attempt in range(1, 4):
        focus = _driver(
            ctx, machine, bus, "calamares", "focus", role, "desc", hint,
            str(index), str(expect),
        )
        if focus["exitcode"] != 0:
            ctx.blocked(
                f"could not find the {name} field on the users page: "
                f"{(focus['stdout'] + focus['stderr']).strip()[:300]}"
            )
        if "focused" not in json.loads(focus["stdout"])["states"]:
            ctx.log(f"{name}: focus not taken on attempt {attempt}")
            time.sleep(1.0)
            continue
        machine.sendkeys(["ctrl-a", "delete"] + _keys_for(text))
        time.sleep(0.8)
        read = _driver(
            ctx, machine, bus, "calamares", "text", role, "desc", hint,
            str(index), str(expect),
        )
        content = json.loads(read["stdout"])["text"] if read["exitcode"] == 0 else ""
        landed = (
            len(content) == len(text) if role == "password" else content == text
        )
        if landed:
            return {"field": name, "attempts": attempt,
                    "read_back": len(content) if role == "password" else content}
        ctx.log(f"{name}: read back {content!r} after attempt {attempt}")
        time.sleep(1.0)
    ctx.check(
        f"the {name} field holds what was typed into it",
        False,
        f"after 3 attempts the field did not hold the typed value",
    )
    return {"field": name, "attempts": 3, "read_back": None}


def _settle_system(machine: Guest, seconds: float = 300) -> str:
    """Wait for systemd to finish starting before judging the system state.

    A freshly installed system answers the guest agent while it is still
    running its first-boot units, and `systemctl is-system-running` says
    "starting". That is not a degraded system and not a healthy one: it is the
    question asked too early. Measured on the first install this harness drove:
    the agent answered at 45s and systemd reached "running" well after.
    """
    deadline = time.monotonic() + seconds
    state = ""
    while time.monotonic() < deadline:
        state = machine.out("systemctl is-system-running 2>&1")
        if state in ("running", "degraded"):
            return state
        time.sleep(5)
    return state


def _installed_report(ctx: Context, machine: Guest, prefix: str) -> dict:
    settled = _settle_system(machine, float(ctx.options.get("settle_timeout", 300)))
    report = system_report(machine)
    report["settled_state"] = settled
    report["efi"] = machine.out("test -d /sys/firmware/efi && echo yes || echo no")
    report["account"] = machine.out(
        f"getent passwd {shlex.quote(INSTALL_ACCOUNT['username'])} || echo MISSING"
    )
    report["hostname"] = machine.out("cat /etc/hostname 2>/dev/null")
    report["home"] = machine.out(
        f"test -d /home/{shlex.quote(INSTALL_ACCOUNT['username'])} && echo yes || echo no"
    )
    report["root_source"] = machine.out("findmnt -no SOURCE /")
    ctx.evidence.write_json(f"{prefix}-installed-report.json", report)
    return report


def _check_installed_system(
    ctx: Context, machine: Guest, report: dict, firmware: str, label: str
) -> None:
    """Everything INSTALL-01 claims about a fresh install, checked on the disk."""
    ctx.check(
        f"{label}: the installed system boots from disk, not from the live medium",
        "boot=live" not in report["cmdline"] and bool(report["root_source"]),
        f"root {report['root_source']!r}; cmdline {report['cmdline'][:120]}",
    )
    ctx.check(
        f"{label}: the installed system reports the release version",
        report["version_marker"] == ctx.options["version"],
        f"version marker {report['version_marker']!r}",
    )
    ctx.check(
        f"{label}: the installed system booted through the "
        f"{'UEFI' if firmware == 'uefi' else 'BIOS'} firmware it was installed under",
        report["efi"] == ("yes" if firmware == "uefi" else "no"),
        f"/sys/firmware/efi present: {report['efi']}",
    )
    ctx.check(
        f"{label}: the account typed into the installer exists on the installed "
        "system",
        report["account"].startswith(INSTALL_ACCOUNT["username"] + ":")
        and INSTALL_ACCOUNT["fullname"] in report["account"]
        and report["home"] == "yes",
        report["account"][:200],
    )
    ctx.check(
        f"{label}: the host name typed into the installer is the installed "
        "system's host name",
        report["hostname"] == INSTALL_ACCOUNT["hostname"],
        f"/etc/hostname = {report['hostname']!r}",
    )
    ctx.check(
        f"{label}: the installed system's package database is consistent",
        report["dpkg_audit"] == "",
        report["dpkg_audit"][:300] or "dpkg --audit is clean",
    )
    ctx.check(
        f"{label}: the installed system reaches a running state",
        report["system_state"] in ("running", "degraded"),
        f"systemctl is-system-running = {report['system_state']!r}"
        + (f"; failed units: {report['failed_units']}" if report["failed_units"] else ""),
    )


def case_install(ctx: Context) -> None:
    """Install the artifact to a blank disk with Calamares, then boot it.

    Every step asserts which page it is acting on before acting, through the
    installer's own accessible controls. Anything unrecognised stops the run as
    BLOCKED with the observed page recorded -- never a guess, and never a pass.
    """
    iso = Path(ctx.artifact["path"])
    firmware = ctx.options.get("firmware", "bios")
    machine = ctx.guest("install", firmware=firmware)
    machine.create_disk(int(ctx.options.get("disk_gib", 40)))
    ctx.observe("installed_disk", str(machine.disk))
    machine.boot("live", iso=iso, note="live boot for installation")
    try:
        machine.wait_agent(float(ctx.options.get("boot_timeout", 900)))
        live = system_report(machine)
        ctx.evidence.write_json("install-live-report.json", live)
        ctx.check(
            "installation starts from the live artifact under test",
            "boot=live" in live["cmdline"]
            and live["version_marker"] == ctx.options["version"],
            f"version marker {live['version_marker']!r}",
        )
        settle = float(ctx.options.get("desktop_settle", 120))
        ctx.log(f"waiting {settle:.0f}s for the live desktop session")
        time.sleep(settle)

        session = _live_session(ctx, machine)
        ctx.observe("live_session", session)
        push_script(machine, ATSPI_PATH, ATSPI_DRIVER)
        bus = _installer_bus(ctx, machine)

        ctx.observe(
            "installer_launch_path",
            "started directly as root on a private session bus; the desktop's "
            "pkexec wrapper is not exercised and is not covered by this case",
        )
        launcher = ctx.options.get("installer_command", "/usr/bin/calamares")
        environment = (
            f"DBUS_SESSION_BUS_ADDRESS={shlex.quote(bus)} "
            f"XDG_RUNTIME_DIR={A11Y_RUNTIME} HOME=/root QT_ACCESSIBILITY=1 "
        )
        if session.get("wayland_display"):
            # An absolute socket path, because XDG_RUNTIME_DIR now points at the
            # installer's own runtime directory rather than the session user's.
            environment += (
                f"WAYLAND_DISPLAY=/run/user/{session['uid']}/"
                f"{session['wayland_display']} QT_QPA_PLATFORM=wayland "
            )
        elif session.get("display"):
            machine.run(
                f"/usr/sbin/runuser -u {shlex.quote(session['user'])} -- "
                f"/usr/bin/env DISPLAY={session['display']} "
                f"XDG_RUNTIME_DIR=/run/user/{session['uid']} "
                "/usr/bin/xhost +si:localuser:root",
                timeout=60,
            )
            environment += f"DISPLAY={session['display']} "
        machine.run(
            f"setsid /usr/bin/env {environment}{shlex.quote(launcher)} -d "
            ">/tmp/sf-installer.log 2>&1 & echo started",
            timeout=60,
        )

        appeared = None
        deadline = time.monotonic() + float(ctx.options.get("installer_settle", 180))
        while time.monotonic() < deadline:
            probe = _driver(ctx, machine, bus, "calamares", "page")
            if probe["exitcode"] == 0:
                appeared = json.loads(probe["stdout"])["showing"]
                break
            time.sleep(5)
        visible = _driver(ctx, machine, bus, "calamares", "apps")
        ctx.evidence.write_text(
            "install-atspi-applications.json",
            visible["stdout"] or visible["stderr"] or "(no output)",
        )
        ctx.snap(machine, "install-installer-launched.png")
        ctx.check(
            "the installer registers on the accessibility bus it is given",
            appeared is not None,
            "one 'calamares' application on the bus"
            if appeared is not None
            else "no 'calamares' application appeared; see "
            "install-atspi-applications.json and install-launcher.log",
        )
        if appeared is None:
            ctx.evidence.write_text(
                "install-launcher.log",
                f"$ {launcher}\n(guest /tmp/sf-installer.log)\n\n"
                + machine.out("cat /tmp/sf-installer.log 2>/dev/null")
                + "\n\nprocesses:\n"
                + machine.out("ps -eo user:16,pid,args | grep -i calamares "
                              "| grep -v grep"),
            )
            ctx.blocked(
                "the installer never appeared on the accessibility bus, so no "
                "page could be identified before acting on it. Its own log and "
                "the applications the bus could see are recorded as evidence."
            )

        version = ctx.options["version"]
        _await_page(
            ctx, machine, bus, "welcome",
            (("label", f"Welcome to the Calamares installer for Shadowfetch "
                       f"Linux {version}"),
             ("button", "Next")),
        )
        ctx.snap(machine, "install-01-welcome.png")

        _click(ctx, machine, bus, "button", "Next")
        _await_page(ctx, machine, bus, "location",
                    (("label", "Region:"), ("label", "Zone:")))
        _click(ctx, machine, bus, "button", "Next")
        _await_page(ctx, machine, bus, "keyboard", (("label", "Keyboard model:"),))
        ctx.snap(machine, "install-02-keyboard.png")

        _click(ctx, machine, bus, "button", "Next")
        partition = _await_page(
            ctx, machine, bus, "partition",
            (("label", "Select storage device:"), ("radio", "Erase disk")),
        )
        ctx.snap(machine, "install-03-partition.png")
        devices = [node["name"] for node in partition if node["role"] == "combo"]
        ctx.observe("partition_page_devices", devices)
        ctx.check(
            "the partition page offers the machine's disk as the install target",
            any("/dev/vda" in name for name in devices),
            f"storage devices offered: {devices}",
        )
        expected_firmware = "EFI" if firmware == "uefi" else "BIOS"
        firmware_labels = [
            _plain(node["name"]) for node in partition
            if node["role"] == "label"
            and _plain(node["name"]).upper() in ("BIOS", "EFI", "UEFI")
        ]
        ctx.check(
            "the partition page reports the firmware the machine booted under",
            any(label.upper().startswith(expected_firmware)
                for label in firmware_labels),
            f"firmware labels on the page: {firmware_labels}, expected "
            f"{expected_firmware}",
        )
        ctx.check(
            "the installer refuses to go on before a partitioning choice is made",
            "sensitive" not in _control(ctx, machine, bus, "button", "Next"),
            "the Next button is insensitive on arrival at the partition page",
        )
        _click(ctx, machine, bus, "radio", "Erase disk")
        time.sleep(3)
        ctx.check(
            "choosing to erase the disk lets the installer go on",
            "sensitive" in _control(ctx, machine, bus, "button", "Next"),
            "the Next button became sensitive after the choice",
        )

        _click(ctx, machine, bus, "button", "Next")
        _await_page(ctx, machine, bus, "users",
                    (("label", "What is your name?"),
                     ("label", "What is the name of this computer?")))
        ctx.check(
            "the installer refuses to go on before the account is filled in",
            "sensitive" not in _control(ctx, machine, bus, "button", "Next"),
            "the Next button is insensitive on arrival at the users page",
        )
        typed = []
        for field in USER_FIELDS:
            value = INSTALL_ACCOUNT[
                "password" if field[0] == "confirm" else field[0]
            ]
            typed.append(_type_into(ctx, machine, bus, field, value))
        ctx.evidence.write_json("install-users-typed.json", typed)
        ctx.snap(machine, "install-04-users.png")
        ctx.check(
            "the installer accepts the account typed into it",
            "sensitive" in _control(ctx, machine, bus, "button", "Next"),
            "the Next button became sensitive once the account was complete",
        )

        _click(ctx, machine, bus, "button", "Next")
        summary = _await_page(
            ctx, machine, bus, "summary",
            (("label", "This is an overview of what will happen"),
             ("button", "Install")),
        )
        ctx.snap(machine, "install-05-summary.png")
        summary_text = _labels(summary)
        ctx.observe("summary", summary_text[:600])
        ctx.check(
            "the summary describes erasing this machine's disk and installing "
            "this release",
            "/dev/vda" in summary_text and version in summary_text,
            summary_text[:300],
        )
        table = "GPT" if firmware == "uefi" else "MSDOS"
        ctx.check(
            "the summary lays out the partition table this firmware requires",
            table.casefold() in summary_text.casefold(),
            f"expected a {table} partition table for {firmware}; summary says: "
            + summary_text[:300],
        )

        _click(ctx, machine, bus, "button", "Install")
        finished = _run_installation(ctx, machine, bus)
        ctx.snap(machine, "install-06-finished.png")
        ctx.evidence.write_text(
            "install-launcher.log",
            f"$ {launcher}\n(guest /tmp/sf-installer.log)\n\n"
            + machine.out("cat /tmp/sf-installer.log 2>/dev/null | tail -400")
            + "\n\ncalamares session log:\n"
            + machine.out("tail -400 /root/.cache/calamares/session.log 2>/dev/null"),
        )
        if not finished:
            return
        machine.shutdown()
        ctx.collect_machine_evidence(machine, "install-live")

        machine.boot("installed", note="first boot of the installed system")
        machine.wait_agent(float(ctx.options.get("boot_timeout", 900)))
        report = _installed_report(ctx, machine, "install")
        _check_installed_system(ctx, machine, report, firmware, "first boot")
        time.sleep(30)
        ctx.snap(machine, "install-07-installed-system.png")
    finally:
        try:
            machine.shutdown()
        finally:
            ctx.collect_machine_evidence(machine, "install")


def _run_installation(ctx: Context, machine: Guest, bus: str) -> bool:
    """Watch the installation run, and say honestly how it ended.

    Calamares reports its own outcome on the page it ends on: a finished page
    that says the installation is complete, or one that says it failed. Both are
    read here; neither is inferred from the absence of the other.
    """
    timeout = float(ctx.options.get("install_timeout", 5400))
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    steps: list[str] = []
    shots = 0
    page: list[dict] = []
    while time.monotonic() < deadline:
        page = _page(ctx, machine, bus)
        labels = _labels(page)
        current = next(
            (_plain(node["name"]) for node in page
             if node["role"] == "label" and node["name"]),
            "",
        )
        if current and (not steps or steps[-1] != current):
            steps.append(current)
            ctx.log(f"installer: {current[:120]}")
            if shots < 4:
                ctx.snap(machine, f"install-progress-{shots}.png")
                shots += 1
        done = _visible(page, "button", "Done")
        failed = ("did not complete successfully" in labels
                  or "Installation Failed" in labels)
        if done or failed:
            elapsed = time.monotonic() - started
            ctx.observe("installation_seconds", round(elapsed, 1))
            ctx.observe("installer_steps", steps)
            ctx.evidence.write_json("install-page-finished.json", page)
            ctx.check(
                "the installation runs to completion and the installer says so",
                done and not failed
                and ("All done" in labels or "has been installed" in labels),
                _labels(page)[:300],
            )
            return bool(done and not failed)
        time.sleep(15)
    ctx.observe("installer_steps", steps)
    ctx.evidence.write_json("install-page-timeout.json", page)
    ctx.check(
        "the installation runs to completion and the installer says so",
        False,
        f"the installer was still on {steps[-1] if steps else 'no step'!r} after "
        f"{timeout:.0f}s",
    )
    return False


# --- INSTALL-01: both firmwares ------------------------------------------------


def case_install_both_firmwares(ctx: Context) -> None:
    """Boot the BIOS install and the UEFI install this artifact produced.

    INSTALL-01 is "Fresh BIOS and UEFI Calamares installs boot from disk": two
    firmwares. One install run proves one firmware, so recording INSTALL-01
    from a single run would claim the other. This case is what closes that gap
    honestly. It refuses to run unless the ledger holds a PASSING install of
    THIS artifact under each firmware, produced by THIS harness; then it boots
    the two disks those runs actually installed, one under each firmware, and
    checks the claim on both.

    It is not a paperwork case: nothing here reads a verdict and repeats it.
    The disks are booted again, in this process, and every check below is
    evaluated against a running machine.
    """
    ledger_path = ctx.work_root / "vm-acceptance" / "ledger.jsonl"
    ledger = Ledger(ledger_path)
    problems = ledger.verify()
    if problems:
        ctx.blocked(
            "the run ledger does not verify, so no run in it can be trusted to "
            "have happened: " + "; ".join(problems)
        )
    harness = harness_fingerprint(Path(__file__).resolve().parent)["digest"]
    legs = {}
    for firmware in ("bios", "uefi"):
        rows = ledger.find(
            case="install",
            verdict="PASS",
            firmware=firmware,
            artifact_sha256=ctx.artifact["sha256"],
        )
        if not rows:
            ctx.blocked(
                f"the ledger holds no PASSING {firmware} install of this "
                f"artifact ({ctx.artifact['sha256'][:12]}), so there is nothing "
                "to prove INSTALL-01's " + firmware + " half with. Run: "
                "vm_acceptance.py run --case install --firmware " + firmware
                + " --artifact " + ctx.artifact["name"]
            )
        legs[firmware] = rows[-1]

    disks = {}
    for firmware, row in legs.items():
        receipt_path = ctx.repo_root / row["receipt_path"]
        if not receipt_path.is_file():
            ctx.blocked(f"the {firmware} install's receipt is missing: {receipt_path}")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        faults = receipt_problems(receipt)
        if receipt.get("receipt_sha256") != row["receipt_sha256"]:
            faults.append("the receipt digest differs from its ledger entry")
        for item in receipt.get("evidence", []):
            path = ctx.repo_root / item["relative_path"]
            if not path.is_file():
                faults.append(f"missing evidence {item['name']}")
            elif sha256_file(path) != item["sha256"]:
                faults.append(f"evidence changed since the run: {item['name']}")
        if faults:
            ctx.blocked(
                f"the {firmware} install run {row['run_id']} does not verify, so "
                "it cannot support INSTALL-01: " + "; ".join(faults)
            )
        ctx.check(
            f"the {firmware} install was produced by this harness",
            receipt["harness"]["digest"] == harness,
            f"run {row['run_id']} harness {receipt['harness']['digest'][:16]}, "
            f"this harness {harness[:16]}",
        )
        disk = Path(receipt.get("observations", {}).get("installed_disk", ""))
        if not disk.is_file():
            ctx.blocked(
                f"the disk the {firmware} install wrote is gone: {disk}. The "
                "install run has to be repeated."
            )
        disks[firmware] = {"run_id": row["run_id"], "disk": disk,
                           "receipt": str(receipt_path.relative_to(ctx.repo_root))}
        ctx.observe(f"{firmware}_install_run", row["run_id"])
    ctx.evidence.write_json(
        "install-both-provenance.json",
        {firmware: {**detail, "disk": str(detail["disk"])}
         for firmware, detail in disks.items()},
    )

    for firmware in ("bios", "uefi"):
        detail = disks[firmware]
        machine = ctx.guest(f"boot-{firmware}", firmware=firmware)
        provenance = machine.clone_disk(detail["disk"])
        ctx.evidence.write_json(f"install-both-{firmware}-base.json", provenance)
        variables = detail["disk"].parent / "OVMF_VARS_4M.fd"
        if firmware == "uefi" and variables.is_file():
            # The firmware state the install itself produced. A fresh NVRAM
            # would be testing the removable-media fallback rather than the boot
            # entry the installer wrote.
            (machine.directory / "OVMF_VARS_4M.fd").write_bytes(
                variables.read_bytes()
            )
        machine.boot("installed", note=f"re-boot of the {firmware} install")
        try:
            machine.wait_agent(float(ctx.options.get("boot_timeout", 900)))
            report = _installed_report(ctx, machine, f"install-both-{firmware}")
            _check_installed_system(ctx, machine, report, firmware, firmware)
            ctx.snap(machine, f"install-both-{firmware}.png")
        finally:
            try:
                machine.shutdown()
            finally:
                ctx.collect_machine_evidence(machine, f"install-both-{firmware}")



# --- RECOVERY-01: the project diff/undo half ----------------------------------

CHECKPOINT = "/usr/bin/shadowfetch-checkpoint"
PROJECT_WORKSPACE = "vm-acceptance"

WORKSPACE_MANIFEST = r'''#!/bin/sh
# Byte-for-byte state of a workspace: every file with its digest, sorted.
cd "$1" 2>/dev/null || { echo "WORKSPACE MISSING"; exit 1; }
/usr/bin/find . -type f | LC_ALL=C sort | while IFS= read -r file; do
  printf '%s  %s\n' "$(/usr/bin/sha256sum "$file" | cut -d' ' -f1)" "$file"
done
'''

MANIFEST_PATH = "/tmp/sf-workspace-manifest.sh"

# What the injected failure does to the workspace, and what a diff must then
# say about it. An agent that ran wild edits, deletes and adds; a file it did
# not touch must not appear in the diff at all.
PROJECT_DAMAGE = {
    "edit.txt": "M",
    "remove.txt": "-",
    "spill.txt": "+",
    "sub/deep.txt": "-",
}
PROJECT_UNTOUCHED = "keep.txt"


def _user_run(
    machine: Guest, user: str, home: str, command: str, timeout: float = 300
) -> dict:
    """Run a command as the desktop user, with that user's HOME.

    runuser without -l keeps the caller's environment, so HOME would still be
    root's and the checkpoint engine would look for ~/Workspaces in the wrong
    home.
    """
    return machine.run(
        f"/usr/sbin/runuser -u {shlex.quote(user)} -- /usr/bin/env "
        f"HOME={shlex.quote(home)} LC_ALL=C.UTF-8 /bin/sh -c {shlex.quote(command)}",
        timeout=timeout,
    )


def _require_passing_run(ctx: Context, ledger: Ledger, harness: str, **criteria) -> dict:
    """The receipt of an earlier PASSING run of this artifact, re-verified.

    Verified rather than trusted: the ledger chain, the receipt's own digest and
    every evidence byte are checked here, so a case that leans on an earlier run
    leans on one that still stands.
    """
    rows = ledger.find(
        verdict="PASS", artifact_sha256=ctx.artifact["sha256"], **criteria
    )
    if not rows:
        ctx.blocked(
            f"the ledger holds no PASSING run matching {criteria} against this "
            f"artifact ({ctx.artifact['sha256'][:12]}), so the other half of "
            "this release case is unproven and this run cannot stand in for it."
        )
    row = rows[-1]
    receipt_path = ctx.repo_root / row["receipt_path"]
    if not receipt_path.is_file():
        ctx.blocked(f"the receipt of run {row['run_id']} is missing: {receipt_path}")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    faults = receipt_problems(receipt)
    if receipt.get("receipt_sha256") != row["receipt_sha256"]:
        faults.append("the receipt digest differs from its ledger entry")
    for item in receipt.get("evidence", []):
        path = ctx.repo_root / item["relative_path"]
        if not path.is_file():
            faults.append(f"missing evidence {item['name']}")
        elif sha256_file(path) != item["sha256"]:
            faults.append(f"evidence changed since the run: {item['name']}")
    if faults:
        ctx.blocked(
            f"run {row['run_id']} does not verify, so it cannot support this "
            "case: " + "; ".join(faults)
        )
    ctx.check(
        f"the {criteria.get('case')} run this case relies on was produced by "
        "this harness",
        receipt["harness"]["digest"] == harness,
        f"run {row['run_id']} harness {receipt['harness']['digest'][:16]}, this "
        f"harness {harness[:16]}",
    )
    ctx.observe(f"{criteria.get('case')}_run", row["run_id"])
    return receipt


def _diff_entries(output: str) -> dict[str, str]:
    """Parse `shadowfetch-checkpoint diff` into {path: marker}."""
    entries = {}
    for line in output.splitlines():
        stripped = line.strip()
        if len(stripped) > 2 and stripped[0] in "M+-" and stripped[1] == " ":
            entries[stripped[2:].strip()] = stripped[0]
    return entries


def case_recovery_project(ctx: Context) -> None:
    """RECOVERY-01's other half: project diff/undo after injected damage.

    The two Phoenix cases prove supported system rollback. RECOVERY-01 also says
    PROJECT DIFF/UNDO, which is Fireline's per-workspace checkpoint store: a
    different mechanism with a different blast radius, and nothing the rollback
    cases touch. This case proves that half against a running system, and
    refuses to run unless the ledger already holds the rollback half for the
    same artifact -- so the case that records RECOVERY-01 is the one that has
    seen both halves proven.
    """
    base = _recovery_base(ctx)
    ledger = Ledger(ctx.work_root / "vm-acceptance" / "ledger.jsonl")
    problems = ledger.verify()
    if problems:
        ctx.blocked(
            "the run ledger does not verify, so no run in it can be trusted to "
            "have happened: " + "; ".join(problems)
        )
    harness = harness_fingerprint(Path(__file__).resolve().parent)["digest"]
    for companion in ("recovery", "recovery-interrupted"):
        _require_passing_run(ctx, ledger, harness, case=companion)

    machine = ctx.guest(
        "recovery-project", firmware=ctx.options.get("firmware", "bios")
    )
    provenance = machine.clone_disk(base)
    ctx.observe("base_image", provenance["base_image"])
    ctx.evidence.write_json("project-base-provenance.json", provenance)
    machine.boot("installed", note="installed system for project diff/undo")
    try:
        machine.wait_agent(float(ctx.options.get("boot_timeout", 900)))
        report = system_report(machine)
        ctx.evidence.write_json("project-system-report.json", report)
        ctx.check(
            "the system under test is the release under test",
            report["version_marker"] == ctx.options["version"],
            f"version marker {report['version_marker']!r}",
        )
        user = machine.out("getent passwd 1000 | cut -d: -f1")
        home = machine.out("getent passwd 1000 | cut -d: -f6")
        if not user or not home:
            ctx.blocked("the base image has no uid 1000 desktop user to act as")
        ctx.observe("desktop_user", user)

        implementation = {
            "path": CHECKPOINT,
            "sha256": machine.out(f"sha256sum {CHECKPOINT} | cut -d' ' -f1"),
            "package_version": machine.out(
                "dpkg-query -W -f='${Version}' shadowfetch-fireline"
            ),
            "subcommands": machine.out(
                f"{CHECKPOINT} --help 2>&1 | sed -n 's/.*{{\\(.*\\)}}.*/\\1/p'"
            ),
            "engine_sha256": machine.out(
                "sha256sum /usr/lib/shadowfetch/mcp/sf_mcp.py | cut -d' ' -f1"
            ),
        }
        ctx.evidence.write_json("project-implementation.json", implementation)
        ctx.observe("checkpoint_sha256", implementation["sha256"])

        workspaces = f"{home}/Workspaces"
        owner = machine.out(
            f"stat -c %U {shlex.quote(workspaces)} 2>/dev/null || echo ABSENT"
        )
        # Recorded, not hidden: on the QA base image this directory ships owned
        # by root, and the checkpoint store lives inside it, so the desktop user
        # cannot take a checkpoint at all until the ownership is corrected. That
        # is a fact about the image, not about diff/undo, so the case fixes it,
        # says it did, and goes on to test the mechanism it is here to test.
        ctx.observe("workspaces_root_owner_before", owner)
        machine.run(
            f"mkdir -p {shlex.quote(workspaces)} && "
            f"chown {shlex.quote(user)}:{shlex.quote(user)} {shlex.quote(workspaces)}",
            check=True,
        )
        workspace = f"{workspaces}/{PROJECT_WORKSPACE}"
        push_script(machine, MANIFEST_PATH, WORKSPACE_MANIFEST)
        machine.run(
            f"rm -rf {shlex.quote(workspace)} && mkdir -p {shlex.quote(workspace)}/sub "
            f"&& chown -R {shlex.quote(user)}:{shlex.quote(user)} {shlex.quote(workspace)}",
            check=True,
        )
        _user_run(
            machine, user, home,
            f"cd {shlex.quote(workspace)} && printf keep > keep.txt && "
            "printf original > edit.txt && printf doomed > remove.txt && "
            "printf nested > sub/deep.txt",
        )
        # A canary outside the workspace: undo must not reach it.
        canary = f"{home}/vm-acceptance-canary.txt"
        _user_run(machine, user, home, f"printf canary > {shlex.quote(canary)}")
        before = _user_run(
            machine, user, home, f"/bin/sh {MANIFEST_PATH} {shlex.quote(workspace)}"
        )["stdout"].strip()
        ctx.evidence.write_text("project-workspace-before.txt", before + "\n")

        snapshot = _user_run(
            machine, user, home,
            f"{CHECKPOINT} snapshot {PROJECT_WORKSPACE} --label vm-acceptance",
            timeout=600,
        )
        listing = _user_run(
            machine, user, home, f"{CHECKPOINT} list {PROJECT_WORKSPACE}", timeout=300
        )
        point = ""
        for word in snapshot["stdout"].split():
            if len(word) == 22 and word[:8].isdigit() and word.count("-") == 2:
                point = word
        ctx.observe("checkpoint", point)
        ctx.evidence.write_text(
            "project-checkpoint.log",
            f"$ {CHECKPOINT} snapshot {PROJECT_WORKSPACE}\nexit="
            f"{snapshot['exitcode']}\n{snapshot['stdout']}{snapshot['stderr']}\n"
            f"$ {CHECKPOINT} list {PROJECT_WORKSPACE}\nexit={listing['exitcode']}\n"
            f"{listing['stdout']}{listing['stderr']}\n",
        )
        ctx.check(
            "the desktop user can take a project checkpoint of a workspace",
            snapshot["exitcode"] == 0 and bool(point)
            and point in listing["stdout"],
            f"snapshot exit {snapshot['exitcode']}, checkpoint {point!r}, listed: "
            f"{listing['stdout'].strip()[:200]}",
        )
        if not point:
            return

        # The injected failure: an agent that edited, deleted and added things.
        _user_run(
            machine, user, home,
            f"cd {shlex.quote(workspace)} && printf CORRUPTED > edit.txt && "
            "rm -f remove.txt && printf spill > spill.txt && rm -rf sub",
        )
        damaged = _user_run(
            machine, user, home, f"/bin/sh {MANIFEST_PATH} {shlex.quote(workspace)}"
        )["stdout"].strip()
        ctx.evidence.write_text("project-workspace-damaged.txt", damaged + "\n")
        ctx.check(
            "the injected failure really changed the workspace",
            damaged != before,
            "the workspace differs from its checkpoint before the undo",
        )

        diff = _user_run(
            machine, user, home,
            f"{CHECKPOINT} diff {PROJECT_WORKSPACE} {shlex.quote(point)}",
            timeout=300,
        )
        entries = _diff_entries(diff["stdout"])
        ctx.evidence.write_text(
            "project-diff.log",
            f"$ {CHECKPOINT} diff {PROJECT_WORKSPACE} {point}\n"
            f"exit={diff['exitcode']}\n{diff['stdout']}{diff['stderr']}\n",
        )
        ctx.check(
            "the diff names every change the injected failure made, and nothing "
            "else",
            diff["exitcode"] == 0 and entries == PROJECT_DAMAGE,
            f"diff reported {entries}, expected {PROJECT_DAMAGE}"
            + (f"; {PROJECT_UNTOUCHED} must not appear"
               if PROJECT_UNTOUCHED in entries else ""),
        )

        undo = _user_run(
            machine, user, home,
            f"{CHECKPOINT} undo {PROJECT_WORKSPACE} {shlex.quote(point)}",
            timeout=900,
        )
        ctx.evidence.write_text(
            "project-undo.log",
            f"$ {CHECKPOINT} undo {PROJECT_WORKSPACE} {point}\n"
            f"exit={undo['exitcode']}\n{undo['stdout']}{undo['stderr']}\n",
        )
        ctx.check(
            "the undo reports success",
            undo["exitcode"] == 0,
            f"exit {undo['exitcode']}: {(undo['stdout'] + undo['stderr']).strip()[:200]}",
        )
        after = _user_run(
            machine, user, home, f"/bin/sh {MANIFEST_PATH} {shlex.quote(workspace)}"
        )["stdout"].strip()
        ctx.evidence.write_text("project-workspace-after.txt", after + "\n")
        ctx.check(
            "the undone workspace is the checkpointed workspace, byte for byte",
            after == before,
            "every file and digest matches the pre-damage manifest"
            if after == before
            else f"after: {after[:200]!r} vs before: {before[:200]!r}",
        )
        confirm = _user_run(
            machine, user, home,
            f"{CHECKPOINT} diff {PROJECT_WORKSPACE} {shlex.quote(point)}",
            timeout=300,
        )
        ctx.check(
            "the tool itself reports no remaining difference after the undo",
            confirm["exitcode"] == 0 and not _diff_entries(confirm["stdout"]),
            confirm["stdout"].strip()[:200],
        )
        outside = machine.out(f"cat {shlex.quote(canary)} 2>/dev/null || echo GONE")
        ctx.check(
            "the undo touched nothing outside the workspace it names",
            outside == "canary",
            f"the file beside the workspace root reads {outside!r}",
        )
        ctx.snap(machine, "project-desktop.png")
    finally:
        try:
            machine.shutdown()
        finally:
            ctx.collect_machine_evidence(machine, "project")


# --- SHADOWCODE ---------------------------------------------------------------
#
# ShadowCode is the 5.0 flagship and the one package in the image nobody here
# built: upstream's signed .deb, preinstalled. The release gates prove the
# bytes are the signed ones and that they are installed; they cannot prove the
# app runs. These cases do, on the live session of the artifact under test:
#
#   shadowcode       pinned version installed, --version answers, the bundled
#                    llama.cpp runtime executes, and the app is launched under
#                    the live desktop, shows a window, and stays up for
#                    --shadowcode-minutes without a crash.
#   shadowcode-soak  the same checks, then open/close cycles for
#                    --soak-minutes, watching window appearance, clean exits,
#                    leftover processes, crashes, memory drift, idle CPU and
#                    whether the window ShadowCode restores grows per launch.
#                    SHADOWCODE-01 is recorded from this case, and only when a
#                    `shadowcode` run of the same artifact has also passed.
#
# How it is driven, and what that costs. The app is started in the live user's
# own systemd user manager (`systemd-run --user`), with the live session's
# Wayland/X11 display, as the desktop user -- the same place a launcher click
# puts it -- so the unit's cgroup accounts for every WebKit child process. It
# is closed with `systemctl --user stop` (SIGTERM, 20s before SIGKILL), NOT the
# window's close button; the receipt says so. A window is confirmed through
# KWin's own window list on the session bus (org.kde.KWin /WindowsRunner),
# never inferred from a screenshot; if that interface is unreachable the case
# is BLOCKED, not guessed.

SHADOWCODE_UNIT = "sf-acceptance-shadowcode"
# Process image names that belong to a ShadowCode run: the launcher, its
# bundled runtime, and the WebKitGTK helper processes the window spawns.
SHADOWCODE_FAMILY = ("shadowcode", "llama-server", "llama-cli", "webkit")


def _shadowcode_pin(ctx: Context) -> dict[str, Any]:
    """The pin the artifact is supposed to carry, read from the source tree.

    Loaded from tools/release/shadowcode.py -- the module the release gates
    use -- rather than restating the version here, which would be a second
    authority a bump could forget.
    """
    import importlib.util
    import re as _re

    path = ctx.repo_root / "tools/release/shadowcode.py"
    try:
        import sys as _sys

        spec = importlib.util.spec_from_file_location("sf_acceptance_shadowcode", path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        # Registered before execution: its dataclasses resolve their own module
        # through sys.modules while the class bodies run.
        _sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        pin = module.load_pin()
        manifest = json.loads(
            (pin.vendor_dir / "RELEASE-MANIFEST.json").read_text(encoding="utf-8")
        )
    except Exception as error:  # noqa: BLE001 - no pin means nothing to compare against
        ctx.blocked(f"the ShadowCode pin cannot be read from the source tree: {error}")
    runtime = _re.search(r"commit=([0-9a-f]{40})", manifest.get("runtime_pin", ""))
    facts = {
        "package": pin.package,
        "version": pin.version,
        "commit": pin.commit,
        "deb_sha256": pin.deb.sha256,
        "runtime_commit": runtime.group(1) if runtime else "",
        "launcher": "/" + module.LAUNCHER,
        "desktop_file": "/" + module.DESKTOP_FILE,
        "llama_server": "/" + module.LLAMA_SERVER,
        "llama_cli": "/" + module.LLAMA_CLI,
    }
    ctx.observe("shadowcode_pin", facts)
    return facts


def _await_session(ctx: Context, machine: Guest) -> dict[str, str]:
    """Wait for the live desktop, then identify it (observed, never assumed)."""
    deadline = time.monotonic() + float(ctx.options.get("desktop_settle", 90)) + 240
    while time.monotonic() < deadline:
        if machine.run("/usr/bin/pgrep -x plasmashell", timeout=60)["exitcode"] == 0:
            break
        time.sleep(5)
    # Plasma is up before its session bus services are; give KWin a moment.
    time.sleep(min(30.0, float(ctx.options.get("desktop_settle", 90))))
    session = _live_session(ctx, machine)
    session["home"] = machine.out(
        f"/usr/bin/getent passwd {shlex.quote(session['user'])} | /usr/bin/cut -d: -f6"
    )
    ctx.observe("live_session", session)
    return session


def _as_session(
    machine: Guest, session: dict[str, str], command: str, timeout: float = 120
) -> dict:
    """Run a command as the live desktop user, inside that user's session."""
    uid = session["uid"]
    environment = [
        f"HOME={session['home']}",
        f"XDG_RUNTIME_DIR=/run/user/{uid}",
        f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus",
        "LC_ALL=C.UTF-8",
    ]
    if session.get("wayland_display"):
        environment.append(f"WAYLAND_DISPLAY={session['wayland_display']}")
    if session.get("display"):
        environment.append(f"DISPLAY={session['display']}")
    return machine.run(
        f"/usr/sbin/runuser -u {shlex.quote(session['user'])} -- /usr/bin/env "
        + " ".join(shlex.quote(item) for item in environment)
        + f" /bin/sh -c {shlex.quote(command)}",
        timeout=timeout,
    )


def _shadowcode_windows(machine: Guest, session: dict[str, str]) -> dict[str, Any]:
    """KWin's own answer to "is there a ShadowCode window". Evidence, not proof."""
    result = _as_session(
        machine, session,
        "/usr/bin/dbus-send --session --print-reply --dest=org.kde.KWin "
        "/WindowsRunner org.kde.krunner1.Match string:ShadowCode",
        timeout=60,
    )
    texts = [
        line.strip()[len('string "'):-1]
        for line in result["stdout"].splitlines()
        if line.strip().startswith('string "')
    ]
    return {
        "reachable": result["exitcode"] == 0,
        "matches": [text for text in texts if "shadowcode" in text.lower()],
        "raw": (result["stdout"] + result["stderr"])[:4000],
    }


def _unit_state(machine: Guest, session: dict[str, str], unit: str) -> dict[str, str]:
    result = _as_session(
        machine, session,
        f"/usr/bin/systemctl --user show {shlex.quote(unit)} -p LoadState "
        "-p ActiveState -p SubState -p Result -p MainPID -p NRestarts "
        "-p MemoryCurrent -p MemoryPeak -p CPUUsageNSec -p ExecMainCode "
        "-p ExecMainStatus",
        timeout=60,
    )
    return dict(
        line.split("=", 1) for line in result["stdout"].splitlines() if "=" in line
    )


# Read together, in one pass over /proc/meminfo. MemAvailable is what the drift
# check judges; Shmem and AnonPages are there to say WHERE a change went. The
# 5.0.0 soak's 476 MiB step was Shmem -- apt lists written into the live
# session's RAM-backed overlay -- and a leak in ShadowCode's own processes
# would show as AnonPages instead. Without them a step is a number with no owner.
MEMINFO_FIELDS = ("MemAvailable", "Shmem", "AnonPages")


def _meminfo_kib(machine: Guest) -> dict[str, int]:
    """MEMINFO_FIELDS in KiB; -1 for a field the guest did not report."""
    text = machine.out(
        "/usr/bin/awk '/^(MemAvailable|Shmem|AnonPages):/ {print $1, $2}' /proc/meminfo"
    )
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, value = line.partition(" ")
        if value.strip().isdigit():
            values[key.rstrip(":")] = int(value.strip())
    return {field: values.get(field, -1) for field in MEMINFO_FIELDS}


def _sample(machine: Guest, session: dict[str, str], unit: str) -> dict[str, Any]:
    state = _unit_state(machine, session, unit)
    meminfo = _meminfo_kib(machine)
    return {
        "monotonic": round(time.monotonic(), 1),
        "active": state.get("ActiveState"),
        "sub": state.get("SubState"),
        "main_pid": state.get("MainPID"),
        "restarts": state.get("NRestarts"),
        "memory_current": state.get("MemoryCurrent"),
        "memory_peak": state.get("MemoryPeak"),
        "cpu_ns": state.get("CPUUsageNSec"),
        "mem_available_kib": meminfo["MemAvailable"],
        "shmem_kib": meminfo["Shmem"],
        "anon_pages_kib": meminfo["AnonPages"],
    }


def _number(value: Any) -> int | None:
    return int(value) if isinstance(value, str) and value.isdigit() else None


def _shadowcode_leftovers(machine: Guest, session: dict[str, str]) -> str:
    return machine.out(
        f"/usr/bin/pgrep -a -u {shlex.quote(session['user'])} "
        "-f '^/usr/bin/shadowcode|/usr/lib/shadowcode/' || true"
    )


def _crashes_since(ctx: Context, machine: Guest, since: str, label: str) -> list[str]:
    """ShadowCode-family crashes recorded since `since`, from coredumps and the kernel.

    systemd-coredump is what DrKonqi's pickup reads, so a crash DrKonqi would
    offer to report is a crash coredumpctl lists. The kernel's own segfault
    lines are read too, for the case where no core was written.
    """
    cores = machine.run(
        "if [ -x /usr/bin/coredumpctl ]; then /usr/bin/coredumpctl --no-pager "
        f"--no-legend list --since=@{since} 2>&1; else echo NO_COREDUMPCTL; fi",
        timeout=120,
    )["stdout"]
    kernel = machine.out(
        f"/usr/bin/journalctl -k --no-pager --since=@{since} 2>&1 "
        "| /usr/bin/grep -iE 'segfault|general protection|traps:' || true",
        timeout=120,
    )
    errors = machine.out(
        f"/usr/bin/journalctl --no-pager --since=@{since} -p err 2>&1 "
        "| /usr/bin/grep -iE 'shadowcode|webkit|llama' || true",
        timeout=120,
    )
    ctx.evidence.write_text(
        f"{label}-crash-scan.log",
        f"coredumps since @{since}:\n{cores.strip() or '(none)'}\n\n"
        f"kernel faults since @{since}:\n{kernel or '(none)'}\n\n"
        f"error-priority journal lines naming shadowcode/webkit/llama:\n{errors or '(none)'}\n",
    )
    ctx.observe(f"{label}_coredumpctl_available", "NO_COREDUMPCTL" not in cores)
    found = [
        line.strip()
        for line in (cores + "\n" + kernel).splitlines()
        if line.strip() and any(name in line.lower() for name in SHADOWCODE_FAMILY)
    ]
    return found


def _start_shadowcode(
    ctx: Context, machine: Guest, session: dict[str, str], unit: str
) -> dict:
    setenv = " ".join(
        f"--setenv={shlex.quote(f'{key}={session[field]}')}"
        for key, field in (("WAYLAND_DISPLAY", "wayland_display"), ("DISPLAY", "display"))
        if session.get(field)
    )
    return _as_session(
        machine, session,
        f"/usr/bin/systemctl --user reset-failed {unit} >/dev/null 2>&1; "
        f"/usr/bin/systemd-run --user --unit={unit} --property=TimeoutStopSec=20 "
        f"{setenv} /usr/bin/shadowcode",
        timeout=120,
    )


def _await_window(
    ctx: Context, machine: Guest, session: dict[str, str], unit: str
) -> tuple[float | None, dict[str, Any]]:
    limit = float(ctx.options.get("window_timeout", 120))
    started = time.monotonic()
    last: dict[str, Any] = {}
    while time.monotonic() - started < limit:
        last = _shadowcode_windows(machine, session)
        if last["matches"]:
            return round(time.monotonic() - started, 1), last
        if _unit_state(machine, session, unit).get("ActiveState") not in ("active", "activating"):
            break
        time.sleep(3)
    return None, last


def _stop_shadowcode(
    machine: Guest, session: dict[str, str], unit: str
) -> dict[str, Any]:
    started = time.monotonic()
    _as_session(machine, session, f"/usr/bin/systemctl --user stop {unit}", timeout=90)
    state = _unit_state(machine, session, unit)
    time.sleep(3)
    return {
        "seconds": round(time.monotonic() - started, 1),
        "active": state.get("ActiveState"),
        "result": state.get("Result"),
        "leftovers": _shadowcode_leftovers(machine, session),
    }


def _shadowcode_install_checks(
    ctx: Context, machine: Guest, session: dict[str, str], pin: dict[str, Any]
) -> None:
    import re as _re

    installed = machine.out(
        "/usr/bin/dpkg-query -W -f='${Version}\\t${db:Status-Abbrev}' "
        f"{shlex.quote(pin['package'])} 2>&1"
    )
    version, _, status = installed.partition("\t")
    ctx.check(
        "the pinned ShadowCode version is installed in the image",
        version == pin["version"] and status.startswith("ii"),
        f"dpkg reports {installed!r}, pin is {pin['version']}",
    )
    present = machine.out(
        f"/usr/bin/test -x {pin['launcher']} && /usr/bin/test -f {pin['desktop_file']} "
        f"&& /usr/bin/test -x {pin['llama_server']} && echo present || echo absent"
    )
    entry = machine.out(f"/usr/bin/cat {pin['desktop_file']} 2>&1")
    ctx.check(
        "the ShadowCode launcher, desktop entry and bundled runtime are installed",
        present == "present" and "\nExec=shadowcode" in "\n" + entry,
        f"{present}; desktop entry Exec: "
        + next((line for line in entry.splitlines() if line.startswith("Exec=")), "none"),
    )
    reported = _as_session(machine, session, f"{pin['launcher']} --version", timeout=90)
    text = (reported["stdout"] + reported["stderr"]).strip()
    ctx.check(
        "shadowcode --version answers with the pinned version",
        reported["exitcode"] == 0 and text == f"ShadowCode {pin['version']}",
        f"exit {reported['exitcode']}: {text[:200]!r}",
    )
    runtime_report = {}
    for label in ("llama_server", "llama_cli"):
        result = _as_session(machine, session, f"{pin[label]} --version", timeout=90)
        output = (result["stdout"] + result["stderr"]).strip()
        runtime_report[label] = {"exit": result["exitcode"], "output": output[-2000:]}
        commit = _re.search(r"commit ([0-9a-f]{7,40})", output)
        ctx.check(
            f"the bundled {pin[label]} executes and reports the pinned llama.cpp commit",
            result["exitcode"] == 0 and bool(commit)
            and bool(pin["runtime_commit"])
            and pin["runtime_commit"].startswith(commit.group(1)),
            f"exit {result['exitcode']}, reported "
            f"{commit.group(1) if commit else None}, pinned {pin['runtime_commit'][:12]}",
        )
    ctx.evidence.write_json("shadowcode-installed.json", {
        "pin": pin,
        "dpkg": installed,
        "desktop_entry": entry,
        "shadowcode_version": text,
        "runtime": runtime_report,
        "installed_sha256": machine.out(
            f"/usr/bin/sha256sum {pin['launcher']} {pin['llama_server']} 2>&1"
        ),
    })


def _boot_live_for_shadowcode(ctx: Context, name: str) -> Guest:
    iso = Path(ctx.artifact["path"])
    if not iso.is_file():
        ctx.blocked(f"the artifact under test is not a readable file: {iso}")
    machine = ctx.guest(name, firmware=ctx.options.get("firmware", "bios"))
    machine.create_disk(int(ctx.options.get("disk_gib", 32)))
    ctx.log(f"booting {iso.name} ({ctx.artifact['sha256'][:16]}...)")
    machine.boot("live", iso=iso, note="live session for ShadowCode acceptance")
    return machine


def _require_window_probe(ctx: Context, machine: Guest, session: dict[str, str]) -> None:
    probe = _shadowcode_windows(machine, session)
    ctx.evidence.write_text("shadowcode-window-probe.log", probe["raw"] or "(empty reply)")
    if not probe["reachable"]:
        ctx.blocked(
            "KWin's window list (org.kde.KWin /WindowsRunner) is not reachable on "
            "the live session bus, so whether a ShadowCode window appears cannot "
            f"be observed here: {probe['raw'][:300]!r}"
        )
    ctx.observe("shadowcode_windows_before_launch", probe["matches"])


def case_shadowcode(ctx: Context) -> None:
    """ShadowCode is installed at the pin, runs, opens a window and stays up."""
    pin = _shadowcode_pin(ctx)
    minutes = float(ctx.options.get("shadowcode_minutes", 5))
    machine = _boot_live_for_shadowcode(ctx, "shadowcode")
    try:
        ctx.observe("guest_agent_seconds",
                    round(machine.wait_agent(float(ctx.options.get("boot_timeout", 900))), 1))
        session = _await_session(ctx, machine)
        _shadowcode_install_checks(ctx, machine, session, pin)
        _require_window_probe(ctx, machine, session)

        since = machine.out("/usr/bin/date +%s")
        unit = SHADOWCODE_UNIT
        started = _start_shadowcode(ctx, machine, session, unit)
        ctx.evidence.write_text(
            "shadowcode-launch.log",
            f"$ systemd-run --user --unit={unit} /usr/bin/shadowcode\n"
            f"exit={started['exitcode']}\n{started['stdout']}{started['stderr']}",
        )
        ctx.check("ShadowCode starts in the live user's session",
                  started["exitcode"] == 0,
                  (started["stdout"] + started["stderr"]).strip()[:200])
        seconds, windows = _await_window(ctx, machine, session, unit)
        ctx.evidence.write_text("shadowcode-window.log", windows.get("raw") or "(empty reply)")
        ctx.check("a ShadowCode window appears on the live desktop",
                  seconds is not None,
                  f"KWin lists {windows.get('matches')} after {seconds}s"
                  if seconds is not None else "no ShadowCode window within the timeout")
        time.sleep(10)
        ctx.snap(machine, "shadowcode-window.png", required=True)

        samples = [_sample(machine, session, unit)]
        deadline = time.monotonic() + minutes * 60
        while time.monotonic() < deadline and samples[-1]["active"] == "active":
            time.sleep(20)
            samples.append(_sample(machine, session, unit))
        ctx.evidence.write_json("shadowcode-samples.json", samples)
        pids = {sample["main_pid"] for sample in samples}
        ctx.check(
            f"ShadowCode stays up for {minutes:g} minutes without restarting",
            all(sample["active"] == "active" for sample in samples)
            and len(pids) == 1 and len(samples) >= 2,
            f"{len(samples)} samples; states "
            f"{sorted({sample['active'] for sample in samples})}; main PIDs {sorted(pids)}",
        )
        ctx.observe("shadowcode_memory_current_last", samples[-1]["memory_current"])
        still_there = _shadowcode_windows(machine, session)
        ctx.check("the ShadowCode window is still there at the end",
                  bool(still_there["matches"]), str(still_there["matches"])[:200])
        ctx.snap(machine, "shadowcode-after-soak.png")

        stopped = _stop_shadowcode(machine, session, unit)
        ctx.observe("shadowcode_stop", stopped)
        ctx.check("ShadowCode exits cleanly when its session unit is stopped",
                  stopped["result"] in ("success", "") and stopped["active"] != "failed",
                  f"Result={stopped['result']!r} ActiveState={stopped['active']!r} "
                  f"in {stopped['seconds']}s")
        ctx.check("no ShadowCode process outlives it",
                  not stopped["leftovers"], stopped["leftovers"][:300])
        crashes = _crashes_since(ctx, machine, since, "shadowcode")
        ctx.check("no ShadowCode crash is recorded (coredumps, kernel faults)",
                  not crashes, "; ".join(crashes)[:400])
        ctx.evidence.write_text("shadowcode-journal.log", machine.out(
            f"/usr/bin/journalctl --no-pager --since=@{since} "
            f"_SYSTEMD_USER_UNIT={unit}.service 2>&1 | /usr/bin/tail -n 400",
            timeout=120,
        ) or "(no journal lines from the ShadowCode unit)")
    finally:
        try:
            machine.shutdown()
        finally:
            ctx.collect_machine_evidence(machine, "shadowcode")


SOAK_INHIBIT_UNIT = "sf-acceptance-soak-inhibit"
SOAK_INHIBIT_WHO = "shadowfetch-vm-acceptance"


def _hold_session_awake(
    ctx: Context, machine: Guest, session: dict[str, str]
) -> dict[str, Any]:
    """Keep the live session unlocked and its display on for the whole soak.

    The first 5.0.0 soak ran into the live session's screen locker: it engaged
    part-way through, the compositor stopped painting ShadowCode's window, and
    the per-cycle idle CPU and MemAvailable readings afterwards described a
    locked desktop rather than the app. Those numbers are not comparable, so
    the soak now turns the locker and display power management off for its
    duration and holds a logind idle/sleep inhibitor, and records that it did.

    Everything is read back. If the locker is still configured to lock, or the
    inhibitor is not held, the case is BLOCKED -- a soak that cannot keep the
    session awake cannot measure what it claims to.
    """
    kw = "/usr/bin/kwriteconfig6"
    power = " ".join(
        f"{kw} --file powerdevilrc --group {profile} --group Display "
        f"--key TurnOffDisplayWhenIdle false; "
        f"{kw} --file powerdevilrc --group {profile} --group Display "
        f"--key DimDisplayWhenIdle false; "
        f"{kw} --file powerdevilrc --group {profile} --group SuspendAndShutdown "
        f"--key AutoSuspendAction 0;"
        for profile in ("AC", "Battery", "LowBattery")
    )
    setup = _as_session(
        machine, session,
        f"{kw} --file kscreenlockerrc --group Daemon --key Autolock false; "
        f"{kw} --file kscreenlockerrc --group Daemon --key LockOnResume false; "
        f"{power} "
        "/usr/bin/dbus-send --session --type=method_call "
        "--dest=org.freedesktop.ScreenSaver /ScreenSaver "
        "org.kde.screensaver.configure; "
        "/usr/bin/dbus-send --session --type=method_call "
        "--dest=org.kde.Solid.PowerManagement /org/kde/Solid/PowerManagement "
        "org.kde.Solid.PowerManagement.reparseConfiguration; "
        # X11 only; a Wayland session has no xset and this is a no-op there.
        "{ test -n \"$DISPLAY\" && command -v xset >/dev/null && xset s off -dpms; } ; "
        f"/usr/bin/systemctl --user reset-failed {SOAK_INHIBIT_UNIT} >/dev/null 2>&1; "
        f"/usr/bin/systemd-run --user --unit={SOAK_INHIBIT_UNIT} "
        f"/usr/bin/systemd-inhibit --what=idle:sleep --mode=block "
        f"--who={SOAK_INHIBIT_WHO} "
        "--why='ShadowCode soak: CPU and memory must be measured on an unlocked desktop' "
        "/usr/bin/sleep infinity",
        timeout=120,
    )
    time.sleep(2)
    autolock = _as_session(
        machine, session,
        "/usr/bin/kreadconfig6 --file kscreenlockerrc --group Daemon --key Autolock",
    )["stdout"].strip()
    inhibitors = machine.run("/usr/bin/systemd-inhibit --list --no-pager 2>&1",
                             timeout=60)["stdout"]
    unit_state = _as_session(
        machine, session, f"/usr/bin/systemctl --user is-active {SOAK_INHIBIT_UNIT}"
    )["stdout"].strip()
    record = {
        "screen_locker_autolock": autolock,
        "inhibitor_unit": SOAK_INHIBIT_UNIT,
        "inhibitor_unit_state": unit_state,
        "inhibitor_held": SOAK_INHIBIT_WHO in inhibitors and unit_state == "active",
        "dpms": "powerdevilrc TurnOffDisplayWhenIdle/DimDisplayWhenIdle=false, "
                "AutoSuspendAction=0 (AC, Battery, LowBattery); xset -dpms on X11",
        "setup_exit": setup["exitcode"],
    }
    ctx.evidence.write_text(
        "shadowcode-soak-awake.log",
        f"setup exit={setup['exitcode']}\n{setup['stdout']}{setup['stderr']}\n"
        f"kscreenlockerrc [Daemon] Autolock = {autolock!r}\n"
        f"{SOAK_INHIBIT_UNIT}: {unit_state!r}\n\n$ systemd-inhibit --list\n{inhibitors}",
    )
    ctx.observe("soak_session_awake", record)
    if autolock != "false" or not record["inhibitor_held"]:
        ctx.blocked(
            "the live session could not be held awake for the soak (screen locker "
            f"Autolock read back as {autolock!r}; idle inhibitor held: "
            f"{record['inhibitor_held']}), so its CPU and memory readings would "
            "describe a locked desktop rather than ShadowCode"
        )
    return record


def _release_session_awake(machine: Guest, session: dict[str, str]) -> None:
    """Drop the inhibitor. Best effort: the live guest is discarded anyway."""
    try:
        _as_session(machine, session,
                    f"/usr/bin/systemctl --user stop {SOAK_INHIBIT_UNIT}", timeout=60)
    except GuestError:
        pass


def _screen_locked(machine: Guest, session: dict[str, str]) -> bool | None:
    """The screen locker's own answer, or None when it cannot be asked."""
    reply = _as_session(
        machine, session,
        "/usr/bin/dbus-send --session --print-reply=literal "
        "--dest=org.freedesktop.ScreenSaver /ScreenSaver "
        "org.freedesktop.ScreenSaver.GetActive",
        timeout=60,
    )
    words = reply["stdout"].split()
    if reply["exitcode"] != 0 or len(words) < 2 or words[-2] != "boolean":
        return None
    return words[-1] == "true"


# KDE's update notifier, as the systemd user unit the Plasma session runs it in.
# systemd-xdg-autostart-generator names the unit after the autostart entry
# (/etc/xdg/autostart/org.kde.discover.notifier.desktop -> app-<id>@autostart),
# and the 2d8a72e0 soak journal shows exactly this unit starting at 42.8s. The
# live-medium drop-in shadowfetch-defaults ships is for the same unit.
DISCOVER_NOTIFIER_UNIT = "app-org.kde.discover.notifier@autostart.service"
# packagekitd's own list of running transactions. --auto-start=no: asking must
# not start the daemon the soak is trying to see idle.
PACKAGEKIT_TRANSACTION_LIST = (
    "/usr/bin/busctl --system --auto-start=no call org.freedesktop.PackageKit "
    "/org/freedesktop/PackageKit org.freedesktop.PackageKit GetTransactionList"
)


def _packagekit_transactions(machine: Guest) -> dict[str, Any]:
    """How many transactions packagekitd is running, asked without starting it.

    `transactions` is 0 when packagekit.service is not running (no daemon, no
    transaction), the daemon's own count when it is, and None when the answer
    cannot be read: a daemon starting or stopping, or a reply that is not
    `ao N ...`. None is never taken for idle.
    """
    state = machine.out("/usr/bin/systemctl is-active packagekit.service 2>/dev/null || true")
    if state in ("inactive", "failed"):
        return {"packagekit": state, "transactions": 0, "reply": ""}
    if state != "active":
        return {"packagekit": state or "(no answer)", "transactions": None, "reply": ""}
    reply = machine.run(PACKAGEKIT_TRANSACTION_LIST + " 2>&1", timeout=60)
    text = reply["stdout"].strip()
    match = re.match(r"ao (\d+)\b", text)
    return {
        "packagekit": state,
        "transactions": int(match.group(1)) if reply["exitcode"] == 0 and match else None,
        "reply": text[:400],
    }


def _quiesce_update_notifier(
    ctx: Context, machine: Guest, session: dict[str, str]
) -> dict[str, Any]:
    """Stop KDE's update notifier and let PackageKit go idle before the baseline.

    The 5.0.0 soak of ISO 2d8a72e0 FAILED its memory check on work that was
    never ShadowCode's. DiscoverNotifier starts at login and arms a hard-coded
    300s timer; when it fires, PackageKit runs refresh-cache. The ISO ships
    without apt indexes (LB_APT_INDICES=false), so the refresh downloads all of
    them, and on the live medium they are written into the RAM-backed overlay:
    Shmem +~350 MB and MemAvailable -476 MiB in one step between cycles 3 and
    4, while the ShadowCode unit's own MemoryCurrent stayed flat. The timer was
    armed before ShadowCode first launched; nothing the app did caused it.

    Images built with shadowfetch-defaults' live-medium drop-in never start the
    notifier on the live medium. This does not rely on that, so an image
    without the drop-in is measured honestly too: the unit is stopped if it is
    running at all, then packagekitd must report no running transaction on two
    polls in a row -- bounded by --soak-quiesce-timeout -- so a refresh that
    already started finishes BEFORE the baseline instead of inside the cycles,
    and the idle daemon is then stopped so that its own exit does not land
    there either. What was found and done is recorded (`soak_quiesce`,
    shadowcode-soak-quiesce.log). A notifier that will not stop, a PackageKit
    that never goes idle, or one that will not stop once idle, is BLOCKED: the
    soak could not then say whose memory it measured.
    """
    timeout = float(ctx.options.get("soak_quiesce_timeout", 900))
    show = (
        f"/usr/bin/systemctl --user show {DISCOVER_NOTIFIER_UNIT} -p LoadState "
        "-p ActiveState -p SubState -p ConditionResult -p DropInPaths"
    )

    def notifier() -> dict[str, str]:
        result = _as_session(machine, session, show, timeout=60)
        return dict(
            line.split("=", 1) for line in result["stdout"].splitlines() if "=" in line
        )

    running = ("active", "activating", "reloading")
    before = notifier()
    stopped: dict[str, Any] | None = None
    if before.get("ActiveState") in running:
        result = _as_session(
            machine, session,
            f"/usr/bin/systemctl --user stop {DISCOVER_NOTIFIER_UNIT}", timeout=90,
        )
        stopped = {"exit": result["exitcode"],
                   "output": (result["stdout"] + result["stderr"]).strip()[:400]}
    after = notifier() if stopped is not None else before

    started = time.monotonic()
    polls: list[dict[str, Any]] = []
    idle = 0
    while True:
        poll = _packagekit_transactions(machine)
        poll["seconds"] = round(time.monotonic() - started, 1)
        polls.append(poll)
        idle = idle + 1 if poll["transactions"] == 0 else 0
        if idle >= 2 or time.monotonic() - started >= timeout:
            break
        time.sleep(5)
    # An idle packagekitd is stopped too. It stays up for its idle timeout
    # (~300s) after the last transaction -- in the 2d8a72e0 diagnostic rerun
    # the refresh finished at 367s and "daemon quit" came at 674s, taking its
    # 39 MB RSS with it -- so left running, its exit lands inside the cycles as
    # memory given BACK, which hides a leak of the same size from the slope.
    # It is D-Bus activated: anything that needs it later starts it again.
    pk_stop: dict[str, Any] | None = None
    if idle >= 2 and polls[-1]["packagekit"] == "active":
        result = machine.run("/usr/bin/systemctl stop packagekit.service 2>&1", timeout=120)
        pk_stop = {
            "exit": result["exitcode"],
            "output": (result["stdout"] + result["stderr"]).strip()[:400],
            "state_after": machine.out(
                "/usr/bin/systemctl is-active packagekit.service 2>/dev/null || true"
            ) or "(no answer)",
        }
    record = {
        "notifier_unit": DISCOVER_NOTIFIER_UNIT,
        "notifier_before": before,
        # None: it was not running, so there was nothing to stop.
        "notifier_stop": stopped,
        "notifier_after": after,
        "packagekit_idle": idle >= 2,
        "packagekit_waited_seconds": polls[-1]["seconds"],
        "packagekit_busy_polls": sum(1 for poll in polls if poll["transactions"] != 0),
        "packagekit_first": polls[0],
        "packagekit_last": polls[-1],
        # None: packagekitd was not running once idle, so nothing to stop.
        "packagekit_stop": pk_stop,
    }
    ctx.evidence.write_text(
        "shadowcode-soak-quiesce.log",
        f"{DISCOVER_NOTIFIER_UNIT} before: {before}\n"
        f"stop: {stopped if stopped is not None else '(not running; nothing to stop)'}\n"
        f"after: {after}\n\npackagekitd transactions (GetTransactionList, "
        f"auto-start off), timeout {timeout:g}s:\n"
        + "".join(f"  {poll}\n" for poll in polls)
        + f"\npackagekitd stop once idle: "
        f"{pk_stop if pk_stop is not None else '(not running; nothing to stop)'}\n",
    )
    ctx.observe("soak_quiesce", record)
    if after.get("ActiveState") in running:
        ctx.blocked(
            f"KDE's update notifier ({DISCOVER_NOTIFIER_UNIT}) is still "
            f"{after.get('ActiveState')!r} after being stopped; its PackageKit refresh "
            "would land in the soak and be charged to ShadowCode"
        )
    if not record["packagekit_idle"]:
        ctx.blocked(
            f"packagekitd did not go idle within {timeout:g}s (last: "
            f"{polls[-1]['packagekit']}, transactions {polls[-1]['transactions']}); "
            "its writes would land in the soak and be charged to ShadowCode"
        )
    if pk_stop is not None and pk_stop["state_after"] not in ("inactive", "failed"):
        ctx.blocked(
            f"packagekitd is still {pk_stop['state_after']!r} after being stopped once "
            "idle; its exit, or its next refresh, would land in the soak"
        )
    return record


# Timers the 5.0.0 image arms on the live medium, by name, so that a listing
# that misses one cannot leave it armed. Every other timer the listing finds
# active is stopped too. The one that mattered: apt-listchanges.timer is
# OnCalendar=hourly with no random delay, and its service (python3 -m
# apt_listchanges.populate_database, ~2 min and a 145 MB peak on the installed
# proof boot) runs at the first full hour of every boot until it disables
# itself. The 2d8a72e0 soak crossed 21:00 UTC: closes 14 (21:00:36) and 15
# (21:01:53) read -68 and -87 MiB against the fit of the other closes and
# recovered at close 16, the dips the first noise model took for after-close
# noise. fwupd-refresh is *:00:00 with up to 1h of random delay, and
# systemd-tmpfiles-clean fires 15 min after boot, inside every soak.
SOAK_SYSTEM_TIMERS = (
    "apt-listchanges.timer", "fwupd-refresh.timer", "apt-daily.timer",
    "apt-daily-upgrade.timer", "man-db.timer", "dpkg-db-backup.timer",
    "logrotate.timer", "flatpak-system-update.timer", "systemd-tmpfiles-clean.timer",
    "snapper-timeline.timer", "snapper-cleanup.timer", "e2scrub_all.timer",
    "fstrim.timer",
)
# The live user's own: tmpfiles cleanup 5 min after the user manager starts,
# and DrKonqi's hourly crash-report submitter.
SOAK_USER_TIMERS = ("systemd-tmpfiles-clean.timer", "drkonqi-sentry-postman.timer")
UNIT_DOWN = ("inactive", "failed")


def _unit_properties(run: Callable[[str], dict], ctl: str,
                     units: list[str]) -> dict[str, dict[str, str]]:
    """`systemctl show` for several units at once, keyed by unit Id."""
    if not units:
        return {}
    result = run(f"{ctl} show -p Id -p LoadState -p ActiveState -p SubState -p Triggers "
                 + " ".join(shlex.quote(unit) for unit in units) + " 2>/dev/null")
    found: dict[str, dict[str, str]] = {}
    for block in result["stdout"].split("\n\n"):
        fields = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if fields.get("Id"):
            found[fields["Id"]] = fields
    return found


def _stop_timers(run: Callable[[str], dict], ctl: str,
                 named: tuple[str, ...]) -> dict[str, Any]:
    """Stop every active timer of one manager, and any job one is running.

    Stopping a timer does not stop the service it already started, so a
    triggered service still running afterwards is stopped too. A unit missing
    from `systemctl show` counts as still armed: it was not seen stopped.
    """
    listing = run(f"{ctl} list-units --type=timer --state=active --no-legend "
                  "--plain --no-pager 2>&1")
    listed: list[str] | None = None
    if listing["exitcode"] == 0:
        listed = sorted({
            line.split()[0] for line in listing["stdout"].splitlines()
            if line.split() and line.split()[0].endswith(".timer")
        })
    candidates = sorted(set(listed or ()) | set(named))
    before = _unit_properties(run, ctl, candidates)
    armed = [unit for unit in candidates
             if before.get(unit, {}).get("ActiveState") not in UNIT_DOWN]
    stop = None
    if armed:
        result = run(f"{ctl} stop " + " ".join(shlex.quote(unit) for unit in armed) + " 2>&1")
        stop = {"exit": result["exitcode"],
                "output": (result["stdout"] + result["stderr"]).strip()[:400]}
    triggered = sorted({
        service for unit in armed
        for service in before.get(unit, {}).get("Triggers", "").split()
    })
    after = _unit_properties(run, ctl, candidates + triggered)
    running = [service for service in triggered
               if after.get(service, {}).get("ActiveState") not in UNIT_DOWN]
    jobs_stop = None
    if running:
        result = run(f"{ctl} stop " + " ".join(shlex.quote(s) for s in running) + " 2>&1")
        jobs_stop = {"jobs": running, "exit": result["exitcode"],
                     "output": (result["stdout"] + result["stderr"]).strip()[:400]}
        after = _unit_properties(run, ctl, candidates + triggered)
    return {
        "listing_ok": listed is not None,
        "listing": listing["stdout"].strip()[:2000],
        "listed_active": listed,
        "stopped": armed,
        "stop": stop,
        "jobs_stopped": jobs_stop,
        "still_armed": [unit for unit in candidates
                        if after.get(unit, {}).get("ActiveState") not in UNIT_DOWN],
        "still_running": [service for service in triggered
                          if after.get(service, {}).get("ActiveState") not in UNIT_DOWN],
        "after": {unit: after.get(unit, {}).get("ActiveState") for unit in candidates + triggered},
    }


def _quiesce_timers(
    ctx: Context, machine: Guest, session: dict[str, str]
) -> dict[str, Any]:
    """Stop the system's and the live user's timers before the baseline.

    A soak is 30 minutes of after-close readings; a timer that fires inside it
    puts a system job's memory into one or two of them. The hourly
    apt-listchanges run did exactly that to the 2d8a72e0 soak (closes 14 and
    15; SOAK_SYSTEM_TIMERS), and where such a pair lands decides what a
    least-squares slope makes of it: near the start of an 18-close soak it
    hid a 10 MiB/cycle leak in about half of simulated soaks. The guest is
    throwaway, so every active timer is stopped, not just the ones named, and
    a job one is already running is stopped with it. Recorded in
    `soak_quiesce` (`timers`) and shadowcode-soak-timers.log. A listing that
    fails, a timer still armed or a job still running is BLOCKED.
    """
    def as_user(command: str) -> dict:
        return _as_session(machine, session, command, timeout=120)

    def as_root(command: str) -> dict:
        return machine.run(command, timeout=120)

    record = {
        "system": _stop_timers(as_root, "/usr/bin/systemctl", SOAK_SYSTEM_TIMERS),
        "user": _stop_timers(as_user, "/usr/bin/systemctl --user", SOAK_USER_TIMERS),
    }
    ctx.evidence.write_text("shadowcode-soak-timers.log", "".join(
        f"== {scope} manager\n$ systemctl list-units --type=timer --state=active\n"
        f"{part['listing'] or '(none)'}\nstopped: {part['stopped']}\nstop: {part['stop']}\n"
        f"jobs a timer was running, stopped: {part['jobs_stopped']}\n"
        f"after: {part['after']}\n\n"
        for scope, part in record.items()
    ))
    problems = []
    for scope, part in record.items():
        if not part["listing_ok"]:
            problems.append(f"the {scope} manager's active timers could not be listed")
        if part["still_armed"]:
            problems.append(f"{scope} timers still armed after being stopped: "
                            + ", ".join(part["still_armed"]))
        if part["still_running"]:
            problems.append(f"{scope} jobs a timer started still running: "
                            + ", ".join(part["still_running"]))
    record["quiet"] = not problems
    if problems:
        ctx.observe("soak_quiesce_timers", record)
        ctx.blocked(
            "; ".join(problems) + " -- a timer that fires inside the soak puts a "
            "system job's memory into the after-close readings, charged to ShadowCode"
        )
    return record


def _units_started(
    machine: Guest, session: dict[str, str], since: str,
    closes_epoch: list[float],
) -> tuple[list[dict[str, Any]], str]:
    """Units the system and the live user's manager started since the baseline.

    From PID 1's (and user@UID's) own "Starting"/"Started" journal lines, each
    tied to the first close whose after-close reading could include it, so a
    dip in the evidence can be matched to a job without guessing.
    `closes_epoch` is the guest time of each close's reading.
    """
    queries = {
        "system": "_PID=1",
        "user": f"_SYSTEMD_UNIT=user@{session['uid']}.service _COMM=systemd",
    }
    raw = []
    found: list[dict[str, Any]] = []
    for manager, match in queries.items():
        text = machine.out(
            f"/usr/bin/journalctl --no-pager -q -o short-unix --since=@{since} {match} "
            "2>&1 | /usr/bin/grep -E ': Start(ing|ed) ' || true",
            timeout=120,
        )
        raw.append(f"== {manager} manager since @{since}\n{text or '(none)'}\n")
        for line in text.splitlines():
            parsed = re.match(r"(\d+(?:\.\d+)?) \S+ [^:]+: Start(?:ing|ed) (\S+)", line)
            if not parsed:
                continue
            epoch, unit = float(parsed.group(1)), parsed.group(2).rstrip(".")
            close = next((index for index, at in enumerate(closes_epoch, start=1)
                          if at >= epoch), None)
            found.append({"manager": manager, "unit": unit, "epoch": epoch,
                          "before_close": close})
    return found, "\n".join(raw)


# How many closes at each end the drift medians are taken over. The first 5.0.0
# check compared ONE reading (the first close) with ONE other (the lowest
# later close), so a single warm-up close or a single late dip decided the
# verdict -- the 2d8a72e0 run's 680 MiB "drift" included the hourly
# apt-listchanges run (SOAK_SYSTEM_TIMERS) at closes 14 and 15, recovered at
# close 16. The median of three ignores any one bad reading at either end and
# still leaves the ends 18+ cycles apart in a default 24-cycle soak.
SOAK_DRIFT_WINDOW = 3
# The first close the per-cycle slope is fitted from. Closes 1 and 2 sit above
# the rest in every 5.0.0 soak -- 2d8a72e0 +87 and +63 MiB over close 3,
# dfea3c9b +89 and +57, the 2d8a72e0 diagnostic rerun +33 and +30 -- while
# the session settles around the first windows (in the rerun, AnonPages +42
# MiB from close 1 to 3, plasmashell and KWin RSS +44 MB, Shmem flat). That is
# a one-off, which the end-to-end drop still counts; fitted into the slope it
# read as a per-launch loss, -3.7 instead of -2.0 MiB a cycle on the 2d8a72e0
# closes, and decided short soaks on its own.
SOAK_SLOPE_FROM_CLOSE = 3
# The fewest closes the per-cycle SLOPE is judged on; every other check is
# made at any length. On a quiet system an after-close reading is noisy by
# about +-13 MiB (2d8a72e0 closes 4-24 without 14 and 15, which were the
# hourly apt-listchanges run, not noise: residual sd 13.2 MiB, -27..+24), and
# a slope's variance falls with the cube of the number of closes. Simulated
# on that noise with first-launch warm-up and a background drift of 0 to -2.5
# MiB a cycle (mc4, 3000 soaks each), a healthy app fails 0% of soaks from 10
# closes on, and a 10 MiB/cycle leak is caught in 98% at 12 closes, 99.5% at
# 14 and 100% at 18. The floor is kept at 18 for noise this one soak did not
# show: with a 90-120 MiB dip at 10% of closes, sd 45 MiB, or +-90 MiB at
# every close, 14 closes fail a healthy app in 2.0, 3.3 and 6.5% of soaks; 18
# in 0.1, 0.4 and 1.2%, while still catching 15 MiB/cycle in 99.8-100%. A
# system job still landing in the soak is what quiescing is for: placed at
# random in an 18-close soak, the -68/-87 pair let a 10 MiB/cycle leak pass
# 8% of the time (0% without it). A default 30-minute soak runs 24 closes,
# and a slower one runs on past --soak-minutes until it has 18.
SOAK_MIN_CLOSES = 18


def _after_close_drift(
    values: list[int | None], window: int = SOAK_DRIFT_WINDOW,
    slope_from: int = SOAK_SLOPE_FROM_CLOSE,
) -> dict[str, Any] | None:
    """How a per-close memory reading moved across the soak, measured two ways.

    `drop_mib` is the median of the first `window` readings minus the median of
    the last `window` (positive: less at the end). `slope_mib_per_cycle` is the
    least-squares slope of the readings from close `slope_from` on against
    their cycle number (negative: shrinking), with its standard error. The
    medians say how far the reading moved end to end without letting one
    reading at either end decide it. The slope says whether it moved steadily,
    which is what a per-launch leak looks like and what a single dip does not:
    a leak too small for the end-to-end limit over one soak still shows as a
    slope. A step part-way through moves it too -- one of ~120 MiB in the
    middle of a 24-close soak reads as -8 MiB a cycle -- which is why system
    jobs are quiesced before the baseline and Shmem and AnonPages are recorded
    beside it.

    With fewer than 2*window readings the window shrinks to half of them, so
    the two ends never share a reading. Readings that are None or not positive
    (the guest did not answer) are left out and the rest keep their cycle
    numbers, so a gap does not bend the slope. None with fewer than two
    readings; the slope (and its error) None with fewer than two (three)
    from `slope_from` on.
    """
    points = [
        (cycle, value) for cycle, value in enumerate(values, start=1)
        if isinstance(value, int) and value > 0
    ]
    if len(points) < 2:
        return None
    ends = max(1, min(window, len(points) // 2))
    head = statistics.median(value for _, value in points[:ends])
    tail = statistics.median(value for _, value in points[-ends:])
    fitted = [(cycle, value) for cycle, value in points if cycle >= slope_from]
    slope = stderr = None
    if len(fitted) >= 2:
        mean_x = statistics.fmean(cycle for cycle, _ in fitted)
        mean_y = statistics.fmean(value for _, value in fitted)
        sxx = sum((cycle - mean_x) ** 2 for cycle, _ in fitted)
        slope = sum((cycle - mean_x) * (value - mean_y) for cycle, value in fitted) / sxx
        if len(fitted) >= 3:
            residual = sum((value - mean_y - slope * (cycle - mean_x)) ** 2
                           for cycle, value in fitted)
            stderr = (residual / (len(fitted) - 2) / sxx) ** 0.5
    return {
        "readings": len(points),
        "window": ends,
        "head_cycles": [cycle for cycle, _ in points[:ends]],
        "tail_cycles": [cycle for cycle, _ in points[-ends:]],
        "head_median_kib": head,
        "tail_median_kib": tail,
        # + 0.0: no "-0" in a detail line for a reading that did not move.
        "drop_mib": round((head - tail) / 1024, 1) + 0.0,
        "slope_cycles": [fitted[0][0], fitted[-1][0]] if fitted else [],
        "slope_readings": len(fitted),
        "slope_mib_per_cycle": None if slope is None else round(slope / 1024, 2) + 0.0,
        "slope_stderr_mib_per_cycle": None if stderr is None else round(stderr / 1024, 2),
    }


# tauri-plugin-window-state's file, in ShadowCode's app config directory (the
# identifier in its tauri.conf.json). The plugin writes it at every exit of a
# run without --profile, which is how the soak launches it.
SHADOWCODE_WINDOW_STATE = ".config/com.shadowfetch.shadowcode/.window-state.json"
# Consecutive launches at which the window grew that fail the soak.
SOAK_WINDOW_GROWTH_RUN = 2


def _positive_size(width: Any, height: Any) -> list[int] | None:
    """[width, height] when both are numbers above zero; None otherwise."""
    if isinstance(width, bool) or isinstance(height, bool):
        return None
    if not isinstance(width, (int, float)) or not isinstance(height, (int, float)):
        return None
    if width <= 0 or height <= 0:
        return None
    return [round(width), round(height)]


def _parse_window_state(text: str) -> dict[str, list[int]] | None:
    """{window label: [width, height]} from the plugin's JSON.

    None when the file is unreadable or saves no size. A width or height of 0
    or less is no size: ShadowCode 1.0.1 leaves SIZE out of the plugin's
    flags, and the plugin then keeps a fresh entry at 0x0 forever. Read as a
    size, that compared equal at every close and "passed" a growth check that
    had measured nothing.
    """
    try:
        document = json.loads(text)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    sizes = {}
    for label, state in document.items():
        if isinstance(state, dict) and isinstance(state.get("width"), int) \
                and isinstance(state.get("height"), int):
            size = _positive_size(state["width"], state["height"])
            if size is not None:
                sizes[str(label)] = size
    return sizes or None


def _window_state(
    machine: Guest, session: dict[str, str]
) -> tuple[dict[str, list[int]] | None, str]:
    """The saved window sizes after a close, and the raw text when it held none."""
    path = f"{session['home']}/{SHADOWCODE_WINDOW_STATE}"
    text = machine.out(f"/usr/bin/cat {shlex.quote(path)} 2>/dev/null || true")
    if not text:
        return None, ""
    sizes = _parse_window_state(text)
    return sizes, "" if sizes is not None else text[:400]


def _window_ids(match_reply: str) -> list[str]:
    """KWin window UUIDs from a WindowsRunner Match reply, ShadowCode's only.

    Each match is a struct whose first two strings are its id ("0_{uuid}",
    the action and KWin's internal window id) and its text (the caption).
    """
    ids = []
    for chunk in match_reply.split("struct {")[1:]:
        strings = re.findall(r'^\s*string "(.*)"\s*$', chunk, re.M)
        if len(strings) < 2 or "shadowcode" not in strings[1].lower():
            continue
        uuid = re.search(r"\{[0-9A-Fa-f-]{36}\}", strings[0])
        if uuid and uuid.group(0) not in ids:
            ids.append(uuid.group(0))
    return ids


def _dbus_scalars(reply: str) -> dict[str, Any]:
    """The string and number entries of an a{sv} dict as dbus-send prints it."""
    values: dict[str, Any] = {}
    for key, kind, value in re.findall(
        r'string "([^"]+)"\s*\n\s*variant\s+(string|double|u?int(?:16|32|64)|boolean)'
        r'\s+("[^"\n]*"|\S+)', reply,
    ):
        if kind == "string":
            values[key] = value.strip('"')
        elif kind == "boolean":
            values[key] = value == "true"
        else:
            try:
                values[key] = float(value)
            except ValueError:
                pass
    return values


def _shadowcode_window_frame(
    machine: Guest, session: dict[str, str]
) -> tuple[dict[str, list[int]] | None, str]:
    """The size of ShadowCode's window as KWin has it on screen, and why not.

    The frame geometry KWin reports for the window (org.kde.KWin /KWin
    getWindowInfo, by the id WindowsRunner gives), so the size the user sees
    is measured whatever the app saves or does not save. With more than one
    ShadowCode window the largest is the main one. ({"frame": [w, h]}, "") or
    (None, what KWin answered).
    """
    found = _shadowcode_windows(machine, session)
    ids = _window_ids(found["raw"])
    if not ids:
        return None, "no ShadowCode window id in KWin's WindowsRunner reply: " + found["raw"][:300]
    frames = []
    replies = []
    for window in ids:
        reply = _as_session(
            machine, session,
            "/usr/bin/dbus-send --session --print-reply --dest=org.kde.KWin /KWin "
            f"org.kde.KWin.getWindowInfo string:{shlex.quote(window)}",
            timeout=60,
        )
        info = _dbus_scalars(reply["stdout"])
        named = " ".join(str(info.get(key, "")) for key in
                         ("resourceClass", "desktopFile", "caption"))
        size = _positive_size(info.get("width"), info.get("height"))
        if size is not None and "shadowcode" in re.sub(r"[^a-z]", "", named.lower()):
            frames.append(size)
        replies.append(f"{window}: exit {reply['exitcode']} "
                       f"{(reply['stdout'] + reply['stderr']).strip()[:300]}")
    if not frames:
        return None, "; ".join(replies)[:600]
    return {"frame": max(frames, key=lambda size: size[0] * size[1])}, ""


def _window_growth(per_close: list[dict[str, list[int]] | None]) -> dict[str, Any]:
    """The longest run of consecutive launches at which a window got larger.

    ShadowCode 1.0.0 opens a larger window at every launch on Plasma Wayland.
    tauri-plugin-window-state saves tao's inner size at exit, which there is
    GTK's configure size -- client-side decorations and the header bar
    included -- and restores it as the inner size, so GTK adds them again:
    about +52 px wide and +99 px tall per launch, past the bottom of a 1080-px
    screen by the third. It is also what drove the 2d8a72e0 soak's idle CPU
    from 1.6% to 6.0%: the main process repaints an ever larger window. On an
    installed system the file is on disk, so it keeps growing across reboots.

    Used on two readings per launch: the size the plugin saved at the close,
    and the frame KWin showed during the hold. A launch is compared with the
    previous one only when both have a reading; a missing one, or a size of 0
    or less, breaks the run rather than bridging it. A window is larger when
    its width OR height went up. One increase can be a first save settling;
    increases at consecutive launches are a size being fed back into itself,
    and 1.0.0 grows at every one.
    """
    longest = run = compared = 0
    grew_at: list[int] = []
    previous: dict[str, list[int]] | None = None
    for cycle, sizes in enumerate(per_close, start=1):
        sizes = {label: size for label, size in (sizes or {}).items()
                 if _positive_size(*size) is not None} or None
        if sizes is None:
            previous, run = None, 0
            continue
        if previous is not None:
            common = [label for label in sizes if label in previous]
            if common:
                compared += 1
            if common and any(sizes[label][0] > previous[label][0]
                              or sizes[label][1] > previous[label][1] for label in common):
                run += 1
                grew_at.append(cycle)
                longest = max(longest, run)
            else:
                run = 0
        previous = sizes
    return {
        "compared": compared,
        "longest_run": longest,
        "grew_at_closes": grew_at,
        "sizes": [
            ",".join(f"{label}={w}x{h}" for label, (w, h) in sorted(sizes.items()))
            if sizes else None
            for sizes in per_close
        ],
    }


def _change_mib(drift: dict[str, Any] | None) -> str:
    return "n/a" if drift is None else f"{-drift['drop_mib'] + 0.0:+g} MiB"


def case_shadowcode_soak(ctx: Context) -> None:
    """Open and close ShadowCode repeatedly; watch crashes, leaks, CPU, exits, window size.

    Memory is judged on MemAvailable after each close, and the first 5.0.0
    soak (ISO 2d8a72e0) showed two ways that reading can blame the app for
    what the system did:

    * The system's own work landed inside the cycles. KDE's update notifier
      has PackageKit refresh the apt indexes 300s after login, and on the live
      medium ~350 MB of them go into RAM-backed Shmem, in one step, mid-soak;
      and the hourly apt-listchanges timer ran at 21:00 UTC, taking 68 and
      87 MiB out of closes 14 and 15. The notifier is stopped, PackageKit let
      finish and its idle daemon stopped (_quiesce_update_notifier), then
      every system and user timer is stopped (_quiesce_timers), all before
      the baseline. The units the system did start during the soak are
      recorded against the close they precede (`soak_units_started`).
    * It compared the first close with the single lowest later close, so one
      step or one dip anywhere became the "leak" (680 MiB, reported as 28 MiB a
      cycle, when the after-step slope was -1.5 to -1.8 MiB a cycle). Drift is
      now the median of the first three closes against the median of the last
      three, AND a least-squares slope from close 3 on (_after_close_drift):
      the first bounds how far memory moved over the soak, first-launch
      warm-up included, the second catches a steady per-launch leak too small
      for the first. --soak-slope-mib is 8: the 2d8a72e0 closes after its step
      slope at -1.7 to -2.0 MiB a cycle. A leak of 8 MiB a launch moves the end
      medians only ~170 MiB in 24 cycles, under the 256 MiB end-to-end limit
      -- which is why the slope is its own check. Shmem and AnonPages are
      sampled beside MemAvailable so a change can be attributed to tmpfs or to
      process memory from the evidence alone.

    The slope is judged only on SOAK_MIN_CLOSES (18) closes or more. The soak
    runs for --soak-minutes and then on until it has that many, bounded by
    --soak-max-minutes. Every other check is made whatever the count: a
    growing window, a SIGKILLed close or a drop past the end-to-end limit
    FAILS a short soak as it would a long one. Only a soak in which nothing
    failed, and which still ended short of the floor, is BLOCKED.

    The window is measured twice a launch (_window_growth): the frame KWin
    shows during the hold, and the size ShadowCode saves at the close.
    ShadowCode 1.0.0 restores a larger window at every launch; growth at two
    consecutive launches fails. 1.0.1 saves no size (the plugin keeps 0x0),
    which is no reading, so there the KWin frame is the measurement. A soak in
    which nothing failed and neither was measured is BLOCKED.
    """
    pin = _shadowcode_pin(ctx)
    soak_minutes = float(ctx.options.get("soak_minutes", 30))
    max_minutes = max(soak_minutes, float(ctx.options.get("soak_max_minutes", 60)))
    hold = float(ctx.options.get("soak_hold", 60))
    drift_mib = float(ctx.options.get("soak_drift_mib", 256))
    slope_mib = float(ctx.options.get("soak_slope_mib", 8))
    cpu_limit = float(ctx.options.get("soak_cpu_percent", 50))
    ctx.observe("soak_thresholds", {
        "minutes": soak_minutes, "max_minutes": max_minutes, "hold_seconds": hold,
        "max_mem_available_drop_mib": drift_mib,
        "mem_available_drop": f"median of the first {SOAK_DRIFT_WINDOW} closes minus "
                              f"median of the last {SOAK_DRIFT_WINDOW}",
        "max_mem_available_loss_mib_per_cycle": slope_mib,
        "mem_available_loss_per_cycle": f"least-squares slope from close "
                                        f"{SOAK_SLOPE_FROM_CLOSE} on",
        "min_closes": SOAK_MIN_CLOSES,
        "min_closes_applies_to": "the per-cycle slope only; cycles continue past "
                                 "minutes until min_closes, up to max_minutes",
        "max_idle_cpu_percent": cpu_limit,
        "window_growth_fails_after_consecutive_launches": SOAK_WINDOW_GROWTH_RUN,
        "window_measured_by": "KWin frame geometry during the hold (getWindowInfo) "
                              "and the size saved in ~/" + SHADOWCODE_WINDOW_STATE,
        "close_method": "systemctl --user stop (SIGTERM, 20s before SIGKILL)",
        "session_awake": "screen locker Autolock=false, DPMS/dim/suspend off, "
                         "logind idle:sleep inhibitor held for the whole soak",
        "quiesce": f"{DISCOVER_NOTIFIER_UNIT} stopped if running, then packagekitd "
                   "idle on two polls in a row and stopped, then every active system "
                   "and user timer stopped, before the baseline",
    })
    machine = _boot_live_for_shadowcode(ctx, "shadowcode-soak")
    session: dict[str, str] | None = None
    try:
        ctx.observe("guest_agent_seconds",
                    round(machine.wait_agent(float(ctx.options.get("boot_timeout", 900))), 1))
        session = _await_session(ctx, machine)
        _shadowcode_install_checks(ctx, machine, session, pin)
        _require_window_probe(ctx, machine, session)
        awake = _hold_session_awake(ctx, machine, session)
        quiesce = _quiesce_update_notifier(ctx, machine, session)
        quiesce["timers"] = _quiesce_timers(ctx, machine, session)
        ctx.observe("soak_quiesce", quiesce)

        since = machine.out("/usr/bin/date +%s")
        baseline_at = time.monotonic()
        baseline = _meminfo_kib(machine)
        cycles: list[dict[str, Any]] = []
        deadline = baseline_at + soak_minutes * 60
        cap = baseline_at + max_minutes * 60
        while time.monotonic() < deadline or (
            len(cycles) < SOAK_MIN_CLOSES and time.monotonic() < cap
        ):
            index = len(cycles) + 1
            unit = f"{SHADOWCODE_UNIT}-{index}"
            cycle: dict[str, Any] = {"cycle": index, "unit": unit,
                                     "after_deadline": time.monotonic() >= deadline}
            started = _start_shadowcode(ctx, machine, session, unit)
            cycle["started"] = started["exitcode"] == 0
            seconds, windows = _await_window(ctx, machine, session, unit)
            cycle["window_seconds"] = seconds
            samples = [_sample(machine, session, unit)]
            held_until = time.monotonic() + hold
            while time.monotonic() < held_until and samples[-1]["active"] == "active":
                time.sleep(15)
                samples.append(_sample(machine, session, unit))
            cycle["samples"] = samples
            cycle["held"] = all(sample["active"] == "active" for sample in samples)
            # Idle CPU from the unit's own cgroup accounting: the last two
            # samples, after the window has had the first 15s to settle.
            first, last = (samples[1], samples[-1]) if len(samples) >= 3 else (None, None)
            if first and _number(first["cpu_ns"]) is not None and _number(last["cpu_ns"]) is not None:
                elapsed = last["monotonic"] - first["monotonic"]
                cycle["idle_cpu_percent"] = round(
                    (_number(last["cpu_ns"]) - _number(first["cpu_ns"])) / 1e9 / elapsed * 100, 1
                ) if elapsed > 0 else None
            if index == 1:
                ctx.snap(machine, "shadowcode-soak-window.png", required=True)
            # The window as KWin shows it, at the end of the hold: settled, and
            # still open. Asked only when a window appeared.
            if seconds is not None:
                cycle["window_frame"], frame_note = _shadowcode_window_frame(machine, session)
                if frame_note:
                    cycle["window_frame_unread"] = frame_note
            # Asked while the app is still open, i.e. during the measured hold.
            cycle["screen_locked"] = _screen_locked(machine, session)
            cycle["stop"] = _stop_shadowcode(machine, session, unit)
            time.sleep(5)
            closed = _meminfo_kib(machine)
            # Guest time of this reading, from the baseline's guest clock, to
            # tie a unit the system started to the close it precedes.
            cycle["closed_seconds_after_baseline"] = round(time.monotonic() - baseline_at, 1)
            cycle["mem_available_after_close_kib"] = closed["MemAvailable"]
            cycle["shmem_after_close_kib"] = closed["Shmem"]
            cycle["anon_pages_after_close_kib"] = closed["AnonPages"]
            # Read after the exit: the plugin writes the file as the app quits.
            cycle["window_state"], no_size = _window_state(machine, session)
            if no_size:
                cycle["window_state_without_size"] = no_size
            cycles.append(cycle)
            ctx.log(
                f"cycle {index}: window={seconds}s held={cycle['held']} "
                f"stop={cycle['stop']['result']} cpu={cycle.get('idle_cpu_percent')}% "
                f"avail={cycle['mem_available_after_close_kib']}KiB "
                f"shmem={cycle['shmem_after_close_kib']}KiB "
                f"anon={cycle['anon_pages_after_close_kib']}KiB "
                f"frame={cycle.get('window_frame')} saved-window={cycle['window_state']}"
            )
            if not cycle["started"] or seconds is None or not cycle["held"]:
                break
        ran_for = round((time.monotonic() - baseline_at) / 60, 1)
        ctx.observe("soak_cycles", len(cycles))
        ctx.observe("soak_run", {
            "closes": len(cycles), "minutes": ran_for,
            "closes_after_deadline": sum(1 for c in cycles if c["after_deadline"]),
            "stopped_by": ("a failed cycle" if cycles and not (
                cycles[-1]["started"] and cycles[-1]["window_seconds"] is not None
                and cycles[-1]["held"]) else "the deadline" if len(cycles) >= SOAK_MIN_CLOSES
                else "--soak-max-minutes"),
        })
        guest_start = float(since) if since.replace(".", "", 1).isdigit() else None
        started_units, journal = _units_started(
            machine, session, since,
            [guest_start + c["closed_seconds_after_baseline"] for c in cycles]
            if guest_start is not None else [],
        )
        ctx.evidence.write_text("shadowcode-soak-units-started.log", journal)
        others = [entry for entry in started_units
                  if not entry["unit"].startswith((SHADOWCODE_UNIT, SOAK_INHIBIT_UNIT))]
        ctx.observe("soak_units_started", [
            f"{entry['unit']} ({entry['manager']}, "
            + (f"before close {entry['before_close']})" if entry["before_close"]
               else "after the last close)")
            for entry in others
        ])
        ctx.evidence.write_json("shadowcode-soak-cycles.json", {
            "baseline_mem_available_kib": baseline["MemAvailable"],
            "baseline_meminfo_kib": baseline, "baseline_guest_epoch": since,
            "session_awake": awake, "quiesce": quiesce, "cycles": cycles,
            "units_started": started_units,
        })
        ctx.check(f"every cycle opened a ShadowCode window ({len(cycles)} cycles)",
                  all(c["started"] and c["window_seconds"] is not None for c in cycles),
                  "window seconds: " + ", ".join(str(c["window_seconds"]) for c in cycles))
        ctx.check("ShadowCode stayed up for every hold",
                  all(c["held"] for c in cycles),
                  f"cycles not held: {[c['cycle'] for c in cycles if not c['held']]}")
        ctx.check("every close was clean (no SIGKILL, no failed unit)",
                  all(c["stop"]["result"] in ("success", "") and c["stop"]["active"] != "failed"
                      for c in cycles),
                  "; ".join(f"{c['cycle']}:{c['stop']['result']}/{c['stop']['seconds']}s"
                            for c in cycles)[:400])
        ctx.check("no ShadowCode process outlived any close",
                  not any(c["stop"]["leftovers"] for c in cycles),
                  "; ".join(c["stop"]["leftovers"] for c in cycles if c["stop"]["leftovers"])[:400])
        # Drift is measured from the closes, not from before the first open:
        # page cache the first launch warms is not a leak.
        drift = _after_close_drift([c["mem_available_after_close_kib"] for c in cycles])
        shmem = _after_close_drift([c.get("shmem_after_close_kib") for c in cycles])
        anon = _after_close_drift([c.get("anon_pages_after_close_kib") for c in cycles])
        ctx.observe("soak_after_close_drift",
                    {"mem_available": drift, "shmem": shmem, "anon_pages": anon})
        where = (f"; over the same closes Shmem {_change_mib(shmem)}, "
                 f"AnonPages {_change_mib(anon)}")
        # Not floored: a drop past the end-to-end limit is a fact at any length.
        ctx.check(
            f"available memory after close does not drift down by more than {drift_mib:g} MiB "
            f"(median of the first {SOAK_DRIFT_WINDOW} closes to median of the last "
            f"{SOAK_DRIFT_WINDOW})",
            drift is not None and drift["drop_mib"] <= drift_mib,
            (f"closes {drift['head_cycles']} median {round(drift['head_median_kib'])} KiB, "
             f"closes {drift['tail_cycles']} median {round(drift['tail_median_kib'])} KiB: "
             f"drop {drift['drop_mib']:g} MiB" if drift is not None
             else "fewer than two after-close readings") + where,
        )
        # The slope alone needs SOAK_MIN_CLOSES: below it, after-close noise
        # and first-launch warm-up decide it, in either direction.
        if len(cycles) >= SOAK_MIN_CLOSES:
            fitted = SOAK_MIN_CLOSES - SOAK_SLOPE_FROM_CLOSE + 1
            slope = drift["slope_mib_per_cycle"] if drift is not None else None
            ctx.check(
                f"available memory after close does not fall by more than {slope_mib:g} MiB "
                f"per cycle (least-squares slope from close {SOAK_SLOPE_FROM_CLOSE})",
                slope is not None and drift["slope_readings"] >= fitted and slope >= -slope_mib,
                (f"slope {slope:+g}"
                 + (f" +- {drift['slope_stderr_mib_per_cycle']:g}"
                    if drift["slope_stderr_mib_per_cycle"] is not None else "")
                 + " MiB/cycle over "
                 f"{drift['slope_readings']} closes, from close {drift['slope_cycles'][0]} "
                 f"to {drift['slope_cycles'][1]}" if slope is not None
                 else "too few after-close readings for a slope")
                + (f" (fewer than {fitted} readings: not enough to judge)"
                   if slope is not None and drift["slope_readings"] < fitted else "")
                + (f"; Shmem {shmem['slope_mib_per_cycle']:+g}, AnonPages "
                   f"{anon['slope_mib_per_cycle']:+g} MiB/cycle"
                   if shmem and anon and shmem["slope_mib_per_cycle"] is not None
                   and anon["slope_mib_per_cycle"] is not None else ""),
            )
        else:
            ctx.observe("memory_slope_unjudged",
                        f"{len(cycles)} closes, fewer than the {SOAK_MIN_CLOSES} a per-cycle "
                        "slope is judged on; no slope claim is made")
        frames = _window_growth([c.get("window_frame") for c in cycles])
        ctx.observe("soak_window_frame_growth", frames)
        if frames["compared"]:
            ctx.check(
                "the ShadowCode window KWin shows does not grow at "
                f"{SOAK_WINDOW_GROWTH_RUN} consecutive launches",
                frames["longest_run"] < SOAK_WINDOW_GROWTH_RUN,
                f"grew at launches {frames['grew_at_closes']} (longest run "
                f"{frames['longest_run']}); KWin frame at the end of each hold: "
                + " ".join(size or "-" for size in frames["sizes"]),
            )
        else:
            ctx.observe("window_frame_unobserved",
                        "KWin's getWindowInfo gave no ShadowCode frame size at two "
                        "consecutive launches; no claim is made from it")
        growth = _window_growth([c.get("window_state") for c in cycles])
        ctx.observe("soak_window_growth", growth)
        if growth["compared"]:
            ctx.check(
                "the window size ShadowCode saves does not grow at "
                f"{SOAK_WINDOW_GROWTH_RUN} consecutive closes",
                growth["longest_run"] < SOAK_WINDOW_GROWTH_RUN,
                f"grew at closes {growth['grew_at_closes']} (longest run "
                f"{growth['longest_run']}); saved size after each close: "
                + " ".join(size or "-" for size in growth["sizes"]),
            )
        else:
            ctx.observe("window_state_unobserved",
                        f"~/{SHADOWCODE_WINDOW_STATE} held no saved size (missing, "
                        "unreadable or 0x0) after two consecutive closes; no claim is "
                        "made from it")
        cpu = [c["idle_cpu_percent"] for c in cycles if c.get("idle_cpu_percent") is not None]
        if cpu:
            ctx.check(
                f"ShadowCode idles below {cpu_limit:g}% of one CPU while open",
                max(cpu) <= cpu_limit,
                f"per-cycle idle CPU %: {cpu}",
            )
        else:
            ctx.observe("idle_cpu_unmeasured",
                        "the user manager reported no CPUUsageNSec for the unit; "
                        "no CPU claim is made")
        crashes = _crashes_since(ctx, machine, since, "shadowcode-soak")
        ctx.check(f"no ShadowCode crash across {len(cycles)} cycles",
                  not crashes, "; ".join(crashes)[:400])
        locked = [c.get("screen_locked") for c in cycles]
        if any(value is not None for value in locked):
            ctx.check(
                "the screen locker never engaged during the soak",
                not any(value is True for value in locked),
                f"per-cycle screen locked: {locked}",
            )
        else:
            ctx.observe("screen_locker_state_unobserved",
                        "org.freedesktop.ScreenSaver.GetActive did not answer; the "
                        "locker was disabled and inhibited (soak_session_awake) but "
                        "its state during the holds is not claimed")
        # What the soak could not judge. A failed check above is the verdict
        # already; only a soak in which nothing failed is held back here.
        if not any(check["state"] == "FAILED" for check in ctx.checks):
            unjudged = []
            if len(cycles) < SOAK_MIN_CLOSES:
                unjudged.append(
                    f"only {len(cycles)} open/close cycles fit in --soak-max-minutes "
                    f"{max_minutes:g} with --soak-hold {hold:g}s; the per-cycle memory "
                    f"slope needs at least {SOAK_MIN_CLOSES} closes to tell a "
                    f"{slope_mib:g} MiB/cycle loss from after-close noise and "
                    "first-launch warm-up"
                )
            if not frames["compared"] and not growth["compared"]:
                unjudged.append(
                    "the window size was measured at no two consecutive launches "
                    "(no KWin frame size, no saved size), so whether it grows is unknown"
                )
            if unjudged:
                ctx.blocked("; ".join(unjudged))
    finally:
        try:
            if session is not None:
                _release_session_awake(machine, session)
            machine.shutdown()
        finally:
            ctx.collect_machine_evidence(machine, "shadowcode-soak")


# --- registry -----------------------------------------------------------------


class Case:
    """One acceptance case, and exactly how much of a release case it proves.

    `manifest_gap` is the honest half of `manifest_case`. A case may contribute
    to a required release case without proving all of it, and the difference
    has to be ENFORCED rather than noted: with a gap recorded here, the harness
    refuses to record that case as passed no matter how many checks the run
    evaluated. Writing "pass" against a case whose other half nobody ran is the
    exact failure this whole stage exists to make impossible.
    """

    def __init__(
        self,
        name: str,
        run: Callable[[Context], None],
        *,
        summary: str,
        manifest_case: str | None = None,
        manifest_gap: str | None = None,
        companions: tuple[str, ...] = (),
        required_runs: tuple[dict[str, Any], ...] = (),
        consumes_artifact: bool = True,
        minutes: int = 10,
    ) -> None:
        self.name = name
        self.run = run
        self.summary = summary
        self.manifest_case = manifest_case
        self.manifest_gap = manifest_gap
        self.companions = companions
        # Ledger rows that must exist, PASSED, against the same artifact before
        # this case's result may be recorded. `companions` names a case;
        # `required_runs` can also pin the options a run was made with, which is
        # what "a BIOS pass AND a UEFI pass" needs -- the two runs are the same
        # case and differ only in how they were driven.
        self.required_runs = required_runs
        self.consumes_artifact = consumes_artifact
        self.minutes = minutes


CASES: dict[str, Case] = {
    case.name: case
    for case in (
        Case(
            "live-boot",
            case_live_boot,
            summary="Boot the ISO under test and prove the live system comes up",
            minutes=8,
        ),
        Case(
            "install",
            case_install,
            summary="Install to a blank disk with Calamares and boot the result",
            manifest_case="INSTALL-01",
            manifest_gap=(
                "INSTALL-01 is \"Fresh BIOS and UEFI Calamares installs boot from "
                "disk\": two firmwares. One run proves one firmware, so recording "
                "from a single run would claim the other. Run this case under "
                "--firmware bios and --firmware uefi, then record INSTALL-01 from "
                "the install-both-firmwares case, which boots both results."
            ),
            minutes=40,
        ),
        Case(
            "install-both-firmwares",
            case_install_both_firmwares,
            summary="Boot the BIOS and the UEFI install of this artifact; "
            "the case INSTALL-01 is recorded from",
            manifest_case="INSTALL-01",
            required_runs=(
                {"case": "install", "firmware": "bios"},
                {"case": "install", "firmware": "uefi"},
            ),
            minutes=12,
        ),
        Case(
            "upgrade",
            case_upgrade,
            summary="Upgrade an installed previous release, preserving user data",
            manifest_case="UPGRADE-01",
            manifest_gap=(
                "UPGRADE-01 is \"Existing 4.1 systems (published ISO, updated "
                "from the published APT suite) upgrade to 5.0 with preserved "
                "user data AND WORKING RECOVERY\". This case proves the upgrade "
                "and its data preservation; it does not restore a Phoenix Point "
                "on the upgraded system, so it cannot record UPGRADE-01 alone."
            ),
            consumes_artifact=False,
            minutes=45,
        ),
        Case(
            "recovery",
            case_recovery,
            summary="Restore a Phoenix Point and prove the restored state boots",
            manifest_case="RECOVERY-01",
            manifest_gap=(
                "RECOVERY-01 is \"PROJECT DIFF/UNDO and supported system rollback "
                "verified after injected failures\". This case and its power-loss "
                "companion prove the system-rollback half against a real injected "
                "failure. The Fireline project diff/undo half is proven by the "
                "recovery-project case, which is where RECOVERY-01 is recorded "
                "from: it refuses to run unless both of these have passed against "
                "the same artifact."
            ),
            companions=("recovery-interrupted",),
            consumes_artifact=False,
            minutes=20,
        ),
        Case(
            "recovery-project",
            case_recovery_project,
            summary="Fireline project diff/undo after injected damage; the case "
            "RECOVERY-01 is recorded from",
            manifest_case="RECOVERY-01",
            required_runs=(
                {"case": "recovery"},
                {"case": "recovery-interrupted"},
            ),
            consumes_artifact=False,
            minutes=8,
        ),
        Case(
            "recovery-interrupted",
            case_recovery_interrupted,
            summary="Cut power mid-restore; the system must never claim a "
            "restore it did not complete",
            consumes_artifact=False,
            minutes=25,
        ),
        Case(
            "shadowcode",
            case_shadowcode,
            summary="ShadowCode at the pinned version: installed, --version, bundled "
            "llama.cpp runs, window opens in the live session and stays up",
            minutes=20,
        ),
        Case(
            "shadowcode-soak",
            case_shadowcode_soak,
            summary="Open/close ShadowCode for --soak-minutes watching crashes, "
            "leftovers, memory, CPU and window growth; the case SHADOWCODE-01 is "
            "recorded from",
            manifest_case="SHADOWCODE-01",
            required_runs=({"case": "shadowcode"},),
            minutes=45,
        ),
    )
}
