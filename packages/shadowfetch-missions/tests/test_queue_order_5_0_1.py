"""5.0.0 RESOURCE-01: same-second missions ran out of order (5.0.1).

Four media missions created within 0.5 s ran 1, 4, 3, 2: created_at has
one-second resolution and the worker broke the tie with the random mission id.
"""
import unittest
import uuid
from unittest.mock import patch

from missions_regression import Harness, m


class SameSecondMissionsRunInCreationOrder(Harness):
    def create_in_one_second(self, count):
        # Ids chosen so that id order is the REVERSE of creation order, which
        # is what the old (created_at, id) sort ran.
        ids = [uuid.UUID(int=(count - i) << 64) for i in range(count)]
        with patch.object(m, "now", lambda: "2026-09-30T22:42:00+00:00"), \
                patch.object(m.uuid, "uuid4", side_effect=ids):
            return [self.mission(workspace=name, title=f"clip {index}")["id"]
                    for index, name in enumerate(["alpha", "beta", "alpha", "beta"][:count], 1)]

    def test_the_worker_takes_the_queue_in_creation_order(self):
        created = self.create_in_one_second(4)
        self.assertNotEqual(sorted(created), created, "fixture must defeat an id sort")
        ran = []
        with patch.object(m, "run_mission", lambda store, mission_id: ran.append(mission_id)):
            m.worker(self.store, once=True)
        self.assertEqual(ran, created)

    def test_queue_is_oldest_first_with_insertion_order_breaking_ties(self):
        created = self.create_in_one_second(4)
        self.assertEqual([row["id"] for row in self.store.queue()], created)
        # The newest-first listing agrees, reversed.
        self.assertEqual([row["id"] for row in self.store.list()], created[::-1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
