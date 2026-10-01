"""Mission Control shows why a queued mission is held (5.0.1).

5.0.0 RESOURCE-01: after one mission reached waiting-review, the other missions
for the same project stayed "Queued" with no reason anywhere -- the engine's
same-workspace review gate refused them silently. The engine now returns a
derived `hold` on show/list; the desktop renders it, in the queue row and the
Overview, as given. It does not work out a hold for itself.

Run with QT_QPA_PLATFORM=offscreen python3 -m unittest discover
-s packages/shadowfetch-control-center/tests
"""
import json
import os
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

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication
from sfcc import missions_page
from sfcc.missions_page import MissionsPage, mission_summary, overview_status_lines

APP = QApplication.instance() or QApplication([])

# Built in the ENGINE's own process, so this test process never imports it.
HELD_FIXTURE = """
import json, sys
sys.path.insert(0, sys.argv[1])
import sf_missions
store = sf_missions.Store()
def media(title):
    return store.create(capability="media_export", provider_id="offline-media",
                        workspace_value="demo", title=title, prompt="p",
                        inputs=["clip.mkv"])["id"]
first = media("First export")
store.transition(first, "running")
store.transition(first, "waiting-review")
print(json.dumps({"first": first, "held": media("Second export")}))
"""

HOLD = {"reason": "review-gate", "mission": "mission-aaaa", "title": "First export",
        "summary": "held until you review \"First export\"",
        "message": "Waiting for your review of \"First export\" (mission-aaaa) in the "
                   "same project. Accept or undo that result and this mission starts."}
HELD = {"id": "mission-bbbb", "title": "Second export", "kind": "media",
        "state": "queued", "workspace": "/w/demo", "config": {}, "artifacts": [],
        "hold": HOLD}


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


class HeldMissionIsExplained(unittest.TestCase):
    def test_the_queue_row_says_what_the_mission_is_waiting_for(self):
        self.assertIn("held until you review \"First export\"", mission_summary(HELD))
        unheld = dict(HELD, hold=None)
        self.assertEqual(mission_summary(unheld), "Queued  ·  Media export")
        self.assertEqual(mission_summary(dict(HELD, hold="garbage")),
                         "Queued  ·  Media export")

    def test_the_overview_quotes_the_engines_reason(self):
        lines = overview_status_lines(HELD)
        self.assertIn("Waiting", lines)
        self.assertIn(HOLD["message"], lines)
        self.assertNotIn(HOLD["message"], overview_status_lines(dict(HELD, hold=None)))

    def test_the_page_renders_it_in_the_row_and_its_tooltip(self):
        Client.missions = [HELD]
        with patch.object(missions_page, "MissionClient", Client):
            page = MissionsPage(lambda _: None)
            APP.processEvents()
            page.timer.stop()
            page._listed([dict(HELD)], None)
            row = page.queue.item(0)
            self.assertIn("held until you review", row.text())
            self.assertIn(HOLD["message"], row.toolTip())
            self.assertIn(HOLD["message"], page.overview.toPlainText())
            page.deleteLater()
            APP.processEvents()

    def test_the_real_engine_hold_reaches_the_screen(self):
        """The field the desktop renders is the one the shipped CLI returns."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            project = root / "Workspaces" / "demo"
            project.mkdir(parents=True)
            (project / "clip.mkv").write_bytes(b"x")
            env = {**os.environ, "SHADOWFETCH_AGENT_WORKSPACES": str(root / "Workspaces"),
                   "SHADOWFETCH_MISSIONS_STATE": str(root / "state")}
            made = subprocess.run([sys.executable, "-c", HELD_FIXTURE, str(ENGINE_LIB)],
                                  env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(made.returncode, 0, made.stderr[-2000:])
            ids = json.loads(made.stdout)
            shown = subprocess.run([sys.executable, str(ENGINE_BIN), "--json", "show",
                                    ids["held"]], env=env, capture_output=True,
                                   text=True, timeout=120)
            self.assertEqual(shown.returncode, 0, shown.stdout + shown.stderr)
            mission = json.loads(shown.stdout)
        self.assertEqual(mission["hold"]["mission"], ids["first"])
        self.assertIn("First export", mission_summary(mission))
        self.assertIn(mission["hold"]["message"], overview_status_lines(mission))


if __name__ == "__main__":
    unittest.main(verbosity=2)
