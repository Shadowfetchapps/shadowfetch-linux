"""5.0.0 RESOURCE-01: a mission held by the review gate showed no reason (5.0.1).

After one mission reached waiting-review, the others for the same project sat
in "queued" with nothing anywhere saying why: run_mission() refused them and the
worker moved on silently. show and list now carry a derived `hold`.
"""
import unittest

import mission_states
from missions_regression import Harness, m


class AHeldMissionSaysWhy(Harness):
    def test_the_review_gate_hold_is_reported_by_show_and_list(self):
        first = self.mission(title="First export")["id"]
        mission_states.reach(self.store, first, "waiting-review")
        held = self.mission(title="Second export")["id"]
        elsewhere = self.mission(workspace="beta", title="Other project")["id"]
        hold = self.store.get(held)["hold"]
        self.assertEqual(hold["reason"], m.HOLD_REVIEW_GATE)
        self.assertEqual(hold["mission"], first)
        self.assertIn("First export", hold["message"])
        self.assertIn("First export", hold["summary"])
        self.assertIsNone(self.store.get(elsewhere)["hold"])
        self.assertIsNone(self.store.get(first)["hold"])
        listed = {row["id"]: row["hold"] for row in self.store.list()}
        self.assertEqual(listed[held], hold)
        self.assertIsNone(listed[elsewhere])
        code, shown, _ = self.cli("show", held)
        self.assertEqual(code, 0, shown)
        self.assertEqual(shown["hold"], hold)
        # The gate itself is unchanged, and it is the same fact.
        with self.assertRaisesRegex(m.MissionError, "Review the previous mission"):
            m.run_mission(self.store, held)
        self.assertEqual(self.store.get(held)["state"], "queued")
        # Deciding the review releases the hold.
        self.store.transition(first, "completed")
        self.assertIsNone(self.store.get(held)["hold"])

    def test_the_hold_is_derived_and_writes_nothing(self):
        first = self.mission()["id"]
        mission_states.reach(self.store, first, "waiting-review")
        held = self.mission()["id"]
        with self.store.db() as db:
            before = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        for _ in range(3):
            self.store.get(held)
            self.store.list()
        with self.store.db() as db:
            after = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            columns = {row[1] for row in db.execute("PRAGMA table_info(missions)")}
        self.assertEqual(before, after)
        self.assertNotIn("hold", columns)


if __name__ == "__main__":
    unittest.main(verbosity=2)
