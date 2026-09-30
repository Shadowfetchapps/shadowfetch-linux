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
            # Never the build machine's /etc/xdg/kdeglobals.
            "XDG_CONFIG_DIRS": str(self.tmp / "xdg"),
            **getattr(self, "extra_env", {}),
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
        # Found in the VM upgrade: applying the scheme bakes the CURRENT accent
        # into [Colors:Selection], so the retired accent must go first.
        accent_call = calls.index(["kwriteconfig6", "--file", str(self.config / "kdeglobals"),
                                   "--group", "General", "--key", "AccentColor", GOLD])
        last_apply = max(i for i, c in enumerate(calls)
                         if c == ["plasma-apply-colorscheme", "ShadowfetchDark"])
        self.assertLess(accent_call, last_apply)

    def test_a_retired_accent_forces_a_reapply_even_when_the_scheme_looks_current(self):
        self.write(".config/kdeglobals", textwrap.dedent(f"""\
            [Colors:Selection]
            BackgroundNormal={GOLD}

            [General]
            ColorScheme=ShadowfetchDark
            AccentColor=216,162,74
            """))
        calls = self.run_script()
        self.assertIn(["plasma-apply-colorscheme", "ShadowfetchDark"], calls)

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

    # -- Konsole default profile (5.0.0 QA: Konsole ignored Shadowfetch.profile) --

    def _gold_desktop(self):
        # A desktop needing no look change, so only the Konsole step can act.
        self.write(".config/kdeglobals", textwrap.dedent(f"""\
            [Colors:Selection]
            BackgroundNormal={GOLD}

            [General]
            ColorScheme=ShadowfetchDark
            """))

    def test_konsole_without_a_default_profile_gets_shadowfetch(self):
        self._gold_desktop()
        self.write(".local/share/konsole/Shadowfetch.profile",
                   "[Appearance]\nColorScheme=ShadowfetchUmbra\n")
        self.write(".config/konsolerc", "[MainWindow]\nMenuBar=Disabled\n")
        calls = self.run_script()
        self.assertEqual([["kwriteconfig6", "--file", str(self.config / "konsolerc"),
                           "--group", "Desktop Entry", "--key", "DefaultProfile",
                           "Shadowfetch.profile"]], calls)
        self.assertTrue(self.stamp.is_file())

    def test_a_chosen_konsole_default_profile_is_kept(self):
        self._gold_desktop()
        self.write(".local/share/konsole/Shadowfetch.profile",
                   "[Appearance]\nColorScheme=ShadowfetchUmbra\n")
        self.write(".local/share/konsole/Mine.profile", "[Appearance]\nColorScheme=Solarized\n")
        self.write(".config/konsolerc", "[Desktop Entry]\nDefaultProfile=Mine.profile\n")
        self.assertEqual([], self.run_script())

    def test_no_default_profile_is_named_when_the_profile_is_absent(self):
        self._gold_desktop()
        self.assertEqual([], self.run_script())

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


# -- KDE as it behaves on an upgraded 4.1 desktop (5.0.0 VM lane D) -----------

# kwriteconfig6 and plasma-apply-colorscheme that change the files the way the
# Plasma 6.7 tools do, so a test can assert on the resulting kdeglobals rather
# than on argv. plasma-apply-colorscheme resolves the current scheme through
# the cascade (user file, ~/.config/kdedefaults, XDG_CONFIG_DIRS) and, like the
# real one, prints a message and exits 0 WITHOUT applying anything when asked
# for that scheme. Applying writes ColorScheme only when it differs from the
# cascaded default (KConfig omits default values) and bakes [Colors:Selection]
# from the current AccentColor: DecorationFocus is the accent, BackgroundNormal
# 70% of it over the window background -- 154,117,56 from Fire's 216,162,74 and
# 172,129,47 from ShadowCode's 242,179,61, the values the VM showed.
KDE_INI = textwrap.dedent("""\
    import json, os, sys

    def load(path):
        groups, cur = {}, None
        if os.path.isfile(path):
            for line in open(path).read().splitlines():
                if line.startswith("["):
                    cur = groups.setdefault(line, {})
                elif "=" in line and cur is not None:
                    k, v = line.split("=", 1)
                    cur[k] = v
        return groups

    def save(path, groups):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as out:
            for name, keys in groups.items():
                out.write(name + "\\n")
                for k, v in keys.items():
                    out.write(f"{k}={v}\\n")
                out.write("\\n")

    def get(path, group, key):
        return load(path).get(group, {}).get(key, "")

    def record(name):
        with open(os.environ["STUB_LOG"], "a") as log:
            log.write(json.dumps([name, *sys.argv[1:]]) + "\\n")
    """)

KWRITECONFIG = textwrap.dedent("""\
    #!{python}
    import sys
    sys.path.insert(0, {lib!r})
    from kdeini import load, save, record
    record("kwriteconfig6")
    args, groups = sys.argv[1:], []
    path = key = None
    while args:
        flag = args.pop(0)
        if flag == "--file": path = args.pop(0)
        elif flag == "--group": groups.append(args.pop(0))
        elif flag == "--key": key = args.pop(0)
        else: value = flag
    data = load(path)
    data.setdefault("".join(f"[{{g}}]" for g in groups), {{}})[key] = value
    save(path, data)
    """)

APPLY_COLORSCHEME = textwrap.dedent("""\
    #!{python}
    import os, sys
    sys.path.insert(0, {lib!r})
    from kdeini import get, load, save, record
    record("plasma-apply-colorscheme")
    config = os.path.join(os.environ["HOME"], ".config")
    user = os.path.join(config, "kdeglobals")
    cascade = [os.path.join(config, "kdedefaults", "kdeglobals")] + [
        os.path.join(d, "kdeglobals")
        for d in os.environ.get("XDG_CONFIG_DIRS", "/etc/xdg").split(":") if d]
    default = next((v for v in (get(p, "[General]", "ColorScheme") for p in cascade) if v),
                   "BreezeLight")
    current = get(user, "[General]", "ColorScheme") or default
    wanted = sys.argv[1]
    if wanted == current:
        print(f'The requested theme "{{wanted}}" is already set as the theme for the '
              "current Plasma session.")
        sys.exit(0)
    if os.environ.get("STUB_NO_BAKE"):
        print(f"Successfully applied the color scheme {{wanted}}")
        sys.exit(0)
    data = load(user)
    general = data.setdefault("[General]", {{}})
    if wanted == default:
        general.pop("ColorScheme", None)
    else:
        general["ColorScheme"] = wanted
    accent = general.get("AccentColor", "255,201,94")
    rgb = [int(c) for c in accent.split(",")]
    normal = ",".join(str(int(0.7 * c + 0.3 * b)) for c, b in zip(rgb, (10, 13, 17)))
    data["[Colors:Selection]"] = {{"BackgroundNormal": normal,
                                  "DecorationFocus": accent}}
    save(user, data)
    print(f"Successfully applied the color scheme {{wanted}}")
    """)

FIRE_41_SELECTION = "154,117,56"
SHADOWCODE_SELECTION = "172,129,47"
FIRE_41_ACCENT = "216,162,74"


class SelectionRebake(LookMigrate):
    """The accent migration against KDE's real config layout.

    On the VM, 4.1 Fire's user kdeglobals had NO ColorScheme key: KConfig drops
    a value equal to ~/.config/kdedefaults/kdeglobals, which names
    ShadowfetchDark. The first 5.0 script read that as "no scheme", asked
    plasma-apply-colorscheme for ShadowfetchDark without a bounce, the tool saw
    the scheme was already current and did nothing, and the stamp went down
    over a selection still baked from the 4.1 gold.
    """

    def setUp(self):
        super().setUp()
        lib = self.tmp / "lib"
        lib.mkdir()
        (lib / "kdeini.py").write_text(KDE_INI)
        for name, text in (("kwriteconfig6", KWRITECONFIG),
                           ("plasma-apply-colorscheme", APPLY_COLORSCHEME)):
            path = self.bin / name
            path.write_text(text.format(python=sys.executable, lib=str(lib)))
            path.chmod(0o755)
        sleep = self.bin / "sleep"
        sleep.write_text("#!/bin/sh\nexit 0\n")
        sleep.chmod(0o755)

    def kdedefaults(self, where: str = ".config/kdedefaults/kdeglobals"):
        text = "[General]\nColorScheme=ShadowfetchDark\n\n[Icons]\nTheme=Papirus-Dark\n"
        if where.startswith("/"):
            path = Path(where)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        else:
            self.write(where, text)

    def upgraded_fire_desktop(self, accent: str = FIRE_41_ACCENT):
        """The VM's user kdeglobals after the 4.1 -> 5.0 package upgrade."""
        self.kdedefaults()
        self.write(".config/kdeglobals", textwrap.dedent(f"""\
            [Colors:Selection]
            BackgroundAlternate={FIRE_41_SELECTION}
            BackgroundNormal={FIRE_41_SELECTION}
            DecorationFocus={FIRE_41_ACCENT}

            [General]
            AccentColor={accent}
            Name=Shadowfetch Umbra
            """))

    def selection(self) -> tuple[str, str]:
        text = (self.config / "kdeglobals").read_text()
        group = text.split("[Colors:Selection]\n", 1)[1].split("\n\n", 1)[0]
        keys = dict(line.split("=", 1) for line in group.splitlines())
        return keys.get("BackgroundNormal"), keys.get("DecorationFocus")

    def schemes_applied(self, calls) -> list[str]:
        return [c[1] for c in calls if c[0] == "plasma-apply-colorscheme"]

    def test_an_upgraded_fire_desktop_loses_the_41_gold(self):
        self.upgraded_fire_desktop()
        calls = self.run_script()
        # ShadowfetchDark is current through kdedefaults, so it takes a bounce.
        self.assertEqual(["BreezeDark", "ShadowfetchDark"], self.schemes_applied(calls))
        self.assertEqual((SHADOWCODE_SELECTION, GOLD), self.selection())
        self.assertNotIn("ColorScheme=", (self.config / "kdeglobals").read_text(),
                         "the user's scheme is still the kdedefaults one")
        self.assertEqual("shadowcode-2\n", self.stamp.read_text())

    def test_a_default_from_xdg_config_dirs_counts_as_current(self):
        self.upgraded_fire_desktop()
        (self.config / "kdedefaults/kdeglobals").unlink()
        self.kdedefaults(str(self.tmp / "xdg/kdeglobals"))
        calls = self.run_script()
        self.assertEqual(["BreezeDark", "ShadowfetchDark"], self.schemes_applied(calls))
        self.assertEqual((SHADOWCODE_SELECTION, GOLD), self.selection())

    def test_an_early_50_stamp_does_not_hide_a_desktop_still_in_41_gold(self):
        # Lane D exactly: accent already replaced, stamp written, selection stale.
        self.upgraded_fire_desktop(accent=GOLD)
        self.write(".config/shadowfetch/.look-migrated", "shadowcode\n")
        calls = self.run_script()
        self.assertEqual(["BreezeDark", "ShadowfetchDark"], self.schemes_applied(calls))
        self.assertEqual((SHADOWCODE_SELECTION, GOLD), self.selection())
        self.assertEqual("shadowcode-2\n", self.stamp.read_text())
        self.assertEqual([], self.run_script())

    def test_an_early_50_stamp_over_good_colours_is_only_upgraded(self):
        self.kdedefaults()
        self.write(".config/kdeglobals", textwrap.dedent(f"""\
            [Colors:Selection]
            BackgroundNormal={SHADOWCODE_SELECTION}
            DecorationFocus={GOLD}

            [General]
            AccentColor={GOLD}
            """))
        self.write(".config/shadowfetch/.look-migrated", "shadowcode\n")
        self.assertEqual([], self.run_script())
        self.assertEqual("shadowcode-2\n", self.stamp.read_text())

    def test_a_reapply_that_leaves_41_gold_is_retried_a_bounded_number_of_times(self):
        self.upgraded_fire_desktop(accent=GOLD)
        self.extra_env = {"STUB_NO_BAKE": "1"}
        for attempt in (1, 2):
            self.assertIn("ShadowfetchDark", self.schemes_applied(self.run_script()))
            self.assertFalse(self.stamp.exists(), f"stamped over 4.1 gold on try {attempt}")
        self.run_script()
        self.assertEqual("shadowcode-2\n", self.stamp.read_text(),
                         "a desktop KDE will not re-bake is retried forever")
        self.assertEqual([], self.run_script())

    def test_an_interrupted_bounce_is_finished_not_mistaken_for_a_choice(self):
        self.kdedefaults()
        self.write(".config/kdeglobals", textwrap.dedent(f"""\
            [Colors:Selection]
            BackgroundNormal=61,174,233

            [General]
            AccentColor={GOLD}
            ColorScheme=BreezeDark
            """))
        self.write(".config/shadowfetch/.look-migrate-bounce", "ShadowfetchDark\n")
        calls = self.run_script()
        self.assertEqual(["ShadowfetchDark"], self.schemes_applied(calls))
        self.assertFalse((self.config / "shadowfetch/.look-migrate-bounce").exists())
        self.assertEqual((SHADOWCODE_SELECTION, GOLD), self.selection())

    def test_another_scheme_with_the_41_accent_keeps_its_scheme(self):
        self.kdedefaults()
        self.write(".config/kdeglobals", textwrap.dedent(f"""\
            [Colors:Selection]
            DecorationFocus={FIRE_41_ACCENT}

            [General]
            AccentColor={FIRE_41_ACCENT}
            ColorScheme=Nordic
            """))
        calls = self.run_script()
        self.assertEqual(["BreezeDark", "Nordic"], self.schemes_applied(calls))
        self.assertIn("ColorScheme=Nordic", (self.config / "kdeglobals").read_text())
        self.assertEqual(GOLD, self.selection()[1])


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

    def test_skel_konsolerc_selects_the_shipped_profile(self):
        skel = DEFAULTS / "data/etc/skel"
        rc = (skel / ".config/konsolerc").read_text()
        self.assertIn("[Desktop Entry]\nDefaultProfile=Shadowfetch.profile\n", rc)
        self.assertTrue((skel / ".local/share/konsole/Shadowfetch.profile").is_file())
        self.assertIn("data/etc/skel/.config/konsolerc", INSTALL.read_text().split())

    def test_the_script_is_posix_sh(self):
        self.assertTrue(SCRIPT.read_text().startswith("#!/bin/sh\n"))
        subprocess.run(["sh", "-n", str(SCRIPT)], check=True)
        if shutil.which("dash"):
            subprocess.run(["dash", "-n", str(SCRIPT)], check=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
