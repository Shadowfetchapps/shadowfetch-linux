"""Shared fixture for the 5.0.1 Mission Control regressions.

A private state directory and two projects under a private workspace root, the
engine loaded by path (as every test module here does), and the real CLI entry
point run in-process with a short lock budget.
"""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "data/usr/lib/shadowfetch/missions/sf_missions.py"
spec = importlib.util.spec_from_file_location("sf_missions", SOURCE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

TEST_BUDGET_SECONDS = 0.5


class Harness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.workspaces = self.base / "Workspaces"
        for name in ("alpha", "beta"):
            (self.workspaces / name).mkdir(parents=True)
            (self.workspaces / name / "clip.mkv").write_bytes(b"x")
        self.state = self.base / "state"
        self.env = patch.dict(os.environ, {
            "SHADOWFETCH_AGENT_WORKSPACES": str(self.workspaces),
            "SHADOWFETCH_MISSIONS_STATE": str(self.state)})
        self.env.start()
        self.store = m.Store()
        self.holders = []

    def tearDown(self):
        for holder in self.holders:
            with contextlib.suppress(Exception):
                holder.rollback()
                holder.close()
        self.env.stop()
        self.temp.cleanup()

    def mission(self, workspace="alpha", title="QA verified audio export"):
        return self.store.create(capability="media_export", provider_id="offline-media",
                                 workspace_value=workspace, title=title,
                                 prompt="Export and decode-verify.", inputs=["clip.mkv"])

    def cli(self, *argv, budget=TEST_BUDGET_SECONDS):
        """The real CLI entry point, in-process, with a short lock budget."""
        printed = []
        started = time.monotonic()
        with patch.object(m, "CLI_LOCK_BUDGET_SECONDS", budget), \
                patch("builtins.print", lambda *v, **k: printed.append(" ".join(map(str, v)))):
            code = m.main(["--json", *argv])
        return code, json.loads(printed[-1]), time.monotonic() - started

    def hold_write_transaction(self):
        """The worker's finalisation shape: a connection inside BEGIN IMMEDIATE
        that has already written and has not committed."""
        holder = m.sqlite3.connect(self.store.db_path, isolation_level=None,
                                   check_same_thread=False)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("UPDATE missions SET updated_at=updated_at")
        self.holders.append(holder)
        return holder

    def events(self, mid, name=None):
        rows = self.store.events(mid)
        return [r for r in rows if name is None or r["event"] == name]
