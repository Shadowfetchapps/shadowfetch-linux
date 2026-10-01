"""Mission Control says when a Stop was saved rather than recorded (5.0.1).

5.0.0 STRESS-01: Stop answered "database is busy" and nothing stopped. The
5.0.1 engine saves the request when its database is busy and answers exit 0
with `cancel_pending: true` and a `notice`. The desktop dropped both: no
message, "Stop requested: no", until the worker's next check. It now quotes the
notice, shows the pending stop, and drops the notice once the engine reports
the stop recorded.

Run with QT_QPA_PLATFORM=offscreen python3 -m unittest discover
-s packages/shadowfetch-control-center/tests
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[3]
SFCC = (ROOT / "packages/shadowfetch-control-center/data/usr/share/shadowfetch"
        / "control-center/sfcc")
ENGINE_LIB = ROOT / "packages/shadowfetch-missions/data/usr/lib/shadowfetch/missions"
ENGINE_BIN = ROOT / "packages/shadowfetch-missions/data/usr/bin/shadowfetch-missions"
sys.path.insert(0, str(SFCC.parent))

from PyQt6.QtWidgets import QApplication
from sfcc import missions_page
from sfcc.missions_page import MissionsPage, mission_text

APP = QApplication.instance() or QApplication([])

# Built in the ENGINE's own process, so this test process never imports it.
RUNNING_FIXTURE = """
import json, sys
sys.path.insert(0, sys.argv[1])
import sf_missions
store = sf_missions.Store()
mid = store.create(capability="media_export", provider_id="offline-media",
                   workspace_value="demo", title="Long export", prompt="p",
                   inputs=["clip.mkv"])["id"]
store.transition(mid, "running")
print(json.dumps({"id": mid, "db": str(store.db_path)}))
"""

NOTICE = ("Stop requested. Mission Control's database is busy, so the request "
          "was saved and is recorded in the mission's history as soon as the "
          "database is free.")
RUNNING = {"id": "mission-cccc", "title": "Long export", "kind": "media",
           "state": "running", "workspace": "/w/demo", "config": {},
           "artifacts": [], "cancel_requested": 0, "cancel_pending": False}


class Client:
    missions = []

    def __init__(self, *_):
        self.calls = []

    def call(self, arguments, callback):
        self.calls.append(list(arguments))
        if arguments[0] == "list":
            callback([dict(m) for m in self.missions], None)
        elif arguments[0] == "show":
            callback(dict(self.missions[0]), None)
        elif arguments[0] == "capabilities":
            callback({}, None)
        else:
            callback(None, "not described by this test")

    def grok_status(self, callback):
        callback({"installed": False, "verified": False, "launchable": False}, None)


class SavedStopIsShown(unittest.TestCase):
    def test_the_overview_line_does_not_say_no_for_a_saved_stop(self):
        self.assertIn("Stop requested: no", mission_text(RUNNING))
        pending = mission_text(dict(RUNNING, cancel_pending=True))
        self.assertIn("Stop requested: saved, waiting to be recorded", pending)
        self.assertIn("Stop requested: yes",
                      mission_text(dict(RUNNING, cancel_requested=1, cancel_pending=False)))

    def page_with(self, missions):
        Client.missions = missions
        patcher = patch.object(missions_page, "MissionClient", Client)
        patcher.start()
        self.addCleanup(patcher.stop)
        page = MissionsPage(lambda _: None)
        APP.processEvents()
        page.timer.stop()
        self.addCleanup(APP.processEvents)
        self.addCleanup(page.deleteLater)
        return page

    def test_the_notice_is_shown_and_goes_once_the_stop_is_recorded(self):
        pending = dict(RUNNING, cancel_pending=True)
        page = self.page_with([pending])
        page._listed([dict(pending)], None)
        page._mutated(dict(pending, notice=NOTICE), None)
        # Before: an empty notice, as if nothing had happened.
        self.assertEqual(page.notice.text(), NOTICE)
        self.assertIn("saved, waiting to be recorded", page.mission_view.text())
        # Still pending on the next listing: the notice stays.
        page._listed([dict(pending)], None)
        self.assertEqual(page.notice.text(), NOTICE)
        # Recorded by the worker: the notice is no longer true, so it goes.
        page._listed([dict(RUNNING, cancel_requested=1)], None)
        self.assertEqual(page.notice.text(), "")

    def test_an_ordinary_stop_leaves_no_notice(self):
        page = self.page_with([RUNNING])
        page._listed([dict(RUNNING)], None)
        page._mutated(dict(RUNNING, cancel_requested=1), None)
        self.assertEqual(page.notice.text(), "")

    def test_the_real_engines_saved_stop_reaches_the_screen(self):
        """The fields the desktop renders are the ones the shipped CLI returns
        when Stop meets a held write lock."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "Workspaces" / "demo"
            project.mkdir(parents=True)
            (project / "clip.mkv").write_bytes(b"x")
            env = {**os.environ, "SHADOWFETCH_AGENT_WORKSPACES": str(root / "Workspaces"),
                   "SHADOWFETCH_MISSIONS_STATE": str(root / "state")}
            made = subprocess.run([sys.executable, "-c", RUNNING_FIXTURE, str(ENGINE_LIB)],
                                  env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(made.returncode, 0, made.stderr[-2000:])
            fixture = json.loads(made.stdout)
            # The worker's finalisation shape: a write transaction held open.
            holder = sqlite3.connect(fixture["db"], isolation_level=None)
            try:
                holder.execute("BEGIN IMMEDIATE")
                holder.execute("UPDATE missions SET updated_at=updated_at")
                stopped = subprocess.run([sys.executable, str(ENGINE_BIN), "--json",
                                          "cancel", fixture["id"]], env=env,
                                         capture_output=True, text=True, timeout=120)
            finally:
                holder.rollback()
                holder.close()
        self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
        payload = json.loads(stopped.stdout)
        self.assertTrue(payload["cancel_pending"])
        page = self.page_with([dict(payload)])
        page._listed([dict(payload)], None)
        page._mutated(payload, None)
        self.assertEqual(page.notice.text(), payload["notice"])
        self.assertIn("saved", page.notice.text())
        self.assertIn("Stop requested: saved, waiting to be recorded", mission_text(payload))


if __name__ == "__main__":
    unittest.main(verbosity=2)
