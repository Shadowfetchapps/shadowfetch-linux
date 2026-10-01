#!/bin/sh
# Run the 5.0.1 Mission Control regression tests against the INSTALLED engine,
# inside the guest, as the desktop user.
#
# The tests load sf_missions.py from ../data/usr/lib/shadowfetch/missions
# relative to themselves (tests/missions_regression.py), so this builds that
# layout in a scratch directory from a byte-for-byte copy of the installed
# /usr/lib/shadowfetch/missions and checks the copy against the installed
# files before running anything. The slow-disk tests delay fsync with strace
# and are skipped where strace is not installed; the log says which ran.
# Usage: run_mission_regressions.sh TESTS_DIR OUT_DIR
set -u
tests=${1:?directory holding the copied test modules}
out=${2:?output directory}
mkdir -p "$out"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/tests" "$work/data/usr/lib/shadowfetch/missions"
cp "$tests"/*.py "$work/tests/"
cp -a /usr/lib/shadowfetch/missions/. "$work/data/usr/lib/shadowfetch/missions/"
( cd /usr/lib/shadowfetch/missions && sha256sum ./*.py ) > "$out/engine-sha256.txt"
( cd "$work/data/usr/lib/shadowfetch/missions" && sha256sum -c --quiet "$out/engine-sha256.txt" ) \
  || { echo "copied engine differs from the installed one" >&2; exit 2; }
{
  echo "engine: $(/usr/bin/shadowfetch-missions --version)"
  echo "package: $(dpkg-query -W -f='${Package} ${Version}' shadowfetch-missions)"
  echo "strace: $(command -v strace || echo absent)"
} > "$out/regressions-env.txt"
cd "$work/tests" || exit 2
python3 -m unittest -v test_busy_database_5_0_1 test_saved_stop_5_0_1 \
  test_queue_order_5_0_1 test_review_hold_5_0_1 > "$out/regressions.log" 2>&1
rc=$?
echo "exit=$rc" >> "$out/regressions-env.txt"
tail -n 4 "$out/regressions.log"
exit $rc
