#!/usr/bin/env python3
"""ShadowCode on the Shadowfetch desktop: system policy, dock, launcher, shortcut, PATH.

Covers the files that make the preinstalled ShadowCode (package shadow-code,
desktop id com.shadowfetch.shadowcode, /usr/bin/shadowcode) fit the desktop:

  /etc/shadowcode/policy.yaml          the update policy ShadowCode reads itself
  first-login.sh + desktop-layout.js   dock pin after the file manager, launcher lead
  skel kglobalshortcutsrc              Meta+Shift+C launches it
  skel .local/bin                      exists at first login, so ~/.profile puts it on PATH
  shadowfetch-menus                    listed in the Workspaces launcher section

Nothing here needs Plasma, D-Bus or ShadowCode installed. first-login.sh runs for
real, against a private HOME and stub programs.
"""
import configparser
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import textwrap
import unittest
import xml.etree.ElementTree as ET

import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
DATA = ROOT / "data"
INSTALL = ROOT / "debian/shadowfetch-defaults.install"
POLICY = DATA / "etc/shadowcode/policy.yaml"
FIRST_LOGIN = DATA / "usr/lib/shadowfetch/first-login.sh"
LAYOUT = DATA / "usr/share/shadowfetch/desktop-layout.js"
SHORTCUTS = DATA / "etc/skel/.config/kglobalshortcutsrc"
LOCAL_BIN = DATA / "etc/skel/.local/bin/.keep"
MENUS = REPO / "packages/shadowfetch-menus"
MENU = MENUS / "etc/xdg/menus/applications-merged/shadowfetch-launcher.menu"

DESKTOP_ID = "com.shadowfetch.shadowcode.desktop"  # shadow-code 0.34.2's own file
SHORTCUT = "Meta+Shift+C"

# ShadowCode v0.34.2, native/core/src/updates.rs: `struct PolicyFile { updates:
# PolicyUpdates }` and `#[serde(default, deny_unknown_fields)] struct
# PolicyUpdates { check: Option<bool>, default: Option<bool>, message:
# Option<String> }`; MAX_POLICY_BYTES = 64 KiB; clean_message keeps one line of
# at most MAX_MESSAGE_CHARS = 300.
POLICY_KEYS = {"check", "default", "message"}
MAX_POLICY_BYTES = 64 * 1024
MAX_MESSAGE_CHARS = 300

# Launch shortcuts Plasma 6.6 declares in the 4.1 image's desktop files
# (X-KDE-Shortcuts), plus KWin's Meta+Shift defaults. ShadowCode's must not clash.
PLASMA_TAKEN = {
    "Alt+Shift+F2", "Alt+Space", "Alt+F2", "Ctrl+Alt+T", "Meta+P", "Meta+Alt+R",
    "Meta+Ctrl+Print", "Meta+Ctrl+R", "Meta+E", "Meta+Esc", "Meta+.", "Meta+Print",
    "Meta+Shift+Print", "Meta+Shift+R", "Meta+R", "Print", "Meta+Shift+S",
    "Shift+Print", "Meta+I", "Meta+V", "Meta+L", "Meta+D", "Meta+W", "Meta+G",
    "Meta+Shift+Left", "Meta+Shift+Right", "Meta+Shift+Up", "Meta+Shift+Down",
    "Meta+Shift+Tab",
}


def installed(source):
    """The destination directory the .install file gives a data/ path, or None."""
    for line in INSTALL.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == source:
            return parts[1]
    return None


class SystemPolicy(unittest.TestCase):
    def setUp(self):
        self.text = POLICY.read_text(encoding="utf-8")
        self.policy = yaml.safe_load(self.text)

    def test_ships_at_the_path_shadowcode_reads(self):
        # updates.rs: SYSTEM_POLICY = "/etc/shadowcode/policy.yaml"
        self.assertEqual("etc/shadowcode/", installed("data/etc/shadowcode/policy.yaml"))

    def test_only_keys_shadowcode_0_34_2_supports(self):
        # An unknown key under `updates` makes ShadowCode read the file as
        # broken: update checks off AND an error in Settings > About.
        self.assertEqual({"updates"}, set(self.policy))
        self.assertLessEqual(set(self.policy["updates"]), POLICY_KEYS)
        self.assertLessEqual(len(self.text.encode()), MAX_POLICY_BYTES)

    def test_update_checks_are_off_and_point_at_system_updates(self):
        updates = self.policy["updates"]
        self.assertIs(False, updates["check"])
        message = updates["message"]
        self.assertIsInstance(message, str)
        self.assertEqual(message, " ".join(message.split()), "one plain line")
        self.assertLessEqual(len(message), MAX_MESSAGE_CHARS)
        self.assertIn("Software", message)


class DockAndLauncher(unittest.TestCase):
    """first-login.sh run for real: stubs for Plasma and D-Bus, a private HOME."""

    def run_first_login(self, apps):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            desktop_dir = home / ".local/share/applications"
            desktop_dir.mkdir(parents=True)
            for app in apps:
                (desktop_dir / app).write_text("[Desktop Entry]\nType=Application\n")
            stubs = Path(tmp) / "bin"
            stubs.mkdir()
            programs = {
                "plasma-apply-wallpaperimage": "exit 0",
                "plasma-apply-lookandfeel": "exit 0",
                "plasma-apply-colorscheme": "exit 0",
                "sleep": "exit 0",
                "dbus-send": "echo '   boolean true'",
                "qdbus6": "exit 0",
            }
            for name, body in programs.items():
                path = stubs / name
                path.write_text(f"#!/bin/sh\n{body}\n")
                path.chmod(path.stat().st_mode | stat.S_IXUSR)
            layout = Path(tmp) / "desktop-layout.js"
            layout.write_text(LAYOUT.read_text(encoding="utf-8"))
            script = FIRST_LOGIN.read_text(encoding="utf-8").replace(
                "SRC=/usr/share/shadowfetch/desktop-layout.js", f"SRC={layout}")
            self.assertNotEqual(script, FIRST_LOGIN.read_text(encoding="utf-8"))
            runner = Path(tmp) / "first-login.sh"
            runner.write_text(script)
            env = {"HOME": str(home), "PATH": f"{stubs}:/usr/bin:/bin"}
            subprocess.run(["/bin/sh", str(runner)], env=env, check=True, timeout=60)
            return (home / ".cache/sf-desktop-layout.js").read_text(encoding="utf-8")

    @staticmethod
    def config(js, key):
        marker = f'writeConfig("{key}", "'
        start = js.index(marker) + len(marker)
        return js[start:js.index('"', start)].split(",")

    def test_shadowcode_is_pinned_right_after_the_file_manager(self):
        js = self.run_first_login(["org.kde.dolphin.desktop", DESKTOP_ID])
        launchers = self.config(js, "launchers")
        self.assertEqual(["applications:org.kde.dolphin.desktop", f"applications:{DESKTOP_ID}"],
                         launchers[:2])

    def test_shadowcode_leads_the_launcher_favorites(self):
        js = self.run_first_login(["org.kde.dolphin.desktop", DESKTOP_ID])
        self.assertEqual(f"applications:{DESKTOP_ID}", self.config(js, "favorites")[0])

    def test_no_broken_pin_when_shadowcode_is_absent(self):
        if Path("/usr/share/applications", DESKTOP_ID).exists():
            self.skipTest("ShadowCode is installed on this host")
        js = self.run_first_login(["org.kde.dolphin.desktop"])
        self.assertNotIn(DESKTOP_ID, js)
        self.assertNotIn(",,", js)


class GlobalShortcut(unittest.TestCase):
    def setUp(self):
        self.parser = configparser.ConfigParser(interpolation=None, delimiters=("=",))
        self.parser.optionxform = str
        self.parser.read(SHORTCUTS, encoding="utf-8")

    def test_launch_shortcut_names_shadowcodes_desktop_id(self):
        # Plasma 6 kglobalaccel keeps .desktop launch shortcuts in the
        # [services][<desktop id>] group under the `_launch` action.
        group = f"services][{DESKTOP_ID}"
        self.assertEqual([group], self.parser.sections())
        self.assertEqual(SHORTCUT, self.parser[group]["_launch"])

    def test_shortcut_does_not_take_a_plasma_default(self):
        self.assertNotIn(SHORTCUT, PLASMA_TAKEN)

    def test_shipped_into_skel(self):
        self.assertEqual("etc/skel/.config/", installed("data/etc/skel/.config/kglobalshortcutsrc"))


class LocalBinOnFirstLogin(unittest.TestCase):
    def test_skel_carries_local_bin(self):
        self.assertTrue(LOCAL_BIN.is_file())
        self.assertEqual("etc/skel/.local/bin/", installed("data/etc/skel/.local/bin/.keep"))

    def test_the_placeholder_is_not_a_command(self):
        self.assertFalse(LOCAL_BIN.stat().st_mode & 0o111)


class LauncherMenu(unittest.TestCase):
    def test_workspaces_section_lists_shadowcode(self):
        root = ET.fromstring(MENU.read_text(encoding="utf-8"))
        section = next(menu for menu in root.iter("Menu") if menu.findtext("Name") == "Shadowfetch-AI")
        files = [node.text for node in section.iter("Filename")]
        self.assertIn(DESKTOP_ID, files)

    def test_directory_mentions_shadowcode(self):
        directory = MENUS / "usr/share/desktop-directories/shadowfetch-local-ai.directory"
        self.assertIn("ShadowCode", directory.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
