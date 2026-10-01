#!/usr/bin/env python3
"""Real rootless container load with bounded, separately reported cleanup.

QA-only v3 (5.0.1). The WORKLOAD is the 4.x canonical one and is unchanged:
each cycle is one `podman run --rm --pull=never --network=none --memory=256m
--pids-limit=64 IMAGE sh -c 'dd 32 MiB; sha256sum'`, then a 5-second pause,
from the shared load start until the load window ends. No cycle is retried.

What v3 changes is how a cycle is JUDGED. In both 5.0.0 STRESS-01 runs, cycle
3's container printed the correct checksum about 105 s after its client
started; the client then spent another 97-138 s in podman's --rm removal and
state syncs on the saturated guest disk. v2 put one 120 s limit over the whole
client, so a correct, fully removed container was reported as "Container
operation or actual data checksum failed" and the loop stopped at 3 of 22.

* RESULT and CLIENT EXIT are judged separately. The result is the container's
  stdout, verified the moment it arrives: exactly the expected checksum line.
  The lifecycle is two phases timed from the client's own output and exit:
  workload (client start -> result) and cleanup (result -> client exit: conmon
  exit, --rm removal and podman's state syncs).
* A cycle FAILS only on a wrong result -- unexpected output, a client that
  exits without the checksum, or a nonzero client exit -- or on a hang: a
  client still running HANG_LIMIT after its phase began. A hung client's
  container state is probed for the record, the client is terminated, and the
  loop stops.
* Slowness is an OBSERVATION, never a failure. A phase over the 4.x 120 s
  target is recorded as slow_workload / slow_cleanup. A cleanup still running
  CLEANUP_TIMEOUT after the result is recorded as cleanup_timeout together with
  a bounded `podman container inspect` of its state (still running, or only
  being removed?), and is watched on until it exits or reaches HANG_LIMIT.
* Cleanup overlaps the next cycle. Once a cycle's result is verified the loop
  pauses 5 s and starts the next one; at most MAX_OUTSTANDING_CLEANUPS earlier
  clients may still be removing, so the load stays bounded and close to 4.x.
  Each cycle has its own name (sfqa-stress-RUN-N): a container that is still
  being removed still holds its name.
* Final cleanup does not trust the clients: exact-name `podman rm --force
  --ignore` for every container whose client did not exit 0, then one
  `podman ps -a` listing proves none of this run's containers remain.

Pure tests inject the client, the operation runner and the clock; they are not
load evidence.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import sys
import threading
import time

# The 4.x per-operation latency target. A phase over it is an observation.
LIMIT = 120
# Evidence-based cleanup timeout: about twice the slowest --rm removal seen in
# 5.0.0 STRESS-01 (138 s after the checksum, run 2; 97 s in run 1). A cleanup
# still running then is recorded with a probe of its container's state.
CLEANUP_TIMEOUT = 300
# Generous hang bound per phase: 5.6x the slowest workload phase seen (107 s)
# and 4.3x the slowest removal. Still running then = hung, and the cycle fails.
HANG_LIMIT = 600
# Final exact-name removal and listing. 5.0.0's harness `podman rm --force`
# took 67 s and 117 s under the same load.
FINAL_OP_TIMEOUT = 300
# A state probe is evidence only; it must not stall the loop for long.
PROBE_TIMEOUT = 60
# Two removal slots: with the slowest phases seen (workload 107 s + 5 s pause,
# removal 138 s) the cycle period stays at the workload's ~112 s, 24 cycles in
# 2700 s against the 22 required; one slot would pace it at 138 s, 19 cycles.
MAX_OUTSTANDING_CLEANUPS = 2
PAUSE = 5.0
POLL = 0.5
CLIENT_GRACE = 5
# After SIGKILL. 5.0.0's killed clients took 2-3.7 s more to go (disk waits).
KILL_GRACE = 30
QA_PROFILE = 'production-default900s-v3'

EXPECTED_SHA256 = hashlib.sha256(bytes(32 * 1024 * 1024)).hexdigest()
EXPECTED_LINE = EXPECTED_SHA256 + '  /tmp/load.bin'
COMMAND = 'set -eu; dd if=/dev/zero of=/tmp/load.bin bs=1M count=32 2>/dev/null; sha256sum /tmp/load.bin'


def text(value):
    return value.decode('utf-8', 'replace') if isinstance(value, bytes) else value or ''


def start_ticks(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rpartition(') ')[2].split()[19]
    except (OSError, IndexError):
        return None


def proc_state(pid):
    """Kernel state and wait channel of a live process (D = waiting on I/O)."""
    try:
        state = Path(f'/proc/{pid}/stat').read_text().rpartition(') ')[2].split()[0]
    except (OSError, IndexError):
        return None
    try:
        wchan = Path(f'/proc/{pid}/wchan').read_text().strip() or None
    except OSError:
        wchan = None
    return {'state': state, 'wchan': wchan}


def operation(argv, timeout=FINAL_OP_TIMEOUT, stopped=lambda: False):
    """Bound a Podman client without losing its first timeout or partial output.

    Linux uninterruptible I/O can outlast signals; report an unexited client.
    """
    started = time.monotonic()
    result = {'argv': argv, 'timeout_seconds': timeout, 'rc': None,
              'stdout': '', 'stderr': '', 'error': None, 'client_pid': None,
              'client_exited': True, 'termination_errors': []}
    proc = None
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        result['client_pid'] = proc.pid
        result['client_start_ticks'] = start_ticks(proc.pid)
        while True:
            remaining = timeout - (time.monotonic() - started)
            if stopped() or remaining <= 0:
                result['error'] = 'cancelled' if stopped() else 'operation timeout'
                break
            try:
                stdout, stderr = proc.communicate(timeout=min(1.0, remaining))
                result.update(stdout=stdout, stderr=stderr, rc=proc.returncode)
                break
            except subprocess.TimeoutExpired as exc:
                result.update(stdout=text(exc.stdout), stderr=text(exc.stderr))
    except Exception as exc:
        result['error'] = result['error'] or 'client operation exception: ' + str(exc)
        if proc is not None:
            result['client_exited'] = proc.poll() is not None
            result['rc'] = proc.returncode
    finally:
        if proc is not None and proc.poll() is None:
            result['client_state'] = proc_state(proc.pid)
            for signum, grace in ((signal.SIGTERM, CLIENT_GRACE), (signal.SIGKILL, KILL_GRACE)):
                try:
                    os.killpg(proc.pid, signum)
                except ProcessLookupError:
                    pass
                except OSError as exc:
                    result['termination_errors'].append(str(exc))
                try:
                    stdout, stderr = proc.communicate(timeout=grace)
                    result.update(stdout=stdout, stderr=stderr, rc=proc.returncode)
                    break
                except subprocess.TimeoutExpired as exc:
                    result.update(stdout=text(exc.stdout), stderr=text(exc.stderr))
                except Exception as exc:
                    result['termination_errors'].append('Client termination observation exception: ' + str(exc))
            result['client_exited'] = proc.poll() is not None
            result['rc'] = proc.returncode
            if not result['client_exited']:
                result['termination_errors'].append('Client remains after bounded TERM/KILL observation; owned-process cleanup required')
        if proc is not None:
            for stream in (proc.stdout, proc.stderr):
                if stream:
                    try:
                        stream.close()
                    except OSError as exc:
                        result['termination_errors'].append('Client output close failed: ' + str(exc))
        result['seconds'] = time.monotonic() - started
    return result


def run_argv(name, image):
    """The 4.x canonical cycle; only the per-cycle name suffix is new."""
    return ['podman', 'run', '--name', name, '--rm', '--pull=never', '--network=none',
            '--memory=256m', '--pids-limit=64', image, 'sh', '-c', COMMAND]


class Client:
    """One `podman run --rm` client, observed without blocking the loop.

    Reader threads timestamp each stdout line as it arrives and a waiter thread
    timestamps the exit, so phase times stay exact while the loop is busy.
    """

    def __init__(self, argv, clock=time.monotonic):
        self.argv = argv
        self.clock = clock
        self.lines = []
        self.stderr = ''
        self.rc = None
        self.exited_at = None
        self.termination_errors = []
        self.started = clock()
        self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, errors='replace', start_new_session=True)
        self.pid = self.proc.pid
        self.start_ticks = start_ticks(self.pid)
        self._readers = [threading.Thread(target=self._read_stdout, daemon=True),
                         threading.Thread(target=self._read_stderr, daemon=True)]
        for reader in self._readers:
            reader.start()
        self._waiter = threading.Thread(target=self._wait, daemon=True)
        self._waiter.start()

    def _read_stdout(self):
        for line in self.proc.stdout:
            self.lines.append((self.clock(), line))

    def _read_stderr(self):
        self.stderr = self.proc.stderr.read()

    def _wait(self):
        rc = self.proc.wait()
        at = self.clock()
        for reader in self._readers:
            reader.join(timeout=5)
        self.rc = rc
        self.exited_at = at

    @property
    def finished(self):
        return self.exited_at is not None

    def state(self):
        return None if self.proc.returncode is not None else proc_state(self.pid)

    def terminate(self):
        """TERM (podman forwards it to the container), then KILL; never raises."""
        for signum, grace in ((signal.SIGTERM, CLIENT_GRACE), (signal.SIGKILL, KILL_GRACE)):
            if self.finished or self.proc.returncode is not None:
                break
            try:
                os.killpg(self.pid, signum)
            except ProcessLookupError:
                pass
            except OSError as exc:
                self.termination_errors.append(str(exc))
            self._waiter.join(timeout=grace)
        if not self.finished:
            self.termination_errors.append('Client remains after bounded TERM/KILL observation; owned-process cleanup required')


def judge(lines, *, finished, rc, started, now, expected_line=EXPECTED_LINE, hang_limit=None):
    """Classify one cycle at one instant. Pure: a list of (time, line), no I/O.

    verdict is 'wrong_result', 'ok' (client exited 0 after the exact checksum),
    'hung' (still running hang_limit after its current phase began) or
    'pending'. A wrong result outranks everything; overdue is reported
    separately so a client that printed garbage is still bounded.
    """
    hang_limit = HANG_LIMIT if hang_limit is None else hang_limit
    output = [line.rstrip('\r\n') for _, line in lines]
    result_at = lines[0][0] if output and output[0] == expected_line else None
    reason = None
    if output and output[0] != expected_line:
        reason = 'container printed an unexpected first line instead of the expected checksum'
    elif any(line.strip() for line in output[1:]):
        reason = 'container printed unexpected output after the checksum'
    elif finished and result_at is None:
        reason = f'client exited (rc {rc}) without printing the checksum'
    elif finished and rc != 0:
        reason = f'client exited rc {rc} after the correct checksum: the container or its --rm removal failed'
    phase = 'done' if finished else 'workload' if result_at is None else 'cleanup'
    phase_started = started if result_at is None else result_at
    overdue = not finished and now - phase_started >= hang_limit
    verdict = 'wrong_result' if reason else 'ok' if finished else 'hung' if overdue else 'pending'
    return {'verdict': verdict, 'phase': phase, 'reason': reason, 'result_at': result_at, 'overdue': overdue}


def phase_observations(workload_seconds, cleanup_seconds, limit=None):
    """Slow but correct phases. Observations only; they never fail a cycle."""
    limit = LIMIT if limit is None else limit
    found = []
    if workload_seconds is not None and workload_seconds > limit:
        found.append({'kind': 'slow_workload', 'seconds': round(workload_seconds, 3), 'target_seconds': limit})
    if cleanup_seconds is not None and cleanup_seconds > limit:
        found.append({'kind': 'slow_cleanup', 'seconds': round(cleanup_seconds, 3), 'target_seconds': limit})
    return found


def spread(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return {'n': 0}
    p95 = values[min(len(values) - 1, math.ceil(.95 * len(values)) - 1)]
    return {'n': len(values), 'p50': round(statistics.median(values), 3),
            'p95': round(p95, 3), 'max': round(values[-1], 3)}


def run_profile(duration, image, run_id, *, run=operation, start_client=Client, stopped=lambda: False,
                clock=time.monotonic, sleep=time.sleep, emit=lambda row: print(json.dumps(row), flush=True),
                load_start=None):
    prefix = f'sfqa-stress-{run_id}-'
    start = clock() if load_start is None else load_start
    cycles, live, rows = [], [], []
    failures, cleanup_errors, cleanup_operations, probe_operations, observations = [], [], [], [], []
    primary_error = None
    final_exists = None

    def invoke(argv, timeout):
        try:
            return run(argv, timeout=timeout, stopped=lambda: False)
        except Exception as exc:
            return {'argv': argv, 'rc': None, 'error': 'operation wrapper exception: ' + str(exc),
                    'stdout': '', 'stderr': '', 'client_exited': None}

    def fail(error):
        nonlocal primary_error
        failures.append(error)
        if primary_error is None:
            primary_error = error

    def probe(cycle, phase, now):
        value = invoke(['podman', 'container', 'inspect', '--format', '{{.State.Status}}', cycle['name']], PROBE_TIMEOUT)
        probe_operations.append(value)
        found = {'phase': phase, 'at_seconds': round(now - cycle['client'].started, 3),
                 'client_state': cycle['client'].state(), 'rc': value.get('rc'),
                 'container_state': value.get('stdout', '').strip() or None,
                 'stderr': value.get('stderr', '').strip()[-500:], 'error': value.get('error')}
        cycle['probes'].append(found)
        return found

    def wrong_result(cycle, reason, rc, lines):
        cycle['verdict'], cycle['reason'] = 'wrong_result', reason
        fail({'error': 'Container produced a wrong result', 'cycle': cycle['number'], 'name': cycle['name'],
              'reason': reason, 'rc': rc,
              'stdout': ''.join(line for _, line in lines)[-2000:], 'stderr': cycle['client'].stderr[-2000:]})

    def look(client):
        """ONE consistent look at a client: (exited_at, rc, lines).

        The waiter thread drains stdout, then sets rc, then exited_at. So
        exited_at is read first, and whenever it is set the rc and output read
        after it are final. Judging one look and acting on a later one let a
        client that exited in between be finalized unjudged: a nonzero exit,
        or an exit with no checksum at all, was recorded as 'ok'.
        """
        exited_at = client.exited_at
        rc = client.rc if exited_at is not None else None
        return exited_at, rc, list(client.lines)

    def finalize(cycle, now):
        client = cycle['client']
        exited_at, rc, lines = look(client)
        if cycle['verdict'] == 'pending':
            # Never promoted by default: only a judgment of the client's final
            # state may say 'ok'.
            if exited_at is None:
                cycle['verdict'], cycle['reason'] = 'aborted', 'finalized before its client exited'
            else:
                view = judge(lines, finished=True, rc=rc, started=client.started, now=now)
                if cycle['result_at'] is None:
                    cycle['result_at'] = view['result_at']
                if view['reason']:
                    wrong_result(cycle, view['reason'], rc, lines)
                else:
                    cycle['verdict'] = 'ok'
        result_at = cycle['result_at']
        workload = None if result_at is None else result_at - client.started
        cleanup = None if result_at is None or exited_at is None else exited_at - result_at
        found = phase_observations(workload, cleanup) + cycle['observations']
        row = {'container_cycle': cycle['number'], 'name': cycle['name'], 'verdict': cycle['verdict'],
               'reason': cycle['reason'],
               'started_elapsed': round(client.started - start, 3),
               'result_elapsed': None if result_at is None else round(result_at - start, 3),
               'workload_seconds': None if workload is None else round(workload, 3),
               'cleanup_seconds': None if cleanup is None else round(cleanup, 3),
               'seconds': round((exited_at if exited_at is not None else now) - client.started, 3),
               'exit': rc, 'stdout': ''.join(line for _, line in lines), 'stderr': client.stderr,
               'client_pid': client.pid, 'client_start_ticks': client.start_ticks,
               'client_exited': exited_at is not None, 'termination_errors': list(client.termination_errors),
               'observations': found, 'probes': cycle['probes'], 'argv': client.argv}
        rows.append(row)
        for item in found:
            observations.append({'cycle': cycle['number'], **item})
        if cycle in live:
            live.remove(cycle)
        emit(row)

    def stop_client(cycle, verdict, now):
        cycle['client'].terminate()
        if cycle['verdict'] == 'pending':
            cycle['verdict'] = verdict
        finalize(cycle, now)

    def update(cycle, now):
        client = cycle['client']
        exited_at, rc, lines = look(client)
        finished = exited_at is not None
        view = judge(lines, finished=finished, rc=rc, started=client.started, now=now)
        if cycle['result_at'] is None and view['result_at'] is not None:
            cycle['result_at'] = view['result_at']
        if view['reason'] and cycle['verdict'] == 'pending':
            wrong_result(cycle, view['reason'], rc, lines)
        if finished:
            if cycle['verdict'] == 'pending':
                cycle['verdict'] = view['verdict']  # 'ok': a finished client is never left pending
            finalize(cycle, now)
            return
        if view['overdue']:
            found = probe(cycle, view['phase'], now)
            if cycle['verdict'] == 'pending':
                cycle['verdict'] = 'hung'
                cycle['reason'] = f'client still running {HANG_LIMIT} s into its {view["phase"]} phase'
                fail({'error': 'Container client hung', 'cycle': cycle['number'], 'name': cycle['name'],
                      'phase': view['phase'], 'hang_limit_seconds': HANG_LIMIT, 'probe': found})
            stop_client(cycle, 'hung', now)
            return
        if cycle['result_at'] is not None and not cycle['timeout_noted'] and now - cycle['result_at'] >= CLEANUP_TIMEOUT:
            cycle['timeout_noted'] = True
            found = probe(cycle, 'cleanup', now)
            note = {'kind': 'cleanup_timeout', 'seconds': round(now - cycle['result_at'], 3),
                    'cleanup_timeout_seconds': CLEANUP_TIMEOUT, 'probe': found}
            cycle['observations'].append(note)
            emit({'container_observation': 'cleanup_timeout', 'cycle': cycle['number'], 'name': cycle['name'], **note})

    def launch():
        number = len(cycles) + 1
        cycle = {'number': number, 'name': f'{prefix}{number}', 'result_at': None, 'verdict': 'pending',
                 'reason': None, 'observations': [], 'probes': [], 'timeout_noted': False}
        cycle['client'] = start_client(run_argv(cycle['name'], image))
        cycles.append(cycle)
        live.append(cycle)
        return cycle

    try:
        workload, next_start = None, start
        while not stopped() and primary_error is None and clock() - start < duration:
            for cycle in list(live):
                update(cycle, clock())
            if workload is not None and (workload['result_at'] is not None or workload not in live):
                workload, next_start = None, clock() + PAUSE
            if primary_error is not None or stopped() or clock() - start >= duration:
                break
            removing = sum(1 for cycle in live if cycle is not workload)
            if workload is None and clock() >= next_start and removing < MAX_OUTSTANDING_CLEANUPS:
                workload = launch()
                continue
            sleep(POLL)
        # Bounded drain: every started cycle is judged to its end or its hang bound.
        while live:
            if stopped():
                for cycle in list(live):
                    stop_client(cycle, 'cancelled', clock())
                break
            for cycle in list(live):
                update(cycle, clock())
            if live:
                sleep(POLL)
    except Exception as exc:
        fail({'error': 'Container loop exception: ' + str(exc)})
    finally:
        for cycle in list(live):
            try:
                stop_client(cycle, 'aborted', clock())
            except Exception as exc:
                cleanup_errors.append({'error': 'Client termination exception: ' + str(exc), 'cycle': cycle['number']})
                if cycle in live:
                    live.remove(cycle)
        # Cleanup does not trust the clients: remove what was not confirmed by a
        # zero exit, then list independently. Each step is bounded and runs
        # even when the one before it failed. Nothing is retried.
        confirmed = {row['name'] for row in rows if row['client_exited'] and row['exit'] == 0}
        unconfirmed = [cycle['name'] for cycle in cycles if cycle['name'] not in confirmed]
        if unconfirmed:
            removal = invoke(['podman', 'rm', '--force', '--ignore', *unconfirmed], FINAL_OP_TIMEOUT)
            cleanup_operations.append(removal)
            if removal.get('error') or removal.get('rc') != 0:
                cleanup_errors.append({'error': 'Exact-name container removal failed', 'operation': removal})
        # Every name, filtered here: a server-side name filter that ever
        # stopped matching would silently hide a leftover.
        listing = invoke(['podman', 'ps', '-a', '--format', '{{.Names}}'], FINAL_OP_TIMEOUT)
        cleanup_operations.append(listing)
        if listing.get('error') or listing.get('rc') != 0:
            cleanup_errors.append({'error': 'Final container listing is unverified', 'operation': listing})
        else:
            ours = {cycle['name'] for cycle in cycles}
            remaining = sorted(name for name in listing.get('stdout', '').split() if name in ours)
            final_exists = bool(remaining)
            if remaining:
                cleanup_errors.append({'error': 'Named QA containers remain after cleanup', 'containers': remaining,
                                       'operation': listing})
        for value in [*rows, *probe_operations, *cleanup_operations]:
            if value.get('client_exited') is not True:
                cleanup_errors.append({'error': 'Podman client exit was not verified',
                                       'operation': {key: value.get(key) for key in ('name', 'argv', 'client_pid', 'termination_errors')}})
    verified = [row for row in rows if row['verdict'] == 'ok']
    marks = sorted(row['result_elapsed'] for row in verified if row['result_elapsed'] is not None)
    coverage = max(0.0, min(marks[-1], duration) - min(marks[0], duration)) if len(marks) > 1 else 0.0
    minimum = max(1, duration // 120)
    if len(verified) < minimum or (duration >= 120 and coverage < .75 * duration):
        failures.append({'error': 'Insufficient sustained container activity', 'required_cycles': minimum,
                         'verified_cycles': len(verified), 'started_cycles': len(rows), 'coverage_seconds': coverage})
    failed = bool(failures or cleanup_errors or final_exists is not False)
    return {'summary': True, 'qa_profile': QA_PROFILE,
            'status': 'CANCELLED' if stopped() else 'FAIL' if failed else 'PASS' if duration >= 2700 else 'SMOKE_PASS',
            'cycles': len(rows), 'verified_cycles': len(verified), 'minimum_cycles': minimum,
            'coverage_seconds': coverage, 'load_window_seconds': duration, 'elapsed_seconds': clock() - start,
            'tail_seconds': max(0.0, clock() - start - duration),
            'expected_sha256': EXPECTED_SHA256, 'image_id': image, 'container_name_prefix': prefix,
            'latency_target_seconds': LIMIT, 'cleanup_timeout_seconds': CLEANUP_TIMEOUT,
            'hang_limit_seconds': HANG_LIMIT, 'final_operation_timeout_seconds': FINAL_OP_TIMEOUT,
            'max_outstanding_cleanups': MAX_OUTSTANDING_CLEANUPS, 'pause_seconds': PAUSE,
            'client_termination_grace_seconds': CLIENT_GRACE, 'client_kill_observation_seconds': KILL_GRACE,
            'phase_seconds': {'workload': spread(row['workload_seconds'] for row in rows),
                              'cleanup': spread(row['cleanup_seconds'] for row in rows)},
            'verdicts': {verdict: sum(row['verdict'] == verdict for row in rows) for verdict in sorted({row['verdict'] for row in rows})},
            'observations': observations, 'primary_error': primary_error, 'failures': failures,
            'cleanup_errors': cleanup_errors, 'cleanup_operations': cleanup_operations,
            'final_container_exists': final_exists, 'cancelled': stopped(), 'load_start_monotonic': start}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--duration', type=int, required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--load-start-monotonic', type=float)
    args = parser.parse_args()
    if os.geteuid() == 0 or args.duration < 10 or not args.run_id.replace('-', '').isalnum() or not re.fullmatch(r'(?:sha256:)?[a-f0-9]{64}', args.image):
        parser.error('Desktop user, duration>=10, fixed SHA256 image and safe run ID required')
    if args.load_start_monotonic is not None and (not math.isfinite(args.load_start_monotonic) or not 0 <= time.monotonic() - args.load_start_monotonic <= 120):
        parser.error('Load start must identify current shared guest monotonic window')
    if args.output.exists() or args.output.is_symlink():
        parser.error('Refusing to overwrite earlier container result')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    stopped = False
    def stop(sig, frame):
        nonlocal stopped
        stopped = True
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    result = run_profile(args.duration, args.image, args.run_id,
                         stopped=lambda: stopped, load_start=args.load_start_monotonic)
    with args.output.open('x') as stream:
        json.dump(result, stream, indent=2)
        stream.write('\n')
    args.output.chmod(0o600)
    print(json.dumps(result), flush=True)
    return 130 if result['cancelled'] else 1 if result['status'] == 'FAIL' else 0


if __name__ == '__main__':
    sys.exit(main())
