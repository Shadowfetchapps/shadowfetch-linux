#!/usr/bin/env bash
# Serve the local 5.0.0 APT repository (repo/) read-only to QEMU user-mode
# guests. Guests reach the host's loopback as 10.0.2.2, so the server binds
# 127.0.0.1 only: nothing outside this machine can reach it.
#   serve_repo.sh start|stop|status   (port: SF_REPO_PORT, default 8805)
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
port="${SF_REPO_PORT:-8805}"
state="$root/work/qa-5.0.0/repo-server"
mkdir -p "$state"
case "${1:-status}" in
  start)
    if [[ -f $state/pid ]] && kill -0 "$(<"$state/pid")" 2>/dev/null; then echo "running pid $(<"$state/pid")"; exit 0; fi
    [[ -f $root/repo/dists/umbra/InRelease ]] || { echo "no signed repo at $root/repo" >&2; exit 1; }
    setsid python3 -m http.server "$port" --bind 127.0.0.1 --directory "$root/repo" \
      >"$state/access.log" 2>&1 </dev/null &
    echo $! >"$state/pid"; sleep 1
    curl -fsS "http://127.0.0.1:$port/dists/umbra/InRelease" >/dev/null && echo "serving $root/repo on 127.0.0.1:$port (guest: http://10.0.2.2:$port/)"
    ;;
  stop) [[ -f $state/pid ]] && kill "$(<"$state/pid")" 2>/dev/null; rm -f "$state/pid"; echo stopped ;;
  status) [[ -f $state/pid ]] && kill -0 "$(<"$state/pid")" 2>/dev/null && echo "running pid $(<"$state/pid")" || echo "not running" ;;
esac
