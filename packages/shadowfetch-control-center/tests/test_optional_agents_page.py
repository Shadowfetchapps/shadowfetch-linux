"""The Hermes & OpenClaw Control Center page: registry, trusted helpers, state-driven buttons.

Run with QT_QPA_PLATFORM=offscreen python3 -m unittest discover
-s packages/shadowfetch-control-center/tests
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
CC = Path(__file__).resolve().parents[1]
SFCC = CC / "data/usr/share/shadowfetch/control-center/sfcc"
sys.path.insert(0, str(SFCC.parent))

from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from sfcc import desktop, pages  # noqa: E402
from sfcc.pages import PageContext  # noqa: E402

APP = QApplication.instance() or QApplication([])


def ready(**extra):
    record = {"state": "ready", "installed": True, "verified": True, "launchable": True,
              "installed_version": "2026.9.6", "pinned_version": "2026.9.6", "version": "2026.9.6",
              "agent_network": "online", "blocked_by_offline": False, "sandbox": "shadowfetch-firebreak",
              "gateway_enabled": False, "installed_commit": "f" * 40}
    record.update(extra)
    return record


class Registry(unittest.TestCase):
    def test_the_section_is_registered_under_its_own_route_words(self):
        for word in ("optional-agents", "hermes", "openclaw", "hermes-openclaw"):
            with self.subTest(word=word):
                row, _rest = pages.resolve(word)
                self.assertEqual("optional-agents", pages.REGISTRY[row].key)
        section = pages.REGISTRY[pages.index_of("optional-agents")]
        self.assertEqual(("sfcc.optional_agents_page", "OptionalAgentsPage"), (section.module, section.factory))

    def test_agents_still_means_workspaces(self):
        self.assertEqual("workspaces", pages.aliases()["agents"])
        self.assertFalse((SFCC / "agents_page.py").exists())

    def test_helpers_are_trusted_shadowfetch_programs(self):
        for name in ("shadowfetch-hermes", "shadowfetch-openclaw"):
            with self.subTest(name=name):
                self.assertEqual((f"/usr/bin/{name}", "shadowfetch"), desktop.PROGRAMS[name])

    def test_the_page_is_shipped(self):
        install = (CC / "debian/shadowfetch-control-center.install").read_text()
        self.assertIn("data/usr/share/shadowfetch/control-center/sfcc/optional_agents_page.py", install)


class Buttons(unittest.TestCase):
    def setUp(self):
        from sfcc import optional_agents_page
        self.module = optional_agents_page
        with patch.object(optional_agents_page.QTimer, "singleShot"):
            self.page = pages.REGISTRY[pages.index_of("optional-agents")].build(
                PageContext(open_route=lambda _route: None))
        self.addCleanup(self.page.deleteLater)
        self.hermes = self.page.cards["hermes"]
        self.openclaw = self.page.cards["openclaw"]

    def enabled(self, card):
        return tuple(button.isEnabled() for button in (card.install, card.update_button, card.open, card.uninstall))

    def test_cards_run_the_trusted_helpers(self):
        self.assertTrue(self.hermes.command.endswith("shadowfetch-hermes"))
        self.assertTrue(self.openclaw.command.endswith("shadowfetch-openclaw"))

    def test_not_installed_offers_install_only(self):
        with patch.object(desktop, "agent_network_offline", return_value=False):
            self.hermes._status({"state": "not-installed", "installed": False, "launchable": False,
                                 "pinned_version": "0.21.5", "blocked_by_offline": False}, None)
        self.assertEqual((True, False, False, False), self.enabled(self.hermes))

    def test_ready_offers_update_open_and_uninstall(self):
        self.openclaw._status(ready(), None)
        self.assertEqual((False, True, True, True), self.enabled(self.openclaw))
        self.assertIn("pin", self.openclaw.detail.text())  # still on the pin: nudge to update
        self.assertIn("Firebreak", self.openclaw.detail.text())

    def test_offline_pauses_everything_but_uninstall(self):
        self.openclaw._status(ready(agent_network="offline", blocked_by_offline=True, launchable=False), None)
        self.assertEqual((False, False, False, True), self.enabled(self.openclaw))
        self.assertIn("offline", self.openclaw.detail.text())

    def test_drift_is_returned_by_update_not_rolled_back_by_install(self):
        self.hermes._status(ready(state="drifted", verified=False), None)
        install, update, _open, uninstall = self.enabled(self.hermes)
        self.assertEqual((False, True, True), (install, update, uninstall))

    def test_a_helper_error_disables_actions(self):
        self.hermes._status(None, "Could not start /usr/bin/shadowfetch-hermes.")
        self.assertEqual((False, False, False, False), self.enabled(self.hermes))
        self.assertIn("Could not start", self.hermes.state.text())

    def test_declining_consent_runs_nothing(self):
        self.openclaw._status(ready(state="not-installed", installed=False, launchable=False), None)
        with patch.object(desktop, "agent_network_offline", return_value=False), \
                patch.object(self.openclaw, "_consent_text", return_value="consent"), \
                patch.object(self.module.QMessageBox, "question", return_value=QMessageBox.StandardButton.Cancel), \
                patch.object(self.module, "ProcessDialog") as dialog:
            self.openclaw._install()
            self.openclaw._uninstall()
        dialog.assert_not_called()

    def test_update_shows_old_and_new_then_passes_the_reviewed_version(self):
        self.openclaw._status(ready(), None)
        seen = {}

        def answer(_parent, _title, text, *_args):
            seen["text"] = text
            return QMessageBox.StandardButton.Yes
        with patch.object(desktop, "agent_network_offline", return_value=False), \
                patch.object(self.module.QMessageBox, "question", side_effect=answer), \
                patch.object(self.openclaw, "_run") as run:
            self.openclaw._update_checked({"update_available": True, "installed_version": "2026.9.6",
                                           "latest_version": "2026.10.1", "latest_integrity": "sha512-abc"}, None)
        self.assertIn("2026.9.6 → 2026.10.1", seen["text"])
        self.assertIn("sha512-abc", seen["text"])
        run.assert_called_once()
        self.assertEqual(["update", "--yes", "--expect", "2026.10.1"], run.call_args[0][1])

    def test_offline_blocks_actions_even_if_a_button_was_clicked(self):
        with patch.object(desktop, "agent_network_offline", return_value=True), \
                patch.object(self.module, "ProcessDialog") as dialog, \
                patch.object(self.module, "JsonCommand") as command:
            self.hermes._install()
            self.hermes._check_update()
            self.hermes._open()
        dialog.assert_not_called()
        command.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
