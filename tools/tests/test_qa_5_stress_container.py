"""5.0.1 container stress judging (tools/qa_5_0_0/stress/container_stress.py).

Pure tests drive run_profile with a fake clock and fake clients, using the
5.0.0 STRESS-01 timings. One integration test runs real client processes
against a fake `podman` with the same timings scaled down. None of this is
load evidence.
"""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

HELPER = Path(__file__).resolve().parents[1] / "qa_5_0_0" / "stress" / "container_stress.py"
spec = importlib.util.spec_from_file_location("qa5_container_stress", HELPER)
target = importlib.util.module_from_spec(spec)
spec.loader.exec_module(target)

LINE = target.EXPECTED_LINE + "\n"
# 5.0.0 STRESS-01 run 2, cycle 3: the checksum 106.6 s after the client
# started, the client's --rm removal done 138.3 s after that.
RUN2_WORKLOAD, RUN2_CLEANUP = 106.6, 138.3


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakeClient:
    """A podman run --rm client on the fake clock.

    result_after: seconds to the first stdout line (None = never prints).
    cleanup: seconds from that line to the client's exit (None = never exits).
    """

    def __init__(self, clock, argv, *, result_after=5.0, cleanup=1.0, rc=0, line=LINE, extra='',
                 exits_on_terminate=True):
        self.clock, self.argv = clock, argv
        self.started = clock.now
        self.result_after, self.cleanup, self.final_rc = result_after, cleanup, rc
        self.line, self.extra = line, extra
        self.exits_on_terminate = exits_on_terminate
        self.terminated_at = None
        self.terminate_calls = 0
        self.pid, self.start_ticks, self.stderr = 4242, "77", ""
        self.termination_errors = []

    @property
    def lines(self):
        if self.result_after is None or self.clock.now < self.started + self.result_after:
            return []
        at = self.started + self.result_after
        return [(at, self.line)] + ([(at, self.extra)] if self.extra else [])

    @property
    def exited_at(self):
        natural = None
        if self.cleanup is not None:
            natural = self.started + (self.result_after or 0) + self.cleanup
            if self.clock.now < natural:
                natural = None
        if self.terminated_at is not None and self.exits_on_terminate:
            return min(x for x in (natural, self.terminated_at) if x is not None)
        return natural

    @property
    def finished(self):
        return self.exited_at is not None

    @property
    def rc(self):
        if not self.finished:
            return None
        if self.terminated_at is not None and self.exited_at == self.terminated_at:
            return -9
        return self.final_rc

    def state(self):
        return None if self.finished else {"state": "D", "wchan": "fake"}

    def terminate(self):
        self.terminate_calls += 1
        if not self.finished:
            self.terminated_at = self.clock.now
            if not self.exits_on_terminate:
                self.termination_errors.append("Client remains after bounded TERM/KILL observation; owned-process cleanup required")


class ExitsBetweenReads(FakeClient):
    """A client whose waiter thread sets exited_at between two reads.

    The first read of exited_at after the client's exit time still says
    "running"; every later read says "exited". That is the real Client when
    the process exits while update() is between two looks at it.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.reads_after_exit = 0

    @property
    def exited_at(self):
        natural = FakeClient.exited_at.fget(self)
        if natural is None:
            return None
        self.reads_after_exit += 1
        return None if self.reads_after_exit == 1 else natural


class Profile:
    def __init__(self, *, plan=lambda n: {}, listing='', listing_rc=0, inspect='exited'):
        self.clock = Clock()
        self.plan, self.listing, self.listing_rc, self.inspect = plan, listing, listing_rc, inspect
        self.clients, self.ops, self.emitted = [], [], []
        self.live_peak = 0

    def start_client(self, argv):
        spec = dict(self.plan(len(self.clients) + 1))
        client = spec.pop('cls', FakeClient)(self.clock, argv, **spec)
        self.clients.append(client)
        live = sum(1 for c in self.clients if not c.finished)
        self.live_peak = max(self.live_peak, live)
        return client

    def run(self, argv, timeout, stopped):
        self.ops.append((argv, timeout))
        verb = argv[1]
        out = {'argv': argv, 'rc': 0, 'stdout': '', 'stderr': '', 'error': None, 'client_exited': True}
        if verb == 'ps':
            out.update(rc=self.listing_rc, stdout=self.listing)
        elif verb == 'container':
            out.update(stdout=self.inspect + '\n')
        return out

    def go(self, duration, stopped=lambda: False):
        return target.run_profile(duration, 'a' * 64, 'pure-test', run=self.run, start_client=self.start_client,
                                  stopped=stopped, clock=self.clock, sleep=self.clock.sleep,
                                  emit=lambda row: self.emitted.append(json.dumps(row)))


class Judge(unittest.TestCase):
    def view(self, lines, **kw):
        args = dict(finished=False, rc=None, started=0.0, now=10.0)
        args.update(kw)
        return target.judge(lines, **args)

    def test_no_output_yet_is_pending_workload(self):
        v = self.view([])
        self.assertEqual((v['verdict'], v['phase'], v['overdue']), ('pending', 'workload', False))

    def test_checksum_while_client_still_removing_is_pending_cleanup_not_failure(self):
        v = self.view([(RUN2_WORKLOAD, LINE)], now=RUN2_WORKLOAD + 127)
        self.assertEqual((v['verdict'], v['phase'], v['result_at']), ('pending', 'cleanup', RUN2_WORKLOAD))

    def test_checksum_and_zero_exit_is_ok(self):
        self.assertEqual(self.view([(1.0, LINE)], finished=True, rc=0)['verdict'], 'ok')

    def test_nonzero_exit_after_checksum_is_wrong_result(self):
        v = self.view([(1.0, LINE)], finished=True, rc=125)
        self.assertEqual(v['verdict'], 'wrong_result')
        self.assertIn('rc 125', v['reason'])

    def test_wrong_checksum_is_wrong_result_before_the_client_exits(self):
        v = self.view([(1.0, '0' * 64 + '  /tmp/load.bin\n')])
        self.assertEqual(v['verdict'], 'wrong_result')

    def test_extra_output_is_wrong_result(self):
        self.assertEqual(self.view([(1.0, LINE), (1.0, 'surprise\n')])['verdict'], 'wrong_result')

    def test_exit_without_checksum_is_wrong_result(self):
        v = self.view([], finished=True, rc=-9)
        self.assertEqual(v['verdict'], 'wrong_result')
        self.assertIn('without printing the checksum', v['reason'])

    def test_no_result_by_the_hang_bound_is_hung(self):
        self.assertEqual(self.view([], now=target.HANG_LIMIT - 0.1)['verdict'], 'pending')
        v = self.view([], now=target.HANG_LIMIT)
        self.assertEqual((v['verdict'], v['phase'], v['overdue']), ('hung', 'workload', True))

    def test_cleanup_hang_bound_runs_from_the_result(self):
        self.assertEqual(self.view([(100.0, LINE)], now=100.0 + target.HANG_LIMIT - 0.1)['verdict'], 'pending')
        v = self.view([(100.0, LINE)], now=100.0 + target.HANG_LIMIT)
        self.assertEqual((v['verdict'], v['phase']), ('hung', 'cleanup'))

    def test_wrong_result_outranks_hang_but_stays_bounded(self):
        v = self.view([(1.0, 'garbage\n')], now=target.HANG_LIMIT + 5)
        self.assertEqual((v['verdict'], v['overdue']), ('wrong_result', True))

    def test_bounds_follow_the_evidence(self):
        self.assertEqual(target.LIMIT, 120)
        self.assertGreaterEqual(target.CLEANUP_TIMEOUT, 2 * RUN2_CLEANUP)
        self.assertGreaterEqual(target.HANG_LIMIT, 4 * RUN2_CLEANUP)
        self.assertGreater(target.HANG_LIMIT, target.CLEANUP_TIMEOUT)

    def test_slow_phases_are_observations(self):
        kinds = [o['kind'] for o in target.phase_observations(RUN2_WORKLOAD, RUN2_CLEANUP)]
        self.assertEqual(kinds, ['slow_cleanup'])
        kinds = [o['kind'] for o in target.phase_observations(130.0, 1.0)]
        self.assertEqual(kinds, ['slow_workload'])
        self.assertEqual(target.phase_observations(10.0, None), [])


class Loop(unittest.TestCase):
    def test_5_0_0_timings_are_judged_correct_but_cannot_reach_the_floor(self):
        # Every cycle as slow as 5.0.0's worst. v2 called the first such cycle
        # a failed operation; v3 verifies each one, and the run still FAILS,
        # honestly, on the 4.x floor: a ~250 s period gives ~11 of 22 cycles.
        p = Profile(plan=lambda n: dict(result_after=RUN2_WORKLOAD, cleanup=RUN2_CLEANUP))
        r = p.go(2700)
        self.assertEqual(r['status'], 'FAIL')
        self.assertEqual(r['minimum_cycles'], 22)
        self.assertEqual(r['verdicts'], {'ok': r['cycles']})
        self.assertEqual(r['verified_cycles'], 11)
        self.assertEqual([f['error'] for f in r['failures']], ['Insufficient sustained container activity'])
        self.assertIsNone(r['primary_error'])
        self.assertEqual(r['cleanup_errors'], [])
        self.assertIs(r['final_container_exists'], False)
        self.assertEqual(r['phase_seconds']['cleanup']['max'], RUN2_CLEANUP)
        self.assertEqual(r['phase_seconds']['workload']['max'], RUN2_WORKLOAD)

    def test_one_client_at_a_time_like_4x(self):
        # Overlap was withdrawn: podman serializes a removal with the next
        # create/start, so overlapping clients only added load.
        p = Profile(plan=lambda n: dict(result_after=RUN2_WORKLOAD, cleanup=RUN2_CLEANUP))
        p.go(2700)
        self.assertEqual(p.live_peak, 1)
        self.assertFalse(hasattr(target, 'MAX_OUTSTANDING_CLEANUPS'))

    def test_every_cycle_is_the_canonical_run_with_its_own_name_and_no_retry(self):
        p = Profile(plan=lambda n: dict(result_after=1.0, cleanup=0.2))
        p.go(60)
        names = []
        for client in p.clients:
            argv = client.argv
            self.assertEqual(argv[:2], ['podman', 'run'])
            for option in ('--rm', '--pull=never', '--network=none', '--memory=256m', '--pids-limit=64'):
                self.assertIn(option, argv)
            self.assertEqual(argv[-4:], ['a' * 64, 'sh', '-c', target.COMMAND])
            names.append(argv[argv.index('--name') + 1])
        self.assertEqual(names, [f'sfqa-stress-pure-test-{n}' for n in range(1, len(names) + 1)])
        # All clients exited 0, so no exact-name rm is needed; the listing still runs.
        self.assertEqual([argv[1] for argv, _ in p.ops], ['ps'])

    def test_pause_after_each_client_exit_like_4x(self):
        p = Profile(plan=lambda n: dict(result_after=1.0, cleanup=0.2))
        p.go(60)
        for before, after in zip(p.clients, p.clients[1:]):
            self.assertGreaterEqual(after.started - before.exited_at, target.PAUSE)
            self.assertLessEqual(after.started - before.exited_at, target.PAUSE + 2 * target.POLL)

    def test_cleanup_past_timeout_is_probed_and_observed_not_failed(self):
        slow = target.CLEANUP_TIMEOUT + 100
        p = Profile(plan=lambda n: dict(result_after=10.0, cleanup=slow if n == 1 else 1.0))
        r = p.go(900)
        self.assertNotIn('Container client hung', [f['error'] for f in r['failures']])
        timeouts = [o for o in r['observations'] if o['kind'] == 'cleanup_timeout']
        self.assertEqual(len(timeouts), 1)
        self.assertEqual(timeouts[0]['probe']['container_state'], 'exited')
        self.assertTrue(any(argv[1:3] == ['container', 'inspect'] for argv, _ in p.ops))
        first = json.loads(next(line for line in p.emitted if '"container_cycle": 1,' in line))
        self.assertEqual(first['verdict'], 'ok')
        self.assertTrue(any(json.loads(line).get('container_observation') == 'cleanup_timeout' for line in p.emitted))

    def test_container_that_never_produces_its_result_is_hung_and_stops_the_loop(self):
        p = Profile(plan=lambda n: dict(result_after=None, cleanup=None) if n == 3 else dict(result_after=5.0, cleanup=1.0))
        r = p.go(2700)
        self.assertEqual(r['status'], 'FAIL')
        self.assertEqual(r['primary_error']['error'], 'Container client hung')
        self.assertEqual(r['primary_error']['phase'], 'workload')
        self.assertEqual(r['cycles'], 3)
        self.assertEqual(p.clients[2].terminate_calls, 1)
        removal = next(argv for argv, _ in p.ops if argv[1] == 'rm')
        self.assertEqual(removal[-1], 'sfqa-stress-pure-test-3')

    def test_client_that_never_finishes_its_cleanup_is_hung(self):
        p = Profile(plan=lambda n: dict(result_after=5.0, cleanup=None))
        r = p.go(2700)
        self.assertEqual(r['primary_error']['error'], 'Container client hung')
        self.assertEqual(r['primary_error']['phase'], 'cleanup')
        self.assertTrue(any(o['kind'] == 'cleanup_timeout' for o in r['observations']))
        self.assertEqual(r['status'], 'FAIL')

    def test_wrong_checksum_fails_and_is_not_retried(self):
        p = Profile(plan=lambda n: dict(line='0' * 64 + '  /tmp/load.bin\n'))
        r = p.go(2700)
        self.assertEqual(r['status'], 'FAIL')
        self.assertEqual(r['primary_error']['error'], 'Container produced a wrong result')
        self.assertEqual(len(p.clients), 1)

    def test_nonzero_exit_after_correct_checksum_fails(self):
        p = Profile(plan=lambda n: dict(rc=1))
        r = p.go(2700)
        self.assertEqual(r['primary_error']['error'], 'Container produced a wrong result')
        self.assertIn('rc 1', r['primary_error']['reason'])

    def test_exit_between_reads_after_checksum_with_rc_1_still_fails(self):
        # Review of v3: update() judged finished=False, then read finished
        # again, saw True and finalize() turned 'pending' into 'ok'.
        p = Profile(plan=lambda n: dict(cls=ExitsBetweenReads, result_after=5.0, cleanup=1.0, rc=1))
        r = p.go(60)
        self.assertEqual(r['status'], 'FAIL')
        self.assertEqual(r['primary_error']['error'], 'Container produced a wrong result')
        self.assertIn('rc 1', r['primary_error']['reason'])
        self.assertEqual(r['verified_cycles'], 0)
        first = json.loads(next(line for line in p.emitted if '"container_cycle": 1,' in line))
        self.assertEqual(first['verdict'], 'wrong_result')

    def test_exit_between_reads_without_output_fails_and_does_not_crash(self):
        # The same race with no checksum: v3 recorded 'ok' with no result
        # time, and sorting the result times raised TypeError after cleanup.
        def plan(n):
            if n == 3:
                return dict(cls=ExitsBetweenReads, result_after=None, cleanup=3.0, rc=1)
            return dict(result_after=1.0, cleanup=0.2)
        p = Profile(plan=plan)
        r = p.go(60)
        self.assertEqual(r['status'], 'FAIL')
        self.assertEqual(r['primary_error']['cycle'], 3)
        self.assertIn('without printing the checksum', r['primary_error']['reason'])
        self.assertEqual(r['verified_cycles'], 2)
        self.assertEqual(r['verdicts'], {'ok': 2, 'wrong_result': 1})

    def test_remaining_container_fails_even_after_clean_cycles(self):
        p = Profile(plan=lambda n: dict(result_after=1.0, cleanup=0.2),
                    listing='sfqa-unrelated\nsfqa-stress-pure-test-2\nsfqa-stress-other-run-2\n')
        r = p.go(60)
        self.assertEqual(r['status'], 'FAIL')
        self.assertIs(r['final_container_exists'], True)
        self.assertEqual(r['cleanup_errors'][0]['containers'], ['sfqa-stress-pure-test-2'])

    def test_unverified_listing_fails(self):
        p = Profile(plan=lambda n: dict(result_after=1.0, cleanup=0.2), listing_rc=125)
        r = p.go(60)
        self.assertEqual(r['status'], 'FAIL')
        self.assertIsNone(r['final_container_exists'])

    def test_client_that_outlives_termination_is_a_cleanup_error(self):
        p = Profile(plan=lambda n: dict(result_after=None, cleanup=None, exits_on_terminate=False))
        r = p.go(2700)
        self.assertTrue(any(e['error'] == 'Podman client exit was not verified' for e in r['cleanup_errors']))
        self.assertEqual(r['status'], 'FAIL')

    def test_cancellation_terminates_live_clients(self):
        p = Profile(plan=lambda n: dict(result_after=50.0, cleanup=50.0))
        r = p.go(2700, stopped=lambda: p.clock.now > 1000.0 + 120)
        self.assertEqual(r['status'], 'CANCELLED')
        self.assertTrue(all(c.finished for c in p.clients))
        # Cycle 1 exits at +100 s; at +120 s cycle 2 (from +105 s) is in its workload.
        self.assertEqual(r['verdicts'], {'cancelled': 1, 'ok': 1})
        self.assertEqual([c.terminate_calls for c in p.clients], [0, 1])

    def test_log_rows_count_cycles_once_and_summary_carries_no_cycle_key(self):
        # stress_45m.sh counts '"container_cycle"' lines in container-loop.log.
        p = Profile(plan=lambda n: dict(result_after=10.0, cleanup=target.CLEANUP_TIMEOUT + 10 if n == 1 else 1.0))
        r = p.go(600)
        counted = [line for line in p.emitted if '"container_cycle"' in line]
        self.assertEqual(len(counted), r['cycles'])
        self.assertNotIn('"container_cycle"', json.dumps(r))


FAKE_PODMAN = r'''#!{python}
import os, signal, sys, time
LINE = {line!r}
args = sys.argv[1:]
if args[0] == 'run':
    time.sleep(float(os.environ['FAKE_WORKLOAD']))
    sys.stdout.write(LINE + '\n'); sys.stdout.flush()
    # podman run forwards TERM to the container (--sig-proxy); after the
    # container has exited the client keeps removing it.
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(float(os.environ['FAKE_CLEANUP']))
    sys.exit(0)
if args[0] == 'container' and args[1] == 'exists':
    sys.exit(1)
if args[0] == 'container' and args[1] == 'inspect':
    print('exited'); sys.exit(0)
sys.exit(0)  # rm --force --ignore, ps -a (prints nothing: nothing remains)
'''


class FakePodmanBinary(unittest.TestCase):
    """Real client processes; 5.0.0 run 2's timings scaled down 100x."""

    def test_correct_result_with_slow_removal_passes_and_is_observed(self):
        scale = 100.0
        with tempfile.TemporaryDirectory() as tmp:
            podman = Path(tmp) / 'podman'
            podman.write_text(FAKE_PODMAN.format(python=sys.executable, line=target.EXPECTED_LINE))
            podman.chmod(podman.stat().st_mode | stat.S_IXUSR)
            env = {'PATH': tmp + os.pathsep + os.environ['PATH'],
                   'FAKE_WORKLOAD': str(RUN2_WORKLOAD / scale), 'FAKE_CLEANUP': str(RUN2_CLEANUP / scale)}
            scaled = dict(LIMIT=120 / scale, CLIENT_GRACE=0.5)
            for name, value in dict(CLEANUP_TIMEOUT=300 / scale, HANG_LIMIT=600 / scale, FINAL_OP_TIMEOUT=10,
                                    PROBE_TIMEOUT=10, PAUSE=0.05, POLL=0.02, KILL_GRACE=2).items():
                if hasattr(target, name):
                    scaled[name] = value
            rows = []
            with patch.dict(os.environ, env), contextlib.ExitStack() as stack:
                for name, value in scaled.items():
                    stack.enter_context(patch.object(target, name, value))
                r = target.run_profile(3, 'a' * 64, 'fake-binary', emit=rows.append)
        self.assertEqual(r['status'], 'SMOKE_PASS', r['failures'])
        self.assertGreaterEqual(r['cycles'], 2)
        self.assertTrue(all(row['verdict'] == 'ok' and row['exit'] == 0 for row in rows if 'container_cycle' in row))
        self.assertTrue(any(o['kind'] == 'slow_cleanup' for o in r['observations']))
        self.assertEqual(r['cleanup_errors'], [])


if __name__ == '__main__':
    unittest.main()
