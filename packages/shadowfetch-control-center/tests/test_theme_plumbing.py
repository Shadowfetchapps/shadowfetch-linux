"""Theme plumbing: one palette source, and the element reaching every screen.

Stage P's brief names "theme plumbing" among the duplications to remove.  Two
were live in this package:

  * app.py painted the sidebar well from a bare `#101114`, one of the unnamed
    shipped colours tools/drift_gate.py reports.  It is `theme.SIDEBAR` now.
  * guide_page.py's System Passport document hard-coded `#d8a24a` -- Fire's
    gold -- and `#e2533b`, a second spelling of `theme.RED`.  So the System
    Passport stayed gold on an Ice desktop: the element did not reach the one
    page a person opens to read what their system is.

What is NOT fixed, and is not claimed to be: the rest of that document's web
palette is still unnamed literals, and so are the ones in grok_bot_page.py.
tools/drift_gate.py reports them as a single BLOCKED finding whose remedy is a
palette.json shipped from shadowfetch-branding and imported by sfcc, Welcome
and Fireproof -- three packages, none of them this one.
"""
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
CC = Path(__file__).resolve().parents[1]
SFCC = CC / "data/usr/share/shadowfetch/control-center/sfcc"
sys.path.insert(0, str(SFCC.parent))

from PyQt6.QtWidgets import QApplication  # noqa: E402

from sfcc import guide_page, theme  # noqa: E402

APP = QApplication.instance() or QApplication([])

DOCUMENT = {"release_version": "4.0.0", "generated_at": "now", "checks": [],
            "verdict": {"title": "T", "summary": "S"}}


class TheElementReachesTheGuide(unittest.TestCase):
    def test_the_passport_accent_is_the_themes_accent(self):
        self.assertIn(theme.GOLD, guide_page._report_html(DOCUMENT))

    def test_the_accent_is_not_written_into_the_document_source(self):
        """A hard-coded accent silently diverges the day the palette moves."""
        source = (SFCC / "guide_page.py").read_text(encoding="utf-8")
        self.assertNotIn(theme.GOLD.lower(), source.lower())

    def test_the_alarm_colour_has_one_spelling(self):
        source = (SFCC / "guide_page.py").read_text(encoding="utf-8")
        self.assertNotIn("#e2533b", source,
                         "theme.RED is spelled out again in the passport CSS")
        self.assertIn(theme.RED, guide_page._report_html(DOCUMENT))


class TheShellPaintsFromTheme(unittest.TestCase):
    def test_the_sidebar_well_is_a_named_colour(self):
        source = (SFCC / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("#101114", source,
                         "the sidebar colour is a bare literal in app.py again")
        self.assertEqual("#101114", theme.SIDEBAR)


class TheSidebarBadgeIsAGoldPill(unittest.TestCase):
    """5.0.0 VM QA (2abd1f6f): the Software update count rendered as ink on
    the dark sidebar with no pill. Unscoped widget-level sheets on the entry
    and on the sidebar well outranked the application's QLabel#badge rule."""

    def test_badge_background_is_the_gold(self):
        from PyQt6.QtCore import QSize
        from PyQt6.QtWidgets import QListWidget, QListWidgetItem, QVBoxLayout, QWidget
        from sfcc import app as ccapp

        window = QWidget()
        window.setStyleSheet(theme.STYLESHEET)
        well = QWidget(window)
        # An unscoped ancestor sheet is what hid the pill; this one mimics a
        # container that still carries one, so the badge must hold its own.
        well.setStyleSheet(f"QWidget#sideWrap {{ background: {theme.SIDEBAR}; }}")
        well.setObjectName("sideWrap")
        sidebar = QListWidget()
        sidebar.setObjectName("sidebar")
        entry = ccapp.SidebarEntry("Software", "Updates & bundles")
        item = QListWidgetItem()
        item.setSizeHint(QSize(200, 46))
        sidebar.addItem(item)
        sidebar.setItemWidget(item, entry)
        QVBoxLayout(well).addWidget(sidebar)
        well.resize(216, 120)
        entry.set_badge(44)
        window.show()
        APP.processEvents()
        image = entry.badge.grab().toImage()
        self.assertEqual(theme.GOLD, image.pixelColor(image.width() // 2, 2).name())

    def test_no_unscoped_background_sheet_in_the_shell(self):
        source = (SFCC / "app.py").read_text(encoding="utf-8")
        self.assertNotIn('setStyleSheet("background: transparent;")', source)
        self.assertNotIn('setStyleSheet(f"background: {theme.SIDEBAR};")', source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
