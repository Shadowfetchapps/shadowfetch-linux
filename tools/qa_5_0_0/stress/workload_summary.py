#!/usr/bin/env python3
"""Lift the two workload helpers' own verdicts into the STRESS-01 result.

stress_45m.sh's result.json used to carry only the helpers' exit codes and a
count of started container cycles, and run_stress.sh's summary.json only the
probe loop. The v3 helpers tolerate two things 4.x stopped on -- a container
client over the 4.x 120 s bound finishes and is recorded, and a "database is
busy" answer from Mission Control is asked again -- so what they tolerated
must show at the level the acceptance record and the 5.0.0 waiver are
written from, not only in the helpers' own files.

A workload "observation" is a run that met every correctness criterion but
not the 4.x bar: container cycles over the 4.x per-operation bound, or busy
answers from Mission Control. stress_45m.sh then reports PASS_WITH_OBSERVATIONS
(SMOKE_PASS_WITH_OBSERVATIONS), never PASS. Accepting such a run for STRESS-01
is the release owner's recorded decision.

CLI (stress_45m.sh): writes --output and prints one word, "observed" or
"clean". Exit 1 when a helper result is missing or is not a helper result:
that is a failure, not an observation.
"""
import argparse
import json
import os
from pathlib import Path
import sys

CONTAINER_KEYS = ('status', 'qa_profile', 'cycles', 'verified_cycles', 'minimum_cycles', 'coverage_seconds',
                  'latency_target_seconds', 'latency_target_met', 'cycles_over_latency_target',
                  'observation_counts', 'phase_seconds', 'primary_error')
MISSION_KEYS = ('status', 'qa_profile', 'cycles', 'completed_within_load_window', 'coverage_seconds')


def load(path):
    """A helper's JSON result, or None when it is missing or unreadable."""
    try:
        with open(path) as stream:
            value = json.load(stream)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def summarize(container, missions):
    """The part of each helper result that decides how STRESS-01 reads."""
    errors, observations = [], []
    out = {'container': None, 'missions': None}
    if container is None or 'cycles_over_latency_target' not in container:
        errors.append('container helper result is missing, unreadable or not a v3 result')
    else:
        out['container'] = {key: container.get(key) for key in CONTAINER_KEYS}
        over = container['cycles_over_latency_target']
        if over:
            counts = container.get('observation_counts') or {}
            detail = ', '.join(f'{kind} {count}' for kind, count in sorted(counts.items()))
            observations.append(
                f"{over} of {container.get('cycles')} container cycles exceeded the 4.x "
                f"{container.get('latency_target_seconds', 120)} s per-operation bound, which failed a 4.x run"
                + (f' ({detail})' if detail else ''))
    if missions is None or 'database_busy' not in missions:
        errors.append('mission helper result is missing, unreadable or not a v3 result')
    else:
        out['missions'] = {key: missions.get(key) for key in MISSION_KEYS}
        busy = missions.get('database_busy') or {}
        out['missions']['database_busy'] = {key: busy.get(key) for key in
                                            ('answers', 'retried', 'not_retried', 'longest_streak_seconds')}
        stop = missions.get('worker_stop') or {}
        out['missions']['worker_stop'] = {key: stop.get(key) for key in ('exited', 'signals', 'seconds')}
        if busy.get('answers'):
            observations.append(
                f"Mission Control answered 'database is busy' {busy['answers']} times "
                f"({busy.get('retried', 0)} asked again, longest streak {busy.get('longest_streak_seconds')} s); "
                "4.x and 5.0.0 stopped on the first")
    out['observations'] = observations
    out['errors'] = errors
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--container', type=Path, required=True)
    parser.add_argument('--missions', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    summary = summarize(load(args.container), load(args.missions))
    partial = args.output.with_name(args.output.name + '.partial')
    partial.write_text(json.dumps(summary, indent=2) + '\n')
    os.replace(partial, args.output)
    print('observed' if summary['observations'] else 'clean')
    return 1 if summary['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
