"""Welcome cosmetics found by 5.0.0 VM QA on ISO 2abd1f6f.

  * The Ignition page was still titled "Pick your flame", Fire/Ice-era copy on
    the single ShadowCode look.
  * The "All set." bar drew a grey "100%" across the gold chunk: a stylesheet'd
    QProgressBar paints its label in one colour over both chunk and trough, so
    the percentage now lives beside the title in TEXT, outside the bar.
"""
import importlib.machinery
import importlib.util
import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[3]
WELCOME = ROOT / "packages/shadowfetch-welcome/src/shadowfetch-welcome"


def load_welcome():
    loader = importlib.machinery.SourceFileLoader("sf_welcome_cosmetics", str(WELCOME))
    spec = importlib.util.spec_from_loader("sf_welcome_cosmetics", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class IgnitionCopy(unittest.TestCase):
    def test_no_flame_copy_on_the_ignition_page(self):
        source = WELCOME.read_text(encoding="utf-8")
        self.assertNotIn("Pick your flame", source)
        self.assertNotIn("picking a flame", source)
        self.assertIn('"Choose your setup"', source)


class ProgressLabels(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = load_welcome()
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_all_set_bar_draws_no_in_bar_label(self):
        page = self.m.InstallPage(lambda: None)
        page._finalize_setup()
        self.assertEqual(page.bar.value(), page.bar.maximum())
        self.assertFalse(page.bar.isTextVisible())

    def test_install_panel_percentage_is_outside_the_bar(self):
        panel = self.m.InstallPanel()
        self.assertFalse(panel.bar.isTextVisible())
        panel.bar.setValue(64)
        self.assertEqual("64%", panel.pct.text())
        self.assertIn(self.m.TEXT, panel.pct.styleSheet())


if __name__ == "__main__":
    unittest.main()
