#!/usr/bin/env bash
# UPGRADE-01 for 5.0.1 (APT-only update): take a disk installed from the
# SHIPPED 5.0.0 ISO (or the 4.1 upgrade base, QA_USER=demo), point its
# Shadowfetch APT source at the locally built and signed 5.0.1 repository
# (tools/qa_5_0_1/serve_repo.sh), run the upgrade, reboot, and check the
# result. RELEASE-5.0.1.md tells 5.0.0 and 4.1 users to install 5.0.1 with
# `sudo apt update && sudo apt full-upgrade` (UPGRADE_METHOD=apt, the default);
# UPGRADE_METHOD=fireproof runs `fireproof update` instead, to record what the
# 5.0.0 Fireproof daemon does with the 5.0.1 packages.
#
#   upgrade_from_500.sh BASE_DISK OUT_DIR PHASE...
#
# Phases, in order (each can be re-run on its own against the running VM):
#   setup     copy-on-write overlay of BASE_DISK (never written), first boot,
#             SDDM autologin for the test user (TEST SETUP), a first login.
#   baseline  5.0.0 facts: packages, sources, doctor, units, the Discover
#             notifier, seeded user data and its manifest, a pre-upgrade
#             mission, the Mission Control probes and regression tests (which
#             are expected to FAIL on 5.0.0).
#   upgrade   repoint ONLY the Shadowfetch source at http://10.0.2.2:PORT/
#             (signed-by kept: the signature check stays on), `apt-get update`,
#             `fireproof check` (analyze only, recorded), then the upgrade:
#             UPGRADE_METHOD=apt (default, the documented command)
#               `apt-get -y full-upgrade` as root, i.e. `sudo apt full-upgrade`
#               answered yes, conffile prompts keeping the local version;
#             UPGRADE_METHOD=fireproof
#               `fireproof update -y` as root (an admin's authenticated update).
#             Then reboot (RELEASE-5.0.1.md says to log out and back in once; a
#             reboot does that and proves the boot).
#             The fireproof client runs in a pty inside its own scope; if
#             fireproofd sits in a deferred stop with dpkg gone for 3 minutes
#             the phase records STUCK and leaves the VM running (no power cut).
#   verify    5.0.1 facts and the checks; writes checks.txt.
#   cleanup   stop and delete the overlay. The repo server is left to the caller.
#
# ShadowCode's settings are seeded through its own window between `setup` and
# `baseline` (see the summary for the clicks); `baseline` records them.
# Nothing is copied from the host home into the guest. Networking is QEMU
# user-mode; the guest reaches the host's loopback-only repo server as 10.0.2.2.
set -uo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
vm="$root/tools/qa_5_0_0/sfvm.py"
here="$root/tools/qa_5_0_1"
base=${1:?base disk (installed from the shipped 5.0.0 ISO)}
out=${2:?output directory}
shift 2
name=${QA_VM:-u501upg}
user=${QA_USER:-qa}
port=${SF_REPO_PORT:-8815}
mkdir -p "$out"
# Evidence never carries the host's home path: it is shown as ~.
exec > >(sed -u "s|$HOME|~|g" | tee -a "$out/run.log") 2>&1

step() { printf '\n== %s %s\n' "$(date -u +%FT%TZ)" "$*"; }
gx() { "$vm" exec "$name" "$1" --timeout "${2:-600}"; }
ux() { "$vm" uexec "$name" "$user" "$1" --timeout "${2:-600}"; }
# Long operations run in a transient system unit, not as a child of the guest
# agent: an update that restarts qemu-guest-agent would otherwise kill them.
gunit() { # unit, command, timeout-seconds, log
  local unit=$1 cmd=$2 limit=$3 log=$4 rc="" end
  gx "rm -f /var/log/$unit.rc; systemd-run --unit=$unit --collect /bin/sh -c '$cmd > /var/log/$unit.log 2>&1; echo \$? > /var/log/$unit.rc'"
  end=$(( $(date +%s) + limit ))
  while (( $(date +%s) < end )); do
    sleep 15
    rc=$("$vm" exec "$name" "cat /var/log/$unit.rc 2>/dev/null" --timeout 60 2>/dev/null | tr -d '\r\n') || true
    [[ -n $rc ]] && break
  done
  gx "cat /var/log/$unit.log" 120 > "$log" || true
  echo "$unit exit=${rc:-TIMEOUT}"
  [[ $rc == 0 ]]
}
wait_session() {
  "$vm" wait "$name" --timeout 900 || return 1
  for _ in $(seq 90); do gx "pgrep -u $user -x plasmashell >/dev/null" 30 >/dev/null 2>&1 && return 0; sleep 5; done
  return 1
}
reboot_vm() {
  "$vm" stop "$name" --timeout 300
  "$vm" start "$name" --medium installed --cpus 4 --mem 6144
  wait_session
  sleep 60   # autostarts and Plasma settle
}
push_tools() {
  gx 'install -d -m 755 /opt/qa /opt/qa/mtests'
  "$vm" push "$name" "$here/guest/missions_probe.py" /opt/qa/missions_probe.py --mode 755
  "$vm" push "$name" "$here/guest/run_mission_regressions.sh" /opt/qa/run_mission_regressions.sh --mode 755
  local tar
  tar=$(mktemp)
  tar -C "$root/packages/shadowfetch-missions/tests" -cf "$tar" missions_regression.py \
    mission_states.py test_busy_database_5_0_1.py test_saved_stop_5_0_1.py \
    test_queue_order_5_0_1.py test_review_hold_5_0_1.py
  "$vm" push "$name" "$tar" /opt/qa/mtests.tar
  rm -f "$tar"
  gx 'tar -C /opt/qa/mtests -xf /opt/qa/mtests.tar && chmod -R a+rX /opt/qa'
  ux 'install -d -m 700 /tmp/qa-out'
}
pull_dir() { # guest dir, local dir
  gx "tar -C '$1' -czf /tmp/qa-pull.tgz . && chmod 644 /tmp/qa-pull.tgz" 300
  "$vm" pull "$name" /tmp/qa-pull.tgz "$2/.pull.tgz" >/dev/null
  tar -C "$2" -xzf "$2/.pull.tgz" && rm -f "$2/.pull.tgz"
}
first_party='shadow-code grub-btrfs "shadowfetch-*"'
pkg_query="dpkg-query -W -f='\${Package}\t\${Version}\t\${db:Status-Abbrev}\n' $first_party 2>/dev/null | grep -v -P '\tun ?\$'"
# Files a person made or chose: compared byte for byte across the upgrade.
manifest_cmd='cd ~ && find Documents Pictures Music Projects Workspaces .local/share/applications \
  .config/shadow-agent .local/state/shadow-agent/shadow-agent.db .bashrc qa-blob.bin \
  -xdev -type f ! -path "*/.git/*" -print0 2>/dev/null | LC_ALL=C sort -z | xargs -0 sha256sum'
# doctor as the desktop user, which is how it is meant to run (as root it
# refuses to collect from /root and reports that as failures).
doctor_cmd='shadowfetch-doctor --json > /tmp/qa-out/doctor.json 2>/tmp/qa-out/doctor.err; echo "exit=$?" >> /tmp/qa-out/doctor.err; shadowfetch-doctor > /tmp/qa-out/doctor.txt 2>&1'

facts() { # prefix
  local p=$1
  gx 'cat /usr/share/shadowfetch/version; grep -E "^(PRETTY_NAME|VERSION_ID)=" /etc/os-release; uname -r; systemctl is-system-running' > "$out/$p-identity.txt" 2>&1
  gx "$pkg_query" > "$out/$p-first-party.tsv" 2>&1
  gx "dpkg-query -W -f='\${Package}:\${Architecture}\t\${Version}\t\${db:Status-Abbrev}\n' | LC_ALL=C sort" > "$out/$p-all-packages.tsv" 2>&1
  gx 'dpkg --audit; echo "audit-exit=$?"' > "$out/$p-dpkg-audit.txt" 2>&1
  gx 'systemctl --failed --no-legend --plain; echo "--"; systemctl list-units --state=failed --all --no-legend --plain | wc -l' > "$out/$p-failed-system.txt" 2>&1
  ux 'systemctl --user --failed --no-legend --plain; echo "--"; systemctl --user list-units --state=failed --all --no-legend --plain | wc -l' > "$out/$p-failed-user.txt" 2>&1
  gx 'for f in /etc/apt/sources.list /etc/apt/sources.list.d/*; do echo "--- $f"; cat "$f"; done' > "$out/$p-sources.txt" 2>&1
  ux 'u="app-org.kde.discover.notifier@autostart.service"; systemctl --user show "$u" -p LoadState -p ActiveState -p SubState -p ConditionResult -p DropInPaths -p MainPID; echo "--- process"; pgrep -a -u "$USER" -f "libexec/[D]iscoverNotifier"; echo "--- live markers"; ls -d /run/live/medium /run/live/rootfs 2>&1; echo "--- drop-in"; ls -la /usr/lib/systemd/user/app-org.kde.discover.notifier@autostart.service.d/ 2>&1' > "$out/$p-discover-notifier.txt" 2>&1
  ux "$doctor_cmd" 900
  "$vm" pull "$name" /tmp/qa-out/doctor.json "$out/$p-doctor.json" >/dev/null
  "$vm" pull "$name" /tmp/qa-out/doctor.txt "$out/$p-doctor.txt" >/dev/null
  "$vm" pull "$name" /tmp/qa-out/doctor.err "$out/$p-doctor.err" >/dev/null
  ux "$manifest_cmd" > "$out/$p-user-data.sha256" 2>&1
  ux 'shadowfetch-missions --json list --limit 0' > "$out/$p-missions-list.json" 2>&1
  ux 'shadowfetch-missions --json audit verify; echo "exit=$?"' > "$out/$p-missions-audit.txt" 2>&1
  ux 'shadowcode --version' > "$out/$p-shadowcode-version.txt" 2>&1
}

phase_setup() {
  step "setup: overlay over the shipped 5.0.0 install (base is never written)"
  qemu-img info -U "$base" | sed -n '1,4p' | sed "s|$HOME|~|g" > "$out/base-image.txt"
  stat -c 'size=%s mtime=%Y' "$base" > "$out/base-stat-before.txt"
  "$vm" clone "$name" --base "$base"
  "$vm" start "$name" --medium installed --cpus 4 --mem 6144
  "$vm" wait "$name" --timeout 900
  gx 'cat /usr/share/shadowfetch/version; hostname; id qa; systemctl is-system-running; loginctl list-sessions --no-legend' > "$out/setup-first-boot.txt" 2>&1
  step "TEST SETUP: SDDM autologin for $user, so a real Plasma session exists without typing a password"
  gx "install -d /etc/sddm.conf.d && printf '[Autologin]\nUser=$user\nSession=plasma\nRelogin=false\n' > /etc/sddm.conf.d/zz-qa-autologin.conf && cat /etc/sddm.conf.d/zz-qa-autologin.conf" > "$out/setup-autologin.txt"
  reboot_vm
  "$vm" shot "$name" "$out/shots/00-5.0.0-first-login.png" >/dev/null
  push_tools
}

phase_baseline() {
  step "baseline (5.0.0)"
  push_tools
  step "seed user data (a person's files, made before the upgrade)"
  ux 'set -e; mkdir -p ~/Documents ~/Pictures ~/Music ~/.local/share/applications
      printf "# Notes kept across the 5.0.1 upgrade\n- written on 5.0.0\n" > ~/Documents/qa-notes.md
      head -c 1048576 /dev/urandom > ~/qa-blob.bin
      ffmpeg -nostdin -v error -y -f lavfi -i testsrc2=size=640x360 -frames:v 1 ~/Pictures/qa-picture.png
      ffmpeg -nostdin -v error -y -f lavfi -i sine=frequency=330:sample_rate=44100 -t 2 ~/Music/qa-tone.flac
      printf "[Desktop Entry]\nType=Application\nName=QA My App\nExec=true\n" > ~/.local/share/applications/qa-my-app.desktop
      grep -q qa-upgrade-marker ~/.bashrc || printf "\n# qa-upgrade-marker\nalias qa-hello=\"echo hello\"\n" >> ~/.bashrc
      ls -la ~/Documents ~/Pictures ~/Music ~/.local/share/applications' > "$out/baseline-seed.txt" 2>&1
  step "a mission made on 5.0.0, left for review"
  ux 'python3 - <<"EOF"
import subprocess, sys
sys.path.insert(0, "/opt/qa")
import missions_probe as p
p.make_workspace("qa-pre-a")
r = p.create("qa-pre-a", "QA pre-upgrade media")
mid = r["payload"]["id"]
print(mid, p.wait_terminal([mid], 300)[mid])
EOF' > "$out/baseline-pre-upgrade-mission.txt" 2>&1
  step "Mission Control probes on the 5.0.0 engine (expected to fail there)"
  ux 'python3 /opt/qa/missions_probe.py busy /tmp/qa-out/busy-5.0.0.json qa-busy-500' 900 > "$out/baseline-busy-verdict.txt" 2>&1
  "$vm" pull "$name" /tmp/qa-out/busy-5.0.0.json "$out/missions/busy-5.0.0.json" >/dev/null
  ux '/opt/qa/run_mission_regressions.sh /opt/qa/mtests /tmp/qa-out/regressions-5.0.0' 1200 > "$out/baseline-regressions-tail.txt" 2>&1
  mkdir -p "$out/missions/regressions-5.0.0"
  pull_dir /tmp/qa-out/regressions-5.0.0 "$out/missions/regressions-5.0.0"
  facts before
  gx 'ls /var/lib/apt/lists/ | grep -c _Packages; ls -la --time-style=full-iso /var/lib/apt/lists/ | grep -i shadowfetch' > "$out/before-apt-lists.txt" 2>&1
}

phase_upgrade() {
  step "upgrade: served repository (host side)"
  {
    echo "served: $(curl -fsS "http://127.0.0.1:$port/dists/umbra/InRelease" | sha256sum | cut -c1-64) InRelease"
    echo "built:  $(sha256sum < "$root/repo/dists/umbra/InRelease" | cut -c1-64) InRelease"
    grep -E '^(Date|Valid-Until):' "$root/repo/dists/umbra/Release"
    echo "Packages sha256 $(sha256sum < "$root/repo/dists/umbra/main/binary-amd64/Packages" | cut -c1-64)"
    for f in "$root"/repo/pool/main/*/*/*.deb; do
      b=$(basename "$f"); if cmp -s "$f" "$root/build/$b"; then echo "pool==build $b"; else echo "POOL!=BUILD $b"; fi
    done
  } > "$out/served-repo-check.txt" 2>&1
  step "point the Shadowfetch source at the 5.0.1 repository; signed-by unchanged"
  gx "cp -a /etc/apt/sources.list.d/shadowfetch.list /root/qa-shadowfetch.list.5.0.0 && \
      sed -i 's|https://www.shadowfetch.com/linux/apt|http://10.0.2.2:$port/|' /etc/apt/sources.list.d/shadowfetch.list && \
      cat /etc/apt/sources.list.d/shadowfetch.list" > "$out/upgrade-source-switch.txt" 2>&1
  grep -q "signed-by=/usr/share/keyrings/shadowfetch.gpg\] http://10.0.2.2:$port/ umbra main" "$out/upgrade-source-switch.txt" \
    || { echo "source switch did not take" >&2; return 1; }
  step "sudo apt update"
  gunit qa-apt-update 'DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 update' 1800 "$out/upgrade-apt-update.log" || return 1
  gx 'apt-cache policy shadowfetch-desktop shadowfetch-missions shadowfetch-defaults shadow-code grub-btrfs' > "$out/upgrade-apt-policy.txt" 2>&1
  gx 'apt-get -s -o Debug::NoLocking=1 full-upgrade' > "$out/upgrade-simulate.txt" 2>&1
  step "fireproof check (analyze only)"
  ux 'fireproof check; echo "exit=$?"' 900 > "$out/upgrade-fireproof-check.txt" 2>&1
  gx 'date -u +%FT%TZ > /var/log/qa-upgrade-start; wc -l < /var/log/dpkg.log > /var/log/qa-dpkg-lines; wc -l < /var/log/apt/history.log > /var/log/qa-history-lines'
  local rc=0
  case ${UPGRADE_METHOD:-apt} in
    fireproof)
      step "fireproof update -y (root: an authenticated admin), in a terminal (pty) in its own scope"
      # A scope, not a service: needrestart restarts services whose libraries
      # an update replaced, and it once restarted the client mid-update (run 1).
      gx "rm -f /var/log/qa-fireproof-update.rc; setsid systemd-run --scope --unit=qa-fireproof-update /bin/sh -c 'printf \"y\\\\n\" | script -qfec \"fireproof update -y\" /var/log/qa-fireproof-update.typescript; echo \$? > /var/log/qa-fireproof-update.rc' >/dev/null 2>&1 </dev/null &"
      local end=$(( $(date +%s) + 5400 )) stuck=0 state
      while (( $(date +%s) < end )); do
        sleep 15
        state=$("$vm" exec "$name" 'cat /var/log/qa-fireproof-update.rc 2>/dev/null || echo "running $(systemctl show fireproofd.service -p SubState --value) dpkg=$(pgrep -c -x dpkg)"' --timeout 60 2>/dev/null | tr -d '\r\n') || true
        [[ $state =~ ^[0-9]+$ ]] && { rc=$state; break; }
        # The daemon waiting out a deferred stop with dpkg already gone does not
        # end by itself before TimeoutStopSec (1 h): record it, do not power-cut.
        if [[ $state == *stop-sigterm*dpkg=0* ]]; then stuck=$((stuck + 15)); else stuck=0; fi
        (( stuck >= 180 )) && { rc=STUCK; break; }
      done
      echo "qa-fireproof-update exit=${rc:-TIMEOUT}"
      gx 'cat /var/log/qa-fireproof-update.typescript' > "$out/upgrade-fireproof-update.typescript" 2>&1
      sed 's/\x1b\[[0-9;]*m//g; s/\r/\n/g' "$out/upgrade-fireproof-update.typescript" \
        | grep -v -E '^\s*\[ *[0-9]+%\]|^\s*$' > "$out/upgrade-fireproof-update.log"
      ;;
    apt)
      step "sudo apt full-upgrade (the command RELEASE-5.0.1.md documents for 5.0.0 and 4.1)"
      gunit qa-apt-full-upgrade 'DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 -o Dpkg::Options::=--force-confold -y full-upgrade' 5400 "$out/upgrade-apt-full-upgrade.log"
      rc=$?
      echo "qa-apt-full-upgrade exit=$rc"
      ;;
  esac
  gx 'tail -n +$(( $(cat /var/log/qa-dpkg-lines) + 1 )) /var/log/dpkg.log' > "$out/upgrade-dpkg.log" 2>&1
  gx 'tail -n +$(( $(cat /var/log/qa-history-lines) + 1 )) /var/log/apt/history.log' > "$out/upgrade-apt-history.log" 2>&1
  gx 'journalctl --no-pager -o short-iso --since "$(cat /var/log/qa-upgrade-start)" -u fireproofd.service' > "$out/upgrade-fireproofd-journal.txt" 2>&1
  gx "$pkg_query; dpkg --audit; echo audit-exit=\$?; systemctl --failed --no-legend --plain; systemctl list-jobs --no-pager; systemctl show fireproofd.service -p ActiveState -p SubState" > "$out/upgrade-after-before-reboot.txt" 2>&1
  if [[ $rc != 0 ]] || grep -q 'unpacked but not yet configured\|in a mess' "$out/upgrade-after-before-reboot.txt"; then
    echo "update did not complete (rc=$rc); not rebooting -- inspect the VM" >&2
    return 1
  fi
  step "reboot (log out and back in once)"
  reboot_vm
  "$vm" shot "$name" "$out/shots/10-5.0.1-first-login-after-upgrade.png" >/dev/null
}

phase_verify() {
  step "verify (5.0.1)"
  push_tools
  facts after
  ux 'python3 /opt/qa/missions_probe.py busy /tmp/qa-out/busy-5.0.1.json qa-busy-501' 900 > "$out/verify-busy-verdict.txt" 2>&1
  "$vm" pull "$name" /tmp/qa-out/busy-5.0.1.json "$out/missions/busy-5.0.1.json" >/dev/null
  ux 'python3 /opt/qa/missions_probe.py order /tmp/qa-out/order-5.0.1.json qa-order 4' 1200 > "$out/verify-order-verdict.txt" 2>&1
  "$vm" pull "$name" /tmp/qa-out/order-5.0.1.json "$out/missions/order-5.0.1.json" >/dev/null
  ux 'python3 /opt/qa/missions_probe.py hold /tmp/qa-out/hold-5.0.1.json qa-order-1' 600 > "$out/verify-hold-verdict.txt" 2>&1
  "$vm" pull "$name" /tmp/qa-out/hold-5.0.1.json "$out/missions/hold-5.0.1.json" >/dev/null
  ux '/opt/qa/run_mission_regressions.sh /opt/qa/mtests /tmp/qa-out/regressions-5.0.1' 1200 > "$out/verify-regressions-tail.txt" 2>&1
  mkdir -p "$out/missions/regressions-5.0.1"
  pull_dir /tmp/qa-out/regressions-5.0.1 "$out/missions/regressions-5.0.1"
  ux 'pgrep -a -u "$USER" -f "shadowfetch-missions [w]orker"; systemctl --user show shadowfetch-missions.service -p ActiveState -p ExecMainStartTimestamp; shadowfetch-missions --version' > "$out/after-missions-worker.txt" 2>&1
  python3 "$here/upgrade_checks.py" "$out" | tee "$out/checks.txt"
}

phase_cleanup() {
  step "cleanup"
  "$vm" stop "$name" --timeout 300 || "$vm" kill "$name"
  rm -rf "$root/work/qa-5.0.0/vm/$name"
  stat -c 'size=%s mtime=%Y' "$base" > "$out/base-stat-after.txt"
  cmp "$out/base-stat-before.txt" "$out/base-stat-after.txt" && echo "base disk unchanged"
}

for phase in "$@"; do
  case $phase in
    setup) phase_setup ;;
    baseline) phase_baseline ;;
    upgrade) phase_upgrade ;;
    verify) phase_verify ;;
    cleanup) phase_cleanup ;;
    *) echo "unknown phase $phase" >&2; exit 2 ;;
  esac
done
