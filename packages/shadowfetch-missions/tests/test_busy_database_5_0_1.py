"""5.0.0 STRESS-01: `show` and `cancel` answered "database is busy" (5.0.1).

In both 45-minute stress runs `shadowfetch-missions --json show` failed with
"The mission database is busy while another Mission Control process is opening
it" and `cancel` with "database is busy", each after its 10 s CLI budget, while
the worker finalised a media step on a disk where one sync took 15-34 s.

The lock a READ waited on was the EXCLUSIVE lock SQLite takes on the database
file when the last connection closes, to checkpoint the WAL and delete it --
held across two fsyncs. Store opened and closed a connection per call, so nearly
every close was a last close. The slow-disk test reproduces it with strace
delaying a worker-shaped writer's fsync/fdatasync calls (skipped where ptrace is
unavailable); the other tests pin the mechanism without it.

A worker commit still holds the write lock across its WAL fsync, so `cancel`
can still meet a lock it cannot wait out. It now saves the request first,
answers that it was saved, and the worker records it in the chain.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import types
import unittest
from unittest.mock import patch

import mission_states
from missions_regression import Harness, SOURCE, TEST_BUDGET_SECONDS, m

# The guest measured 15-34 s per sync; seconds are enough to outlast a budget.
SLOW_SYNC_SECONDS = 1.5


def strace_delay_available():
    """True when strace can delay a child's fdatasync (ptrace allowed, inject
    supported). A CI sandbox without ptrace skips the end-to-end test only."""
    exe = shutil.which("strace")
    if not exe:
        return False
    try:
        probe = subprocess.run(
            [exe, "-f", "-qq", "-o", os.devnull, "-e", "trace=fdatasync",
             "-e", "inject=fdatasync:delay_enter=1000", sys.executable, "-c",
             "import os, tempfile\nwith tempfile.TemporaryFile() as f: os.fdatasync(f.fileno())"],
            capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


WRITER = """
import importlib.util, sys, time
spec = importlib.util.spec_from_file_location("sf_missions", sys.argv[1])
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
store = m.Store()
for index in range(int(sys.argv[3])):
    store.event(sys.argv[2], "slow-disk-append", "worker-shaped chained append %d" % index)
print("done", flush=True)
"""


class ReadersNeverWaitOnAClosingWriter(Harness):
    def test_closing_the_last_connection_does_not_checkpoint_or_delete_the_wal(self):
        """The mechanism. Before 5.0.1 the last close checkpointed under an
        EXCLUSIVE lock on the database file and deleted the WAL."""
        self.mission()
        wal = Path(str(self.store.db_path) + "-wal")
        # Every Store call has returned, so no connection is open.
        self.assertTrue(wal.is_file(), "the last close checkpointed and deleted the WAL")
        self.assertGreater(wal.stat().st_size, 0)
        reopened = m.Store()
        self.assertEqual(len(reopened.list()), 1)

    def test_bounded_commands_never_checkpoint_after_their_commit(self):
        with m.Store(lock_budget=5).db() as db:
            self.assertEqual(db.execute("PRAGMA wal_autocheckpoint").fetchone()[0], 0)
        with self.store.db() as db:
            self.assertEqual(db.execute("PRAGMA wal_autocheckpoint").fetchone()[0], 1000)

    @unittest.skipUnless(strace_delay_available(), "strace cannot delay fsync here")
    def test_show_and_cancel_answer_while_a_writer_finalises_on_a_slow_disk(self):
        """The STRESS-01 shape, host-side: a worker-shaped process appends
        chained events with every fsync/fdatasync delayed, while `show` polls
        and `cancel` is pressed, each with a budget shorter than one sync."""
        mid = self.mission()["id"]
        self.store.transition(mid, "running")
        delay_us = int(SLOW_SYNC_SECONDS * 1_000_000)
        writer = subprocess.Popen(
            [shutil.which("strace"), "-f", "-qq", "-o", os.devnull,
             "-e", "trace=fsync,fdatasync",
             "-e", f"inject=fsync,fdatasync:delay_enter={delay_us}",
             sys.executable, "-c", WRITER, str(SOURCE), mid, "2"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        started = time.monotonic()
        shows, cancels = [], []
        try:
            while writer.poll() is None and time.monotonic() - started < 120:
                shows.append(self.cli("show", mid))
                if len(shows) == 10:
                    cancels.append(self.cli("cancel", mid))
                time.sleep(0.05)
            out, err = writer.communicate(timeout=120)
        finally:
            if writer.poll() is None:
                writer.kill()
                writer.communicate()
        elapsed = time.monotonic() - started
        self.assertEqual(writer.returncode, 0, err[-2000:])
        self.assertIn("done", out)
        # The delay was real and the reads overlapped it: a test that never
        # contended would prove nothing.
        self.assertGreaterEqual(elapsed, 2 * SLOW_SYNC_SECONDS)
        self.assertGreater(len(shows), 10)
        busy = [payload for _, payload, _ in shows if payload.get("busy")]
        self.assertEqual(busy, [], "a read waited on the writer")
        for code, payload, seconds in shows:
            self.assertEqual(code, 0, payload)
            self.assertEqual(payload["id"], mid)
            self.assertLess(seconds, TEST_BUDGET_SECONDS + 1.0)
        self.assertEqual(len(cancels), 1)
        code, payload, seconds = cancels[0]
        self.assertEqual(code, 0, payload)
        self.assertLess(seconds, TEST_BUDGET_SECONDS + 1.0)
        # Saved or written directly, depending on where in the writer's commit
        # the press landed; recorded exactly once either way.
        self.store.apply_cancel_requests()
        self.assertEqual(self.store.get(mid)["cancel_requested"], 1)
        self.assertEqual(len(self.events(mid, "cancel-requested")), 1)
        self.assertEqual(len(self.events(mid, "slow-disk-append")), 2)
        self.assertTrue(self.store.verify_chain()["chain_ok"])


class CancelUnderAHeldWriteLock(Harness):
    def test_cancel_saves_the_stop_and_answers_inside_its_budget(self):
        mid = self.mission()["id"]
        self.store.transition(mid, "running")
        holder = self.hold_write_transaction()
        code, payload, seconds = self.cli("cancel", mid)
        # Before 5.0.1: exit 1, {"busy": true} -- and no stop anywhere.
        self.assertEqual(code, 0, payload)
        self.assertLess(seconds, TEST_BUDGET_SECONDS + 1.5)
        self.assertEqual(payload["state"], "running")
        self.assertEqual(payload["cancel_requested"], 0, "a saved request is not a record")
        self.assertTrue(payload["cancel_pending"])
        self.assertIn("saved", payload["notice"])
        # A reader sees the same thing while the writer still holds the lock.
        code, shown, seconds = self.cli("show", mid)
        self.assertEqual(code, 0, shown)
        self.assertTrue(shown["cancel_pending"])
        self.assertLess(seconds, TEST_BUDGET_SECONDS + 0.5)
        holder.rollback()
        # The worker's half: recorded once, in the chain, as the person's stop.
        self.assertEqual(self.store.apply_cancel_requests(), [mid])
        row = self.store.get(mid)
        self.assertEqual(row["cancel_requested"], 1)
        self.assertFalse(row["cancel_pending"])
        recorded = self.events(mid, "cancel-requested")
        self.assertEqual(len(recorded), 1)
        self.assertIn("saved request", recorded[0]["detail"])
        self.assertIsNone(self.store.cancel_request(mid))
        self.assertEqual(self.store.apply_cancel_requests(), [])
        self.assertEqual(len(self.events(mid, "cancel-requested")), 1)
        self.assertTrue(self.store.verify_chain()["chain_ok"])

    def test_with_the_real_budget_stop_answers_in_seconds(self):
        """The saved request is safe, so Stop does not wait out the 10 s."""
        mid = self.mission()["id"]
        self.store.transition(mid, "running")
        self.hold_write_transaction()
        code, payload, seconds = self.cli("cancel", mid, budget=m.CLI_LOCK_BUDGET_SECONDS)
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["cancel_pending"])
        self.assertLess(m.CANCEL_LOCK_WAIT_SECONDS, m.CLI_LOCK_BUDGET_SECONDS)
        self.assertLess(seconds, m.CANCEL_LOCK_WAIT_SECONDS + 1.5)

    def test_a_running_executor_stops_on_a_saved_request(self):
        mid = self.mission()["id"]
        self.store.transition(mid, "running")
        self.store.save_cancel_request(mid)
        executor = types.SimpleNamespace(store=self.store, mid=mid,
                                         deadline=time.monotonic() + 600)
        with self.assertRaises(m.Cancelled):
            m.Executor.check(executor)
        self.assertEqual(self.store.get(mid)["cancel_requested"], 1)
        self.assertEqual(len(self.events(mid, "cancel-requested")), 1)

    def test_a_saved_stop_for_a_queued_mission_cancels_it_before_it_runs(self):
        mid = self.mission()["id"]
        holder = self.hold_write_transaction()
        code, payload, _ = self.cli("cancel", mid)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["state"], "queued")
        self.assertTrue(payload["cancel_pending"])
        holder.rollback()
        ran = []
        with patch.object(m, "run_mission", lambda store, mission_id: ran.append(mission_id)):
            m.worker(self.store, once=True)
        self.assertEqual(ran, [], "a stopped mission was started")
        self.assertEqual(self.store.get(mid)["state"], "cancelled")
        self.assertIn("saved request", self.events(mid, "cancelled")[-1]["detail"])
        self.assertTrue(self.store.verify_chain()["chain_ok"])

    def test_an_uncontended_cancel_leaves_no_saved_request(self):
        mid = self.mission()["id"]
        self.store.transition(mid, "running")
        code, payload, _ = self.cli("cancel", mid)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["cancel_requested"], 1)
        self.assertFalse(payload["cancel_pending"])
        self.assertEqual(list(self.state.glob(m.CANCEL_REQUEST_PREFIX + "*")), [])

    def test_a_stale_request_never_stops_a_retry_or_a_finished_mission(self):
        mid = self.mission()["id"]
        mission_states.reach(self.store, mid, "failed")
        self.store.save_cancel_request(mid)
        self.store.retry(mid)
        self.assertIsNone(self.store.cancel_request(mid))
        other = self.mission(workspace="beta")["id"]
        mission_states.reach(self.store, other, "waiting-review")
        self.store.save_cancel_request(other)
        self.assertEqual(self.store.apply_cancel_requests(), [])
        self.assertIsNone(self.store.cancel_request(other))
        self.assertEqual(self.store.get(other)["state"], "waiting-review")
        self.assertEqual(self.store.get(mid)["state"], "queued")

    def test_a_request_name_is_never_a_path(self):
        self.assertIsNone(self.store._cancel_request_path("../../etc/passwd"))
        with self.assertRaises(m.MissionError):
            self.store.save_cancel_request("mission-x/../../escape")


if __name__ == "__main__":
    unittest.main(verbosity=2)
