"""A saved Stop is never lost, never ignored, and never stops the worker (5.0.1).

The first 5.0.1 build saved a stop request in the state directory when the
database was busy (5.0.0 STRESS-01) and told the person "the stop is recorded
in its history as soon as the database is free". Its review found the ways that
promise could break:

- a stop saved after the executor's last check() was deleted, unrecorded, when
  the run finished -- exactly the STRESS-01 timing, Stop pressed during
  finalisation;
- a queued mission whose stop the worker could not record yet was started
  anyway, spending an attempt and taking the workspace checkpoint;
- a request file that could not be written (a full disk) made `cancel` fail
  without trying the database, with the state directory's path in the error;
- a directory or a FIFO named like a request crashed or hung the worker loop.
"""
import errno
import json
import os
import threading
import time
import unittest
from unittest.mock import patch

from missions_regression import Harness, m


def actors(store, mid, event):
    """Who each `event` of `mid` was recorded as coming from (events() does
    not return the actor)."""
    with store.db() as db:
        return [row["actor"] for row in db.execute(
            "SELECT actor FROM events WHERE mission=? AND event=? ORDER BY seq",
            (mid, event))]


def last_check_then(press_stop):
    """An Executor.execute() that passes its last check() and then, while the
    mission finalises, has the person press Stop."""
    def execute(executor):
        executor.check()
        press_stop(executor.mid)
    return execute


class AStopSavedWhileTheMissionFinishes(Harness):
    def test_a_stop_saved_after_the_last_check_is_recorded_before_the_outcome(self):
        mid = self.mission()["id"]
        answers = []

        def press_stop(mission_id):
            # The worker's finalisation holds the write lock: the CLI saves.
            holder = self.hold_write_transaction()
            answers.append(self.cli("cancel", mission_id))
            holder.rollback()

        with patch.object(m.Executor, "execute", last_check_then(press_stop)):
            result = m.run_mission(self.store, mid)
        code, payload, _ = answers[0]
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["cancel_pending"])
        self.assertIn("recorded", payload["notice"])
        # The work had finished, so it is still reviewed -- as in 5.0.0, where
        # a stop that reached the database during finalisation was recorded.
        self.assertEqual(result["state"], "waiting-review", result["error"])
        self.assertEqual(result["cancel_requested"], 1)
        names = [event["event"] for event in self.events(mid)]
        # Before: [..., 'running', ..., 'waiting-review'] and the request deleted.
        self.assertEqual(names.count("cancel-requested"), 1, names)
        self.assertLess(names.index("cancel-requested"), names.index("waiting-review"))
        stop = self.events(mid, "cancel-requested")[0]
        self.assertEqual(actors(self.store, mid, "cancel-requested"), [m.ACTOR_USER])
        self.assertIn("recorded from the saved request", stop["detail"])
        self.assertIn("not stopped", stop["detail"])
        self.assertIsNone(self.store.cancel_request(mid))
        self.assertTrue(self.store.verify_chain()["chain_ok"])

    def test_a_stop_check_could_not_record_commits_with_the_cancel(self):
        mid = self.mission()["id"]

        def execute(executor):
            executor.store.save_cancel_request(executor.mid)
            with patch.object(m.Store, "_record_cancel",
                              side_effect=m.DatabaseBusy("busy")):
                executor.check()

        with patch.object(m.Executor, "execute", execute):
            result = m.run_mission(self.store, mid)
        self.assertEqual(result["state"], "cancelled")
        names = [event["event"] for event in self.events(mid)]
        # Before: cancelled with no record of who asked.
        self.assertEqual(names.count("cancel-requested"), 1, names)
        self.assertLess(names.index("cancel-requested"), names.index("cancelled"))
        stop = self.events(mid, "cancel-requested")[0]
        self.assertEqual(actors(self.store, mid, "cancel-requested"), [m.ACTOR_USER])
        self.assertIn("Running process is terminated", stop["detail"])
        self.assertEqual(result["cancel_requested"], 1)
        self.assertIsNone(self.store.cancel_request(mid))
        self.assertTrue(self.store.verify_chain()["chain_ok"])

    def test_a_stop_saved_while_the_outcome_commits_is_recorded_as_too_late(self):
        """The narrowest window: saved after run_mission() looked, while the
        terminal transaction held the lock. The mission has finished, so this
        is not a 'cancel-requested' (never after a terminal event); it is
        recorded as asked for and not applied, not deleted."""
        mid = self.mission()["id"]
        finish = m.Store.finish_execution

        def finishing(store, mission_id, *args, **kwargs):
            store.save_cancel_request(mission_id)
            return finish(store, mission_id, *args, **kwargs)

        with patch.object(m.Executor, "execute", lambda executor: executor.check()), \
                patch.object(m.Store, "finish_execution", finishing):
            result = m.run_mission(self.store, mid)
        self.assertEqual(result["state"], "waiting-review", result["error"])
        names = [event["event"] for event in self.events(mid)]
        self.assertNotIn("cancel-requested", names)
        self.assertEqual(names.count(m.CANCEL_LATE_EVENT), 1, names)
        self.assertGreater(names.index(m.CANCEL_LATE_EVENT), names.index("waiting-review"))
        late = self.events(mid, m.CANCEL_LATE_EVENT)[0]
        self.assertEqual(actors(self.store, mid, m.CANCEL_LATE_EVENT), [m.ACTOR_USER])
        self.assertIn("not applied", late["detail"])
        self.assertEqual(self.store.get(mid)["cancel_requested"], 0)
        self.assertIsNone(self.store.cancel_request(mid))
        self.assertTrue(self.store.verify_chain()["chain_ok"])


class AQueuedMissionWithAStopIsNotStarted(Harness):
    def test_an_unrecorded_stop_keeps_it_queued_until_it_is_recorded(self):
        mid = self.mission()["id"]
        self.store.save_cancel_request(mid)
        executed = []
        with patch.object(m.Store, "_record_cancel", side_effect=m.DatabaseBusy("busy")), \
                patch.object(m.Executor, "execute",
                             lambda executor: executed.append(executor.mid)):
            with self.assertRaises(m.MissionError) as refused:
                m.run_mission(self.store, mid)
            self.assertNotIsInstance(refused.exception, m.ApprovalRequired)
            self.assertIn("stop was requested", str(refused.exception))
            m.worker(self.store, once=True)
        # Before: started -- attempt 1, the checkpoint taken in execute(), then
        # cancelled with no 'cancel-requested' and the request deleted.
        self.assertEqual(executed, [])
        row = self.store.get(mid)
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["attempt"], 0)
        self.assertIsNone(row["checkpoint"])
        self.assertTrue(row["cancel_pending"])
        self.assertEqual([event["event"] for event in self.events(mid)], ["queued"])
        # Once the database takes it: cancelled, by the person, as saved.
        self.assertEqual(self.store.apply_cancel_requests(), [mid])
        self.assertEqual(self.store.get(mid)["state"], "cancelled")
        cancelled = self.events(mid, "cancelled")
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(actors(self.store, mid, "cancelled"), [m.ACTOR_USER])
        self.assertIn("recorded from the saved request", cancelled[0]["detail"])
        self.assertTrue(self.store.verify_chain()["chain_ok"])


class AStopThatCannotBeSaved(Harness):
    def no_request_files(self):
        """Every request file write fails as on a full disk."""
        real_open = os.open

        def opener(path, flags, *args, **kwargs):
            if m.CANCEL_REQUEST_PREFIX in os.fspath(path) and flags & os.O_CREAT:
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC), os.fspath(path))
            return real_open(path, flags, *args, **kwargs)

        return patch.object(m.os, "open", opener)

    def test_a_full_disk_still_stops_the_mission_through_the_database(self):
        mid = self.mission()["id"]
        self.store.transition(mid, "running")
        with self.no_request_files():
            code, payload, _ = self.cli("cancel", mid)
        # Before: exit 1, "[Errno 28] No space left on device: <state path>",
        # and the database, which was free, never tried.
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["cancel_requested"], 1)
        self.assertEqual(len(self.events(mid, "cancel-requested")), 1)
        self.assertEqual(sorted(p.name for p in self.state.iterdir()
                                if m.CANCEL_REQUEST_PREFIX in p.name), [])

    def test_with_neither_a_file_nor_the_lock_it_says_busy_and_names_no_path(self):
        mid = self.mission()["id"]
        self.store.transition(mid, "running")
        holder = self.hold_write_transaction()
        with self.no_request_files():
            code, payload, _ = self.cli("cancel", mid)
        self.assertEqual(code, 1, payload)
        self.assertTrue(payload["busy"])
        self.assertNotIn(str(self.base), json.dumps(payload))
        holder.rollback()
        self.assertEqual(self.store.get(mid)["cancel_requested"], 0)
        self.assertFalse(self.store.get(mid)["cancel_pending"])


class EntriesThatAreNotRequests(Harness):
    def test_a_directory_fifo_or_link_named_like_a_request_is_not_one(self):
        directory = self.mission()["id"]
        fifo = self.mission(workspace="beta")["id"]
        link = self.mission(title="Linked")["id"]
        (self.state / (m.CANCEL_REQUEST_PREFIX + directory)).mkdir()
        fifo_path = self.state / (m.CANCEL_REQUEST_PREFIX + fifo)
        os.mkfifo(fifo_path)
        target = self.base / "elsewhere.json"
        target.write_text(json.dumps({"mission": link}))
        (self.state / (m.CANCEL_REQUEST_PREFIX + link)).symlink_to(target)

        def release_fifo():
            # Unblocks a reader stuck in open(), should the guard regress.
            try:
                os.close(os.open(fifo_path, os.O_WRONLY | os.O_NONBLOCK))
            except OSError:
                pass

        self.addCleanup(release_fifo)
        outcome = {}

        def worker_pass():
            try:
                outcome["recorded"] = self.store.apply_cancel_requests()
                outcome["read"] = [self.store.cancel_request(mid)
                                   for mid in (directory, fifo, link)]
            except BaseException as exc:                      # noqa: BLE001
                outcome["error"] = exc

        thread = threading.Thread(target=worker_pass, daemon=True)
        thread.start()
        thread.join(10)
        # Before: IsADirectoryError out of the loop (the worker exited), and a
        # FIFO blocked it in open() for good.
        self.assertFalse(thread.is_alive(), "a FIFO named like a request blocked the worker")
        self.assertNotIn("error", outcome)
        self.assertEqual(outcome["recorded"], [])
        self.assertEqual(outcome["read"], [None, None, None])
        for mid in (directory, fifo, link):
            row = self.store.get(mid)
            self.assertEqual(row["state"], "queued")
            self.assertFalse(row["cancel_pending"], "show and the worker disagree")
        self.store.clear_cancel_request(directory)
        # A whole worker pass, as systemd runs it.
        started = []
        with patch.object(m, "run_mission",
                          lambda store, mission_id: started.append(mission_id)):
            began = time.monotonic()
            self.assertEqual(m.worker(self.store, once=True), 0)
        self.assertLess(time.monotonic() - began, 10)
        self.assertEqual(sorted(started), sorted((directory, fifo, link)))

    def test_stop_still_works_with_a_directory_in_the_requests_place(self):
        mid = self.mission()["id"]
        (self.state / (m.CANCEL_REQUEST_PREFIX + mid)).mkdir()
        code, payload, _ = self.cli("cancel", mid)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["state"], "cancelled")
        self.assertEqual(sorted(p.name for p in self.state.iterdir()
                                if p.name.startswith("." + m.CANCEL_REQUEST_PREFIX)), [])

    def test_an_oversized_or_garbled_request_is_still_a_request(self):
        mid = self.mission()["id"]
        path = self.state / (m.CANCEL_REQUEST_PREFIX + mid)
        path.write_bytes(b"{" + b" " * (m.CANCEL_REQUEST_MAX_BYTES * 4))
        self.assertEqual(self.store.cancel_request(mid), {})
        self.assertTrue(self.store.get(mid)["cancel_pending"])
        self.assertEqual(self.store.apply_cancel_requests(), [mid])
        self.assertIn("an unrecorded time", self.events(mid, "cancelled")[0]["detail"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
