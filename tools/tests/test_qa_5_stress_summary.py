"""5.0.1 STRESS-01 verdict surfacing (tools/qa_5_0_0/stress/workload_summary.py).

What the v3 workload helpers tolerate that 4.x stopped on -- container cycles
over the 4.x 120 s bound, "database is busy" answers -- must reach the
result.json stress_45m.sh writes and the summary.json run_stress.sh writes,
and must keep the run from a plain PASS. These tests render the real
result.json block and run the real summary block on fixtures. None of this is
stress evidence.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
STRESS = ROOT / "tools" / "qa_5_0_0" / "stress"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


target = load("qa5_workload_summary", STRESS / "workload_summary.py")
# The container result comes from the real helper on its pure test fakes, so a
# renamed field breaks here rather than in a 45-minute run.
container_tests = load("qa5_container_stress_tests", Path(__file__).resolve().parent / "test_qa_5_stress_container.py")


def container_result(plan, duration=2700):
    with contextlib.redirect_stdout(io.StringIO()):
        return container_tests.Profile(plan=plan).go(duration)


def mission_result(answers=0):
    return {"qa_profile": "production-default900s-v3", "status": "PASS", "cycles": 3,
            "completed_within_load_window": 3, "coverage_seconds": 2600.0,
            "database_busy": {"answers": answers, "retried": answers, "not_retried": 0,
                              "longest_streak_seconds": 21.5 if answers else 0.0,
                              "budget_seconds": 300, "retried_actions": ["show"]},
            "worker_stop": {"pid": 1, "exited": True, "signals": ["SIGTERM"], "seconds": 1.5}}


FAST = dict(result_after=20.0, cleanup=10.0)


def one_slow(n):
    return dict(result_after=100.0, cleanup=40.0) if n == 3 else FAST


class Summarize(unittest.TestCase):
    def test_a_run_that_meets_the_4x_bar_has_no_observations(self):
        s = target.summarize(container_result(lambda n: FAST), mission_result())
        self.assertEqual((s["observations"], s["errors"]), ([], []))
        self.assertEqual(s["container"]["status"], "PASS")
        self.assertGreaterEqual(s["container"]["verified_cycles"], 22)
        self.assertIs(s["container"]["latency_target_met"], True)
        self.assertEqual(set(s["container"]["phase_seconds"]), {"workload", "cleanup", "client"})
        self.assertEqual(s["missions"]["database_busy"]["answers"], 0)

    def test_a_cycle_over_the_4x_bound_is_an_observation(self):
        s = target.summarize(container_result(one_slow), mission_result())
        self.assertEqual(len(s["observations"]), 1)
        self.assertIn("1 of", s["observations"][0])
        self.assertIn("120 s", s["observations"][0])
        self.assertIn("slow_client 1", s["observations"][0])
        self.assertEqual(s["container"]["status"], "PASS_WITH_OBSERVATIONS")
        self.assertEqual(s["container"]["observation_counts"], {"slow_client": 1})

    def test_busy_answers_are_an_observation(self):
        s = target.summarize(container_result(lambda n: FAST), mission_result(answers=4))
        self.assertEqual(len(s["observations"]), 1)
        self.assertIn("database is busy' 4 times", s["observations"][0])
        self.assertEqual(s["missions"]["database_busy"]["longest_streak_seconds"], 21.5)

    def test_missing_or_foreign_results_are_errors(self):
        s = target.summarize(None, {"cycles": 3})
        self.assertEqual(len(s["errors"]), 2)
        self.assertEqual((s["container"], s["missions"]), (None, None))

    def test_cli_prints_one_word_and_fails_on_missing_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "c.json").write_text(json.dumps(container_result(one_slow)))
            (tmp / "m.json").write_text(json.dumps(mission_result()))
            args = ["--container", str(tmp / "c.json"), "--missions", str(tmp / "m.json"), "--output", str(tmp / "s.json")]
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = target.main(args)
            self.assertEqual((rc, out.getvalue().strip()), (0, "observed"))
            self.assertEqual(len(json.loads((tmp / "s.json").read_text())["observations"]), 1)
            (tmp / "m.json").unlink()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = target.main(args)
            self.assertEqual(rc, 1)
            self.assertEqual(json.loads((tmp / "s.json").read_text())["missions"], None)


def block(text, start, end="\nEOF\n"):
    begin = text.index(start)
    return text[begin:text.index(end, begin) + len(end)]


class StressResultJson(unittest.TestCase):
    """The result.json heredoc of stress_45m.sh, rendered by bash."""

    BLOCK = block((STRESS / "stress_45m.sh").read_text(), 'cat > "$out/result.json" <<EOF\n')

    def render(self, *, summary, observed, failures=0, cancelled=0, smoke=0):
        with tempfile.TemporaryDirectory() as tmp:
            if summary is not None:
                Path(tmp, "workload-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
            values = dict(out=tmp, release="5.0.1", boot_id="b", start_iso="s", end_iso="e", duration=2700,
                          elapsed=3000, load_elapsed=2700, drain_elapsed=300, mission_outer_seconds=4140,
                          container_outer_seconds=5100, image_id="sha256:" + "a" * 64, image_resolution_rc=0,
                          plans_rc=0, stress_rc=0, container_rc=0, container_cycles=30, mission_rc=0, probe_rc=0,
                          service_classifier_rc=0, journal_end_epoch=1, journal_end_utc="u", cancelled=cancelled,
                          probe_cycles=200, workload_summary_rc=0, workload_observed=observed, failures=failures,
                          QA_DEVELOPMENT_SMOKE=smoke)
            env = {"PATH": os.environ["PATH"], **{key: str(value) for key, value in values.items()}}
            subprocess.run(["bash", "-c", self.BLOCK], env=env, check=True)
            return json.loads(Path(tmp, "result.json").read_text())

    def test_clean_full_run_is_pass_and_carries_the_workload_verdicts(self):
        summary = target.summarize(container_result(lambda n: FAST), mission_result())
        r = self.render(summary=summary, observed="false")
        self.assertEqual(r["status"], "PASS")
        self.assertIs(r["workload_observations"], False)
        self.assertEqual(r["workloads"]["container"]["verified_cycles"], summary["container"]["verified_cycles"])

    def test_observed_run_is_never_a_plain_pass(self):
        summary = target.summarize(container_result(one_slow), mission_result())
        r = self.render(summary=summary, observed="true")
        self.assertEqual(r["status"], "PASS_WITH_OBSERVATIONS")
        self.assertEqual(r["workloads"]["container"]["observation_counts"], {"slow_client": 1})
        self.assertEqual(self.render(summary=summary, observed="true", smoke=1)["status"], "SMOKE_PASS_WITH_OBSERVATIONS")

    def test_failures_and_cancellation_outrank_observations(self):
        summary = target.summarize(container_result(one_slow), mission_result())
        self.assertEqual(self.render(summary=summary, observed="true", failures=1)["status"], "FAIL")
        self.assertEqual(self.render(summary=summary, observed="true", cancelled=1)["status"], "CANCELLED")

    def test_missing_summary_still_renders_valid_json(self):
        r = self.render(summary=None, observed="false", failures=1)
        self.assertIsNone(r["workloads"])
        self.assertEqual(r["status"], "FAIL")


class HostSummaryJson(unittest.TestCase):
    """The summary.json block of run_stress.sh, run on a fixture evidence tarball."""

    TEXT = (ROOT / "tools" / "qa_5_0_0" / "run_stress.sh").read_text()
    START = 'python3 - "$out" "$host_gate" "$run_max_load" "$release" "$root/tools/qa_5_0_0/stress" <<\'EOF\'\n'

    def test_summary_carries_stress_status_and_workload_verdicts(self):
        program = block(self.TEXT, self.START)
        program = program[len(self.START):-len("EOF\n")]
        container = container_result(one_slow)
        files = {"probe-loop.jsonl": json.dumps({"probe_cycle": 1}) + "\n" + json.dumps({"summary": True, "errors": 0}) + "\n",
                 "result.json": json.dumps({"status": "PASS_WITH_OBSERVATIONS"}),
                 "container-result.json": json.dumps(container),
                 "mission-evidence/result.json": json.dumps(mission_result(answers=2))}
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / "samples.tsv").write_text("utc\tload1\tload5\tmem\tswap\tsc\twin\thost\tvms\n"
                                             "t\t3.1\t2.0\t2400000\t0\tactive\t1\t1.5\t0\n")
            with tarfile.open(out / "guest-evidence.tgz", "w:gz") as tar:
                for name, body in files.items():
                    data = body.encode()
                    info = tarfile.TarInfo(f"sf-stress-5.0.1/{name}")
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            subprocess.run([sys.executable, "-", str(out), "idle", "8", "5.0.1", str(STRESS)],
                           input=program, text=True, check=True, capture_output=True)
            summary = json.loads((out / "summary.json").read_text())
        self.assertEqual(summary["stress_status"], "PASS_WITH_OBSERVATIONS")
        self.assertEqual(summary["workloads"]["container"]["verified_cycles"], container["verified_cycles"])
        self.assertEqual(summary["workloads"]["container"]["cycles_over_latency_target"], 1)
        self.assertEqual(len(summary["workloads"]["observations"]), 2)
        self.assertEqual(summary["probe_summary"], {"summary": True, "errors": 0})
        self.assertEqual(summary["host"]["environment"], "idle")


if __name__ == "__main__":
    unittest.main()
