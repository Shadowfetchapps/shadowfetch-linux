#!/usr/bin/env bash
# Diagnose the shadowcode-soak FAIL (idle CPU, MemAvailable drift) on a live VM
# whose display is kept ON. For each cycle: start ShadowCode exactly as the
# harness does (systemd-run --user), let it settle 15 s, then measure the
# unit's CPU over 45 s with per-process attribution, MemAvailable, the live
# overlay's tmpfs usage and ShadowCode's on-disk state; stop it and re-measure.
# Usage: shadowcode_idle_probe.sh VM [CYCLES] [USER]
set -uo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
vm="$root/tools/qa_5_0_0/sfvm.py"
name=${1:?vm}; cycles=${2:-6}; user=${3:-shadow}
out="$root/work/qa-5.0.0/diag/shadowcode-idle-$(date -u +%H%M%S)"; mkdir -p "$out"
ux() { "$vm" uexec "$name" "$user" "$1" --timeout "${2:-200}"; }
gx() { "$vm" exec "$name" "$1" --timeout "${2:-120}"; }
state() { gx "echo avail_kib=\$(awk '/MemAvailable/{print \$2}' /proc/meminfo); df -k --output=used /run/live/overlay 2>/dev/null | tail -1 | sed 's/^ */overlay_used_kib=/'; du -sk /home/$user/.cache /home/$user/.local/share /home/$user/.config 2>/dev/null | tr '\n' ' '; echo"; }
{
echo "== baseline"; state
for i in $(seq 1 "$cycles"); do
  unit=sf-diag-sc-$i
  "$vm" move "$name" 1000 500 >/dev/null; "$vm" move "$name" 1900 300 >/dev/null
  ux "systemd-run --user --unit=$unit --setenv=WAYLAND_DISPLAY=\$WAYLAND_DISPLAY /usr/bin/shadowcode >/dev/null 2>&1"
  sleep 15
  ux "a=\$(systemctl --user show $unit -p CPUUsageNSec --value); p1=\$(for p in \$(systemctl --user show $unit -p MainPID --value) \$(pgrep -f WebKit); do echo \$p:\$(awk '{print \$14+\$15}' /proc/\$p/stat 2>/dev/null); done); sleep 45; b=\$(systemctl --user show $unit -p CPUUsageNSec --value); echo cycle=$i unit_cpu_pct=\$(( (b-a)/450000000 )); for e in \$p1; do p=\${e%%:*}; t=\${e#*:}; n=\$(awk '{print \$14+\$15}' /proc/\$p/stat 2>/dev/null); [ -n \"\$n\" ] && echo \"  \$(cat /proc/\$p/comm) pid=\$p cpu_pct=\$(( (n-t)*100/100/45 ))\"; done; systemctl --user show $unit -p MemoryCurrent" 200
  "$vm" shot "$name" "$out/cycle-$i.png" >/dev/null
  ux "systemctl --user stop $unit"; sleep 5
  echo "after close:"; state
done
echo "== ShadowCode state on disk"; gx "du -sh /home/$user/.local/share/* /home/$user/.cache/* 2>/dev/null | sort -h | tail -15"
} 2>&1 | tee "$out/probe.log"
