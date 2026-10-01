"""5.0.1 mission stress: busy answers and worker stop (tools/qa_5_0_0/stress).

Unit tests for the busy-retry and worker-stop logic, and main() driven
against a fake `shadowfetch-missions` CLI that keeps Mission Control's
documented busy contract: exit 1 and {"error": ..., "busy": true}. None of
this is mission or stress evidence.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

STRESS = Path(__file__).resolve().parents[1] / "qa_5_0_0" / "stress"
HELPER = STRESS / "mission_stress.py"
spec = importlib.util.spec_from_file_location("qa5_mission_stress", HELPER)
target = importlib.util.module_from_spec(spec)
spec.loader.exec_module(target)

BUSY = json.dumps({"error": "The mission database is busy while another Mission Control process is opening it; try again shortly", "busy": True})


class Clock:
    def __init__(self, step=0.0):
        self.now, self.step, self.sleeps = 0.0, step, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class BusyAnswer(unittest.TestCase):
    def test_only_the_engine_busy_contract_counts(self):
        self.assertIn("busy", target.busy_answer(1, BUSY))
        self.assertIsNone(target.busy_answer(0, BUSY))
        self.assertIsNone(target.busy_answer(1, json.dumps({"error": "Mission does not exist"})))
        self.assertIsNone(target.busy_answer(1, json.dumps({"error": "x", "busy": "yes"})))
        self.assertIsNone(target.busy_answer(1, "Traceback (most recent call last):"))
        self.assertIsNone(target.busy_answer(1, json.dumps(["busy"])))


class RetryBusy(unittest.TestCase):
    def calls(self, answers, clock, cost=10.0):
        answers = list(answers)
        def call():
            clock.now += cost  # one busy answer costs the CLI's 10 s lock budget
            value = answers.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        return call

    def test_busy_reads_are_asked_again_and_recorded(self):
        clock, notes = Clock(), []
        call = self.calls([target.DatabaseBusy("busy"), target.DatabaseBusy("busy"), {"state": "running"}], clock)
        value = target.retry_busy(call, action="show", clock=clock, sleep=clock.sleep, note=notes.append)
        self.assertEqual(value, {"state": "running"})
        self.assertEqual(clock.sleeps, [2, 4])
        self.assertEqual([n["retried"] for n in notes], [True, True])
        self.assertEqual([n["attempt"] for n in notes], [1, 2])

    def test_a_database_busy_past_the_budget_is_a_failure(self):
        clock, notes = Clock(), []
        call = self.calls([target.DatabaseBusy("busy")] * 100, clock)
        with self.assertRaises(target.BusyBudgetExhausted) as caught:
            target.retry_busy(call, action="show", clock=clock, sleep=clock.sleep, note=notes.append)
        self.assertIn("busy budget", str(caught.exception))
        self.assertFalse(notes[-1]["retried"])
        self.assertLessEqual(notes[-1]["streak_seconds"], target.BUSY_RETRY_BUDGET_SECONDS)
        self.assertGreater(notes[-1]["streak_seconds"] + 10, target.BUSY_RETRY_BUDGET_SECONDS - 20)

    def test_the_cycle_deadline_bounds_the_retries(self):
        clock = Clock()
        call = self.calls([target.DatabaseBusy("busy")] * 100, clock)
        with self.assertRaises(target.BusyBudgetExhausted) as caught:
            target.retry_busy(call, action="show", clock=clock, sleep=clock.sleep, deadline=35.0)
        self.assertIn("deadline", str(caught.exception))
        # Answers at 10, 22 and 36 s: no backoff is started that would end past
        # the deadline (command() bounds each attempt by it in main()).
        self.assertEqual(clock.sleeps, [2, 4])

    def test_review_is_never_replayed(self):
        clock, notes = Clock(), []
        call = self.calls([target.DatabaseBusy("busy"), {"state": "undone"}], clock)
        with self.assertRaises(target.DatabaseBusy):
            target.retry_busy(call, action="review", clock=clock, sleep=clock.sleep, note=notes.append)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(notes[0]["retried"], False)
        self.assertIn("not replayed", notes[0]["reason"])

    def test_retried_actions_are_reads_cancel_and_the_guarded_create(self):
        self.assertEqual(target.BUSY_RETRY_ACTIONS, {"show", "events", "list", "create", "cancel"})

    def test_before_retry_can_stop_a_create(self):
        clock = Clock()
        call = self.calls([target.DatabaseBusy("busy"), {"id": "mission-2"}], clock)
        def guard():
            raise RuntimeError("queue gained mission-1")
        with self.assertRaises(RuntimeError) as caught:
            target.retry_busy(call, action="create", clock=clock, sleep=clock.sleep, before_retry=guard)
        self.assertIn("mission-1", str(caught.exception))

    def test_other_failures_are_not_retried(self):
        clock, notes = Clock(), []
        call = self.calls([RuntimeError("Command 3 failed: 1"), {"state": "running"}], clock)
        with self.assertRaises(RuntimeError):
            target.retry_busy(call, action="show", clock=clock, sleep=clock.sleep, note=notes.append)
        self.assertEqual(notes, [])


class FakeWorker:
    """The private worker as Popen sees it. exits_on: the signals it obeys."""

    pid = 2 ** 22 + 4321   # above the default pid_max: never a real process

    def __init__(self, exits_on=(signal.SIGTERM, signal.SIGKILL)):
        self.exits_on, self.returncode, self.signals, self.waits = exits_on, None, [], []

    def signal_group(self, pid, signum):
        self.signals.append(signum)
        if signum in self.exits_on:
            self.returncode = -signum

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.returncode is None:
            raise subprocess.TimeoutExpired(["shadowfetch-missions", "worker"], timeout)
        return self.returncode


class StopWorker(unittest.TestCase):
    def test_already_exited_worker_is_not_signalled(self):
        worker = FakeWorker()
        worker.returncode = 0
        record = target.stop_worker(worker, killpg=worker.signal_group)
        self.assertEqual((record["exited"], record["signals"]), (True, []))

    def test_term_is_enough(self):
        worker = FakeWorker()
        record = target.stop_worker(worker, killpg=worker.signal_group)
        self.assertEqual((record["exited"], record["signals"]), (True, ["SIGTERM"]))
        self.assertEqual(worker.waits, [target.WORKER_TERM_GRACE])

    def test_kill_after_term_grace(self):
        worker = FakeWorker(exits_on=(signal.SIGKILL,))
        record = target.stop_worker(worker, killpg=worker.signal_group)
        self.assertEqual((record["exited"], record["signals"]), (True, ["SIGTERM", "SIGKILL"]))

    def test_worker_that_outlives_sigkill_is_recorded_not_raised(self):
        # 5.0.0 runs 1 and 2: the second worker.wait(timeout=5) raised
        # TimeoutExpired out of main() and no result.json was written.
        worker = FakeWorker(exits_on=())
        record = target.stop_worker(worker, killpg=worker.signal_group)
        self.assertFalse(record["exited"])
        self.assertEqual(record["signals"], ["SIGTERM", "SIGKILL"])
        self.assertEqual(worker.waits, [target.WORKER_TERM_GRACE, target.WORKER_KILL_GRACE])
        self.assertIn("process", record)

    def test_signal_errors_are_recorded(self):
        worker = FakeWorker(exits_on=())
        def refuse(pid, signum):
            if signum == signal.SIGTERM:
                raise ProcessLookupError
            raise PermissionError("not permitted")
        record = target.stop_worker(worker, killpg=refuse)
        self.assertFalse(record["exited"])
        self.assertEqual(len(record["errors"]), 1)
        self.assertIn("SIGKILL", record["errors"][0])

    def test_graces_cover_the_disk_waits_seen(self):
        self.assertGreaterEqual(target.WORKER_TERM_GRACE, 30)
        self.assertGreaterEqual(target.WORKER_KILL_GRACE, 34)


class Stuck:
    """A command that outlives SIGKILL (an uninterruptible disk wait)."""

    pid = 2 ** 22 + 4321   # above the default pid_max: never a real process
    returncode = None

    def __init__(self, *args, **kwargs):
        self.waits, self.killed = [], False

    def communicate(self, timeout=None):
        self.waits.append(timeout)
        raise subprocess.TimeoutExpired("shadowfetch-missions", timeout, output=b"partial", stderr=b"")

    def kill(self):
        self.killed = True


class RunCommand(unittest.TestCase):
    def test_output_and_exit_status(self):
        done = target.run_command([sys.executable, "-c", "print('hi'); raise SystemExit(3)"], timeout=30)
        self.assertEqual((done.returncode, done.stdout), (3, "hi\n"))

    def test_timeout_kills_and_reaps_keeping_partial_output(self):
        begun = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            target.run_command([sys.executable, "-c", "import sys, time; print('partial'); sys.stdout.flush(); time.sleep(60)"],
                               timeout=1.0)
        self.assertLess(time.monotonic() - begun, 20)
        self.assertIs(caught.exception.reaped, True)
        self.assertIn("partial", caught.exception.stdout)

    def test_a_command_that_outlives_sigkill_is_left_after_the_reap_grace(self):
        # subprocess.run() would wait for it without a bound.
        stuck = Stuck()
        with patch.object(subprocess, "Popen", return_value=stuck):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                target.run_command(["shadowfetch-missions", "--json", "show", "m"], timeout=5, reap_grace=7)
        self.assertTrue(stuck.killed)
        self.assertEqual(stuck.waits, [5, 7])
        self.assertIs(caught.exception.reaped, False)
        self.assertIn("process", vars(caught.exception))
        self.assertEqual(caught.exception.stdout, "partial")


class OuterBound(unittest.TestCase):
    def test_the_outer_timeout_has_real_slack_over_the_helpers_own_worst_case(self):
        # Review of v3: duration + 1275 left 15 s over the helper's own
        # duration + 1260 before any overshoot, and the outer TERM's
        # --kill-after=15s then lost result.json during the worker stop.
        text = (STRESS / "stress_45m.sh").read_text()
        found = re.search(r"^mission_outer_seconds=\$\(\(duration \+ ([0-9+ ]+)\)\)$", text, re.M)
        outer = sum(int(part) for part in found.group(1).split("+"))
        worst = (target.CYCLE_BOUND_SECONDS + target.COMMAND_REAP_GRACE + target.POLL_SECONDS
                 + target.CLEANUP_BOUND_SECONDS + target.COMMAND_REAP_GRACE
                 + target.WORKER_TERM_GRACE + target.WORKER_KILL_GRACE)
        self.assertEqual(target.WORST_CASE_TAIL_SECONDS, worst)
        self.assertGreaterEqual(outer, worst + 120)


FAKE_CLI = r'''#!{python}
"""Fake shadowfetch-missions keeping the engine's JSON and busy contracts."""
import hashlib, json, os, shutil, sys
from pathlib import Path
state = Path(os.environ["SHADOWFETCH_MISSIONS_STATE"]); state.mkdir(parents=True, exist_ok=True)
plan = json.loads(os.environ.get("FAKE_PLAN", "{{}}"))
db = state / "fake.json"
data = json.loads(db.read_text()) if db.exists() else {{"missions": {{}}, "calls": {{}}, "next": 1}}
args = sys.argv[1:]
if args[0] == "--json":
    args = args[1:]
action = args[0]
data["calls"][action] = data["calls"].get(action, 0) + 1
call = data["calls"][action]
def save():
    db.write_text(json.dumps(data))
def busy():
    save(); print(json.dumps({{"error": "Mission Control's database is busy: another Mission Control process is holding it. Try again shortly.", "busy": True}})); sys.exit(1)
def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def flag(key):
    return call in plan.get(key, [])
if action == "create":
    if flag("busy_create") and not plan.get("busy_create_commits"):
        busy()
    ws = Path.home() / "Workspaces" / args[args.index("--workspace") + 1]
    mid = "mission-%04d" % data["next"]; data["next"] += 1
    data["missions"][mid] = {{"id": mid, "state": "queued", "workspace": str(ws), "config": {{"timeout": 900}}, "receipt": None, "checkpoint": None}}
    if flag("busy_create"):
        busy()
    save(); print(json.dumps(data["missions"][mid])); sys.exit(0)
if action == "list":
    save(); print(json.dumps(list(data["missions"].values()))); sys.exit(0)
mid = args[1]
mission = data["missions"][mid]
if action == "show":
    if flag("busy_show"):
        busy()
    if plan.get("show_error") and call >= plan["show_error"]:
        save(); print(json.dumps({{"error": "Mission records were incomplete"}})); sys.exit(1)
    if mission["state"] == "queued":
        mission["state"] = "running"
    elif mission["state"] == "running" and not plan.get("never_finish"):
        ws = Path(mission["workspace"]); out = ws / "mission-output" / mid; out.mkdir(parents=True)
        wav = out / "tone.wav"; shutil.copy2(ws / "tone.wav", wav)
        exports = out / "exports.json"; exports.write_text(json.dumps([{{"path": str(wav), "decode_verified": True}}]))
        receipt = state / mid / "receipt.json"; receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps({{"state": "waiting-review", "checkpoint": "cp-" + mid, "artifacts": [
            {{"path": str(p), "bytes": p.stat().st_size, "sha256": sha(p)}} for p in (wav, exports)]}}))
        mission.update(state="waiting-review", receipt=str(receipt), checkpoint="cp-" + mid)
    save(); print(json.dumps(mission)); sys.exit(0)
if action == "events":
    if flag("busy_events"):
        busy()
    save(); print(json.dumps([{{"event": "queued"}}])); sys.exit(0)
if action == "review":
    if flag("busy_review"):
        busy()
    shutil.rmtree(Path(mission["workspace"]) / "mission-output")
    mission["state"] = "undone"; save(); print(json.dumps(mission)); sys.exit(0)
if action == "cancel":
    if flag("busy_cancel"):
        busy()
    if mission["state"] in ("queued", "running"):
        mission["state"] = "cancelled"
    save(); print(json.dumps(mission)); sys.exit(0)
sys.exit(2)
'''
FAKE_FFPROBE = r'''#!{python}
import json
print(json.dumps({{"streams": [{{"codec_type": "audio", "codec_name": "pcm_s16le", "sample_rate": "48000"}}], "format": {{"duration": "1.000000"}}}}))
'''
FAKE_FFMPEG = "#!{python}\nraise SystemExit(0)\n"


class MainAgainstFakeEngine(unittest.TestCase):
    """main() for real, with fake engine binaries and an in-process fake worker."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.bin, self.home, self.out = root / "bin", root / "home", root / "out"
        self.bin.mkdir(); self.home.mkdir()
        for name, body in (("shadowfetch-missions", FAKE_CLI), ("ffprobe", FAKE_FFPROBE), ("ffmpeg", FAKE_FFMPEG)):
            path = self.bin / name
            path.write_text(body.format(python=sys.executable))
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        self.handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}

    def tearDown(self):
        for sig, handler in self.handlers.items():
            signal.signal(sig, handler)
        self.tmp.cleanup()

    def run_main(self, plan, worker, duration=10):
        real_popen, real_sleep = subprocess.Popen, time.sleep
        def popen(argv, *args, **kwargs):
            return worker if list(argv[-1:]) == ["worker"] else real_popen(argv, *args, **kwargs)
        env = {"HOME": str(self.home), "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}", "FAKE_PLAN": json.dumps(plan)}
        argv = ["mission_stress.py", "--duration", str(duration), "--run-id", "fake-1", "--output", str(self.out)]
        with patch.dict(os.environ, env), patch.object(sys, "argv", argv), \
                patch.object(subprocess, "Popen", side_effect=popen), \
                patch.object(os, "killpg", side_effect=worker.signal_group), \
                patch.object(time, "sleep", side_effect=lambda s: real_sleep(min(s, 0.01))), \
                contextlib.redirect_stdout(io.StringIO()):
            rc = target.main()
        result = json.loads((self.out / "result.json").read_text())
        return rc, result

    def lines(self, name):
        path = self.out / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_busy_answers_are_ridden_out_and_recorded(self):
        # 5.0.0 run 2: show answered busy at +70 s and the loop stopped.
        rc, result = self.run_main({"busy_show": [2, 3], "busy_events": [1], "busy_create": [2]}, FakeWorker())
        self.assertEqual((rc, result["status"]), (0, "SMOKE_PASS"), result["failures"])
        self.assertGreaterEqual(result["cycles"], 2)
        self.assertEqual(result["database_busy"]["answers"], 4)
        self.assertEqual(result["database_busy"]["retried"], 4)
        self.assertEqual(result["database_busy"]["budget_seconds"], target.BUSY_RETRY_BUDGET_SECONDS)
        self.assertEqual([row["action"] for row in self.lines("busy.jsonl")], ["show", "show", "events", "create"])
        self.assertEqual(sum(1 for row in self.lines("commands.jsonl") if row.get("busy")), 4)
        self.assertEqual(result["worker_stop"]["signals"], ["SIGTERM"])
        self.assertEqual(result["qa_profile"], "production-default900s-v3")

    def test_worker_that_outlives_sigkill_fails_the_run_without_losing_it(self):
        rc, result = self.run_main({"show_error": 1}, FakeWorker(exits_on=()))
        self.assertEqual((rc, result["status"]), (1, "FAIL"))
        errors = [f["error"] for f in result["failures"]]
        self.assertIn("Command 2 failed: 1", errors)
        self.assertIn(f"Mission worker did not exit within {target.WORKER_KILL_GRACE} s of SIGKILL", errors)
        self.assertFalse(result["worker_stop"]["exited"])

    def test_busy_review_is_not_replayed(self):
        rc, result = self.run_main({"busy_review": [1]}, FakeWorker())
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("busy", result["failures"][0]["error"])
        reviews = [row for row in self.lines("commands.jsonl") if row["argv"][2] == "review"]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(result["database_busy"]["not_retried"], 1)

    def test_a_provisional_result_is_on_disk_during_the_worker_stop(self):
        out = self.out
        class Watching(FakeWorker):
            seen = None
            def signal_group(self, pid, signum):
                if self.seen is None:
                    Watching.seen = json.loads((out / "result.json").read_text())
                super().signal_group(pid, signum)
        rc, result = self.run_main({}, Watching())
        self.assertEqual((Watching.seen["status"], Watching.seen["provisional"]), ("INCOMPLETE", True))
        self.assertEqual(Watching.seen["status_so_far"], "SMOKE_PASS")
        self.assertIsNone(Watching.seen["worker_stop"])
        self.assertEqual((rc, result["status"]), (0, "SMOKE_PASS"))
        self.assertNotIn("provisional", result)
        self.assertFalse((out / "result.json.partial").exists())

    def test_a_run_short_of_the_load_criteria_exits_nonzero(self):
        empty = {"coverage_seconds": 0.0, "completed_within_load_window": 0, "tail_cycles": 0}
        with patch.object(target, "coverage_summary", return_value=empty):
            rc, result = self.run_main({}, FakeWorker())
        self.assertEqual((rc, result["status"]), (1, "FAIL"))
        self.assertEqual(result["failures"][-1]["error"], "Declared continuous-load acceptance criteria not met")

    def test_busy_create_that_queued_a_mission_is_never_sent_again(self):
        rc, result = self.run_main({"busy_create": [1], "busy_create_commits": True}, FakeWorker())
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("queue gained mission-0001", result["failures"][0]["error"])
        creates = [row for row in self.lines("commands.jsonl") if row["argv"][2] == "create"]
        self.assertEqual(len(creates), 1)


if __name__ == "__main__":
    unittest.main()
