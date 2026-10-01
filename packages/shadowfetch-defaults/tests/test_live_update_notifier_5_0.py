#!/usr/bin/env python3
"""KDE's update notifier stays off on the live medium (5.0.0 soak finding).

DiscoverNotifier asks PackageKit to refresh the apt indexes 300 s after login.
The ISO ships without them, so on the live medium the 5.0.0 soak (ISO 2d8a72e0)
saw ~350 MB of lists land in the RAM-backed overlay mid-session. shadowfetch-
defaults ships a user-unit drop-in that skips the notifier when /run/live/medium
exists -- the ISO only -- so installed and upgraded systems keep their update
notifications. The drop-in is checked here as systemd reads it: attached to the
unit systemd-xdg-autostart-generator makes from Discover's autostart entry, and
evaluated with and without the live marker.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

DEFAULTS = Path(__file__).resolve().parents[1]
ROOT = DEFAULTS.parents[1]
AUTOSTART = "org.kde.discover.notifier.desktop"
UNIT = "app-org.kde.discover.notifier@autostart.service"
DIRECTORY = f"usr/lib/systemd/user/{UNIT}.d"
REL = f"{DIRECTORY}/10-shadowfetch-live-medium.conf"
DROPIN = DEFAULTS / "data" / REL
INSTALL = DEFAULTS / "debian/shadowfetch-defaults.install"
GENERATOR = Path("/usr/lib/systemd/user-generators/systemd-xdg-autostart-generator")


def directives(text: str) -> list[tuple[str | None, str]]:
    """(section, line) for every non-comment line, in order."""
    section, found = None, []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        found.append((section, line))
    return found


class DropIn(unittest.TestCase):
    def test_it_only_skips_the_live_medium(self):
        self.assertEqual(
            [("Unit", "ConditionPathExists=!/run/live/medium"),
             ("Unit", "ConditionPathExists=!/run/live/rootfs")],
            directives(DROPIN.read_text()))

    def test_it_uses_the_same_live_test_as_the_image(self):
        # hooks/0020 gates shadowfetch-gpu-detect.service on exactly these two.
        hook = (ROOT / "live-build/config/hooks/0020-gpu-firstboot.hook.chroot").read_text()
        for line in ("ConditionPathExists=!/run/live/medium",
                     "ConditionPathExists=!/run/live/rootfs"):
            self.assertIn(line, hook)

    def test_it_is_installed_in_the_vendor_user_unit_directory(self):
        # /usr/lib, not /etc: an administrator's /etc/systemd/user drop-in of
        # the same name overrides it, and no conffile prompt on a change.
        lines = [line.split() for line in INSTALL.read_text().splitlines() if line.strip()]
        self.assertIn([f"data/{REL}", f"{DIRECTORY}/"], lines)

    def test_it_is_for_the_autostart_entry_the_image_ships(self):
        # live-build/chroot is the last build's root; absent on a clean checkout.
        chroot = ROOT / "live-build/chroot/etc/xdg/autostart"
        if not chroot.is_dir():
            self.skipTest("no built live-build chroot to compare with")
        self.assertTrue((chroot / AUTOSTART).is_file(),
                        f"Discover's autostart entry is no longer {AUTOSTART}")


@unittest.skipUnless(os.access(GENERATOR, os.X_OK) and shutil.which("systemd-analyze"),
                     "needs systemd-xdg-autostart-generator and systemd-analyze")
class AsSystemdReadsIt(unittest.TestCase):
    """The generator's unit, this drop-in from a separate vendor directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-notifier-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        autostart = self.tmp / "xdg/autostart"
        autostart.mkdir(parents=True)
        # The generator refuses an entry whose Exec does not exist on this host.
        (autostart / AUTOSTART).write_text(
            "[Desktop Entry]\nName=Discover\nExec=/bin/true\nType=Application\n"
            "NoDisplay=true\nX-KDE-autostart-phase=1\nOnlyShowIn=KDE\n")
        self.generated = self.tmp / "generator.late"
        self.generated.mkdir()
        subprocess.run(
            [str(GENERATOR), *(str(self.generated),) * 3],
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.tmp),
                 "XDG_CONFIG_HOME": str(self.tmp / "home"),
                 "XDG_CONFIG_DIRS": str(self.tmp / "xdg")},
            check=True, capture_output=True, timeout=30)

    def conditions(self, vendor: Path) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["systemd-analyze", "--user", "condition", f"--unit={UNIT}"],
            env={**os.environ, "SYSTEMD_UNIT_PATH": f"{self.generated}:{vendor}"},
            cwd=self.tmp, capture_output=True, text=True, timeout=30)

    def vendor_tree(self, text: str) -> Path:
        vendor = self.tmp / "vendor"
        target = vendor / f"{UNIT}.d"
        target.mkdir(parents=True, exist_ok=True)
        (target / DROPIN.name).write_text(text)
        return vendor

    def test_the_generator_makes_the_unit_the_drop_in_names(self):
        self.assertTrue((self.generated / UNIT).is_file(),
                        sorted(path.name for path in self.generated.iterdir()))

    def test_an_installed_system_starts_the_notifier(self):
        if Path("/run/live/medium").exists() or Path("/run/live/rootfs").exists():
            self.skipTest("this host is itself a live session")
        result = self.conditions(self.vendor_tree(DROPIN.read_text()))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_the_live_medium_skips_it(self):
        # The live marker cannot be created here; a directory that exists
        # stands in for it, which is the same condition to systemd.
        marker = self.tmp / "medium"
        marker.mkdir()
        text = DROPIN.read_text().replace("/run/live/medium", str(marker))
        result = self.conditions(self.vendor_tree(text))
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(f"ConditionPathExists=!{marker} failed", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
