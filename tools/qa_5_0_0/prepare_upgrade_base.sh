#!/usr/bin/env bash
# Turn a fresh 4.1.0 Calamares install (made by install_image.py, booted from the
# 4.1 "Ice" menu entry) into the previous-release base the harness's `upgrade`
# case consumes. Everything done here is recorded under work/qa-5.0.0/upgrade/prep
# and is TEST SETUP, not product behaviour:
#
#   1. SDDM autologin for the generic test user (so the 4.1 per-user Ice look is
#      actually applied by a real Plasma login, and so the 5.0 look migration
#      later runs at a real login too).
#   2. One login on 4.1: evidence that the machine really is an Ice desktop.
#   3. `apt-get update && apt-get full-upgrade` from the sources the 4.1 install
#      ships (published Shadowfetch suite + Debian) -- "updated from the
#      published APT suite", as UPGRADE-01 words it.
#   4. An extra APT source for the LOCAL 5.0.0 repository (served by
#      serve_repo.sh at http://10.0.2.2:PORT/), signed-by the same keyring the
#      4.1 system already trusts, then `apt-get update` -- the harness's upgrade
#      case runs `apt-get install` without an update of its own.
#
# The result is an overlay (backing file: the 4.1 install) at
#   work/qa-5.0.0/upgrade/base-41-ice-prepared.qcow2
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
vm="$root/tools/qa_5_0_0/sfvm.py"
user="${QA_USER:-demo}"
port="${SF_REPO_PORT:-8805}"
src="$root/work/qa-5.0.0/images/base-41-ice/disk.qcow2"
out="$root/work/qa-5.0.0/upgrade/prep"
name=upg41-prep
mkdir -p "$out"
exec > >(tee -a "$out/prepare.log") 2>&1
step() { printf '\n== %s %s\n' "$(date -u +%FT%TZ)" "$*"; }
gx() { "$vm" exec "$name" "$1" --timeout "${2:-600}"; }
# Long package operations run in their own transient systemd unit, NOT as a
# child of the guest agent: a full-upgrade that upgrades qemu-guest-agent
# restarts it, and systemd then kills everything in the agent's cgroup -- the
# first attempt of this script died exactly that way, half-configured.
gunit() { # unit, command, timeout-seconds, log
  local unit=$1 cmd=$2 limit=$3 log=$4 rc=""
  gx "rm -f /var/log/$unit.rc; systemd-run --unit=$unit --collect /bin/sh -c '$cmd > /var/log/$unit.log 2>&1; echo \$? > /var/log/$unit.rc'"
  local end=$(( $(date +%s) + limit ))
  while (( $(date +%s) < end )); do
    sleep 30
    rc=$("$vm" exec "$name" "cat /var/log/$unit.rc 2>/dev/null" --timeout 60 2>/dev/null | tr -d '\r\n') || true
    [[ -n $rc ]] && break
  done
  gx "cat /var/log/$unit.log" 120 > "$log" || true
  echo "$unit exit=${rc:-TIMEOUT}"
  [[ $rc == 0 ]]
}

[[ -f $src ]] || { echo "missing 4.1 install: $src" >&2; exit 1; }
step "clone $src"
"$vm" clone "$name" --base "$src"
"$vm" start "$name" --medium installed
"$vm" wait "$name"
gx 'cat /usr/share/shadowfetch/version; cat /etc/shadowfetch/element; cat /proc/cmdline' | tee "$out/identity.txt"
[[ $(sed -n 2p "$out/identity.txt") == ice ]] || { echo "the 4.1 install is not Ice" >&2; exit 1; }

step "SDDM autologin for $user (test setup)"
gx "install -d /etc/sddm.conf.d && printf '[Autologin]\nUser=$user\nSession=plasma\nRelogin=false\n' > /etc/sddm.conf.d/zz-qa-autologin.conf && ls /usr/share/wayland-sessions/"
"$vm" stop "$name"; "$vm" start "$name" --medium installed; "$vm" wait "$name"
for _ in $(seq 60); do gx "pgrep -u $user -x plasmashell >/dev/null" 30 && break; sleep 5; done
sleep 60
"$vm" shot "$name" "$out/41-ice-desktop.png"
gx "cd /home/$user/.config && grep -H -E 'ColorScheme|LookAndFeelPackage|AccentColor' kdeglobals; grep -H -A2 'Wallpaper\\]\\[org.kde.image\\]\\[General\\]' plasma-org.kde.plasma.desktop-appletsrc; ls -la shadowfetch; cat shadowfetch/element 2>/dev/null; ls /usr/share/color-schemes | grep -i shadowfetch; ls /usr/share/wallpapers | grep -i umbra" | tee "$out/41-ice-look.txt"

step "update 4.1 from its own published sources"
gx 'cat /etc/apt/sources.list 2>/dev/null; for f in /etc/apt/sources.list.d/*; do echo "--- $f"; cat "$f"; done' | tee "$out/41-sources.txt"
gunit qa-apt-update 'DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 update' 1800 "$out/41-apt-update.log"
gunit qa-apt-full-upgrade 'DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 -o Dpkg::Options::=--force-confold -y full-upgrade' 10800 "$out/41-apt-full-upgrade.log" || { echo "full-upgrade failed"; exit 1; }
"$vm" stop "$name"; "$vm" start "$name" --medium installed; "$vm" wait "$name"
gx 'cat /usr/share/shadowfetch/version; dpkg --audit; dpkg-query -W -f="\${Package}\t\${Version}\n" "shadowfetch-*"' | tee "$out/41-after-update.txt"

step "add the local 5.0.0 repository (test setup)"
key=$(gx 'ls /usr/share/keyrings/shadowfetch*.gpg | head -1' | tr -d '\r\n')
gx "printf 'Types: deb\nURIs: http://10.0.2.2:$port/\nSuites: umbra\nComponents: main\nSigned-By: $key\n' > /etc/apt/sources.list.d/zz-qa-shadowfetch-5.0.0-local.sources"
gunit qa-apt-update-local 'DEBIAN_FRONTEND=noninteractive apt-get -o DPkg::Lock::Timeout=300 update' 1800 "$out/local-apt-update.log"
gx 'apt-cache policy shadowfetch-desktop shadowfetch-defaults shadow-code' | tee "$out/local-apt-policy.txt"
"$vm" stop "$name"
mkdir -p "$root/work/qa-5.0.0/upgrade"
ln -sf "$root/work/qa-5.0.0/vm/$name/disk.qcow2" "$root/work/qa-5.0.0/upgrade/base-41-ice-prepared.qcow2"
step "done: $root/work/qa-5.0.0/upgrade/base-41-ice-prepared.qcow2"
