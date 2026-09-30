"""The Guide page must survive run_check (5.0.0 QA).

guide_page.py called busutil.trusted_program() without importing busutil, so
opening Guide -- including Welcome's "Check this computer" -- raised NameError
inside a Qt slot and aborted Mission Control. Drive run_check for real, with
the passport tool both missing and present.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
SFCC = (Path(__file__).resolve().parents[1] / "data/usr/share/shadowfetch/"
        "control-center/sfcc")
sys.path.insert(0, str(SFCC.parent))

from PyQt6.QtWidgets import QApplication  # noqa: E402

from sfcc import busutil, guide_page  # noqa: E402

APP = QApplication.instance() or QApplication([])


class GuidePageRunCheck(unittest.TestCase):
    def test_missing_passport_shows_an_error_instead_of_crashing(self):
        page = guide_page.GuidePage(lambda *_: None)
        with mock.patch.object(busutil, "trusted_program", return_value=None):
            page.run_check()
        self.assertTrue(page.run_button.isEnabled())

    def test_present_passport_starts_the_check(self):
        page = guide_page.GuidePage(lambda *_: None)
        with mock.patch.object(busutil, "trusted_program", return_value="/bin/true"):
            page.run_check()
        self.assertFalse(page.run_button.isEnabled())
        page._process.waitForFinished(5000)


if __name__ == "__main__":
    unittest.main()
