#!/usr/bin/env bash
# Run the VM acceptance cases for 5.0.0 through `make vm-acceptance`, in
# independent lanes so several KVM guests work at once. Every verdict comes from
# the harness itself; this script only sequences and logs. --record is passed
# only where requested (shadowcode-soak, install-both-firmwares); the harness
# refuses to record anything but a PASS.
#   run_acceptance_lanes.sh A|B|C|upgrade
set -uo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
logs="$root/work/qa-5.0.0/logs"; mkdir -p "$logs"
cd "$root" || exit 1
run() { # case, extra args...
  local case=$1; shift
  local log stamp
  stamp=$(date -u +%H%M%S)
  log="$logs/case-$case-$stamp.log"
  echo "$(date -u +%FT%TZ) START $case $*" | tee -a "$logs/lanes.log"
  make vm-acceptance VM_CASE="$case" VM_ACCEPTANCE_ARGS="$*" >"$log" 2>&1
  local rc=$?
  echo "$(date -u +%FT%TZ) END $case rc=$rc $(grep -m1 '^VERDICT' "$log") log=$log" | tee -a "$logs/lanes.log"
  return $rc
}
installed_disk() { # firmware -> disk of the newest PASSING install run
  python3 - "$1" <<'EOF'
import json, sys
from pathlib import Path
root = Path("work/qa-5.0.0/vm-acceptance")
rows = [json.loads(l) for l in (root / "ledger.jsonl").read_text().splitlines() if l.strip()]
rows = [r for r in rows if r["case"] == "install" and r["verdict"] == "PASS" and r.get("firmware") == sys.argv[1]]
if rows:
    receipt = json.loads(Path(rows[-1]["receipt_path"]).read_text())
    print(receipt["observations"]["installed_disk"])
EOF
}
case "${1:?lane}" in
  A)
    run live-boot
    run shadowcode
    run shadowcode-soak --record
    ;;
  B)
    run install --firmware bios
    disk=$(installed_disk bios)
    if [[ -n $disk ]]; then
      run recovery --base-image "$disk"
      run recovery-interrupted --base-image "$disk"
      run recovery-project --base-image "$disk"
    else
      echo "no passing BIOS install; recovery cases not run" | tee -a "$logs/lanes.log"
    fi
    ;;
  C)
    run install --firmware uefi
    until [[ $(grep -c ' END install rc=' "$logs/lanes.log") -ge 2 ]]; do sleep 60; done
    run install-both-firmwares --record
    ;;
  upgrade)
    base="${UPGRADE_BASE:-$root/work/qa-5.0.0/upgrade/base-41-ice-prepared.qcow2}"
    pkgs="${UPGRADE_PACKAGES:?set UPGRADE_PACKAGES to the package list apt-get installs}"
    "$root/tools/qa_5_0_0/serve_repo.sh" start
    run upgrade --upgrade-base-image "$base" --upgrade-from-version 4.1.0 --upgrade-repo "'$pkgs'"
    ;;
esac
