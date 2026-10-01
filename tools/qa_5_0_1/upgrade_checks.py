#!/usr/bin/env python3
"""Judge the evidence upgrade_from_500.sh collected; print one PASSED/FAILED line per check.

Usage: upgrade_checks.py OUT_DIR
Reads only files in OUT_DIR (plus the served repository's Packages index, to
know what 5.0.1 published). Exit status 0 only if every check passed.
"""
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(sys.argv[1])
RESULTS = []


def read(name):
    path = OUT / name
    return path.read_text(errors="replace") if path.exists() else ""


def load(name):
    try:
        return json.loads(read(name))
    except ValueError:
        return None


def check(name, ok, detail=""):
    RESULTS.append((bool(ok), name, detail))


def tsv(name):
    rows = {}
    for line in read(name).splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            rows[parts[0]] = (parts[1], parts[2].strip())
    return rows


def repo_versions():
    versions, package = {}, None
    for line in (ROOT / "repo/dists/umbra/main/binary-amd64/Packages").read_text().splitlines():
        if line.startswith("Package: "):
            package = line.split(": ", 1)[1]
        elif line.startswith("Version: ") and package:
            versions[package] = line.split(": ", 1)[1]
    return versions


def main():
    repo = repo_versions()
    before, after = tsv("before-first-party.tsv"), tsv("after-first-party.tsv")

    identity = read("after-identity.txt").splitlines()
    check("release is 5.0.1 (version file and os-release)",
          identity[:1] == ["5.0.1"] and 'VERSION_ID="5.0.1"' in identity,
          " | ".join(identity[:3]))

    # Every installed package the 5.0.1 repository publishes is at the repo's version.
    wrong = {p: after.get(p, ("missing", ""))[0] for p in repo
             if p in before and after.get(p, ("", ""))[0] != repo[p]}
    not_ii = {p: s for p, (_, s) in after.items() if s != "ii"}
    first_party_501 = sorted(p for p, (v, _) in after.items() if v == "5.0.1-1")
    check("every installed first-party package is at the 5.0.1 repository's version",
          not wrong and not not_ii,
          f"{len(first_party_501)} at 5.0.1-1; shadow-code {after.get('shadow-code', ('?',))[0]}; "
          f"grub-btrfs {after.get('grub-btrfs', ('?',))[0]}; wrong={wrong}; not-ii={not_ii}")
    check("shadow-code stays 1.0.0 and answers --version",
          after.get("shadow-code", ("",))[0] == "1.0.0"
          and read("after-shadowcode-version.txt").strip() == "ShadowCode 1.0.0",
          read("after-shadowcode-version.txt").strip())
    leftover = sorted(p for p, (v, _) in after.items()
                      if p.startswith("shadowfetch-") and v != "5.0.1-1")
    check("only packages the repository does not publish stay behind",
          all(p not in repo for p in leftover), f"not at 5.0.1-1: {leftover}")

    def installed(name):
        return {line.split("\t")[0] for line in read(name).splitlines()
                if line.count("\t") >= 2 and line.split("\t")[2].startswith("ii")}
    gone = sorted(installed("before-all-packages.tsv") - installed("after-all-packages.tsv"))
    dpkg_removals = [line for line in read("upgrade-dpkg.log").splitlines()
                     if re.search(r"\s(remove|purge)\s", line)]
    analysis = re.search(r"(\d+) upgrade, (\d+) new, (\d+) removal", read("upgrade-fireproof-check.txt"))
    check("no package removed by the upgrade",
          not gone and not dpkg_removals and analysis and analysis.group(3) == "0",
          f"fireproof analyze: {analysis.group(0) if analysis else 'not found'}; "
          f"removed={gone}; dpkg remove/purge lines={len(dpkg_removals)}")

    run_log = read("run.log")
    if "CONTROL: sudo apt full-upgrade" in run_log:
        # The documented command was NOT used; say so instead of passing it.
        check("upgrade command exited 0 (CONTROL run: apt full-upgrade, not fireproof update)",
              "qa-apt-full-upgrade exit=0" in run_log)
    else:
        update_log = read("upgrade-fireproof-update.log")
        check("fireproof update exited 0",
              "qa-fireproof-update exit=0" in run_log,
              (update_log.strip().splitlines() or ["(empty log)"])[-1][:200])

    apt = read("upgrade-apt-update.log")
    switched = read("upgrade-source-switch.txt")
    check("APT signature check stayed on and the 5.0.1 index verified",
          "signed-by=/usr/share/keyrings/shadowfetch.gpg] http://10.0.2.2:" in switched
          and re.search(r"(Get|Hit):\d+ http://10\.0\.2\.2:\d+ umbra InRelease", apt)
          and not re.search(r"NO_PUBKEY|not signed|is not signed|insecure|EXPKEYSIG|BADSIG", apt, re.I),
          "; ".join(line for line in apt.splitlines() if "10.0.2.2" in line)[:300])

    audit = read("after-dpkg-audit.txt").strip().splitlines()
    check("dpkg --audit is clean", audit == ["audit-exit=0"], " | ".join(audit)[:200])
    system_failed = read("after-failed-system.txt").strip().splitlines()
    user_failed = read("after-failed-user.txt").strip().splitlines()
    check("no failed system or user units",
          system_failed[-1:] == ["0"] and user_failed[-1:] == ["0"]
          and system_failed[0] == "--" and user_failed[0] == "--",
          f"system={system_failed} user={user_failed}")

    doctor_before, doctor_after = load("before-doctor.json"), load("after-doctor.json")
    if doctor_before and doctor_after:
        def flagged(doc):
            return {f["id"]: f["status"] for f in doc["findings"]
                    if f["status"] not in ("pass", "info")}
        new = {k: v for k, v in flagged(doctor_after).items()
               if flagged(doctor_before).get(k) != v}
        check("shadowfetch-doctor: no FAIL, nothing flagged that 5.0.0 did not flag",
              doctor_after["summary"].get("fail", 1) == 0 and not new,
              f"after={doctor_after['summary']} flagged={flagged(doctor_after)} new={new}")
    else:
        check("shadowfetch-doctor: no FAIL, nothing flagged that 5.0.0 did not flag", False,
              "doctor JSON missing")

    for probe, label in (("busy", "Mission Control busy-database probe"),
                         ("order", "same-second missions run in creation order"),
                         ("hold", "review-gate hold reported by show and list")):
        doc = load(f"missions/{probe}-5.0.1.json") or {}
        verdict = doc.get("verdict") or {}
        booleans = {k: v for k, v in verdict.items() if isinstance(v, bool)}
        check(f"{label} (5.0.1)", booleans and all(booleans.values()),
              json.dumps(verdict, sort_keys=True)[:400])
    old = (load("missions/busy-5.0.0.json") or {}).get("verdict") or {}
    check("the same busy probe fails on the 5.0.0 engine (the probe discriminates)",
          old and not old.get("stop_saved_and_answered_inside_lock_wait")
          and not old.get("worker_recorded_saved_stop_once"),
          json.dumps(old, sort_keys=True)[:300])

    reg_after = read("missions/regressions-5.0.1/regressions-env.txt")
    reg_before = read("missions/regressions-5.0.0/regressions-env.txt")
    tail = [line for line in read("missions/regressions-5.0.1/regressions.log").splitlines()
            if line.startswith(("Ran ", "OK", "FAILED"))]
    check("5.0.1 Mission Control regression tests pass on the installed engine",
          "exit=0" in reg_after and "shadowfetch-missions 5.0.1-1" in reg_after,
          " ".join(tail) + f"; 5.0.0 engine: {'exit=0' not in reg_before and 'fails' or 'PASSES'}")

    notifier = read("after-discover-notifier.txt")
    check("live-notifier drop-in installed; the Discover notifier still runs here",
          "10-shadowfetch-live-medium.conf" in notifier
          and re.search(r"DropInPaths=.*10-shadowfetch-live-medium\.conf", notifier)
          and "ConditionResult=yes" in notifier and "ActiveState=active" in notifier
          and "DiscoverNotifier" in notifier.split("--- process", 1)[-1]
          and "No such file" in notifier.split("--- live markers", 1)[-1],
          " ".join(line for line in notifier.splitlines()
                   if line.startswith(("ActiveState", "ConditionResult", "DropInPaths")))[:300])

    def manifest(name):
        return dict(reversed(line.split("  ", 1)) for line in read(name).splitlines() if "  " in line)
    old_files, new_files = manifest("before-user-data.sha256"), manifest("after-user-data.sha256")
    changed = sorted(p for p, h in old_files.items() if new_files.get(p) != h)
    check("user data byte-identical across the upgrade",
          old_files and not changed, f"{len(old_files)} files; changed/missing={changed}")
    check("ShadowCode settings preserved (config.yaml byte-identical, edits-allowed mode kept)",
          any(p.endswith(".config/shadow-agent/config.yaml") for p in old_files)
          and not [p for p in changed if "shadow-agent" in p],
          ", ".join(p for p in old_files if "shadow-agent" in p))

    def missions(name):
        doc = load(name)
        return {m["id"]: (m["title"], m["state"], m["created_at"]) for m in doc or []}
    m_before, m_after = missions("before-missions-list.json"), missions("after-missions-list.json")
    check("missions made on 5.0.0 kept, unchanged, with an intact audit chain",
          m_before and all(m_after.get(k) == v for k, v in m_before.items())
          and "exit=0" in read("after-missions-audit.txt"),
          f"{len(m_before)} missions before, {len(m_after)} after")

    failed = 0
    for ok, name, detail in RESULTS:
        failed += not ok
        print(f"{'PASSED' if ok else 'FAILED'} {name}" + (f"  [{detail}]" if detail else ""))
    print(f"upgrade_checks: {len(RESULTS) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
