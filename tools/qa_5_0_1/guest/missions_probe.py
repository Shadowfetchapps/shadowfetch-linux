#!/usr/bin/env python3
"""Mission Control probes for the 5.0.1 upgrade proof, run INSIDE the guest.

Runs as the desktop user, in that user's session, against the INSTALLED
engine (/usr/bin/shadowfetch-missions) and the real worker service
(shadowfetch-missions.service). Each probe writes one JSON document with the
raw observations and the verdict the host summary reads. The same probe runs
before the upgrade (on 5.0.0) and after it, so the two documents can be
compared; only the 5.0.1 run is expected to pass.

  missions_probe.py busy  OUT.json WORKSPACE
      The shape of packages/shadowfetch-missions/tests/test_busy_database_5_0_1.py
      (CancelUnderAHeldWriteLock): a connection holds a write transaction, as a
      worker commit stalled on a slow disk does, while `show`, `list` and
      `cancel` are pressed. 5.0.1: reads answer at once, Stop is saved and
      answered inside its 2 s lock wait, and the worker records the saved Stop
      once the lock is free. 5.0.0: Stop waits out its 10 s budget and is lost.
  missions_probe.py order OUT.json PREFIX [N]
      N media missions created inside one wall-clock second, with mission ids
      that do not sort in creation order, must run in creation order.
  missions_probe.py hold  OUT.json WORKSPACE
      A mission queued behind an unreviewed result in the same workspace
      reports the review-gate hold in `show` and `list`, and stays queued.
  missions_probe.py release OUT.json WORKSPACE HELD_ID
      Accept the blocking review; the held mission then runs and its hold is gone.

Nothing here edits the mission database except through the engine's own CLI.
The one direct connection is the busy probe's lock holder, which rolls back.
"""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

CLI = "/usr/bin/shadowfetch-missions"
SERVICE = "shadowfetch-missions.service"
TERMINAL = ("waiting-review", "failed", "cancelled", "completed", "undone")


def state_root():
    explicit = os.environ.get("SHADOWFETCH_MISSIONS_STATE")
    if explicit:
        return Path(explicit).expanduser().resolve()
    base = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    return (base / "shadowfetch/missions").resolve()


def workspace_root():
    return Path(os.environ.get("SHADOWFETCH_AGENT_WORKSPACES",
                               str(Path.home() / "Workspaces"))).expanduser()


def cli(*argv, timeout=60):
    """Run the installed CLI once; return rc, payload (parsed last JSON line), seconds."""
    started = time.monotonic()
    try:
        done = subprocess.run([CLI, "--json", *argv], capture_output=True, text=True,
                              timeout=timeout)
        rc, out, err = done.returncode, done.stdout, done.stderr
    except subprocess.TimeoutExpired as exc:
        rc, out, err = None, exc.stdout or "", "TIMEOUT"
        out = out.decode() if isinstance(out, bytes) else out
    seconds = round(time.monotonic() - started, 3)
    payload = None
    for line in reversed((out or "").strip().splitlines()):
        try:
            payload = json.loads(line)
            break
        except ValueError:
            continue
    if payload is None:
        try:
            payload = json.loads(out)
        except ValueError:
            payload = {"raw_stdout": (out or "")[-2000:]}
    return {"argv": list(argv), "rc": rc, "seconds": seconds, "payload": payload,
            "stderr": (err or "")[-2000:]}


def systemctl(*argv):
    done = subprocess.run(["systemctl", "--user", *argv], capture_output=True, text=True,
                          timeout=60)
    return {"argv": list(argv), "rc": done.returncode,
            "out": (done.stdout + done.stderr).strip()[-1000:]}


def worker_active():
    return systemctl("is-active", SERVICE)["out"] == "active"


def make_workspace(name):
    path = workspace_root() / name
    path.mkdir(parents=True, exist_ok=True)
    clip = path / "clip.avi"
    if not clip.exists():
        shared = workspace_root() / ".qa-clip.avi"
        if not shared.exists():
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                            "testsrc2=size=320x240:rate=25", "-f", "lavfi", "-i",
                            "sine=frequency=440:sample_rate=48000", "-t", "4",
                            "-c:v", "mjpeg", "-q:v", "5", "-c:a", "pcm_s16le", str(shared)],
                           check=True, timeout=300)
        shutil.copyfile(shared, clip)
    return path


def create(workspace, title):
    return cli("create", "--kind", "media", "--workspace", workspace, "--title", title,
               "--prompt", "Export and decode-verify the selected video.",
               "--runtime", "offline", "--network", "none", "--input", "clip.avi")


def show(mid):
    return cli("show", mid)


def wait_terminal(ids, limit=300):
    deadline = time.monotonic() + limit
    states = {}
    while time.monotonic() < deadline:
        states = {mid: (show(mid)["payload"] or {}).get("state") for mid in ids}
        if all(state in TERMINAL for state in states.values()):
            break
        time.sleep(1)
    return states


def ro_db():
    path = state_root() / "missions.sqlite3"
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)


def events(mid):
    result = cli("events", mid)
    payload = result["payload"]
    return payload if isinstance(payload, list) else []


def wal_state():
    root = state_root()
    wal = root / "missions.sqlite3-wal"
    return {"wal_exists": wal.exists(), "wal_bytes": wal.stat().st_size if wal.exists() else None,
            "shm_exists": (root / "missions.sqlite3-shm").exists()}


def write(out, document):
    Path(out).write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(json.dumps(document.get("verdict", {}), indent=2, sort_keys=True))


# --------------------------------------------------------------- busy ---
def probe_busy(out, workspace):
    doc = {"probe": "busy", "workspace": workspace}
    doc["engine_version"] = subprocess.run([CLI, "--version"], capture_output=True,
                                           text=True).stdout.strip()
    doc["worker_stop"] = systemctl("stop", SERVICE)
    make_workspace(workspace)
    created = create(workspace, "QA busy-database probe")
    doc["create"] = created
    mid = (created["payload"] or {}).get("id")
    if not mid:
        doc["verdict"] = {"error": "create failed"}
        return write(out, doc)
    doc["mission"] = mid
    # The worker's finalisation shape: inside BEGIN IMMEDIATE, already written,
    # not committed. timeout=0: this holder never waits itself.
    holder = sqlite3.connect(state_root() / "missions.sqlite3", isolation_level=None,
                             timeout=0, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("UPDATE missions SET updated_at=updated_at WHERE id=?", (mid,))
    held_at = time.monotonic()
    try:
        doc["show_held"] = show(mid)
        doc["list_held"] = cli("list", "--limit", "5")
        doc["cancel_held"] = cli("cancel", mid)
        doc["show_held_after_cancel"] = show(mid)
        doc["cancel_request_file"] = (state_root() / f"cancel-request.{mid}").exists()
        doc["lock_held_seconds"] = round(time.monotonic() - held_at, 3)
    finally:
        holder.rollback()
        holder.close()
    # With the lock free, the worker's half: record the saved Stop in the chain.
    doc["worker_start"] = systemctl("start", SERVICE)
    doc["final_state"] = wait_terminal([mid], limit=180)[mid]
    doc["final_show"] = show(mid)
    doc["events"] = [{k: e.get(k) for k in ("at", "event", "actor", "detail")} for e in events(mid)]
    doc["cancel_request_file_after"] = (state_root() / f"cancel-request.{mid}").exists()
    doc["audit_verify"] = cli("audit", "verify", timeout=300)
    # Mechanism: with no connection open, does the WAL survive the last close?
    doc["worker_stop_for_wal"] = systemctl("stop", SERVICE)
    doc["list_for_wal"] = cli("list", "--limit", "1")
    doc["wal_after_last_close"] = wal_state()
    doc["worker_restart"] = systemctl("start", SERVICE)

    show_held = doc["show_held"]
    cancel = doc["cancel_held"]
    cpay = cancel["payload"] or {}
    cancelled = [e for e in doc["events"] if e["event"] in ("cancelled", "cancel-requested")]
    doc["verdict"] = {
        "show_answers_while_write_lock_held": show_held["rc"] == 0
            and not (show_held["payload"] or {}).get("busy") and show_held["seconds"] < 3.0,
        "list_answers_while_write_lock_held": doc["list_held"]["rc"] == 0
            and doc["list_held"]["seconds"] < 3.0,
        "stop_answered_seconds": cancel["seconds"],
        "stop_saved_and_answered_inside_lock_wait": cancel["rc"] == 0
            and bool(cpay.get("cancel_pending")) and "saved" in str(cpay.get("notice", ""))
            and cancel["seconds"] < 4.0,
        "show_reports_pending_stop": bool((doc["show_held_after_cancel"]["payload"] or {})
                                          .get("cancel_pending")),
        "worker_recorded_saved_stop_once": doc["final_state"] == "cancelled"
            and len(cancelled) == 1 and "saved request" in str(cancelled[0]["detail"]),
        "audit_chain_ok": doc["audit_verify"]["rc"] == 0,
        "wal_persists_after_last_close": doc["wal_after_last_close"]["wal_exists"],
    }
    write(out, doc)


# -------------------------------------------------------------- order ---
def probe_order(out, prefix, count):
    doc = {"probe": "order", "attempts": []}
    doc["engine_version"] = subprocess.run([CLI, "--version"], capture_output=True,
                                           text=True).stdout.strip()
    doc["worker_stop"] = systemctl("stop", SERVICE)
    names = [f"{prefix}-{index}" for index in range(1, count + 1)]
    for name in names:
        make_workspace(name)
    chosen = None
    for attempt in range(1, 6):
        # Start just after a second boundary so N quick creates share one second.
        time.sleep(1.0 - (time.time() % 1.0) + 0.02)
        rows = []
        for index, name in enumerate(names, 1):
            result = create(name, f"QA same-second {attempt}.{index}")
            rows.append({"workspace": name, "id": (result["payload"] or {}).get("id"),
                         "created_at": (result["payload"] or {}).get("created_at"),
                         "rc": result["rc"], "seconds": result["seconds"]})
        ids = [row["id"] for row in rows]
        record = {"attempt": attempt, "created": rows,
                  "same_second": len({row["created_at"] for row in rows}) == 1,
                  "ids_sort_in_creation_order": ids == sorted(ids)}
        doc["attempts"].append(record)
        if record["same_second"] and not record["ids_sort_in_creation_order"]:
            chosen = record
            break
        # Not a discriminating set: stop it before it runs and try again.
        record["cancelled"] = [cli("cancel", mid)["rc"] for mid in ids]
    if chosen is None:
        doc["verdict"] = {"error": "could not create a same-second, id-unsorted set"}
        return write(out, doc)
    created = [row["id"] for row in chosen["created"]]
    listing = cli("list", "--limit", "50")["payload"]
    rows = listing.get("missions", listing) if isinstance(listing, dict) else listing
    listed = [row["id"] for row in rows if row.get("id") in created]
    doc["list_newest_first"] = listed
    doc["worker_start"] = systemctl("start", SERVICE)
    doc["final_states"] = wait_terminal(created, limit=600)
    with ro_db() as db:
        marks = ",".join("?" * len(created))
        doc["running_events"] = [
            {"seq": seq, "mission": mission, "at": at} for seq, mission, at in db.execute(
                f"SELECT seq, mission, at FROM events WHERE event='running' AND mission IN ({marks}) "
                "ORDER BY seq", created)]
        doc["created_rowid_order"] = [row[0] for row in db.execute(
            f"SELECT id FROM missions WHERE id IN ({marks}) ORDER BY rowid", created)]
    ran = [row["mission"] for row in doc["running_events"]]
    doc["verdict"] = {
        "created_in_one_second": chosen["same_second"],
        "id_order_differs_from_creation_order": not chosen["ids_sort_in_creation_order"],
        "ran_in_creation_order": ran == created,
        "list_is_reverse_creation_order": listed == created[::-1],
        "all_reached_waiting_review": all(s == "waiting-review"
                                          for s in doc["final_states"].values()),
    }
    doc["created_order"] = created
    doc["ran_order"] = ran
    write(out, doc)


# --------------------------------------------------------------- hold ---
def probe_hold(out, workspace):
    doc = {"probe": "hold", "workspace": workspace}
    doc["engine_version"] = subprocess.run([CLI, "--version"], capture_output=True,
                                           text=True).stdout.strip()
    doc["worker_active"] = worker_active()
    listing = cli("list", "--limit", "0")["payload"]
    rows = listing.get("missions", listing) if isinstance(listing, dict) else listing
    path = str(workspace_root() / workspace)
    blockers = [row for row in rows if row.get("workspace") == path
                and row.get("state") == "waiting-review"]
    doc["blocker"] = blockers[0]["id"] if blockers else None
    doc["blocker_title"] = blockers[0]["title"] if blockers else None
    created = create(workspace, "QA held behind a review")
    held = (created["payload"] or {}).get("id")
    doc["held"] = held
    time.sleep(15)  # the worker has had every chance to start it
    doc["show"] = show(held)
    listing = cli("list", "--limit", "20")["payload"]
    rows = listing.get("missions", listing) if isinstance(listing, dict) else listing
    doc["list_row"] = next((row for row in rows if row.get("id") == held), None)
    shown = doc["show"]["payload"] or {}
    hold = shown.get("hold") or {}
    doc["verdict"] = {
        "worker_running": doc["worker_active"],
        "held_mission_still_queued": shown.get("state") == "queued",
        "show_reports_review_gate_hold": hold.get("reason") == "review-gate"
            and hold.get("mission") == doc["blocker"]
            and str(doc["blocker_title"]) in str(hold.get("message", "")),
        "list_reports_same_hold": (doc["list_row"] or {}).get("hold") == shown.get("hold"),
    }
    write(out, doc)


def probe_release(out, workspace, held):
    doc = {"probe": "release", "workspace": workspace, "held": held}
    before = show(held)["payload"] or {}
    blocker = (before.get("hold") or {}).get("mission")
    doc["blocker"] = blocker
    doc["review_accept"] = cli("review", blocker, "--decision", "accept") if blocker else None
    doc["final_state"] = wait_terminal([held], limit=180)[held]
    doc["final_hold"] = (show(held)["payload"] or {}).get("hold")
    doc["verdict"] = {"blocker_accepted": bool(doc["review_accept"])
                      and doc["review_accept"]["rc"] == 0,
                      "held_mission_ran_after_review": doc["final_state"] == "waiting-review",
                      "hold_gone": doc["final_hold"] is None}
    write(out, doc)


def main(argv):
    if len(argv) < 3:
        raise SystemExit(__doc__)
    probe, out = argv[1], argv[2]
    if probe == "busy":
        probe_busy(out, argv[3])
    elif probe == "order":
        probe_order(out, argv[3], int(argv[4]) if len(argv) > 4 else 4)
    elif probe == "hold":
        probe_hold(out, argv[3])
    elif probe == "release":
        probe_release(out, argv[3], argv[4])
    else:
        raise SystemExit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
