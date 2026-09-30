#!/usr/bin/env bash
# Run the VM acceptance cases for 5.0.0 through `make vm-acceptance`, in
# independent lanes so several KVM guests work at once. Every verdict comes from
# the harness itself; this script only sequences and logs. --record is passed
# only where requested (shadowcode-soak, install-both-firmwares,
# recovery-project); the harness refuses to record anything but a PASS.
# Recording runs hold the manifest lock other recorders use, and installs are
# matched to the artifact bound in qa/5.0.0/acceptance.json so an older ISO's
# disks are never reused.
#   run_acceptance_lanes.sh A|B|C|upgrade
set -uo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
logs="$root/work/qa-5.0.0/logs"; mkdir -p "$logs"
cd "$root" || exit 1
lock=${ACCEPTANCE_LOCK:-/tmp/claude-$(id -u)/sf-acceptance-record.lock}
sha=$(python3 -c 'import json; print(json.load(open("qa/5.0.0/acceptance.json"))["artifact"]["iso_sha256"])')
bios_done="$logs/.bios-install-${sha:0:12}.done"
run() { # [LOCKED] case, extra args...
  local wrap=()
  if [[ $1 == LOCKED ]]; then mkdir -p "$(dirname "$lock")"; wrap=(flock "$lock"); shift; fi
  local case=$1; shift
  local log stamp
  stamp=$(date -u +%H%M%S)
  log="$logs/case-$case-$stamp.log"
  echo "$(date -u +%FT%TZ) START $case $*" | tee -a "$logs/lanes.log"
  "${wrap[@]}" make vm-acceptance VM_CASE="$case" VM_ACCEPTANCE_ARGS="$*" >"$log" 2>&1
  local rc=$?
  echo "$(date -u +%FT%TZ) END $case rc=$rc $(grep -m1 '^VERDICT' "$log") log=$log" | tee -a "$logs/lanes.log"
  return $rc
}
installed_disk() { # firmware -> disk of the newest PASSING install of this artifact
  python3 - "$1" "$sha" <<'EOF'
import json, sys
from pathlib import Path
root = Path("work/qa-5.0.0/vm-acceptance")
rows = [json.loads(l) for l in (root / "ledger.jsonl").read_text().splitlines() if l.strip()]
rows = [r for r in rows if r["case"] == "install" and r["verdict"] == "PASS" and r.get("firmware") == sys.argv[1]
        and r.get("artifact_sha256") == sys.argv[2]]
if rows:
    receipt = json.loads(Path(rows[-1]["receipt_path"]).read_text())
    print(receipt["observations"]["installed_disk"])
EOF
}
case "${1:?lane}" in
  A)
    run live-boot
    run shadowcode
    run LOCKED shadowcode-soak --record
    ;;
  B)
    run install --firmware bios
    touch "$bios_done"
    disk=$(installed_disk bios)
    if [[ -n $disk ]]; then
      run recovery --base-image "$disk"
      run recovery-interrupted --base-image "$disk"
      run LOCKED recovery-project --base-image "$disk" --record
    else
      echo "no passing BIOS install; recovery cases not run" | tee -a "$logs/lanes.log"
    fi
    ;;
  C)
    run install --firmware uefi
    until [[ -f $bios_done ]]; do sleep 60; done
    run LOCKED install-both-firmwares --record
    ;;
  upgrade)
    base="${UPGRADE_BASE:-$root/work/qa-5.0.0/upgrade/base-41-ice-prepared.qcow2}"
    pkgs="${UPGRADE_PACKAGES:?set UPGRADE_PACKAGES to the package list apt-get installs}"
    "$root/tools/qa_5_0_0/serve_repo.sh" start
    run upgrade --upgrade-base-image "$base" --upgrade-from-version 4.1.0 --upgrade-repo "'$pkgs'"
    ;;
esac
