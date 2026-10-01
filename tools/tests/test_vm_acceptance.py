#!/usr/bin/env python3
"""Tests for the VM acceptance harness.

These run without a virtual machine. They cover the parts a VM run cannot
exercise honestly: that unusable evidence is refused, that the ledger notices
its own history being edited, and -- the point of the harness -- that there is
no path from "did not run" to "PASS".
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
import unittest.mock
import zlib

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))

from acceptance import release_link  # noqa: E402
from acceptance import trusted  # noqa: E402
from acceptance import vm_acceptance  # noqa: E402
from acceptance.cases import CASES, Blocked, Context  # noqa: E402
from acceptance.evidence import EvidenceError, EvidenceSet, digest_of  # noqa: E402
from acceptance.ledger import (  # noqa: E402
    GENESIS,
    Ledger,
    receipt_problems,
    write_receipt,
)
from acceptance.vm import ppm_to_png  # noqa: E402

# PINNED, DELIBERATELY, AND ONLY UNTIL 4.1.0 LANDS.
#
# load_release(None) resolves to the sole non-historical data file, and there
# are two now that versions/4.1.0.toml exists. That ambiguity is refused rather
# than guessed -- test_release_gate.py names exactly that behaviour -- so this
# module has to say which release its fixtures are about, and they are about
# 4.0.0: the ISO it drives, the qa/4.0.0/acceptance.json it reads, and the
# work/qa-4.0.0 evidence root all exist for 4.0.0 and do not exist for 4.1.0.
#
# THAT BUMP HAS LANDED. versions/4.0.0.toml is historical, 4.1.0 is the sole
# live file and qa/4.1.0/acceptance.json exists, so this takes no argument
# again -- which is the point: a harness that names its release is a harness
# that keeps testing the last one. It still has no ARTIFACT, and that is
# correct: none of the cases below run a VM, they exercise the recorder's
# refusals, and a refusal proven against the live manifest is the refusal that
# will actually be met. SHADOWFETCH_RELEASE_VERSION remains an override for
# re-running this suite against a historical manifest on purpose.
_pin = os.environ.get("SHADOWFETCH_RELEASE_VERSION")
RELEASE = release_link.load_release(_pin) if _pin else release_link.load_release(None)


def make_png(path: Path, width: int, height: int) -> None:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    raw = bytearray()
    for row in range(height):
        raw.append(0)
        for column in range(width):
            raw += bytes(((row * 7 + column) % 251, (column * 13) % 251, 91))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 1))
        + chunk(b"IEND", b"")
    )


class ReceiptDigestIsStampedIntoTheCallersReceipt(unittest.TestCase):
    """A case that PASSED could not be promoted.

    write_receipt() computed the digest over a private copy and returned it, so
    the caller went on holding a receipt with no digest in it -- and _record()
    reads receipt["receipt_sha256"] to name the run in the manifest. Measured:
    `install-both-firmwares` PASSED with 16 checks against two running machines
    and then raised KeyError: 'receipt_sha256' on the way to being recorded.
    A receipt read back from disk carried the field; one still in hand did not,
    and only the promotion path ever noticed.
    """

    def test_the_caller_holds_the_digest_after_writing(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipt.json"
            receipt = {"run_id": "r-1", "verdict": "PASS", "checks": []}
            returned = write_receipt(path, receipt)
            self.assertEqual(receipt.get("receipt_sha256"), returned,
                             "the caller's receipt has no digest in it")

    def test_the_file_and_the_caller_agree(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipt.json"
            receipt = {"run_id": "r-2", "verdict": "PASS", "checks": []}
            write_receipt(path, receipt)
            on_disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["receipt_sha256"], receipt["receipt_sha256"])

    def test_stamping_does_not_change_what_a_second_write_computes(self):
        """Idempotent, because the digest is over everything EXCEPT itself."""
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipt.json"
            receipt = {"run_id": "r-3", "verdict": "PASS", "checks": []}
            first = write_receipt(path, receipt)
            second = write_receipt(path, receipt)
            self.assertEqual(first, second)


class TrustedPathTests(unittest.TestCase):
    """The permanent invariant: no security-relevant binary via PATH.

    Resolution is delegated to tools/release/gate.py, so these tests assert the
    harness's contract with it -- every program declared, every one resolved to
    an absolute root-owned path, and PATH ignored -- rather than restating the
    resolver's own unit tests.
    """

    def test_every_declared_program_has_a_role(self) -> None:
        gate = release_link.gate()
        for name, role in trusted._requirements():
            self.assertIn(role, (gate.ROLE_SECURITY, gate.ROLE_QUALITY))
            self.assertEqual(trusted.classification(name), role)

    def test_every_declared_program_resolves_root_owned_and_absolute(self) -> None:
        gate = release_link.gate()
        for name, _ in trusted._requirements():
            found = trusted.program(name)
            self.assertTrue(found.path.is_absolute(), name)
            self.assertEqual(found.trust, gate.TRUST_SYSTEM, f"{name} at {found.path}")
            self.assertEqual(found.path.stat().st_uid, 0, name)

    def test_undeclared_program_is_refused(self) -> None:
        """journalctl is the defect this invariant was written for."""
        with self.assertRaises(trusted.TrustError):
            trusted.resolve("journalctl")

    def test_resolution_never_consults_the_environment(self) -> None:
        """A hostile PATH must not change what gets resolved.

        The impostor is executable, named exactly like the real program, and
        first on PATH. A which()-based lookup would run it.
        """
        with tempfile.TemporaryDirectory() as directory:
            impostor = Path(directory) / "qemu-img"
            impostor.write_text("#!/bin/sh\necho forged\n")
            impostor.chmod(0o755)
            original = os.environ.get("PATH")
            try:
                os.environ["PATH"] = f"{directory}:{original or ''}"
                self.assertEqual(trusted.resolve("qemu-img"), Path("/usr/bin/qemu-img"))
                self.assertNotEqual(trusted.resolve("qemu-img"), impostor)
            finally:
                if original is None:
                    os.environ.pop("PATH", None)
                else:
                    os.environ["PATH"] = original

    def test_argv_is_the_absolute_path(self) -> None:
        self.assertEqual(trusted.argv("qemu-img", ["--version"])[0], "/usr/bin/qemu-img")

    def test_trust_base_is_recordable(self) -> None:
        rows = trusted.describe()
        self.assertEqual({row["name"] for row in rows},
                         {name for name, _ in trusted._requirements()})
        for row in rows:
            self.assertTrue(row["path"].startswith("/"), row)

    def test_safe_env_carries_no_inherited_path(self) -> None:
        self.assertEqual(trusted.SAFE_ENV["PATH"], "/usr/sbin:/usr/bin:/sbin:/bin")

    def test_guest_output_is_classified_as_the_subject_not_a_verdict(self) -> None:
        self.assertEqual(trusted.GUEST_SUBJECT, "guest-subject")


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.directory.name) / "ledger.jsonl")
        self.addCleanup(self.directory.cleanup)

    def append(self, case: str, verdict: str) -> dict:
        return self.ledger.append(
            {
                "run_id": f"{case}-{verdict}",
                "case": case,
                "verdict": verdict,
                "artifact_sha256": "a" * 64,
            }
        )

    def test_chain_starts_at_genesis_and_links(self) -> None:
        first = self.append("recovery", "FAIL")
        second = self.append("recovery", "PASS")
        self.assertEqual(first["prev"], GENESIS)
        self.assertEqual(second["prev"], first["entry_sha256"])
        self.assertEqual(self.ledger.verify(), [])

    def test_editing_an_entry_is_detected(self) -> None:
        self.append("recovery", "FAIL")
        rows = self.ledger.entries()
        rows[0]["verdict"] = "PASS"
        self.ledger.path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
        )
        self.assertTrue(
            any("digest does not match" in problem for problem in self.ledger.verify())
        )

    def test_deleting_a_failure_before_a_pass_is_detected(self) -> None:
        """The exact fraud a plain directory of receipts cannot notice."""
        self.append("recovery", "FAIL")
        self.append("recovery", "FAIL")
        self.append("recovery", "PASS")
        rows = self.ledger.entries()
        del rows[0:2]
        self.ledger.path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
        )
        problems = self.ledger.verify()
        self.assertTrue(any("prev does not chain" in problem for problem in problems))

    def test_find_filters_on_every_criterion(self) -> None:
        self.append("recovery", "PASS")
        self.append("live-boot", "PASS")
        self.assertEqual(
            [row["case"] for row in self.ledger.find(case="recovery", verdict="PASS")],
            ["recovery"],
        )
        self.assertEqual(self.ledger.find(case="recovery", verdict="FAIL"), [])

    def test_concurrent_appends_from_separate_processes_keep_one_chain(self) -> None:
        """Every harness run appends, so parallel VM lanes append concurrently.

        Each writer sleeps between reading the head and writing (utc_now() is
        called in that window), which forked the chain every time before the
        append took the ledger lock.
        """
        import multiprocessing

        writers, per_writer = 6, 5
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(writers)
        processes = [
            context.Process(
                target=_append_concurrently,
                args=(str(self.ledger.path), barrier, writer, per_writer),
            )
            for writer in range(writers)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(60)
            self.assertEqual(process.exitcode, 0)
        rows = self.ledger.entries()
        self.assertEqual(len(rows), writers * per_writer)
        self.assertEqual(
            [row["seq"] for row in rows], list(range(1, writers * per_writer + 1))
        )
        self.assertEqual(len({row["run_id"] for row in rows}), writers * per_writer)
        self.assertEqual(self.ledger.verify(), [])
        self.assertTrue(self.ledger.lock_path.is_file())


def _append_concurrently(path: str, barrier, writer: int, count: int) -> None:
    import time
    from acceptance import ledger as ledger_module

    real_now = ledger_module.utc_now

    def slow_now() -> str:
        time.sleep(0.01)
        return real_now()

    ledger_module.utc_now = slow_now
    ledger = Ledger(Path(path))
    barrier.wait()
    for number in range(count):
        ledger.append({"run_id": f"w{writer}-{number}", "case": "race", "verdict": "PASS"})


class ReceiptTests(unittest.TestCase):
    def test_receipt_digest_covers_the_whole_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            digest = write_receipt(path, {"case": "recovery", "verdict": "FAIL"})
            receipt = json.loads(path.read_text())
            self.assertEqual(receipt["receipt_sha256"], digest)
            self.assertEqual(receipt_problems(receipt), [])
            receipt["verdict"] = "PASS"
            self.assertEqual(
                receipt_problems(receipt),
                ["receipt digest does not match its content"],
            )

    def test_digest_is_order_independent(self) -> None:
        self.assertEqual(digest_of({"a": 1, "b": 2}), digest_of({"b": 2, "a": 1}))


class EvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.evidence = EvidenceSet(REPO_ROOT, self.root / "evidence" / "run")

    def test_empty_file_is_not_evidence(self) -> None:
        target = self.evidence.path("empty.log")
        target.write_bytes(b"")
        with self.assertRaises(EvidenceError) as caught:
            self.evidence.add(target, "log")
        self.assertIn("empty", str(caught.exception))

    def test_informationless_file_is_not_evidence(self) -> None:
        target = self.evidence.path("zeros.log")
        target.write_bytes(b"\x00" * 4096)
        with self.assertRaises(EvidenceError):
            self.evidence.add(target, "log")

    def test_undersized_screenshot_is_not_evidence(self) -> None:
        target = self.evidence.path("small.png")
        make_png(target, 640, 480)
        with self.assertRaises(EvidenceError) as caught:
            self.evidence.add(target, "screenshot")
        self.assertIn("640x480", str(caught.exception))

    def test_real_screenshot_is_accepted_and_hashed(self) -> None:
        target = self.evidence.path("desktop.png")
        make_png(target, 1920, 1080)
        item = self.evidence.add(target, "screenshot")
        self.assertEqual(item["kind"], "screenshot")
        self.assertEqual(len(item["sha256"]), 64)
        self.assertGreater(item["bytes"], 1024)

    def test_evidence_outside_the_run_directory_is_refused(self) -> None:
        outside = self.root / "elsewhere.log"
        outside.write_text("a plausible looking log from somewhere else\n")
        with self.assertRaises(EvidenceError) as caught:
            self.evidence.add(outside, "log")
        self.assertIn("must be written inside", str(caught.exception))

    def test_try_add_never_silently_registers_bad_evidence(self) -> None:
        target = self.evidence.path("empty2.log")
        target.write_bytes(b"")
        self.assertIsNone(self.evidence.try_add(target, "log"))
        self.assertEqual(self.evidence.items, [])

    def test_floors_come_from_the_release_recorder(self) -> None:
        """Imported, not reimplemented: the floors cannot drift apart."""
        self.assertEqual(self.evidence.recorder.MIN_SCREENSHOT_BYTES, 1024)
        self.assertIn("screenshot", self.evidence.recorder.VALID_KINDS)


class VerdictTests(unittest.TestCase):
    def context(self) -> Context:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        return Context(
            name="unit",
            repo_root=REPO_ROOT,
            run_dir=root / "run",
            evidence=EvidenceSet(REPO_ROOT, root / "evidence"),
            artifact={"path": "/dev/null", "sha256": "b" * 64},
            options={"version": "4.0.0"},
        )

    def test_a_case_that_checked_nothing_is_blocked_not_passed(self) -> None:
        verdict, reason = vm_acceptance.verdict_for(self.context())
        self.assertEqual(verdict, "BLOCKED")
        self.assertIn("nothing was proven", reason)

    def test_one_failing_check_fails_the_case(self) -> None:
        ctx = self.context()
        ctx.check("something true", True)
        ctx.check("the important one", False, "it did not hold")
        verdict, reason = vm_acceptance.verdict_for(ctx)
        self.assertEqual(verdict, "FAIL")
        self.assertIn("the important one", reason)

    def test_all_checks_passing_is_a_pass(self) -> None:
        ctx = self.context()
        ctx.check("a", True)
        ctx.check("b", True)
        self.assertEqual(vm_acceptance.verdict_for(ctx)[0], "PASS")

    def test_observations_are_not_checks(self) -> None:
        """Recording a fact must never move a case toward passing."""
        ctx = self.context()
        ctx.observe("version_marker", "4.0.0")
        ctx.observe("everything_looked_fine", True)
        self.assertEqual(ctx.checks, [])
        self.assertEqual(vm_acceptance.verdict_for(ctx)[0], "BLOCKED")


class NoPathToAnUnearnedPassTests(unittest.TestCase):
    """The harness's central claim, tested as an adversary would."""

    def test_there_is_no_subcommand_that_records_a_result(self) -> None:
        parser = vm_acceptance.build_parser()
        actions = [
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        self.assertEqual(len(actions), 1)
        self.assertEqual(
            sorted(actions[0].choices), ["list", "run", "status", "verify"]
        )

    def test_run_offers_no_way_to_supply_a_verdict(self) -> None:
        parser = vm_acceptance.build_parser()
        run = [
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ][0].choices["run"]
        flags = {option for action in run._actions for option in action.option_strings}
        for forbidden in ("--status", "--verdict", "--pass", "--result", "--case-id"):
            self.assertNotIn(forbidden, flags)
        self.assertIn("--record", flags)

    def _gapless(self, name: str):
        """The same case with its coverage gap removed.

        The gap refusal fires first and would mask every other guard, so the
        guards below are tested against a case that is allowed to record.
        """
        case = CASES[name]
        return type(case)(
            case.name,
            case.run,
            summary=case.summary,
            manifest_case=case.manifest_case,
            companions=case.companions,
            consumes_artifact=case.consumes_artifact,
        )

    def _receipt(self, verdict: str, evidence: list | None = None) -> dict:
        return {
            "verdict": verdict,
            "run_id": "unit-run",
            "receipt_sha256": "c" * 64,
            "harness": {"digest": "d" * 64},
            "artifact": {"sha256": "e" * 64, "name": "unit.iso"},
            "checks": [{"name": "x", "state": "PASSED", "detail": ""}],
            "evidence": evidence if evidence is not None else [],
        }

    def test_recording_refuses_every_verdict_but_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.jsonl")
            case = self._gapless("recovery")
            for verdict in ("FAIL", "BLOCKED", "ERROR"):
                code = vm_acceptance._record(
                    REPO_ROOT,
                    RELEASE,
                    case,
                    self._receipt(verdict),
                    Path(directory) / "receipt.json",
                    ledger,
                )
                self.assertEqual(code, 0, verdict)

    def test_recording_refuses_a_pass_with_no_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.jsonl")
            # live-boot has no companions, so evidence is the only thing missing.
            code = vm_acceptance._record(
                REPO_ROOT,
                RELEASE,
                self._gapless("install"),
                self._receipt("PASS", evidence=[]),
                Path(directory) / "receipt.json",
                ledger,
            )
            self.assertEqual(code, vm_acceptance.EXIT_ERROR)

    def test_recovery_will_not_record_without_its_power_loss_companion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.jsonl")
            code = vm_acceptance._record(
                REPO_ROOT,
                RELEASE,
                self._gapless("recovery"),
                self._receipt(
                    "PASS",
                    evidence=[{"relative_path": "Makefile", "sha256": "f" * 64}],
                ),
                Path(directory) / "receipt.json",
                ledger,
            )
            self.assertEqual(code, vm_acceptance.EXIT_BLOCKED)

    def test_recording_refuses_when_the_ledger_does_not_verify(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.jsonl")
            ledger.append(
                {
                    "run_id": "companion",
                    "case": "recovery-interrupted",
                    "verdict": "PASS",
                    "artifact_sha256": "e" * 64,
                }
            )
            rows = ledger.entries()
            # Leave the fields the companion lookup matches on intact, so the
            # entry still LOOKS like the required passing companion. Only the
            # chain notices that its history was edited.
            rows[0]["run_id"] = "companion-renamed"
            ledger.path.write_text(json.dumps(rows[0], sort_keys=True) + "\n")
            code = vm_acceptance._record(
                REPO_ROOT,
                RELEASE,
                self._gapless("recovery"),
                self._receipt(
                    "PASS",
                    evidence=[{"relative_path": "Makefile", "sha256": "f" * 64}],
                ),
                Path(directory) / "receipt.json",
                ledger,
            )
            self.assertEqual(code, vm_acceptance.EXIT_ERROR)


    def test_a_case_that_only_half_covers_a_release_case_cannot_record(self) -> None:
        """Contributing to a required case is not the same as proving it.

        RECOVERY-01 is "project diff/undo AND supported system rollback". The
        recovery cases prove the rollback half against a real injected failure.
        Recording the whole case from them would claim the half nobody ran.
        """
        manifest = RELEASE.acceptance_manifest()
        before = manifest.read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.jsonl")
            for name in ("recovery", "upgrade", "install"):
                case = CASES[name]
                self.assertIsNotNone(case.manifest_case, name)
                self.assertTrue(case.manifest_gap, name)
                code = vm_acceptance._record(
                    REPO_ROOT,
                    RELEASE,
                    case,
                    self._receipt(
                        "PASS",
                        evidence=[{"relative_path": "Makefile", "sha256": "f" * 64}],
                    ),
                    Path(directory) / "receipt.json",
                    ledger,
                )
                self.assertEqual(code, 0, name)
        self.assertEqual(manifest.read_bytes(), before,
                         "the release manifest must not have been touched")

    def test_recording_writes_the_manifest_when_a_case_fully_covers_one(self) -> None:
        """The one workflow, proven end to end against a scratch manifest.

        Every guard above is a refusal. This is the other half: given a case
        that does prove its release case, a PASS with real evidence reaches the
        manifest -- through the release recorder, not through this harness
        writing JSON of its own.
        """
        scratch = REPO_ROOT / "work" / f"vm-acceptance-record-test-{os.getpid()}"
        evidence_dir = (
            REPO_ROOT / "work" / f"qa-{RELEASE.version}" / "evidence"
            / "vm-acceptance" / f"_record_test_{os.getpid()}"
        )
        try:
            scratch.mkdir(parents=True)
            evidence_dir.mkdir(parents=True)
            manifest = scratch / "acceptance.json"
            manifest.write_bytes(RELEASE.acceptance_manifest().read_bytes())
            proof = evidence_dir / "transcript.log"
            proof.write_text(
                "PASSED the restored Point is what boots\n"
                "PASSED root and /boot are the same generation\n"
            )

            class ScratchRelease:
                version = RELEASE.version

                @staticmethod
                def acceptance_manifest() -> Path:
                    return manifest

            case = CASES["recovery"]
            complete = vm_acceptance.CASES.__class__  # noqa: F841 - readability
            full = type(case)(
                "recovery-complete",
                case.run,
                summary=case.summary,
                manifest_case="RECOVERY-01",
                consumes_artifact=False,
            )
            receipt = self._receipt(
                "PASS",
                evidence=[
                    {
                        "relative_path": str(proof.relative_to(REPO_ROOT)),
                        "sha256": "0" * 64,
                    }
                ],
            )
            code = vm_acceptance._record(
                REPO_ROOT,
                ScratchRelease,
                full,
                receipt,
                scratch / "receipt.json",
                Ledger(scratch / "ledger.jsonl"),
            )
            self.assertEqual(code, 0)
            recorded = json.loads(manifest.read_text())
            case_row = next(
                row for row in recorded["cases"] if row["id"] == "RECOVERY-01"
            )
            self.assertEqual(case_row["status"], "pass")
            self.assertEqual(len(case_row["evidence"]), 1)
            self.assertIn("vm_acceptance.py run", case_row["notes"])
            self.assertIn(receipt["run_id"], case_row["notes"])
        finally:
            for path in (
                scratch / "acceptance.json",
                scratch / "ledger.jsonl",
                evidence_dir / "transcript.log",
            ):
                path.unlink(missing_ok=True)
            for directory in (scratch, evidence_dir):
                if directory.is_dir():
                    directory.rmdir()

    def test_blocked_does_not_exit_zero(self) -> None:
        self.assertNotEqual(vm_acceptance.EXIT_BLOCKED, vm_acceptance.EXIT_PASS)
        self.assertNotEqual(vm_acceptance.EXIT_FAIL, vm_acceptance.EXIT_PASS)
        self.assertNotEqual(vm_acceptance.EXIT_ERROR, vm_acceptance.EXIT_PASS)


class CaseRegistryTests(unittest.TestCase):
    def test_every_coverage_gap_quotes_the_title_it_explains(self) -> None:
        """A manifest_gap opens by QUOTING the required case's title, so it is
        a second copy of the requirement. Retitling UPGRADE-01 for 4.1.0 --
        it asked for an upgrade from 3.5, whose base image this host no longer
        has -- made that copy disagree with the manifest instantly, and nothing
        noticed. Compared case-insensitively: these messages SHOUT the half the
        case fails to cover, and that emphasis is the message.
        """
        titles = {row["id"]: row["title"]
                  for row in json.loads(
                      RELEASE.acceptance_manifest().read_text(encoding="utf-8")
                  )["cases"]}
        quoted = 0
        for case in CASES.values():
            if not case.manifest_gap or not case.manifest_case:
                continue
            opening = f'{case.manifest_case} is "'
            if not case.manifest_gap.startswith(opening):
                continue
            rest = case.manifest_gap[len(opening):]
            claim = rest[:rest.index('"')]
            quoted += 1
            with self.subTest(case=case.name):
                self.assertIn(case.manifest_case, titles,
                              "the gap explains a case the manifest does not have")
                self.assertEqual(titles[case.manifest_case].lower(), claim.lower())
        self.assertGreater(quoted, 0,
                           "no gap message quotes a title any more; this test "
                           "no longer checks anything")

    def test_the_assigned_cases_exist(self) -> None:
        for name in ("live-boot", "install", "upgrade", "recovery",
                     "recovery-interrupted"):
            self.assertIn(name, CASES)

    def test_recovery_requires_the_power_loss_companion(self) -> None:
        self.assertIn("recovery-interrupted", CASES["recovery"].companions)

    def test_cases_without_a_base_image_block_rather_than_pass(self) -> None:
        for name in ("recovery", "recovery-interrupted"):
            ctx = Context(
                name=name,
                repo_root=REPO_ROOT,
                run_dir=Path("/nonexistent"),
                evidence=None,
                artifact={"path": "/dev/null", "sha256": "b" * 64},
                options={"version": "4.0.0"},
            )
            with self.assertRaises(Blocked):
                CASES[name].run(ctx)
            self.assertEqual(ctx.checks, [])

    def test_upgrade_blocks_without_a_previous_release_image(self) -> None:
        ctx = Context(
            name="upgrade",
            repo_root=REPO_ROOT,
            run_dir=Path("/nonexistent"),
            evidence=None,
            artifact={"path": "/dev/null", "sha256": "b" * 64},
            options={"version": "4.0.0"},
        )
        with self.assertRaises(Blocked) as caught:
            CASES["upgrade"].run(ctx)
        self.assertIn("previous-release installed image", str(caught.exception))


class FakeGuest:
    """A guest whose agent answers from a handler. Records every command, in order."""

    def __init__(self, root: Path, handler) -> None:
        self.handler = handler
        self.commands: list[str] = []
        self.serial_log = root / "serial.log"
        self.qemu_log = root / "qemu.log"

    def run(self, command: str, timeout: float = 300.0, *, check: bool = False) -> dict:
        self.commands.append(command)
        result = self.handler(command)
        if result is None:
            result = ""
        if isinstance(result, str):
            result = {"exitcode": 0, "stdout": result, "stderr": ""}
        return result

    def out(self, command: str, timeout: float = 120.0) -> str:
        return self.run(command, timeout=timeout)["stdout"].strip()

    def create_disk(self, gib: int) -> None:
        pass

    def boot(self, *args, **kwargs) -> None:
        pass

    def wait_agent(self, timeout: float) -> float:
        return 51.0

    def clone_disk(self, base: Path) -> dict:
        return {"base": str(base)}

    def reboot(self, timeout: float) -> dict:
        return {}

    def screenshot(self, target: Path) -> dict:
        make_png(target, 1920, 1080)
        return {"width": 1920, "height": 1080}

    def shutdown(self) -> None:
        pass

    def is_running(self) -> bool:
        return False


class GuestRaceTests(unittest.TestCase):
    """The three 5.0.0 VM-qualification races, replayed against a fake guest.

    live-boot judged systemd 51s after boot while it still said "starting";
    upgrade ran apt before the guest had an IPv4 address; the soak measured a
    session the screen locker had taken over. Each case must now wait (bounded)
    or refuse -- and a refusal is BLOCKED, never a PASS.
    """

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        # The cases poll in 5s steps; the steps are not what is under test.
        from acceptance import cases
        self.cases = cases
        sleeper = unittest.mock.patch.object(cases.time, "sleep", lambda _s: None)
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def context(self, name: str, guest: FakeGuest, **options) -> Context:
        ctx = Context(
            name=name,
            repo_root=REPO_ROOT,
            run_dir=self.root / "run",
            evidence=EvidenceSet(REPO_ROOT, self.root / "evidence"),
            artifact={"path": str(self.root / "unit.iso"), "sha256": "b" * 64},
            options={"version": "5.0.0", "desktop_settle": 0, **options},
        )
        ctx.guest = lambda *_a, **_k: guest  # type: ignore[method-assign]
        return ctx

    @staticmethod
    def live_handler(states: list[str], failed: str = ""):
        def handler(command: str):
            if command.startswith("systemctl is-system-running"):
                return states.pop(0) if len(states) > 1 else states[0]
            if command.startswith("systemctl --failed"):
                return failed
            if command == "cat /proc/cmdline":
                return "BOOT_IMAGE=/live/vmlinuz boot=live quiet"
            if command.startswith("cat /usr/share/shadowfetch/version"):
                return "5.0.0"
            if command == "cat /etc/os-release":
                return 'NAME="Shadowfetch Linux"\nVERSION_ID="5.0.0"'
            return ""
        return handler

    # -- live-boot ----------------------------------------------------------

    def test_live_boot_waits_for_systemd_to_leave_starting(self) -> None:
        guest = FakeGuest(self.root, self.live_handler(["starting", "starting", "running"]))
        ctx = self.context("live-boot", guest)
        CASES["live-boot"].run(ctx)
        state = [c for c in ctx.checks if c["name"] == "systemd reaches a running state"]
        self.assertEqual(state[0]["state"], "PASSED", state)
        polls = [c for c in guest.commands if c.startswith("systemctl is-system-running")]
        self.assertGreaterEqual(len(polls), 3, "the state was judged before systemd settled")
        self.assertEqual(ctx.observations["live_systemd_settle"]["state"], "running")
        self.assertEqual(vm_acceptance.verdict_for(ctx)[0], "PASS")

    def test_live_boot_reports_degraded_with_its_failed_units(self) -> None:
        guest = FakeGuest(self.root, self.live_handler(
            ["starting", "degraded"], failed="foo.service loaded failed failed Foo"))
        ctx = self.context("live-boot", guest)
        CASES["live-boot"].run(ctx)
        state = [c for c in ctx.checks if c["name"] == "systemd reaches a running state"][0]
        self.assertEqual(state["state"], "PASSED")
        self.assertIn("'degraded'", state["detail"])
        self.assertIn("foo.service", state["detail"])
        self.assertIn("foo.service", ctx.observations["degraded_failed_units"])

    def test_live_boot_that_never_settles_fails_saying_how_long_it_waited(self) -> None:
        guest = FakeGuest(self.root, self.live_handler(["starting"]))
        ctx = self.context("live-boot", guest, settle_timeout=0.05)
        CASES["live-boot"].run(ctx)
        state = [c for c in ctx.checks if c["name"] == "systemd reaches a running state"][0]
        self.assertEqual(state["state"], "FAILED")
        self.assertIn("still not settled", state["detail"])
        self.assertEqual(vm_acceptance.verdict_for(ctx)[0], "FAIL")

    # -- upgrade ------------------------------------------------------------

    def upgrade_context(self, guest: FakeGuest, **options) -> Context:
        base = self.root / "base.qcow2"
        base.write_bytes(b"not really a disk")
        return self.context(
            "upgrade", guest, upgrade_base_image=str(base),
            upgrade_repo="shadowfetch-missions", upgrade_from_version="4.1.0",
            **options)

    def upgrade_handler(self, online_after: int | None):
        state = {"probes": 0, "upgraded": False}

        def handler(command: str):
            if command == self.cases.NETWORK_ONLINE_PROBE:
                state["probes"] += 1
                ready = online_after is not None and state["probes"] > online_after
                return {"exitcode": 0 if ready else 1, "stdout": "", "stderr": ""}
            if "apt-get" in command:
                state["upgraded"] = True
                return "Setting up shadowfetch-missions (5.0.0-1) ..."
            if command.startswith("cat /usr/share/shadowfetch/version"):
                return "5.0.0" if state["upgraded"] else "4.1.0"
            if command.startswith("systemctl is-system-running"):
                return "running"
            if command.startswith("sha256sum"):
                return "a" * 64
            if command.startswith("cat /etc/machine-id"):
                return "c" * 32
            return ""
        return handler, state

    def test_upgrade_waits_for_network_online_before_apt(self) -> None:
        handler, state = self.upgrade_handler(online_after=2)
        guest = FakeGuest(self.root, handler)
        ctx = self.upgrade_context(guest)
        CASES["upgrade"].run(ctx)
        probes = [i for i, c in enumerate(guest.commands)
                  if c == self.cases.NETWORK_ONLINE_PROBE]
        apt = [i for i, c in enumerate(guest.commands) if "apt-get" in c]
        self.assertEqual(state["probes"], 3)
        self.assertTrue(apt and probes[-1] < apt[0], "apt ran before the network was online")
        self.assertIn("upgrade_network_online_seconds", ctx.observations)
        self.assertEqual(vm_acceptance.verdict_for(ctx)[0], "PASS", ctx.checks)

    def test_upgrade_without_network_is_blocked_and_never_runs_apt(self) -> None:
        handler, _state = self.upgrade_handler(online_after=None)
        guest = FakeGuest(self.root, handler)
        ctx = self.upgrade_context(guest, network_timeout=0.05)
        with self.assertRaises(Blocked) as caught:
            CASES["upgrade"].run(ctx)
        self.assertIn("did not come online", str(caught.exception))
        self.assertIn("apt was not run", str(caught.exception))
        self.assertFalse(any("apt-get" in c for c in guest.commands))
        self.assertTrue((self.root / "evidence" / "upgrade-network-wait.log").is_file())
        self.assertFalse(any(c["name"].startswith("the upgrade installs") for c in ctx.checks))

    # -- shadowcode-soak ----------------------------------------------------

    SESSION = {"user": "live", "uid": "1000", "display": "", "wayland_display": "wayland-0",
               "home": "/home/live"}

    def awake_handler(self, autolock: str, inhibited: bool):
        def handler(command: str):
            if "kreadconfig6" in command:
                return autolock
            if command.startswith("/usr/bin/systemd-inhibit --list"):
                return (f"WHO {self.cases.SOAK_INHIBIT_WHO} UID 1000 WHAT idle:sleep\n"
                        if inhibited else "0 inhibitors listed.\n")
            if "is-active " + self.cases.SOAK_INHIBIT_UNIT in command:
                return "active" if inhibited else "inactive"
            return ""
        return handler

    def test_soak_disables_the_screen_locker_and_holds_an_inhibitor(self) -> None:
        guest = FakeGuest(self.root, self.awake_handler("false", True))
        ctx = self.context("shadowcode-soak", guest)
        record = self.cases._hold_session_awake(ctx, guest, dict(self.SESSION))
        self.assertTrue(record["inhibitor_held"])
        self.assertEqual(record["screen_locker_autolock"], "false")
        setup = guest.commands[0]
        self.assertIn("kscreenlockerrc --group Daemon --key Autolock false", setup)
        self.assertIn("TurnOffDisplayWhenIdle false", setup)
        self.assertIn("systemd-inhibit --what=idle:sleep --mode=block", setup)
        self.assertEqual(ctx.observations["soak_session_awake"], record)
        self.assertTrue((self.root / "evidence" / "shadowcode-soak-awake.log").is_file())

    def test_soak_that_cannot_hold_the_session_awake_is_blocked(self) -> None:
        for autolock, inhibited in (("true", True), ("false", False)):
            with self.subTest(autolock=autolock, inhibited=inhibited):
                guest = FakeGuest(self.root, self.awake_handler(autolock, inhibited))
                ctx = self.context("shadowcode-soak", guest)
                with self.assertRaises(Blocked):
                    self.cases._hold_session_awake(ctx, guest, dict(self.SESSION))
                self.assertEqual(ctx.checks, [])

    def test_screen_locker_state_is_read_not_assumed(self) -> None:
        for reply, expected in (
            ({"exitcode": 0, "stdout": "   boolean true\n", "stderr": ""}, True),
            ({"exitcode": 0, "stdout": "   boolean false\n", "stderr": ""}, False),
            ({"exitcode": 1, "stdout": "", "stderr": "no such name"}, None),
        ):
            guest = FakeGuest(self.root, lambda _c, r=reply: r)
            self.assertIs(self.cases._screen_locked(guest, dict(self.SESSION)), expected)

    def test_the_soak_holds_the_session_awake_before_its_first_cycle(self) -> None:
        import inspect
        source = inspect.getsource(self.cases.case_shadowcode_soak)
        self.assertLess(source.index("_hold_session_awake("),
                        source.index("while time.monotonic() < deadline"))
        self.assertIn("_screen_locked(", source)
        self.assertIn("_release_session_awake(", source)


MIB = 1024
# MemAvailable after each close, KiB, from the 5.0.0 soak of ISO 2d8a72e0
# (work/qa-5.0.0/evidence/vm-acceptance/shadowcode-soak-20260930T204046Z-*).
# The 476 MiB step between closes 3 and 4 is PackageKit writing apt lists into
# the live overlay's RAM; ShadowCode's unit memory stayed flat across it.
# Closes 14 (21:00:36 UTC) and 15 (21:01:53), -68 and -87 MiB against the
# fit of the others and recovered at close 16, are the hourly
# apt-listchanges.timer run (OnCalendar=hourly, 21:00:00; ~2 min, 145 MB peak
# on the installed proof boot): a system job, not after-close noise.
SOAK_2D8A_HOURLY_JOB_CLOSES = (14, 15)
SOAK_2D8A_AFTER_CLOSE = [
    6274964, 6250740, 6185764, 5698732, 5709232, 5672980, 5653092, 5676564,
    5680008, 5658800, 5672648, 5666612, 5683308, 5598556, 5578144, 5669044,
    5659576, 5670608, 5648816, 5681960, 5658940, 5635984, 5655888, 5649292,
]


def jitter(cycle: int, amplitude_mib: int = 20) -> int:
    """Deterministic +-amplitude noise, in KiB."""
    return ((cycle * 7919) % (2 * amplitude_mib + 1) - amplitude_mib) * MIB


def plugin_file(width: int, height: int) -> str:
    """What tauri-plugin-window-state 2.4 writes: one object per window label."""
    return json.dumps({"main": {
        "width": width, "height": height, "x": 270, "y": 80, "prev_x": 270,
        "prev_y": 80, "maximized": False, "visible": True, "decorated": True,
        "fullscreen": False,
    }})


def windows_runner_reply(uuid: str, caption: str = "ShadowCode") -> str:
    """One match of KWin's WindowsRunner, as `dbus-send --print-reply` prints it."""
    return (
        "method return time=1790800392.800074 sender=:1.17 -> destination=:1.81 "
        "serial=498 reply_serial=2\n   array [\n      struct {\n"
        f'         string "0_{{{uuid}}}"\n         string "{caption}"\n'
        '         string "wayland"\n         int32 100\n         double 0.8\n'
        '         array [\n            dict entry(\n               string "subtext"\n'
        '               variant                   string "Activate running window on '
        'Desktop 1"\n            )\n         ]\n      }\n   ]\n'
    )


def window_info_reply(width: float, height: float, caption: str = "ShadowCode") -> str:
    """KWin's getWindowInfo a{sv}, as `dbus-send --print-reply` prints it (KWin 6: doubles)."""
    entries = [
        ("activities", "array [\n            ]"), ("caption", f'string "{caption}"'),
        ("desktopFile", 'string "shadowcode"'), ("fullscreen", "boolean false"),
        ("height", f"double {height:g}"), ("resourceClass", 'string "shadowcode"'),
        ("width", f"double {width:g}"), ("x", "double 0"), ("y", "double 0"),
    ]
    return ("method return time=1790800393.1 sender=:1.17 -> destination=:1.82 serial=499 "
            "reply_serial=2\n   array [\n" + "".join(
                f'      dict entry(\n         string "{key}"\n'
                f"         variant             {value}\n      )\n"
                for key, value in entries) + "   ]\n")


class SoakMeasurementTests(unittest.TestCase):
    """The shadowcode-soak memory metric and window check, on synthetic series.

    The first 5.0.0 soak failed ShadowCode for 680 MiB of "drift" that was a
    system job's one-time step plus a one-cycle dip, measured first close to
    lowest later close. These pin what the replacement does and does not
    charge: end medians and a slope, plus growth of the saved window size.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from acceptance import cases
        cls.cases = cases

    def drift(self, values):
        return self.cases._after_close_drift(values)

    def passes(self, drift, total_mib: float = 256, slope_mib: float = 8) -> bool:
        return drift["drop_mib"] <= total_mib and drift["slope_mib_per_cycle"] >= -slope_mib

    # -- memory drift ---------------------------------------------------------

    def test_a_flat_noisy_soak_passes(self) -> None:
        values = [6_000_000 + jitter(cycle) for cycle in range(1, 25)]
        drift = self.drift(values)
        self.assertEqual(drift["window"], 3)
        self.assertEqual((drift["head_cycles"], drift["tail_cycles"]), ([1, 2, 3], [22, 23, 24]))
        self.assertLess(abs(drift["drop_mib"]), 40)
        self.assertLess(abs(drift["slope_mib_per_cycle"]), 2)
        self.assertTrue(self.passes(drift))

    def test_a_one_cycle_dip_is_not_charged_as_drift(self) -> None:
        # 2d8a72e0 close 15: -118 MiB below close 1's neighbour, recovered 89
        # MiB at the next close. That one was the hourly apt-listchanges run,
        # which the soak now stops before its baseline; a dip of any origin
        # still must not decide the end-to-end drop on its own.
        values = [6_000_000 + jitter(cycle) for cycle in range(1, 25)]
        values[14] -= 118 * MIB
        old_metric_mib = (values[0] - min(values[1:])) / MIB
        self.assertGreater(old_metric_mib, 100, "the old metric charged the dip")
        drift = self.drift(values)
        self.assertTrue(self.passes(drift), drift)
        self.assertLess(drift["drop_mib"], 40)

    def test_first_launch_warm_up_is_not_a_leak(self) -> None:
        values = [6_000_000 - min(cycle - 1, 2) * 44 * MIB + jitter(cycle, 5)
                  for cycle in range(1, 25)]
        self.assertTrue(self.passes(self.drift(values)))

    def test_the_2d8a72e0_soak_without_its_packagekit_step_passes(self) -> None:
        step = SOAK_2D8A_AFTER_CLOSE[2] - SOAK_2D8A_AFTER_CLOSE[3]
        self.assertAlmostEqual(step / MIB, 475.6, places=1)
        without = SOAK_2D8A_AFTER_CLOSE[:3] + [v + step for v in SOAK_2D8A_AFTER_CLOSE[3:]]
        drift = self.drift(without)
        self.assertTrue(self.passes(drift), drift)
        self.assertAlmostEqual(drift["drop_mib"], 111.7, places=1)
        self.assertAlmostEqual(drift["slope_mib_per_cycle"], -2.03, places=2)
        # From close 4 on (after the step) the slope is noise.
        self.assertTrue(self.passes(self.drift(SOAK_2D8A_AFTER_CLOSE[3:])))

    def test_the_slope_starts_after_the_first_launches_settle(self) -> None:
        # Closes 1 and 2 sit above the rest in every 5.0.0 soak: 2d8a72e0 +87
        # and +63 MiB over close 3, dfea3c9b +89 and +57, the diagnostic rerun
        # +33 and +30, as plasmashell and KWin settle around the new window. A
        # one-off, not a per-launch loss -- fitted into the slope it doubled
        # the 2d8a72e0 "leak" and decided short soaks on its own.
        step = SOAK_2D8A_AFTER_CLOSE[2] - SOAK_2D8A_AFTER_CLOSE[3]
        without = SOAK_2D8A_AFTER_CLOSE[:3] + [v + step for v in SOAK_2D8A_AFTER_CLOSE[3:]]
        drift = self.drift(without)
        self.assertEqual(self.cases.SOAK_SLOPE_FROM_CLOSE, 3)
        self.assertEqual((drift["slope_cycles"], drift["slope_readings"]), ([3, 24], 22))
        self.assertAlmostEqual(drift["slope_mib_per_cycle"], -2.03, places=2)
        self.assertLess(drift["slope_stderr_mib_per_cycle"], 1.0)
        # The end-to-end drop still counts the warm-up: it is memory gone.
        self.assertAlmostEqual(drift["drop_mib"], 111.7, places=1)

    def test_measured_noise_neither_fails_a_healthy_soak_nor_hides_a_leak(self) -> None:
        """Seeded simulation on the noise the 2d8a72e0 soak actually had.

        Residuals of its closes after the PackageKit step are resampled,
        WITHOUT closes 14 and 15: those were the hourly apt-listchanges run
        (SOAK_2D8A_HOURLY_JOB_CLOSES), which the soak now stops before its
        baseline, not after-close noise (sd 13 MiB without them, 26 with).
        The harsher model adds a 90-120 MiB one-close dip at 10% of closes,
        for noise that soak did not show. Every soak also gets a first-launch
        warm-up (+30..90 and +0..68 MiB at closes 1 and 2) and a background
        drift of 0 to -2.5 MiB a cycle, as measured. At the shortest soak the
        slope is judged on and at the default length, a healthy app must not
        fail and one losing 15 MiB a launch must not pass; on the measured
        noise one losing 10 MiB a launch must not pass either.
        """
        import random
        kept = [(cycle, value / MIB) for cycle, value in enumerate(SOAK_2D8A_AFTER_CLOSE, 1)
                if cycle >= 4 and cycle not in SOAK_2D8A_HOURLY_JOB_CLOSES]
        mean_x = sum(x for x, _ in kept) / len(kept)
        mean_y = sum(y for _, y in kept) / len(kept)
        slope = (sum((x - mean_x) * (y - mean_y) for x, y in kept)
                 / sum((x - mean_x) ** 2 for x, _ in kept))
        residuals = [y - mean_y - slope * (x - mean_x) for x, y in kept]
        self.assertLess(max(abs(r) for r in residuals), 30, "a system job is in the model")
        rng = random.Random(501)
        trials = 300
        for closes in (self.cases.SOAK_MIN_CLOSES, 24):
            for harsh in (False, True):
                false_fail = missed = missed_10 = 0
                for _ in range(trials):
                    first = rng.uniform(30, 90)
                    warm = {1: first, 2: rng.uniform(0, 0.75 * first)}
                    background = rng.uniform(-2.5, 0)
                    noise = [rng.choice(residuals)
                             - (rng.uniform(90, 120) if harsh and rng.random() < 0.1 else 0)
                             for _ in range(closes)]
                    def series(leak: float) -> list[int]:
                        return [round((6000 + warm.get(c, 0) + (background - leak) * c
                                       + noise[c - 1]) * MIB) for c in range(1, closes + 1)]
                    false_fail += not self.passes(self.drift(series(0)))
                    missed += self.passes(self.drift(series(15)))
                    missed_10 += self.passes(self.drift(series(10)))
                with self.subTest(closes=closes, harsh=harsh):
                    self.assertLessEqual(false_fail, trials // 100, "healthy soaks failed")
                    self.assertLessEqual(missed, trials // 100, "15 MiB/cycle leaks passed")
                    if not harsh:
                        self.assertLessEqual(missed_10, trials // 100,
                                             "10 MiB/cycle leaks passed")

    def test_a_system_job_early_in_the_soak_hides_a_leak(self) -> None:
        # Why the timers are stopped rather than modelled as noise: the
        # 2d8a72e0 hourly pair (-70, -90 MiB) at closes 3 and 4 of an
        # 18-close soak lifts a 10 MiB/cycle leak's slope above -8.
        values = [6_000_000 - 10 * MIB * cycle + jitter(cycle, 13) for cycle in range(1, 19)]
        self.assertFalse(self.passes(self.drift(values)))
        values[2] -= 70 * MIB
        values[3] -= 90 * MIB
        drift = self.drift(values)
        self.assertGreater(drift["slope_mib_per_cycle"], -8)
        self.assertTrue(self.passes(drift), "the job did not hide the leak")

    def test_a_step_inside_the_soak_still_fails(self) -> None:
        # The metric does not explain a step away; quiescing PackageKit before
        # the baseline is what keeps that one out of the cycles.
        drift = self.drift(SOAK_2D8A_AFTER_CLOSE)
        self.assertAlmostEqual(drift["drop_mib"], 587.4, places=1)
        self.assertFalse(self.passes(drift))

    def test_a_steady_leak_under_the_total_limit_fails_on_its_slope(self) -> None:
        values = [6_000_000 - cycle * 10 * MIB + jitter(cycle, 5) for cycle in range(1, 25)]
        drift = self.drift(values)
        self.assertLess(drift["drop_mib"], 256, "the end medians alone would pass it")
        self.assertLess(drift["slope_mib_per_cycle"], -8)
        self.assertFalse(self.passes(drift))

    def test_short_soaks_never_share_a_reading_between_the_ends(self) -> None:
        self.assertIsNone(self.drift([]))
        self.assertIsNone(self.drift([6_000_000]))
        three = self.drift([6_000_000, 5_990_000, 5_980_000])
        self.assertEqual((three["window"], three["head_cycles"], three["tail_cycles"]),
                         (1, [1], [3]))
        five = self.drift([6_000_000] * 5)
        self.assertEqual((five["window"], five["head_cycles"], five["tail_cycles"]),
                         (2, [1, 2], [4, 5]))

    def test_missing_readings_are_skipped_and_keep_their_cycle_numbers(self) -> None:
        drift = self.cases._after_close_drift([6_000_000, -1, None, 6_000_000 - 3 * 8 * MIB],
                                              slope_from=1)
        self.assertEqual(drift["readings"], 2)
        self.assertEqual((drift["head_cycles"], drift["tail_cycles"]), ([1], [4]))
        self.assertAlmostEqual(drift["slope_mib_per_cycle"], -8.0)

    # -- saved window size ------------------------------------------------------

    def sizes(self, *pairs):
        return [None if pair is None else {"main": list(pair)} for pair in pairs]

    def test_the_plugin_file_is_read_per_window_label(self) -> None:
        parse = self.cases._parse_window_state
        self.assertEqual(parse(plugin_file(1432, 1019)), {"main": [1432, 1019]})
        for text in ("", "not json", "[]", "{}", '{"main": {"width": "wide"}}'):
            self.assertIsNone(parse(text), text)

    def test_a_0x0_entry_is_no_saved_size(self) -> None:
        # ShadowCode 1.0.1 leaves SIZE out of the plugin's flags and the
        # plugin keeps a fresh entry at 0x0. That was read as a size, compared
        # equal at every close and passed a check that had measured nothing.
        parse = self.cases._parse_window_state
        for width, height in ((0, 0), (0, 920), (1380, 0), (-1, 920)):
            self.assertIsNone(parse(plugin_file(width, height)), (width, height))
        both = json.dumps({"main": json.loads(plugin_file(0, 0))["main"],
                           "settings": json.loads(plugin_file(640, 480))["main"]})
        self.assertEqual(parse(both), {"settings": [640, 480]})

    def test_shadowcode_1_0_0_growth_fails(self) -> None:
        # +52 px wide, +99 px tall at every launch on Plasma Wayland.
        closes = self.sizes(*[(1432 + 52 * n, 1019 + 99 * n) for n in range(6)])
        growth = self.cases._window_growth(closes)
        self.assertEqual(growth["longest_run"], 5)
        self.assertEqual(growth["grew_at_closes"], [2, 3, 4, 5, 6])
        self.assertGreaterEqual(growth["longest_run"], self.cases.SOAK_WINDOW_GROWTH_RUN)

    def test_a_size_that_does_not_move_passes(self) -> None:
        growth = self.cases._window_growth(self.sizes(*[(1380, 920)] * 6))
        self.assertEqual((growth["compared"], growth["longest_run"]), (5, 0))

    def test_a_0x0_size_is_never_compared(self) -> None:
        # No size is not a size that held: nothing was measured.
        growth = self.cases._window_growth(self.sizes(*[(0, 0)] * 6))
        self.assertEqual((growth["compared"], growth["longest_run"]), (0, 0))
        self.assertEqual(growth["sizes"][0], "main=0x0")

    def test_kwin_window_ids_and_frame_size_are_read_from_dbus_send(self) -> None:
        match = windows_runner_reply("f00288e7-48eb-4725-abb2-e6732144c378")
        match += windows_runner_reply("0d1e2f3a-4b5c-4d6e-8f90-a1b2c3d4e5f6", caption="Dolphin")
        self.assertEqual(self.cases._window_ids(match),
                         ["{f00288e7-48eb-4725-abb2-e6732144c378}"])
        info = self.cases._dbus_scalars(window_info_reply(1484, 1071))
        self.assertEqual((info["width"], info["height"]), (1484.0, 1071.0))
        self.assertEqual(info["caption"], "ShadowCode")
        self.assertIs(info["fullscreen"], False)
        self.assertEqual(self.cases._window_ids('   array [\n      string "ShadowCode"\n   ]\n'),
                         [])

    def test_one_increase_is_not_a_run_but_height_alone_is(self) -> None:
        once = self.cases._window_growth(self.sizes((1380, 920), (1432, 1019), (1432, 1019)))
        self.assertEqual(once["longest_run"], 1)
        taller = self.cases._window_growth(self.sizes((1380, 920), (1380, 1019), (1380, 1118)))
        self.assertEqual(taller["longest_run"], 2)

    def test_a_missing_reading_breaks_a_run_instead_of_bridging_it(self) -> None:
        growth = self.cases._window_growth(
            self.sizes((1380, 920), (1432, 1019), None, (1536, 1217), (1536, 1217)))
        self.assertEqual((growth["compared"], growth["longest_run"]), (2, 1))
        self.assertEqual(growth["sizes"][2], None)
        self.assertEqual(growth["sizes"][0], "main=1380x920")

    def test_no_readings_make_no_comparison(self) -> None:
        growth = self.cases._window_growth([None, None, None])
        self.assertEqual((growth["compared"], growth["longest_run"]), (0, 0))


class FakeClock:
    """cases.time for a whole soak: sleep advances a clock instead of waiting."""

    def __init__(self) -> None:
        import time as real
        self._real = real
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def __getattr__(self, name: str):
        return getattr(self._real, name)


class SoakGuest:
    """A live session running ShadowCode, answering what shadowcode-soak asks.

    `saved` gives the window size the app writes at its Nth exit (None: no
    file), `frame` the size KWin shows for the window of its Nth launch (None:
    getWindowInfo gives nothing), `available` the MemAvailable KiB after N
    closes, `stop_result` the unit's Result after the Nth close. The update
    notifier starts `notifier`; PackageKit answers `packagekit` in turn (its
    last state repeats) with `transactions` running, and goes inactive when
    stopped unless `packagekit_stops` is False. `timers` and `user_timers` are
    the active timers of each manager ({timer: service it triggers}); they
    stop unless `timers_stop` is False, and `running_jobs` are services a
    timer had already started. `journal` is what PID 1 logged during the soak.
    """

    TIMERS = {"apt-listchanges.timer": "apt-listchanges.service",
              "fwupd-refresh.timer": "fwupd-refresh.service",
              "systemd-tmpfiles-clean.timer": "systemd-tmpfiles-clean.service"}
    USER_TIMERS = {"systemd-tmpfiles-clean.timer": "systemd-tmpfiles-clean.service"}

    def __init__(self, pin: dict, saved, *, notifier: str = "active",
                 packagekit=("inactive",), transactions: int = 0,
                 packagekit_stops: bool = True, available=lambda _closes: 6000000,
                 frame=lambda _launch: (1380, 920), stop_result=lambda _close: "success",
                 timers=None, user_timers=None, timers_stop: bool = True,
                 timer_listing_fails: bool = False, running_jobs=(),
                 journal: str = "") -> None:
        self.pin = pin
        self.saved = saved
        self.frame = frame
        self.stop_result = stop_result
        self.notifier = notifier
        self.packagekit = list(packagekit)
        self.transactions = transactions
        self.packagekit_stops = packagekit_stops
        self.available = available
        self.closes = 0
        self.launches = 0
        self.stopped: set[str] = set()
        self.log: list[str] = []
        self.timers = {"system": dict(self.TIMERS if timers is None else timers),
                       "user": dict(self.USER_TIMERS if user_timers is None else user_timers)}
        self.armed = {scope: set(found) for scope, found in self.timers.items()}
        self.running = set(running_jobs)
        self.timers_stop = timers_stop
        self.timer_listing_fails = timer_listing_fails
        self.journal = journal

    def unit_state(self, scope: str, unit: str) -> str:
        if unit.endswith(".timer"):
            return "active" if unit in self.armed[scope] else "inactive"
        return "activating" if unit in self.running else "inactive"

    def systemctl(self, scope: str, command: str):
        """list-units/show/stop for timers and the jobs they trigger."""
        import shlex as _shlex
        inner = command[command.index("/usr/bin/systemctl"):]  # past runuser's wrapper
        words = _shlex.split(inner.split(" 2>", 1)[0])
        verb = next(word for word in words if word in ("list-units", "show", "stop"))
        units = [word for word in words[words.index(verb) + 1:] if not word.startswith("-")]
        if verb == "list-units":
            self.log.append(f"{scope}-timers-listed")
            if self.timer_listing_fails:
                return {"exitcode": 1, "stdout": "", "stderr": "Failed to connect to bus"}
            return "".join(f"{timer} loaded active waiting Some timer\n"
                           for timer in sorted(self.armed[scope]))
        if verb == "show":
            return "\n".join(
                f"Id={unit}\nLoadState={'loaded' if unit in self.timers[scope] or unit in self.timers[scope].values() else 'not-found'}\n"
                f"ActiveState={self.unit_state(scope, unit)}\nSubState=x\n"
                f"Triggers={self.timers[scope].get(unit, '')}\n"
                for unit in units
            )
        for unit in units:
            self.log.append(f"{scope}-stop:{unit}")
            if self.timers_stop:
                self.armed[scope].discard(unit)
                self.running.discard(unit)
        return ""

    def __call__(self, command: str):
        import re
        pin = self.pin
        if ".window-state.json" in command:
            self.log.append("window-state")
            size = self.saved(self.closes)
            return "" if size is None else plugin_file(*size)
        if "/proc/meminfo" in command:
            self.log.append("meminfo")
            return (f"MemAvailable: {self.available(self.closes)}\n"
                    "Shmem: 200000\nAnonPages: 900000")
        if "journalctl" in command and "Start(ing|ed)" in command:
            return self.journal if "_PID=1" in command else ""
        timer_verb = r"(list-units --type=timer|(show|stop) .*\.(timer|service))"
        if re.search(r"systemctl --user " + timer_verb, command) \
                and "sf-acceptance" not in command and "discover" not in command:
            return self.systemctl("user", command)
        if re.search(r"^/usr/bin/systemctl " + timer_verb, command) \
                and "packagekit" not in command:
            return self.systemctl("system", command)
        if "for p in $(pgrep -x plasmashell)" in command:
            return "live\t1000\t\twayland-0\n"
        if "getent passwd" in command:
            return "/home/live"
        if "dpkg-query" in command:
            return f"{pin['version']}\tii "
        if "echo present" in command:
            return "present"
        if command.startswith("/usr/bin/cat " + pin["desktop_file"]):
            return "[Desktop Entry]\nName=ShadowCode\nExec=shadowcode %U\n"
        if f"{pin['launcher']} --version" in command:
            return f"ShadowCode {pin['version']}"
        if "llama-server --version" in command or "llama-cli --version" in command:
            return f"version: 1\ncommit {pin['runtime_commit']}"
        if "WindowsRunner" in command:
            if self.launches == 0:
                return "   array [\n   ]\n"
            return windows_runner_reply(f"00000000-0000-4000-8000-{self.launches:012d}")
        if "getWindowInfo" in command:
            size = self.frame(self.launches)
            if size is None:
                return {"exitcode": 1, "stdout": "",
                        "stderr": "Error org.freedesktop.DBus.Error.UnknownMethod"}
            return window_info_reply(*size)
        if "kreadconfig6" in command:
            return "false"
        if command.startswith("/usr/bin/systemd-inhibit --list"):
            return "WHO shadowfetch-vm-acceptance UID 1000 WHAT idle:sleep\n"
        if "is-active sf-acceptance-soak-inhibit" in command:
            return "active"
        notifier = "app-org.kde.discover.notifier@autostart.service"
        if f"systemctl --user show {notifier}" in command:
            self.log.append(f"notifier-show:{self.notifier}")
            return (f"LoadState=loaded\nActiveState={self.notifier}\nSubState=running\n"
                    "ConditionResult=yes\nDropInPaths=")
        if f"systemctl --user stop {notifier}" in command:
            self.log.append("notifier-stop")
            self.notifier = "inactive"
            return ""
        if "systemctl stop packagekit.service" in command:
            self.log.append("packagekit-stop")
            if self.packagekit_stops:
                self.packagekit = ["inactive"]
            return ""
        if "is-active packagekit.service" in command:
            state = self.packagekit.pop(0) if len(self.packagekit) > 1 else self.packagekit[0]
            self.log.append(f"packagekit:{state}")
            return state
        if "GetTransactionList" in command:
            return f"ao {self.transactions}" + ' "/1_x"' * self.transactions
        if "date +%s" in command:
            self.log.append("since")
            return "1790000000"
        match = re.search(r"systemctl --user show (sf-acceptance-shadowcode-\d+)", command)
        if match:
            if match.group(1) in self.stopped:
                result = self.stop_result(self.closes)
                return (f"ActiveState={'failed' if result != 'success' else 'inactive'}\n"
                        f"SubState=dead\nResult={result}\nMainPID=0\n")
            return ("LoadState=loaded\nActiveState=active\nSubState=running\nResult=success\n"
                    "MainPID=4242\nNRestarts=0\nMemoryCurrent=180000000\n"
                    "MemoryPeak=220000000\nCPUUsageNSec=900000000\n")
        match = re.search(r"systemctl --user stop (sf-acceptance-shadowcode-\d+)", command)
        if match:
            self.stopped.add(match.group(1))
            self.closes += 1
            return ""
        if "systemd-run --user --unit=sf-acceptance-shadowcode-" in command:
            self.launches += 1
            return ""
        if "ScreenSaver.GetActive" in command:
            return "   boolean false\n"
        return ""


class SoakCaseTests(unittest.TestCase):
    """shadowcode-soak end to end against a fake live session.

    A fake clock makes the cycle count exact: twenty minutes at the default
    60s hold is eighteen open/close cycles, the fewest the slope is judged on
    (SOAK_MIN_CLOSES).
    """

    SAVED = "the window size ShadowCode saves does not grow"
    FRAME = "the ShadowCode window KWin shows does not grow"

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "unit.iso").write_bytes(b"not really an iso")
        from acceptance import cases
        self.cases = cases
        clock = unittest.mock.patch.object(cases, "time", FakeClock())
        clock.start()
        self.addCleanup(clock.stop)
        probe = self.context(FakeGuest(self.root, lambda _c: ""))
        self.pin = cases._shadowcode_pin(probe)

    def context(self, guest, **options) -> Context:
        ctx = Context(
            name="shadowcode-soak",
            repo_root=REPO_ROOT,
            run_dir=self.root / "run",
            evidence=EvidenceSet(REPO_ROOT, self.root / "evidence"),
            artifact={"path": str(self.root / "unit.iso"), "sha256": "b" * 64},
            options={"version": "5.0.0", "desktop_settle": 0, "soak_minutes": 20, **options},
        )
        ctx.guest = lambda *_a, **_k: guest  # type: ignore[method-assign]
        return ctx

    def soak(self, session: SoakGuest, **options) -> Context:
        ctx = self.context(FakeGuest(self.root, session), **options)
        CASES["shadowcode-soak"].run(ctx)
        return ctx

    def blocked(self, session: SoakGuest, **options) -> tuple[Context, str]:
        ctx = self.context(FakeGuest(self.root, session), **options)
        with self.assertRaises(Blocked) as caught:
            CASES["shadowcode-soak"].run(ctx)
        return ctx, str(caught.exception)

    def check(self, ctx: Context, prefix: str) -> dict:
        found = [c for c in ctx.checks if c["name"].startswith(prefix)]
        self.assertEqual(len(found), 1, [c["name"] for c in ctx.checks])
        return found[0]

    def cycles(self) -> dict:
        return json.loads((self.root / "evidence" / "shadowcode-soak-cycles.json").read_text())

    # -- window size ------------------------------------------------------------

    def test_a_window_that_grows_at_every_launch_fails_the_soak(self) -> None:
        # ShadowCode 1.0.0: the file appears at the first exit and grows at
        # each, and the window KWin shows grows with it.
        ctx = self.soak(SoakGuest(self.pin, lambda n: (1380 + 52 * n, 920 + 99 * n),
                                  frame=lambda n: (1380 + 52 * n, 920 + 99 * n)))
        self.assertEqual(ctx.observations["soak_cycles"], self.cases.SOAK_MIN_CLOSES)
        saved = self.check(ctx, self.SAVED)
        self.assertEqual(saved["state"], "FAILED", saved)
        self.assertIn(f"grew at closes {list(range(2, 19))}", saved["detail"])
        frame = self.check(ctx, self.FRAME)
        self.assertEqual(frame["state"], "FAILED", frame)
        self.assertIn(f"grew at launches {list(range(2, 19))}", frame["detail"])
        self.assertEqual(vm_acceptance.verdict_for(ctx)[0], "FAIL")

    def test_shadowcode_1_0_1_is_judged_on_the_kwin_frame_not_its_0x0_file(self) -> None:
        # 1.0.1: SIZE is not a saved flag; the file holds 0x0, which is no
        # size. The window KWin shows holds its size, and that is the pass.
        ctx = self.soak(SoakGuest(self.pin, lambda n: (0, 0) if n else None))
        self.assertFalse([c for c in ctx.checks if c["name"].startswith(self.SAVED)])
        self.assertIn("window_state_unobserved", ctx.observations)
        frame = self.check(ctx, self.FRAME)
        self.assertEqual(frame["state"], "PASSED", frame)
        self.assertIn("frame=1380x920", frame["detail"])
        for prefix in ("available memory after close does not drift",
                       "available memory after close does not fall"):
            self.assertEqual(self.check(ctx, prefix)["state"], "PASSED")
        self.assertEqual(vm_acceptance.verdict_for(ctx)[0], "PASS", ctx.checks)
        cycles = self.cycles()
        close = cycles["cycles"][0]
        self.assertEqual((close["shmem_after_close_kib"], close["anon_pages_after_close_kib"]),
                         (200000, 900000))
        self.assertEqual(close["samples"][0]["shmem_kib"], 200000)
        self.assertIsNone(close["window_state"])
        self.assertIn('"width": 0', close["window_state_without_size"])
        self.assertEqual(close["window_frame"], {"frame": [1380, 920]})
        self.assertEqual(cycles["baseline_meminfo_kib"],
                         {"MemAvailable": 6000000, "Shmem": 200000, "AnonPages": 900000})

    def test_a_0x0_state_file_is_not_recorded_as_a_pass(self) -> None:
        # Without KWin's frame, 1.0.1's 0x0 file measures nothing: no window
        # check passes, and a soak that measured no window size is BLOCKED.
        ctx, reason = self.blocked(SoakGuest(self.pin, lambda n: (0, 0), frame=lambda n: None))
        self.assertFalse([c for c in ctx.checks if "window" in c["name"]
                          and "grow" in c["name"]], ctx.checks)
        self.assertIn("window size was measured at no two consecutive launches", reason)
        self.assertIn("window_state_unobserved", ctx.observations)
        self.assertIn("window_frame_unobserved", ctx.observations)
        self.assertIn("UnknownMethod", self.cycles()["cycles"][0]["window_frame_unread"])

    def test_no_window_state_file_is_judged_on_the_frame_alone(self) -> None:
        ctx = self.soak(SoakGuest(self.pin, lambda _n: None))
        self.assertFalse([c for c in ctx.checks if c["name"].startswith(self.SAVED)])
        self.assertIn("window_state_unobserved", ctx.observations)
        self.assertEqual(self.check(ctx, self.FRAME)["state"], "PASSED")

    def test_a_saved_size_alone_still_judges_the_window(self) -> None:
        ctx = self.soak(SoakGuest(self.pin, lambda n: (1380 + 52 * n, 920 + 99 * n),
                                  frame=lambda n: None))
        self.assertEqual(self.check(ctx, self.SAVED)["state"], "FAILED")
        self.assertIn("window_frame_unobserved", ctx.observations)
        self.assertEqual(vm_acceptance.verdict_for(ctx)[0], "FAIL")

    # -- quiescing --------------------------------------------------------------

    def test_the_notifier_is_stopped_and_packagekit_idle_before_the_baseline(self) -> None:
        session = SoakGuest(self.pin, lambda n: (1380, 920),
                            packagekit=("active", "active", "inactive"), transactions=1)
        ctx = self.soak(session)
        log = session.log
        self.assertIn("notifier-stop", log)
        baseline = log.index("since")
        self.assertLess(log.index("notifier-stop"), baseline)
        self.assertLess(max(i for i, e in enumerate(log) if e.startswith("packagekit:")),
                        baseline)
        record = ctx.observations["soak_quiesce"]
        self.assertEqual(record["notifier_before"]["ActiveState"], "active")
        self.assertEqual(record["notifier_after"]["ActiveState"], "inactive")
        self.assertTrue(record["packagekit_idle"])
        self.assertEqual(record["packagekit_busy_polls"], 2)
        self.assertTrue((self.root / "evidence" / "shadowcode-soak-quiesce.log").is_file())

    def test_a_notifier_that_is_not_running_is_left_alone(self) -> None:
        session = SoakGuest(self.pin, lambda n: (1380, 920), notifier="inactive")
        ctx = self.soak(session)
        self.assertNotIn("notifier-stop", session.log)
        self.assertIsNone(ctx.observations["soak_quiesce"]["notifier_stop"])

    def test_packagekit_that_never_goes_idle_blocks_the_soak(self) -> None:
        session = SoakGuest(self.pin, lambda n: (1380, 920),
                            packagekit=("active",), transactions=1)
        ctx, reason = self.blocked(session, soak_quiesce_timeout=60)
        self.assertIn("packagekitd did not go idle", reason)
        self.assertNotIn("since", session.log, "the soak started anyway")
        self.assertFalse(ctx.observations["soak_quiesce"]["packagekit_idle"])

    def test_an_idle_packagekit_is_stopped_before_the_baseline(self) -> None:
        # packagekitd stays up for its idle timeout (~300 s) after the last
        # transaction -- 2d8a72e0's diagnostic rerun: refresh done at 367 s,
        # "daemon quit" at 674 s, its 39 MB RSS gone with it. Left running, that
        # exit lands inside the cycles as memory given BACK, which hides a leak
        # of the same size from the slope.
        session = SoakGuest(self.pin, lambda n: (1380, 920), packagekit=("active",))
        ctx = self.soak(session)
        log = session.log
        self.assertIn("packagekit-stop", log)
        self.assertLess(log.index("packagekit-stop"), log.index("since"))
        record = ctx.observations["soak_quiesce"]
        self.assertEqual(record["packagekit_stop"]["state_after"], "inactive")
        self.assertEqual(vm_acceptance.verdict_for(ctx)[0], "PASS", ctx.checks)

    def test_a_packagekit_that_will_not_stop_blocks_the_soak(self) -> None:
        session = SoakGuest(self.pin, lambda n: (1380, 920), packagekit=("active",),
                            packagekit_stops=False)
        _ctx, reason = self.blocked(session)
        self.assertIn("packagekitd is still 'active'", reason)
        self.assertNotIn("since", session.log, "the soak started anyway")

    def test_apt_listchanges_timer_is_stopped_before_the_baseline(self) -> None:
        # The hourly apt-listchanges run took 68 and 87 MiB out of closes 14
        # and 15 of the 2d8a72e0 soak. Every active timer, system and user,
        # is stopped before the baseline, and a job one already started
        # (here apt-listchanges itself, mid-run) is stopped with it.
        session = SoakGuest(self.pin, lambda n: (1380, 920),
                            running_jobs=("apt-listchanges.service",))
        ctx = self.soak(session)
        log = session.log
        baseline = log.index("since")
        for entry in ("system-stop:apt-listchanges.timer", "system-stop:fwupd-refresh.timer",
                      "system-stop:systemd-tmpfiles-clean.timer",
                      "system-stop:apt-listchanges.service",
                      "user-stop:systemd-tmpfiles-clean.timer"):
            self.assertIn(entry, log)
            self.assertLess(log.index(entry), baseline, entry)
        timers = ctx.observations["soak_quiesce"]["timers"]
        self.assertTrue(timers["quiet"])
        self.assertEqual(timers["system"]["stopped"], sorted(SoakGuest.TIMERS))
        self.assertEqual(timers["system"]["jobs_stopped"]["jobs"], ["apt-listchanges.service"])
        self.assertEqual(timers["system"]["after"]["apt-listchanges.timer"], "inactive")
        self.assertEqual(timers["user"]["stopped"], ["systemd-tmpfiles-clean.timer"])
        self.assertEqual(self.cycles()["quiesce"]["timers"], timers)
        self.assertTrue((self.root / "evidence" / "shadowcode-soak-timers.log").is_file())
        self.assertEqual(vm_acceptance.verdict_for(ctx)[0], "PASS", ctx.checks)

    def test_a_named_timer_the_listing_missed_is_still_stopped(self) -> None:
        session = SoakGuest(self.pin, lambda n: (1380, 920))
        session.armed["system"].add("man-db.timer")
        session.timers["system"]["man-db.timer"] = "man-db.service"
        listed = session.systemctl
        session.systemctl = lambda scope, command: (
            "" if "list-units" in command else listed(scope, command))
        ctx = self.soak(session)
        self.assertIn("system-stop:man-db.timer", session.log)
        self.assertTrue(ctx.observations["soak_quiesce"]["timers"]["quiet"])

    def test_a_timer_that_will_not_stop_blocks_the_soak(self) -> None:
        session = SoakGuest(self.pin, lambda n: (1380, 920), timers_stop=False)
        _ctx, reason = self.blocked(session)
        self.assertIn("system timers still armed after being stopped: apt-listchanges.timer",
                      reason)
        self.assertNotIn("since", session.log, "the soak started anyway")

    def test_a_timer_listing_that_fails_blocks_the_soak(self) -> None:
        session = SoakGuest(self.pin, lambda n: (1380, 920), timer_listing_fails=True)
        _ctx, reason = self.blocked(session)
        self.assertIn("active timers could not be listed", reason)
        self.assertNotIn("since", session.log, "the soak started anyway")

    def test_units_started_during_the_soak_are_tied_to_the_close_they_precede(self) -> None:
        journal = ("1790000100.250000 shadowfetch systemd[1]: Starting "
                   "apt-listchanges.service - Initialize apt-listchanges database for APT...\n"
                   "1790000001.0 shadowfetch systemd[1]: Started "
                   "sf-acceptance-shadowcode-1.service - /usr/bin/shadowcode.\n")
        ctx = self.soak(SoakGuest(self.pin, lambda n: (1380, 920), journal=journal))
        cycles = self.cycles()
        expected = next(c["cycle"] for c in cycles["cycles"]
                        if c["closed_seconds_after_baseline"] >= 100.25)
        self.assertGreater(expected, 1)
        self.assertEqual(ctx.observations["soak_units_started"],
                         [f"apt-listchanges.service (system, before close {expected})"])
        self.assertEqual(cycles["units_started"][0]["before_close"], expected)
        self.assertTrue((self.root / "evidence" / "shadowcode-soak-units-started.log").is_file())

    # -- soak length ------------------------------------------------------------

    def test_a_short_soak_minutes_runs_on_to_the_floor(self) -> None:
        session = SoakGuest(self.pin, lambda n: (1380, 920))
        ctx = self.soak(session, soak_minutes=6)
        run = ctx.observations["soak_run"]
        self.assertEqual(run["closes"], self.cases.SOAK_MIN_CLOSES)
        self.assertGreater(run["closes_after_deadline"], 0)
        self.assertEqual(self.check(ctx, "available memory after close does not fall")["state"],
                         "PASSED")
        self.assertEqual(vm_acceptance.verdict_for(ctx)[0], "PASS", ctx.checks)

    def test_a_clean_soak_capped_short_of_the_floor_is_blocked(self) -> None:
        # Six closes of a healthy app: the first-launch warm-up alone made the
        # slope -17.8 MiB a cycle, and the noise is +-13 MiB a close or more.
        # Fewer than SOAK_MIN_CLOSES cannot tell an 8 MiB/cycle loss from
        # either -- but every other check is still made.
        warm = {1: 87 * MIB, 2: 63 * MIB}
        session = SoakGuest(self.pin, lambda n: (0, 0),
                            available=lambda n: 6000000 + warm.get(n, 0))
        ctx, reason = self.blocked(session, soak_minutes=6, soak_max_minutes=6)
        self.assertIn(f"at least {self.cases.SOAK_MIN_CLOSES}", reason)
        self.assertIn("--soak-max-minutes 6", reason)
        self.assertEqual(ctx.observations["soak_cycles"], 6)
        self.assertIn("memory_slope_unjudged", ctx.observations)
        self.assertFalse([c for c in ctx.checks if "per cycle" in c["name"]])
        for prefix in ("every close was clean", "available memory after close does not drift",
                       self.FRAME, "no ShadowCode crash"):
            self.assertEqual(self.check(ctx, prefix)["state"], "PASSED", prefix)
        self.assertTrue((self.root / "evidence" / "shadowcode-soak-cycles.json").is_file())

    def test_a_short_soak_still_fails_on_what_it_measured(self) -> None:
        # What the 18-close floor exists for is the slope. A growing window, a
        # SIGKILLed close and a drop three times the end-to-end limit are
        # facts at any length, and a short soak that saw them FAILS.
        failures = {
            "the 1.0.0 window": (
                dict(saved=lambda n: (1380 + 52 * n, 920 + 99 * n),
                     frame=lambda n: (1380 + 52 * n, 920 + 99 * n)), self.FRAME),
            "every close SIGKILLed": (
                dict(saved=lambda n: (1380, 920), stop_result=lambda n: "signal"),
                "every close was clean"),
            "100 MiB lost a cycle": (
                dict(saved=lambda n: (1380, 920),
                     available=lambda n: 6000000 - 100 * MIB * n),
                "available memory after close does not drift"),
        }
        for label, (kwargs, failing) in failures.items():
            for minutes in (6, 15):
                with self.subTest(label, minutes=minutes):
                    saved = kwargs["saved"]
                    rest = {k: v for k, v in kwargs.items() if k != "saved"}
                    ctx = self.soak(SoakGuest(self.pin, saved, **rest),
                                    soak_minutes=minutes, soak_max_minutes=minutes)
                    self.assertLess(ctx.observations["soak_cycles"], self.cases.SOAK_MIN_CLOSES)
                    self.assertEqual(self.check(ctx, failing)["state"], "FAILED")
                    self.assertIn("memory_slope_unjudged", ctx.observations)
                    self.assertEqual(vm_acceptance.verdict_for(ctx)[0], "FAIL")

    def test_a_shortest_judged_soak_with_first_launch_warm_up_passes(self) -> None:
        warm = {1: 87 * MIB, 2: 63 * MIB}
        session = SoakGuest(self.pin, lambda n: (0, 0),
                            available=lambda n: 6000000 + warm.get(n, 0) - 2 * MIB * n)
        ctx = self.soak(session)
        slope = self.check(ctx, "available memory after close does not fall")
        self.assertEqual(slope["state"], "PASSED", slope)
        self.assertIn("from close 3", slope["detail"])
        self.assertEqual(vm_acceptance.verdict_for(ctx)[0], "PASS", ctx.checks)

    def test_a_15_mib_per_launch_loss_fails_the_shortest_judged_soak(self) -> None:
        session = SoakGuest(self.pin, lambda n: (0, 0),
                            available=lambda n: 6000000 - 15 * MIB * n)
        ctx = self.soak(session)
        slope = self.check(ctx, "available memory after close does not fall")
        self.assertEqual(slope["state"], "FAILED", slope)
        self.assertEqual(vm_acceptance.verdict_for(ctx)[0], "FAIL")

    def test_an_unreadable_packagekit_answer_is_never_taken_for_idle(self) -> None:
        for state in ("activating", ""):
            with self.subTest(state=state):
                guest = FakeGuest(self.root, lambda c, s=state: s if "is-active" in c else "")
                self.assertIsNone(self.cases._packagekit_transactions(guest)["transactions"])
        busy = FakeGuest(self.root, lambda c: "active" if "is-active" in c else
                         {"exitcode": 1, "stdout": "", "stderr": "not activatable"})
        self.assertIsNone(self.cases._packagekit_transactions(busy)["transactions"])

    def test_meminfo_is_read_in_one_pass_and_missing_fields_are_marked(self) -> None:
        guest = FakeGuest(self.root, lambda _c: "MemAvailable: 6000000\nShmem: 200000")
        self.assertEqual(self.cases._meminfo_kib(guest),
                         {"MemAvailable": 6000000, "Shmem": 200000, "AnonPages": -1})
        self.assertEqual(len(guest.commands), 1)


class FramebufferTests(unittest.TestCase):
    def test_ppm_is_converted_to_a_png_of_the_same_size(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ppm = Path(directory) / "frame.ppm"
            png = Path(directory) / "frame.png"
            width, height = 1920, 1080
            pixels = bytes(((index * 37) % 256) for index in range(width * height * 3))
            ppm.write_bytes(f"P6\n{width} {height}\n255\n".encode() + pixels)
            self.assertEqual(ppm_to_png(ppm, png), (width, height))
            header = png.read_bytes()[:24]
            self.assertEqual(header[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", header[16:24]), (width, height))

    def test_a_truncated_framebuffer_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ppm = Path(directory) / "frame.ppm"
            ppm.write_bytes(b"P6\n1920 1080\n255\n" + b"\x00" * 100)
            with self.assertRaises(Exception) as caught:
                ppm_to_png(ppm, Path(directory) / "frame.png")
            self.assertIn("truncated", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
