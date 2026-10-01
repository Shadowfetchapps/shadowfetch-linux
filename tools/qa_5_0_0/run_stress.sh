#!/usr/bin/env bash
# STRESS-01 on an installed VM of the release under test (already running
# under sfvm.py, with the QA user's Plasma session up):
# tools/qa_5_0_0/stress/stress_45m.sh (the 4.x canonical workload, see stress/
# for the two documented 5.0 deltas), with ShadowCode open in the user's
# session for the whole run, plus host-side sampling and the
# crash/OOM/thermal/responsiveness audit.
# Usage: QA_RELEASE=MAJOR.MINOR.PATCH run_stress.sh VM_NAME [DURATION_SECONDS]
# QA_RELEASE is required and is passed to the guest runner, which refuses an
# installed release that differs from it.
#
# Idle host only (5.0.1). Both 5.0.0 runs shared the host with unrelated builds
# (host load up to 32, kswapd at 100%) and with other VMs, which starved the
# guest's disk on top of its own stress load. The run refuses to start while
# the host's 1-minute load is above QA_HOST_MAX_LOAD (default 4) or another
# QEMU VM is running, samples the host beside the guest, and summary.json
# says whether the host stayed quiet ("idle") or not ("contended": load above
# QA_HOST_MAX_RUN_LOAD, default half the host's CPUs, or another VM appeared).
# A contended run's result says nothing about the image either way.
# QA_ALLOW_BUSY_HOST=1 starts anyway and records that it did.
set -uo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
vm="$root/tools/qa_5_0_0/sfvm.py"
name="${1:?vm name}"; duration="${2:-2700}"
release="${QA_RELEASE:?Set QA_RELEASE to the release under test, e.g. QA_RELEASE=5.0.1}"
[[ $release =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "QA_RELEASE must be MAJOR.MINOR.PATCH" >&2; exit 2; }
[[ $duration =~ ^[0-9]+$ ]] || { echo "DURATION_SECONDS must be a whole number" >&2; exit 2; }
user="${QA_USER:-demo}"
[[ $user =~ ^[a-z_][a-z0-9_-]*$ ]] || { echo "QA_USER must be a plain user name" >&2; exit 2; }
max_load="${QA_HOST_MAX_LOAD:-4}"
run_max_load="${QA_HOST_MAX_RUN_LOAD:-$(( $(nproc) / 2 ))}"
out="$root/work/qa-$release/stress/$(date -u +%Y%m%dT%H%M%SZ)"
guest_dir=/opt/shadowfetch-qa-5
guest_out=/var/tmp/sf-stress-$release
# The guest runner's exit status, written when it exits: a runner that stops
# before result.json (a refused release, a missing tool) ends the wait.
runner_rc=/var/tmp/sf-stress-runner.rc
host_load1() { cut -d' ' -f1 /proc/loadavg; }
# QEMU processes other than this run's VM (sfvm.py names it sf-qa-NAME).
other_vms() { pgrep -a -f '^[^ ]*qemu-system' | grep -cvF -- "-name sf-qa-$name " || true; }
above() { awk -v a="$1" -v b="$2" 'BEGIN { exit !(a > b) }'; }
gate_load="$(host_load1)"; gate_vms="$(other_vms)"
if above "$gate_load" "$max_load" || (( gate_vms > 0 )); then
  if [[ ${QA_ALLOW_BUSY_HOST:-0} != 1 ]]; then
    echo "Host is not idle (1-min load $gate_load, limit $max_load; other VMs $gate_vms). Stress only on an idle host, or set QA_ALLOW_BUSY_HOST=1 to record a contended run." >&2
    exit 2
  fi
  host_gate=overridden
else
  host_gate=idle
fi
mkdir -p "$out/shots"
exec > >(tee -a "$out/run.log") 2>&1
printf 'gate=%s\nload1=%s\nmax_load=%s\nrun_max_load=%s\nother_vms=%s\ncpus=%s\n' \
  "$host_gate" "$gate_load" "$max_load" "$run_max_load" "$gate_vms" "$(nproc)" > "$out/host-before.txt"
gx() { "$vm" exec "$name" "$1" --timeout "${2:-300}"; }
ux() { "$vm" uexec "$name" "$user" "$1" --timeout "${2:-300}"; }
say() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
say "host gate: $host_gate (1-min load $gate_load, other VMs $gate_vms)"

say "push helpers"
gx "rm -rf $guest_dir && install -d -m 0755 $guest_dir"
for f in stress_45m.sh mission_stress.py container_stress.py classify_service_journal.py latency_probe.py; do
  "$vm" push "$name" "$root/tools/qa_5_0_0/stress/$f" "$guest_dir/$f" --mode 0755
done
gx "chown -R root:root $guest_dir; sha256sum $guest_dir/*" | tee "$out/guest-helpers.sha256"

say "cache the Alpine image as $user (network pull is setup, not part of the load)"
ux 'podman pull -q docker.io/library/alpine:3.22 >/dev/null; podman image inspect --format "{{.Id}}" docker.io/library/alpine:3.22' 600 | tee "$out/alpine-id.txt"
alpine=$(tail -1 "$out/alpine-id.txt" | tr -d '\r\n'); alpine=${alpine#sha256:}
[[ $alpine =~ ^[a-f0-9]{64}$ ]] || { say "guest did not report a 64-hex Alpine image ID"; exit 2; }
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
"$vm" exec "$name" "rm -rf $guest_out $runner_rc; setsid /bin/sh -c '/usr/bin/env QA_RELEASE=$release QA_USER=$user QA_DURATION_SECONDS=$duration $override $guest_dir/stress_45m.sh $guest_out; echo \$? > $runner_rc' > /var/tmp/sf-stress-runner.log 2>&1 < /dev/null & echo started" | tee -a "$out/run.log"

printf 'utc\tload1\tload5\tmem_avail_kib\tswap_used_kib\tshadowcode\tkwin_window\thost_load1\thost_other_vms\n' > "$out/samples.tsv"
# The guest's container helper may legitimately run to duration + 2400 s (see
# stress_45m.sh), and the audit after it takes minutes.
deadline=$(( $(date +%s) + duration + 3000 ))
n=0
while (( $(date +%s) < deadline )); do
  sleep 60; n=$((n+1))
  row=$(gx "read a b c _ < /proc/loadavg; m=\$(awk '/MemAvailable/{print \$2}' /proc/meminfo); s=\$(awk '/SwapTotal/{t=\$2}/SwapFree/{f=\$2}END{print t-f}' /proc/meminfo); printf '%s\t%s\t%s\t%s' \$a \$b \$m \$s" 60)
  sc=$(ux 'systemctl --user is-active sf-qa-shadowcode' 60 | tr -d '\r\n')
  win=$(ux 'dbus-send --session --print-reply --dest=org.kde.KWin /WindowsRunner org.kde.krunner1.Match string:ShadowCode | grep -c "string \"ShadowCode"' 60 | tr -d '\r\n')
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$(date -u +%FT%TZ)" "$row" "$sc" "$win" "$(host_load1)" "$(other_vms)" >> "$out/samples.tsv"
  (( n % 10 == 0 )) && "$vm" shot "$name" "$out/shots/$(printf %02d $n)-during.png" >/dev/null
  gx "test -s $guest_out/result.json || test -e $runner_rc" 30 >/dev/null 2>&1 && break
done
gx "cat $runner_rc 2>/dev/null || echo still-running" 30 > "$out/runner.rc" 2>&1
say "stress finished or timed out (guest runner exit: $(tr -d '\r\n' < "$out/runner.rc"))"
"$vm" shot "$name" "$out/shots/99-after.png"
gx "cat $guest_out/result.json" | tee "$out/result.json"
gx "cat /var/tmp/sf-stress-runner.log" > "$out/runner.log" 2>&1
gx "tar -C /var/tmp -czf /var/tmp/sf-stress-out.tgz sf-stress-$release" 600
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
python3 - "$out" "$host_gate" "$run_max_load" "$release" <<'EOF'
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
def number(value):
    try:
        return float(value)
    except ValueError:
        return None
host_load = [v for v in (number(r[7]) for r in rows if len(r) > 8) if v is not None]
host_vms = [int(r[8]) for r in rows if len(r) > 8 and r[8].strip().isdigit()]
limit = float(sys.argv[3])
contended = sys.argv[2] != "idle" or max(host_load, default=0) > limit or any(host_vms)
summary["host"] = {"gate": sys.argv[2], "run_max_load1": limit, "samples": len(host_load),
                   "peak_load1": max(host_load, default=None),
                   "mean_load1": round(statistics.mean(host_load), 2) if host_load else None,
                   "max_other_vms": max(host_vms, default=None),
                   "environment": "contended" if contended else "idle" if host_load else "unknown"}
try:
    with tarfile.open(out/"guest-evidence.tgz") as t:
        probe = t.extractfile(f"sf-stress-{sys.argv[4]}/probe-loop.jsonl").read().decode().splitlines()
    last = json.loads(probe[-1]); summary["probe_summary"] = last
except Exception as e:
    summary["probe_summary_error"] = str(e)
(out/"summary.json").write_text(json.dumps(summary, indent=2) + "\n")
print(json.dumps(summary, indent=2))
EOF
say "evidence: ${out#"$root"/} (host environment: see summary.json)"
