"""Welcome's checkbox and radio indicators (5.0.0 VM qualification).

QA found a ticked checkbox drawn as a solid gold square with no tick (a Qt
stylesheet that styles ::indicator drops the native mark), a selected radio
drawn square (its border grew from 2px to 5px, so border-radius no longer made
a circle), and a dark band behind each radio row (the QWidget background rule
reached the radio buttons inside the card). The page is rendered offscreen and
the pixels are checked, so this is about what the user sees.
"""
import importlib.machinery
import importlib.util
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PKG = ROOT / "packages/shadowfetch-welcome"
WELCOME = PKG / "src/shadowfetch-welcome"
CHECK_SVG = PKG / "data/usr/share/shadowfetch/welcome/check.svg"


def load_welcome():
    loader = importlib.machinery.SourceFileLoader("sf_welcome_style_test", str(WELCOME))
    spec = importlib.util.spec_from_loader("sf_welcome_style_test", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class Packaging(unittest.TestCase):
    def test_tick_image_is_shipped_and_installed(self):
        self.assertTrue(CHECK_SVG.is_file())
        self.assertIn("<path", CHECK_SVG.read_text())
        install = (PKG / "debian/shadowfetch-welcome.install").read_text().split()
        self.assertIn("data/usr/share/shadowfetch/welcome/check.svg", install)
        # Qt reads the SVG through the image-format plugin.
        self.assertIn("qt6-svg-plugins", (PKG / "debian/control").read_text())

    def test_stylesheet_names_the_tick_and_keeps_radio_border_constant(self):
        src = WELCOME.read_text(encoding="utf-8")
        self.assertIn("image: url({CHECK_MARK})", src)
        self.assertNotIn("border: 5px solid {GOLD}", src)
        self.assertIn("QCheckBox, QRadioButton {{ spacing: 10px; background: transparent; }}", src)


@unittest.skipUnless(os.environ.get("QT_QPA_PLATFORM") == "offscreen",
                     "renders offscreen only (make test sets QT_QPA_PLATFORM)")
class Rendered(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from PyQt6.QtWidgets import QApplication
        except ImportError as exc:  # pragma: no cover
            raise unittest.SkipTest(f"PyQt6 unavailable: {exc}")
        cls.app = QApplication.instance() or QApplication(sys.argv[:1])
        cls.m = load_welcome()
        if not Path(cls.m.CHECK_MARK).is_file():
            raise AssertionError(f"tick image not resolved: {cls.m.CHECK_MARK!r}")

    def setUp(self):
        self.win = self.m.ShadowfetchWelcome(mode="catalog")
        self.page = self.m.AgentSetupPage(on_next=lambda *a: None)
        self.win.stack.addWidget(self.page)
        self.win.stack.setCurrentWidget(self.page)
        self.win.resize(940, 680)
        self.win.show()
        self.page.network_choice["online"].setChecked(True)
        for checkbox in self.page.coding_agents.values():
            checkbox.setChecked(True)
        self.app.processEvents()
        self.image = self.win.grab().toImage()

    def tearDown(self):
        self.win.close()
        self.win.deleteLater()
        self.app.processEvents()

    def _pixels(self, widget, rect):
        """{(x, y): (r, g, b)} for rect in widget coordinates."""
        from PyQt6.QtCore import QPoint
        origin = widget.mapTo(self.win, QPoint(0, 0))
        dpr = self.image.devicePixelRatio()
        out = {}
        for x in range(rect.left(), rect.right() + 1):
            for y in range(rect.top(), rect.bottom() + 1):
                c = self.image.pixelColor(int((origin.x() + x) * dpr), int((origin.y() + y) * dpr))
                out[(x, y)] = (c.red(), c.green(), c.blue())
        return out

    def _gold_box(self, button):
        """Pixels of the indicator and the bounding box of its gold part."""
        from PyQt6.QtWidgets import QStyle, QStyleOptionButton
        option = QStyleOptionButton()
        button.initStyleOption(option)
        element = (QStyle.SubElement.SE_CheckBoxIndicator
                   if button.metaObject().className() == "QCheckBox"
                   else QStyle.SubElement.SE_RadioButtonIndicator)
        rect = button.style().subElementRect(element, option, button)
        pixels = self._pixels(button, rect.adjusted(-4, -4, 4, 4).intersected(button.rect()))
        gold = tuple(int(self.m.GOLD[i:i + 2], 16) for i in (1, 3, 5))
        golden = [xy for xy, rgb in pixels.items()
                  if sum(abs(a - b) for a, b in zip(rgb, gold)) < 40]
        self.assertTrue(golden, "no gold indicator was drawn")
        xs = [x for x, _ in golden]
        ys = [y for _, y in golden]
        return pixels, gold, (min(xs), min(ys), max(xs), max(ys))

    def test_a_ticked_checkbox_shows_a_dark_mark_on_gold(self):
        checkbox = next(iter(self.page.coding_agents.values()))
        pixels, _gold, (x0, y0, x1, y1) = self._gold_box(checkbox)
        inner = [rgb for (x, y), rgb in pixels.items()
                 if x0 + 3 <= x <= x1 - 3 and y0 + 3 <= y <= y1 - 3]
        dark = [rgb for rgb in inner if sum(rgb) < 150]
        self.assertGreater(len(dark), 6, "checked checkbox is a plain gold square with no tick")

    def test_the_selected_radio_is_round(self):
        radio = self.page.network_choice["online"]
        pixels, gold, (x0, y0, x1, y1) = self._gold_box(radio)
        self.assertLessEqual(abs((x1 - x0) - (y1 - y0)), 1)
        cx, cy, r = (x0 + x1) / 2, (y0 + y1) / 2, (x1 - x0) / 2
        # A point at 0.75 r along each diagonal lies outside a circle but on
        # the border of a rounded square.
        for dx in (-1, 1):
            for dy in (-1, 1):
                rgb = pixels[(round(cx + dx * 0.75 * r), round(cy + dy * 0.75 * r))]
                self.assertGreater(sum(abs(a - b) for a, b in zip(rgb, gold)), 120,
                                   "selected radio indicator has square corners")

    def test_radio_rows_have_no_dark_band(self):
        radio = self.page.network_choice["offline"]
        right = radio.rect().adjusted(radio.width() - 20, 2, -2, -2)
        bg = tuple(int(self.m.BG[i:i + 2], 16) for i in (1, 3, 5))
        self.assertNotIn(bg, set(self._pixels(radio, right).values()),
                         "the window background shows through behind the radio row")

if __name__ == "__main__":
    unittest.main(verbosity=2)
