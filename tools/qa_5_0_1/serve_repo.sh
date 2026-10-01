#!/usr/bin/env bash
# Serve the local 5.0.1 APT repository (repo/, from `make repo`) read-only to
# QEMU user-mode guests -- tools/qa_5_0_0/serve_repo.sh with this release's
# paths. Guests reach the host's loopback as 10.0.2.2, so the server binds
# 127.0.0.1 only: nothing outside this machine can reach it.
#   serve_repo.sh start|stop|status   (port: SF_REPO_PORT, default 8815)
# The default port is not 5.0.0's 8805, so a 5.0.0 server left running by
# another QA lane is never mistaken for this one.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
port="${SF_REPO_PORT:-8815}"
state="$root/work/qa-5.0.1/repo-server"
mkdir -p "$state"
case "${1:-status}" in
  start)
    if [[ -f $state/pid ]] && kill -0 "$(<"$state/pid")" 2>/dev/null; then echo "running pid $(<"$state/pid")"; exit 0; fi
    [[ -f $root/repo/dists/umbra/InRelease ]] || { echo "no signed repo at $root/repo" >&2; exit 1; }
    setsid python3 -m http.server "$port" --bind 127.0.0.1 --directory "$root/repo" \
      >"$state/access.log" 2>&1 </dev/null &
    echo $! >"$state/pid"; sleep 1
    # The served index must be the built one, byte for byte.
    curl -fsS "http://127.0.0.1:$port/dists/umbra/InRelease" | cmp - "$root/repo/dists/umbra/InRelease" \
      && echo "serving repo/ on 127.0.0.1:$port (guest: http://10.0.2.2:$port/)"
    ;;
  stop) [[ -f $state/pid ]] && kill "$(<"$state/pid")" 2>/dev/null; rm -f "$state/pid"; echo stopped ;;
  status) [[ -f $state/pid ]] && kill -0 "$(<"$state/pid")" 2>/dev/null && echo "running pid $(<"$state/pid")" || echo "not running" ;;
esac
