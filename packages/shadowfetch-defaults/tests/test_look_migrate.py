#!/usr/bin/env python3
"""look-migrate.sh: 4.1 Fire/Ice desktops land on ShadowCode, and nothing else moves.

5.0.0 removed the Ice colour scheme, the Glacier Konsole scheme, the
org.shadowfetch.ice look-and-feel and the retired Umbra wallpapers. The script
repoints settings that name one of those, and ONLY those. Each case runs the
real script under /bin/sh with a throwaway HOME and stubbed KDE tools on PATH
that record their argv, so the assertions are about what the script asked KDE
to do, not about KDE.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

DEFAULTS = Path(__file__).resolve().parents[1]
SCRIPT = DEFAULTS / "data/usr/lib/shadowfetch/look-migrate.sh"
AUTOSTART = DEFAULTS / "data/etc/xdg/autostart/shadowfetch-look-migrate.desktop"
INSTALL = DEFAULTS / "debian/shadowfetch-defaults.install"

NEW_WALLPAPER = "/usr/share/backgrounds/shadowfetch/shadowcode-4k.jpg"
UMBRA_FIRE = "file:///usr/share/wallpapers/UmbraFire/contents/images/3840x2160.jpg"
UMBRA_ICE = "/usr/share/wallpapers/UmbraIce/contents/images/3840x2160.jpg"
CUSTOM = "file:///home/someone/Pictures/cat.jpg"
GOLD = "242,179,61"

STUBS = ("plasma-apply-colorscheme", "plasma-apply-wallpaperimage",
         "plasma-apply-lookandfeel", "kwriteconfig6", "kreadconfig6", "qdbus6")

# Each stub appends [name, *argv] as one JSON line to $STUB_LOG.
STUB = textwrap.dedent("""\
    #!{python}
    import json, os, sys
    with open(os.environ["STUB_LOG"], "a") as log:
        log.write(json.dumps(["{name}", *sys.argv[1:]]) + "\\n")
    """)


def appletsrc(*images: str) -> str:
    out = []
    for n, image in enumerate(images, start=1):
        out += [f"[Containments][{n}]", "plugin=org.kde.plasma.folder",
                "wallpaperplugin=org.kde.image", "",
                f"[Containments][{n}][Wallpaper][org.kde.image][General]",
                f"Image={image}", ""]
    return "\n".join(out)


class LookMigrate(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="look-migrate-"))
        self.home = self.tmp / "home"
        self.config = self.home / ".config"
        self.config.mkdir(parents=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.log"
        self.log.touch()
        for name in STUBS:
            path = self.bin / name
            path.write_text(STUB.format(name=name, python=sys.executable))
            path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel: str, text: str) -> Path:
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def run_script(self) -> list[list[str]]:
        start = len(self.log.read_text().splitlines())
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "STUB_LOG": str(self.log),
        }
        result = subprocess.run(["/bin/sh", str(SCRIPT)], env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(0, result.returncode, result.stderr)
        lines = self.log.read_text().splitlines()[start:]
        return [json.loads(line) for line in lines]

    @property
    def stamp(self) -> Path:
        return self.config / "shadowfetch/.look-migrated"

    def test_retired_cli_launchers_go_and_everything_else_stays(self):
        apps = ".local/share/applications/"
        codex = self.write(apps + "codex-cli.desktop",
                           "[Desktop Entry]\nName=Codex CLI\nExec=/usr/bin/shadowfetch-codex open\n")
        claude = self.write(apps + "claude-code.desktop",
                            "[Desktop Entry]\nName=Claude Code\nExec=/usr/bin/shadowfetch-code-agent claude open\n")
        edited = self.write(apps + "cursor-agent.desktop",
                            "[Desktop Entry]\nName=Cursor\nExec=/home/u/.local/bin/cursor-agent\n")
        cli = self.write(".local/bin/codex", "#!/bin/sh\n")
        self.run_script()
        self.assertFalse(codex.exists())
        self.assertFalse(claude.exists())
        self.assertTrue(edited.exists())
        self.assertTrue(cli.exists())

    # -- the four required cases ------------------------------------------------

    def test_an_ice_desktop_is_moved_to_shadowcode(self):
        self.write(".config/kdeglobals", textwrap.dedent("""\
            [General]
            ColorScheme=ShadowfetchIce
            AccentColor=74,162,216

            [KDE]
            LookAndFeelPackage=org.shadowfetch.ice
            """))
        self.write(".config/ksplashrc", "[KSplash]\nEngine=KSplashQML\nTheme=org.shadowfetch.ice\n")
        self.write(".config/plasma-org.kde.plasma.desktop-appletsrc", appletsrc(UMBRA_ICE))
        self.write(".config/kscreenlockerrc", textwrap.dedent(f"""\
            [Greeter][Wallpaper][org.kde.image][General]
            Image={UMBRA_ICE}
            """))
        profile = self.write(".local/share/konsole/Shadowfetch.profile",
                             "[Appearance]\nColorScheme=ShadowfetchGlacier\n")
        self.write(".config/shadowfetch/.element-applied", "ice\n")

        calls = self.run_script()

        self.assertIn(["plasma-apply-lookandfeel", "-a", "org.shadowfetch.dark"], calls)
        self.assertIn(["plasma-apply-colorscheme", "ShadowfetchDark"], calls)
        self.assertIn(["plasma-apply-wallpaperimage", NEW_WALLPAPER], calls)
        self.assertIn(["kwriteconfig6", "--file", str(profile), "--group", "Appearance",
                       "--key", "ColorScheme", "ShadowfetchUmbra"], calls)
        self.assertIn(["kwriteconfig6", "--file", str(self.config / "kdeglobals"),
                       "--group", "General", "--key", "AccentColor", GOLD], calls)
        self.assertIn(["kwriteconfig6", "--file", str(self.config / "ksplashrc"),
                       "--group", "KSplash", "--key", "Theme", "org.shadowfetch.dark"], calls)
        self.assertIn(["kwriteconfig6", "--file", str(self.config / "kscreenlockerrc"),
                       "--group", "Greeter", "--group", "Wallpaper", "--group",
                       "org.kde.image", "--group", "General", "--key", "Image",
                       NEW_WALLPAPER], calls)
        self.assertTrue(self.stamp.is_file())
        self.assertFalse((self.config / "shadowfetch/.element-applied").exists(),
                         "the retired element stamp is left behind")

    def test_a_fire_desktop_on_the_umbrafire_wallpaper_is_moved(self):
        self.write(".config/kdeglobals", textwrap.dedent("""\
            [Colors:Selection]
            BackgroundNormal=216,162,74

            [General]
            ColorScheme=ShadowfetchDark
            AccentColor=216,162,74

            [KDE]
            LookAndFeelPackage=org.shadowfetch.dark
            """))
        self.write(".config/plasma-org.kde.plasma.desktop-appletsrc", appletsrc(UMBRA_FIRE))
        profile = self.write(".local/share/konsole/Shadowfetch.profile",
                             "[Appearance]\nColorScheme=ShadowfetchUmbra\n")

        calls = self.run_script()

        self.assertIn(["plasma-apply-wallpaperimage", NEW_WALLPAPER], calls)
        # Same scheme, re-applied so kdeglobals picks up the ShadowCode colours.
        self.assertEqual("ShadowfetchDark",
                         [c for c in calls if c[0] == "plasma-apply-colorscheme"][-1][1])
        self.assertNotIn("plasma-apply-lookandfeel", [c[0] for c in calls])
        # 4.1 Fire's default accent would override the ShadowCode scheme's.
        self.assertIn(["kwriteconfig6", "--file", str(self.config / "kdeglobals"),
                       "--group", "General", "--key", "AccentColor", GOLD], calls)
        self.assertFalse(any(str(profile) in c for c in calls),
                         "a Konsole profile on a scheme that still exists was rewritten")
        self.assertTrue(self.stamp.is_file())

    def test_a_custom_wallpaper_and_scheme_are_left_alone(self):
        self.write(".config/kdeglobals", textwrap.dedent("""\
            [General]
            ColorScheme=BreezeDark
            AccentColor=61,174,233

            [KDE]
            LookAndFeelPackage=org.kde.breezedark.desktop
            """))
        self.write(".config/plasma-org.kde.plasma.desktop-appletsrc", appletsrc(CUSTOM))
        self.write(".config/kscreenlockerrc", textwrap.dedent(f"""\
            [Greeter][Wallpaper][org.kde.image][General]
            Image={CUSTOM}
            """))
        self.write(".local/share/konsole/Mine.profile",
                   "[Appearance]\nColorScheme=Solarized\n")

        calls = self.run_script()

        self.assertEqual([], calls, "a desktop naming no removed asset was changed")
        self.assertTrue(self.stamp.is_file())

    def test_it_runs_only_once(self):
        self.write(".config/kdeglobals", "[General]\nColorScheme=ShadowfetchIce\n")
        self.write(".config/plasma-org.kde.plasma.desktop-appletsrc", appletsrc(UMBRA_ICE))
        self.assertTrue(self.run_script())
        self.assertTrue(self.stamp.is_file())
        # The user picks Ice-era values again (say, restored from a backup):
        # a second login must not touch them.
        self.assertEqual([], self.run_script())

    # -- edges that decide whether a choice survives ---------------------------

    def test_only_the_desktops_on_removed_wallpapers_are_repointed(self):
        self.write(".config/kdeglobals", textwrap.dedent(f"""\
            [Colors:Selection]
            BackgroundNormal={GOLD}

            [General]
            ColorScheme=ShadowfetchDark
            """))
        self.write(".config/plasma-org.kde.plasma.desktop-appletsrc",
                   appletsrc(UMBRA_FIRE, CUSTOM))

        calls = self.run_script()

        self.assertNotIn("plasma-apply-wallpaperimage", [c[0] for c in calls],
                         "every screen was repainted, including the custom one")
        (script_call,) = [c for c in calls if c[0] == "qdbus6"]
        script = script_call[-1]
        self.assertIn("UmbraFire", script.replace("Umbra(Fire", "UmbraFire"))
        self.assertIn(f"'file://{NEW_WALLPAPER}'", script)
        self.assertNotIn("plasma-apply-colorscheme", [c[0] for c in calls],
                         "a scheme already carrying the ShadowCode colours was re-applied")

    def test_a_missing_scheme_gets_shadowcode(self):
        self.write(".config/kdeglobals", "[KDE]\nSingleClick=false\n")
        calls = self.run_script()
        self.assertEqual([["plasma-apply-colorscheme", "ShadowfetchDark"]], calls)

    def test_a_failed_step_leaves_the_stamp_unwritten(self):
        failing = self.bin / "plasma-apply-colorscheme"
        failing.write_text("#!/bin/sh\nexit 1\n")
        sleep = self.bin / "sleep"
        sleep.write_text("#!/bin/sh\nexit 0\n")
        sleep.chmod(0o755)
        self.write(".config/kdeglobals", "[General]\nColorScheme=ShadowfetchIce\n")
        self.write(".config/shadowfetch/.element-applied", "ice\n")
        self.run_script()
        self.assertFalse(self.stamp.exists(), "a half-done migration was marked done")
        self.assertTrue((self.config / "shadowfetch/.element-applied").exists())


class Packaging(unittest.TestCase):
    def test_autostart_entry_runs_the_script_in_phase_1_on_kde(self):
        entry = AUTOSTART.read_text()
        self.assertIn("Exec=/usr/lib/shadowfetch/look-migrate.sh\n", entry)
        self.assertIn("OnlyShowIn=KDE;\n", entry)
        self.assertIn("X-KDE-autostart-phase=1\n", entry)

    def test_both_files_are_installed(self):
        install = INSTALL.read_text().split()
        self.assertIn("data/usr/lib/shadowfetch/look-migrate.sh", install)
        self.assertIn("data/etc/xdg/autostart/shadowfetch-look-migrate.desktop", install)
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_the_script_is_posix_sh(self):
        self.assertTrue(SCRIPT.read_text().startswith("#!/bin/sh\n"))
        subprocess.run(["sh", "-n", str(SCRIPT)], check=True)
        if shutil.which("dash"):
            subprocess.run(["dash", "-n", str(SCRIPT)], check=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
