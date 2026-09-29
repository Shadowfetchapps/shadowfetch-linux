"""The ShadowCode Control Center page: registry, trusted programs, session PATH, states.

Run with QT_QPA_PLATFORM=offscreen python3 -m unittest discover
-s packages/shadowfetch-control-center/tests
"""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
CC = Path(__file__).resolve().parents[1]
SFCC = CC / "data/usr/share/shadowfetch/control-center/sfcc"
sys.path.insert(0, str(SFCC.parent))

from PyQt6.QtWidgets import QApplication  # noqa: E402

from sfcc import desktop, pages  # noqa: E402
from sfcc.pages import PageContext  # noqa: E402

APP = QApplication.instance() or QApplication([])


class Registry(unittest.TestCase):
    def test_routes(self):
        for word in ("shadowcode", "shadow-code"):
            with self.subTest(word=word):
                row, _rest = pages.resolve(word)
                self.assertEqual("shadowcode", pages.REGISTRY[row].key)
        section = pages.REGISTRY[pages.index_of("shadowcode")]
        self.assertEqual(("sfcc.shadowcode_page", "ShadowCodePage"), (section.module, section.factory))

    def test_follows_mission_control_in_the_sidebar(self):
        self.assertEqual(pages.index_of("missions") + 1, pages.index_of("shadowcode"))

    def test_existing_route_words_keep_their_pages(self):
        for word, key in (("agents", "workspaces"), ("ai", "workspaces"), ("home", "missions")):
            with self.subTest(word=word):
                self.assertEqual(key, pages.aliases()[word])

    def test_programs_are_trusted_system_paths(self):
        self.assertEqual(("/usr/bin/shadowcode", "system"), desktop.PROGRAMS["shadowcode"])
        self.assertEqual(("/usr/bin/dpkg-query", "system"), desktop.PROGRAMS["dpkg-query"])

    def test_the_page_is_shipped(self):
        install = (CC / "debian/shadowfetch-control-center.install").read_text()
        self.assertIn("data/usr/share/shadowfetch/control-center/sfcc/shadowcode_page.py", install)

    def test_names_no_optional_or_retired_agent_runtime(self):
        # package_gate: only allowlisted files may name the optional agents,
        # and no Shadowfetch-built package may name the retired local runtime.
        text = (SFCC / "shadowcode_page.py").read_text(encoding="utf-8").lower()
        for word in ("hermes", "openclaw", "ollama", "llama"):
            with self.subTest(word=word):
                self.assertNotIn(word, text)


class Helpers(unittest.TestCase):
    def setUp(self):
        from sfcc import shadowcode_page
        self.module = shadowcode_page

    def dpkg(self, stdout, returncode=0):
        result = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")
        with patch.object(desktop, "trusted_program", return_value="/usr/bin/dpkg-query"), \
                patch.object(self.module.subprocess, "run", return_value=result) as run:
            version = self.module.installed_version()
        return version, run

    def test_version_comes_from_the_trusted_dpkg_query(self):
        version, run = self.dpkg("installed\t0.34.2")
        self.assertEqual("0.34.2", version)
        argv = run.call_args[0][0]
        self.assertEqual("/usr/bin/dpkg-query", argv[0])
        self.assertEqual("shadow-code", argv[-1])
        self.assertEqual(desktop.TRUSTED_PATH, run.call_args[1]["env"]["PATH"])

    def test_removed_or_unknown_package_has_no_version(self):
        self.assertIsNone(self.dpkg("config-files\t0.34.2")[0])
        self.assertIsNone(self.dpkg("", returncode=1)[0])

    def test_open_keeps_the_session_path_for_the_vendor_clis(self):
        session = "/home/me/.local/bin:/usr/bin:/bin"
        with patch.object(desktop, "trusted_program", return_value="/usr/bin/shadowcode"), \
                patch.dict(os.environ, {"PATH": session, "LD_PRELOAD": "/tmp/x.so"}), \
                patch.object(desktop.subprocess, "Popen") as popen:
            self.assertTrue(self.module.open_shadowcode())
        self.assertEqual(["/usr/bin/shadowcode"], popen.call_args[0][0])
        env = popen.call_args[1]["env"]
        self.assertEqual(session, env["PATH"])
        self.assertNotIn("LD_PRELOAD", env)
        self.assertTrue(popen.call_args[1]["start_new_session"])

    def test_open_refuses_when_not_installed(self):
        with patch.object(desktop, "trusted_program", return_value=None), \
                patch.object(desktop.subprocess, "Popen") as popen:
            self.assertFalse(self.module.open_shadowcode())
        popen.assert_not_called()


class States(unittest.TestCase):
    def setUp(self):
        from sfcc import shadowcode_page
        self.module = shadowcode_page
        with patch.object(shadowcode_page.QTimer, "singleShot"):
            self.page = pages.REGISTRY[pages.index_of("shadowcode")].build(
                PageContext(open_route=lambda _route: None))
        self.addCleanup(self.page.deleteLater)

    def test_installed(self):
        self.page.show_state("0.34.2", True, False)
        self.assertIn("0.34.2", self.page.state.text())
        self.assertTrue(self.page.open_button.isEnabled())
        self.assertTrue(self.page.software.isHidden())

    def test_not_installed_points_at_software(self):
        self.page.show_state(None, False, False)
        self.assertFalse(self.page.open_button.isEnabled())
        self.assertFalse(self.page.software.isHidden())
        self.assertIn("Software", self.page.state.text())

    def test_offline_explains_shadowcodes_own_setting_without_blocking_it(self):
        self.page.show_state("0.34.2", True, True)
        self.assertTrue(self.page.open_button.isEnabled(), "local models work offline")
        self.assertIn("Offline", self.page.network_fact.text())

    def test_a_failed_launch_says_so(self):
        self.page.show_state("0.34.2", True, False)
        with patch.object(self.module, "open_shadowcode", return_value=False):
            self.page._open()
        self.assertIn("could not be started", self.page.state.text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
