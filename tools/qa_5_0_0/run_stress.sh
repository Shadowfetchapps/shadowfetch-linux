#!/usr/bin/env bash
# STRESS-01 on an installed 5.0.0 VM (already running under sfvm.py, with the
# QA user's Plasma session up): tools/qa_5_0_0/stress/stress_45m.sh (the 4.x
# canonical workload, see stress/ for the two documented 5.0 deltas), with
# ShadowCode open in the user's session for the whole run, plus host-side
# sampling and the crash/OOM/thermal/responsiveness audit.
# Usage: run_stress.sh VM_NAME [DURATION_SECONDS]
set -uo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
vm="$root/tools/qa_5_0_0/sfvm.py"
name="${1:?vm name}"; duration="${2:-2700}"
user="${QA_USER:-demo}"
out="$root/work/qa-5.0.0/stress/$(date -u +%Y%m%dT%H%M%SZ)"
guest_dir=/opt/shadowfetch-qa-5
guest_out=/var/tmp/sf-stress-5.0.0
mkdir -p "$out/shots"
exec > >(tee -a "$out/run.log") 2>&1
gx() { "$vm" exec "$name" "$1" --timeout "${2:-300}"; }
ux() { "$vm" uexec "$name" "$user" "$1" --timeout "${2:-300}"; }
say() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }

say "push helpers"
gx "rm -rf $guest_dir && install -d -m 0755 $guest_dir"
for f in stress_45m.sh mission_stress.py container_stress.py classify_service_journal.py latency_probe.py; do
  "$vm" push "$name" "$root/tools/qa_5_0_0/stress/$f" "$guest_dir/$f" --mode 0755
done
gx "chown -R root:root $guest_dir; sha256sum $guest_dir/*" | tee "$out/guest-helpers.sha256"

say "cache the Alpine image as $user (network pull is setup, not part of the load)"
ux 'podman pull -q docker.io/library/alpine:3.22 >/dev/null; podman image inspect --format "{{.Id}}" docker.io/library/alpine:3.22' 600 | tee "$out/alpine-id.txt"
alpine=$(tail -1 "$out/alpine-id.txt" | tr -d '\r\n'); alpine=${alpine#sha256:}
override=""
if [[ $alpine != b66e0ce64844f5c6435b0c4bfd965558199ab0f53270846861c979cb1ac29365 ]]; then
  say "NOTE: docker.io alpine:3.22 is now $alpine, not the 4.x pin; overriding (recorded)"
  override="QA_ALPINE_IMAGE_ID=$alpine"
fi

say "open ShadowCode in the session for the whole run"
ux 'systemctl --user reset-failed sf-qa-shadowcode 2>/dev/null; systemd-run --user --unit=sf-qa-shadowcode --property=TimeoutStopSec=20 --setenv=WAYLAND_DISPLAY=$WAYLAND_DISPLAY /usr/bin/shadowcode' | tee "$out/shadowcode-start.txt"
sleep 30
"$vm" shot "$name" "$out/shots/00-shadowcode-open.png"

gx 'date +%s' > "$out/start-epoch.txt"; since=$(tr -d '\r\n' < "$out/start-epoch.txt")
gx 'coredumpctl --no-pager --no-legend list 2>&1 | tail -20' > "$out/coredumps-before.txt"
say "start stress (duration ${duration}s)"
"$vm" exec "$name" "rm -rf $guest_out; setsid /usr/bin/env QA_RELEASE=5.0.0 QA_USER=$user QA_DURATION_SECONDS=$duration $override $guest_dir/stress_45m.sh $guest_out > /var/tmp/sf-stress-runner.log 2>&1 < /dev/null & echo started" | tee -a "$out/run.log"

printf 'utc\tload1\tload5\tmem_avail_kib\tswap_used_kib\tshadowcode\tkwin_window\n' > "$out/samples.tsv"
deadline=$(( $(date +%s) + duration + 2400 ))
n=0
while (( $(date +%s) < deadline )); do
  sleep 60; n=$((n+1))
  row=$(gx "read a b c _ < /proc/loadavg; m=\$(awk '/MemAvailable/{print \$2}' /proc/meminfo); s=\$(awk '/SwapTotal/{t=\$2}/SwapFree/{f=\$2}END{print t-f}' /proc/meminfo); printf '%s\t%s\t%s\t%s' \$a \$b \$m \$s" 60)
  sc=$(ux 'systemctl --user is-active sf-qa-shadowcode' 60 | tr -d '\r\n')
  win=$(ux 'dbus-send --session --print-reply --dest=org.kde.KWin /WindowsRunner org.kde.krunner1.Match string:ShadowCode | grep -c "string \"ShadowCode"' 60 | tr -d '\r\n')
  printf '%s\t%s\t%s\t%s\n' "$(date -u +%FT%TZ)" "$row" "$sc" "$win" >> "$out/samples.tsv"
  (( n % 10 == 0 )) && "$vm" shot "$name" "$out/shots/$(printf %02d $n)-during.png" >/dev/null
  gx "test -s $guest_out/result.json" 30 >/dev/null 2>&1 && break
done
say "stress finished or timed out"
"$vm" shot "$name" "$out/shots/99-after.png"
gx "cat $guest_out/result.json" | tee "$out/result.json"
gx "cat /var/tmp/sf-stress-runner.log" > "$out/runner.log" 2>&1
gx "tar -C /var/tmp -czf /var/tmp/sf-stress-out.tgz sf-stress-5.0.0" 600
"$vm" pull "$name" /var/tmp/sf-stress-out.tgz "$out/guest-evidence.tgz"

say "audit"
ux 'systemctl --user show sf-qa-shadowcode -p ActiveState -p SubState -p NRestarts -p MainPID -p MemoryCurrent -p MemoryPeak -p CPUUsageNSec' > "$out/shadowcode-unit.txt"
ux 'systemctl --user stop sf-qa-shadowcode; sleep 3; systemctl --user show sf-qa-shadowcode -p Result -p ActiveState; pgrep -a -f "^/usr/bin/[s]hadowcode|^/usr/lib/[s]hadowcode/" || echo no-leftovers' > "$out/shadowcode-stop.txt"
gx "coredumpctl --no-pager --no-legend list --since=@$since 2>&1" > "$out/coredumps-since-start.txt"
ux "journalctl --user --no-pager --since=@$since -u drkonqi-coredump-pickup.service 2>&1 | tail -200" > "$out/drkonqi-pickup.log"
gx "journalctl -k --no-pager --since=@$since 2>&1 | grep -iE 'out of memory|oom-kill|oom_reaper|segfault|traps:|general protection|BUG:|Call Trace|thermal|throttl' || echo none" > "$out/kernel-oom-faults-thermal.txt"
gx "journalctl --no-pager --since=@$since -p err 2>&1 | tail -300" > "$out/journal-errors.txt"
gx "ls /sys/class/thermal/ 2>&1; for z in /sys/class/thermal/thermal_zone*; do echo \$z \$(cat \$z/type \$z/temp 2>/dev/null); done" > "$out/guest-thermal.txt"
(command -v sensors >/dev/null && sensors -A 2>/dev/null | grep -E '^(Tctl|Tccd|Package|Core|edge)' ) > "$out/host-thermal-after.txt" || true
python3 - "$out" <<'EOF'
import json, sys, tarfile, statistics
from pathlib import Path
out = Path(sys.argv[1])
rows = [l.split("\t") for l in (out/"samples.tsv").read_text().splitlines()[1:] if l.count("\t") >= 6]
summary = {"samples": len(rows)}
if rows:
    load = [float(r[1]) for r in rows if r[1]]
    mem = [int(r[3]) for r in rows if r[3].isdigit()]
    summary.update(peak_load1=max(load), mean_load1=round(statistics.mean(load), 2),
                   min_mem_available_mib=round(min(mem)/1024), peak_swap_used_mib=round(max(int(r[4]) for r in rows if r[4].isdigit())/1024),
                   shadowcode_active_all=all(r[5] == "active" for r in rows),
                   shadowcode_window_all=all(r[6].strip() not in ("", "0") for r in rows))
try:
    with tarfile.open(out/"guest-evidence.tgz") as t:
        probe = t.extractfile("sf-stress-5.0.0/probe-loop.jsonl").read().decode().splitlines()
    last = json.loads(probe[-1]); summary["probe_summary"] = last
except Exception as e:
    summary["probe_summary_error"] = str(e)
(out/"summary.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary, indent=2))
EOF
say "evidence: $out"
