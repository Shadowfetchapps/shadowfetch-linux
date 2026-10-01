#!/usr/bin/env python3
"""Adversarial tests for tools/drift_gate.py and tools/generate_theme_assets.py.

A gate is only worth landing if it FAILS on the thing it claims to catch.  Every
test here plants a divergence a careless edit would really make -- a bumped
version in one file, one wrong hex digit in a signing fingerprint, a hand-edited
generated colour, a new unnamed colour, a look-and-feel that names the other
package, a pkexec call missing its verb -- and asserts the gate reports DRIFT.

Two tests assert the CURRENT tree state (0 DRIFT, and the exact set of BLOCKED
findings).  They are snapshots on purpose: when somebody fixes one of the
blocked duplications, the snapshot test tells them to move it into the enforced
set instead of letting the report quietly shrink.

Run:  python3 -m unittest discover -s tools/tests -p 'test_drift_gate.py' -v
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import drift_gate  # noqa: E402
import generate_theme_assets  # noqa: E402

# Release identity comes from Stage Q's per-release gate data, not from a
# second copy under tools/truth/.
TRUTH = drift_gate.load_truth()
RELEASE_DATA = TRUTH["_data_file"]
POINTER = "tools/truth/release.json"


@contextlib.contextmanager
def sandbox(*rels: str):
    """A temporary ROOT holding copies of exactly these files.

    The gate reads the tree through module-level ROOT constants, so pointing
    those at a copy is what lets a test mutate a shipped file without touching
    the real one.  Any check reading a file that was not copied fails loudly
    rather than silently reading the real tree.
    """
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp)
        for rel in rels:
            target = fake / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / rel, target)
        saved = (drift_gate.ROOT, generate_theme_assets.ROOT,
                 generate_theme_assets.PALETTE,
                 generate_theme_assets.COLOR_SCHEME_DIR,
                 generate_theme_assets.KONSOLE_DIR)
        drift_gate.ROOT = fake
        generate_theme_assets.ROOT = fake
        generate_theme_assets.PALETTE = fake / "tools/truth/palette.json"
        generate_theme_assets.COLOR_SCHEME_DIR = (
            fake / "packages/shadowfetch-themes/data/usr/share/color-schemes")
        generate_theme_assets.KONSOLE_DIR = (
            fake / "packages/shadowfetch-themes/data/usr/share/konsole")
        try:
            yield fake
        finally:
            (drift_gate.ROOT, generate_theme_assets.ROOT,
             generate_theme_assets.PALETTE,
             generate_theme_assets.COLOR_SCHEME_DIR,
             generate_theme_assets.KONSOLE_DIR) = saved


def drifts(findings):
    return [f for f in findings if f.kind == "DRIFT"]


# The version the tree is on, and one that is NOT it. Both are derived: a test
# that plants "the wrong version" by writing 4.0.0 down stops planting anything
# the day the tree ships 4.0.0, and passes for the wrong reason.
LIVE = TRUTH["version"]
_major, _minor, _patch = LIVE.split(".")
WRONG = f"{_major}.{_minor}.{int(_patch) + 1}"


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{path}: expected exactly one {old!r}"
    path.write_text(text.replace(old, new), encoding="utf-8")


PALETTE_REL = "tools/truth/palette.json"
GENERATED = (
    "packages/shadowfetch-themes/data/usr/share/color-schemes/ShadowfetchDark.colors",
    "packages/shadowfetch-themes/data/usr/share/konsole/ShadowfetchUmbra.colorscheme",
)
VERSION_RELS = tuple({rel for rel, _, _ in drift_gate.VERSION_SITES}) + (
    f"qa/{TRUTH['version']}/acceptance.json", RELEASE_DATA, POINTER)
FINGERPRINT_RELS = drift_gate.FINGERPRINT_REQUIRED + (
    f"qa/{TRUTH['version']}/acceptance.json", RELEASE_DATA, POINTER)
LNF_RELS = (drift_gate.LNF_DEFAULTS.format(plugin="org.shadowfetch.dark"),)
SPLASH_REL = drift_gate.SPLASH.format(plugin="org.shadowfetch.dark")
PALETTE_LITERAL_RELS = (
    tuple(drift_gate.SURFACE_OF)
    + (SPLASH_REL, drift_gate.THEME_CONF, PALETTE_REL))
HELPER_RELS = tuple(set(drift_gate.HELPER_CONSUMERS) | set(drift_gate.BUNDLE_CALL_SITES))


class TestTreeIsClean(unittest.TestCase):
    """The state this stage is landing in. Not a claim that nothing is duplicated."""

    def test_no_drift_anywhere(self):
        found = drifts(drift_gate.run())
        self.assertEqual([], found, "\n".join(str(f) for f in found))

    def test_blocked_findings_are_the_recorded_set(self):
        blocked = [f for f in drift_gate.run() if f.kind == "BLOCKED"]
        by_check = {}
        for finding in blocked:
            by_check[finding.check] = by_check.get(finding.check, 0) + 1
        # Snapshot. A DIFFERENT number means a duplication was fixed (move it to
        # the enforced set and update this) or a new one appeared (fix it).
        #
        # desktop-helpers went 1 -> 4 when the greppy barrier was replaced by a
        # structural one. The old 1 was `net`. The other three are things the
        # rewrite could SEE for the first time and that this stage's territory
        # cannot fix: Welcome resolves the shared module by name (sys.modules is
        # consulted before sys.path, so its existence check decides nothing),
        # and workbench_page.py declares /usr/bin/shadowfetch-workbench itself,
        # twice, a program the library's trusted-program table does not carry.
        # Both are reported with exact anchors and replacements.
        #
        # workspace-name went 13 -> 0 and release-pointer 1 -> 0, and both are
        # recorded here rather than merely deleted, because a snapshot that
        # only ever shrinks teaches nothing:
        #
        #   workspace-name: four implementations of the rule disagreed about
        #   hidden directories, backslashes, control characters and length.
        #   They agree now, DECISION FOR DECISION against a shared corpus --
        #   the gate executes each one rather than comparing source text,
        #   because three regexes that look alike still disagreed. And the gate
        #   had been grading its own private copy of the sanitiser it checks,
        #   so a fix to the real one changed nothing it reported; it lifts the
        #   shipped function out of the file and runs that.
        #
        #   release-pointer: the pointer now has a writer as well as a reader.
        #   The finding did not disappear because it was fixed -- it
        #   disappeared because the check fired only on the exact string
        #   "NOT IMPLEMENTED" and the status had moved to something else, so
        #   neither branch could reach it. The status is derived from what the
        #   reader and the writer contain now and compared with what is written
        #   down, which is a check that can see its own staleness.
        #
        # desktop-helpers went 4 -> 3: Welcome's loader was the same
        # sys.modules hole the Control Center's had, and is closed.
        #
        # 5.0.0: element-assets (1) became look-assets (0). With one look there
        # is no second look-and-feel package for nothing to apply. palette
        # gains a second finding, RETIRED_LOOK_LITERALS, for as long as another
        # owner's file still spells a Fire/Ice colour; it is counted from the
        # tree because that conversion is landing in parallel.
        lingering = any(
            colour in (ROOT / rel).read_text(encoding="utf-8").lower()
            for rel, colours in drift_gate.RETIRED_LOOK_LITERALS.items()
            for colour in colours)
        expected = {"palette": 2 if lingering else 1, "desktop-helpers": 3}
        self.assertEqual(
            expected,
            by_check,
            "the blocked-duplication inventory changed:\n"
            + "\n".join(str(f) for f in blocked))

    def test_generator_reproduces_the_shipped_assets(self):
        palette = generate_theme_assets.load_palette()
        self.assertEqual([], generate_theme_assets.check(palette))


class TestVersionDrift(unittest.TestCase):
    def test_a_single_bumped_copy_is_caught(self):
        rel = ("packages/shadowfetch-branding/data/usr/share/shadowfetch/"
               "os-release.shadowfetch")
        with sandbox(*VERSION_RELS) as fake:
            edit(fake / rel, f'VERSION_ID="{LIVE}"', f'VERSION_ID="{WRONG}"')
            found = drifts(drift_gate.check_version(TRUTH))
        self.assertTrue(found)
        self.assertIn(f"os-release VERSION_ID is '{WRONG}'", found[0].detail)

    def test_a_deleted_assignment_is_caught(self):
        """A copy that stops existing is drift too -- silence is not agreement."""
        with sandbox(*VERSION_RELS) as fake:
            target = fake / ("packages/shadowfetch-defaults/data/usr/bin/"
                             "shadowfetch-agent-network")
            edit(target, f'VERSION="{LIVE}"',
                 'VERSION=$(cat /usr/share/shadowfetch/version)')
            found = drifts(drift_gate.check_version(TRUTH))
        self.assertTrue(any("could not be located" in f.detail for f in found))

    def test_acceptance_manifest_release_block_is_checked(self):
        with sandbox(*VERSION_RELS) as fake:
            manifest = fake / f"qa/{TRUTH['version']}/acceptance.json"
            # Derived, like LIVE/WRONG: 5.0.0 renamed the edition, and a
            # written-down "Fire and Ice" stopped matching anything.
            edit(manifest, f'"edition": "{TRUTH["edition"]}"',
                 f'"edition": "{TRUTH["edition"]} (stale)"')
            found = drifts(drift_gate.check_version(TRUTH))
        self.assertTrue(any("release.edition" in f.detail for f in found))

    def test_clean_copy_passes(self):
        with sandbox(*VERSION_RELS):
            self.assertEqual([], drifts(drift_gate.check_version(TRUTH)))


class TestFingerprintDrift(unittest.TestCase):
    """The signing key is a security fact; a retyped copy is a real exposure."""

    REAL = TRUTH["signing"]["fingerprint"]

    def test_one_wrong_hex_digit_is_caught(self):
        with sandbox(*FINGERPRINT_RELS) as fake:
            wrong = self.REAL[:-1] + ("2" if self.REAL[-1] != "2" else "3")
            edit(fake / "Makefile", self.REAL, wrong)
            found = drifts(drift_gate.check_fingerprint(TRUTH))
        self.assertTrue(found)
        self.assertTrue(any("Makefile" in f.site for f in found))

    def test_the_sweep_is_not_vacuous(self):
        """A wrong key in ANY swept file is caught, not just the listed ones."""
        with sandbox(*FINGERPRINT_RELS) as fake:
            planted = fake / "packages/anything/data/usr/bin/some-tool"
            planted.parent.mkdir(parents=True, exist_ok=True)
            fake_key = ("0123456789AB" * 4)[:40]
            planted.write_text(f'GPG_FINGERPRINT = "{fake_key}"\n', encoding="utf-8")
            found = drifts(drift_gate.check_fingerprint(TRUTH))
        self.assertTrue(any("some-tool" in f.site for f in found))

    def test_the_gpg_spaced_grouping_is_not_mistaken_for_a_different_key(self):
        """README and SECURITY.md group the hex in fours. Same key, not drift."""
        with sandbox(*FINGERPRINT_RELS):
            self.assertIn("8F13 CE15", (ROOT / "SECURITY.md").read_text())
            self.assertEqual([], drifts(drift_gate.check_fingerprint(TRUTH)))

    def test_a_required_file_that_stops_naming_the_key_is_caught(self):
        with sandbox(*FINGERPRINT_RELS) as fake:
            edit(fake / "repo/conf/distributions", self.REAL, "")
            found = drifts(drift_gate.check_fingerprint(TRUTH))
        self.assertTrue(any("no 40-hex fingerprint found" in f.detail for f in found))

    def test_a_commit_sha_is_not_mistaken_for_a_key(self):
        """A recorded case carries a 40-hex Git SHA beside the signing key.

        The SHA is PLANTED rather than read out of the live manifest. A
        manifest for a release with no candidate yet carries no recording at
        all, so reading one made this test assert nothing on exactly the days
        it mattered most -- and this property has to hold on the day the first
        recording lands, not only afterwards."""
        with sandbox(*FINGERPRINT_RELS) as fake:
            manifest = fake / f"qa/{TRUTH['version']}/acceptance.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["cases"][0]["evidence"] = [{
                "path": "src/tests.log",
                "source_commit": "b" * 40,
                "artifact_sha256": "c" * 64,
            }]
            manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            self.assertRegex(manifest.read_text(encoding="utf-8"),
                             r'"source_commit":\s*"[0-9a-f]{40}"')
            self.assertEqual([], drifts(drift_gate.check_fingerprint(TRUTH)))

    def test_a_prose_commit_sha_beside_a_fingerprint_is_not_a_key(self):
        """sf41-ia-readme.txt names the signing key, then two git SHAs.

        The SHAs sit inside the three-line fingerprint window, so a sweep that
        only looks at nearby labels would treat them as a second key."""
        with sandbox(*FINGERPRINT_RELS) as fake:
            planted = fake / "sf41-ia-readme.txt"
            planted.write_text(
                "OpenPGP fingerprint: {fp}\n"
                "Source commit (ISO): {sha1}\n"
                "Release-tooling commit: {sha2}\n".format(
                    fp=TRUTH["signing"]["fingerprint"],
                    sha1="78ee38ceac0ff989d596e5a5e0b97aac17c3b936",
                    sha2="aa8fd1b22e8e3c8a098a28e43fd6b156fbb74b56"),
                encoding="utf-8")
            self.assertEqual([], drifts(drift_gate.check_fingerprint(TRUTH)))

    def test_a_third_party_key_must_be_named(self):
        with sandbox(*FINGERPRINT_RELS) as fake:
            planted = fake / "packages/anything/vendor/provenance.json"
            planted.parent.mkdir(parents=True, exist_ok=True)
            other_key = ("ABCDEF9876" * 4)[:40]
            planted.write_text(
                '{"upstream_signing_fingerprint": "%s"}\n' % other_key,
                encoding="utf-8")
            found = drifts(drift_gate.check_fingerprint(TRUTH))
        self.assertTrue(found)
        self.assertIn("OTHER_KEYS", found[0].remedy)

    def test_the_named_third_party_keys_are_accepted(self):
        for value, why in drift_gate.OTHER_KEYS.items():
            self.assertEqual(40, len(value), value)
            self.assertTrue(why.strip(), f"{value} has no named owner")


class TestReleaseData(unittest.TestCase):
    """The release data file is the authority; the Makefile is a copy."""

    def test_a_makefile_that_disagrees_with_the_release_data_is_caught(self):
        with sandbox("Makefile", f"qa/{TRUTH['version']}/acceptance.json",
                     RELEASE_DATA, POINTER) as fake:
            edit(fake / "Makefile", f"VERSION  ?= {LIVE}", f"VERSION  ?= {WRONG}")
            found = drifts(drift_gate.check_release_data(TRUTH))
        self.assertTrue(found)
        self.assertIn(f"Makefile VERSION is '{WRONG}'", found[0].detail)

    def test_an_apt_only_update_names_its_base_image(self):
        """5.0.1 ships no ISO: its manifest describes the 5.0.0 image."""
        def doc(version, delivery=None, base=None):
            release = {"version": version}
            if delivery:
                release["delivery"] = delivery
            document = {"release": release}
            if base:
                document["apt_only"] = {"base_release": base}
            return document

        docs = {"4.1.0": doc("4.1.0"), "5.0.0": doc("5.0.0"),
                "5.0.1": doc("5.0.1", "apt-only"),
                "5.0.2": doc("5.0.2", "apt-only")}
        self.assertEqual("5.0.0", drift_gate.artifact_iso_version(docs["5.0.0"], docs))
        self.assertEqual("5.0.0", drift_gate.artifact_iso_version(docs["5.0.1"], docs))
        # An earlier apt-only update is never a base image.
        self.assertEqual("5.0.0", drift_gate.artifact_iso_version(docs["5.0.2"], docs))
        named = doc("5.0.1", "apt-only", base="4.1.0")
        self.assertEqual("4.1.0", drift_gate.artifact_iso_version(named, docs))
        with self.assertRaises(RuntimeError):
            drift_gate.artifact_iso_version(docs["5.0.1"], {"5.0.1": docs["5.0.1"]})

    def test_exactly_one_release_is_live(self):
        """Two non-historical data files is an ambiguity, not a default."""
        truth = drift_gate.load_truth()
        self.assertEqual(TRUTH["version"], truth["version"])
        self.assertTrue(truth["_data_file"].startswith("tools/release/versions/"))

    def test_clean_copy_passes(self):
        with sandbox("Makefile", f"qa/{TRUTH['version']}/acceptance.json",
                     RELEASE_DATA, POINTER):
            self.assertEqual([], drifts(drift_gate.check_release_data(TRUTH)))


class TestGeneratedThemeAssets(unittest.TestCase):
    def test_a_hand_edited_generated_file_is_caught(self):
        with sandbox(PALETTE_REL, *GENERATED) as fake:
            target = fake / GENERATED[0]
            edit(target, "inactiveForeground=154,163,173", "inactiveForeground=0,0,0")
            found = drifts(drift_gate.check_theme_assets(TRUTH))
        self.assertTrue(found)
        self.assertIn("inactiveForeground=0,0,0", found[0].detail)

    def test_semantic_colours_come_from_semantic_not_brand(self):
        """The defect this generator was written to end (4.1.0).

        The old Ice assets had been produced by R/B-mirroring every value, so
        ANSI red rendered blue in Konsole and Plasma error text rendered blue.
        Semantic roles are read from "semantic", never from the brand.
        """
        palette = generate_theme_assets.load_palette()
        konsole = generate_theme_assets.render_konsole(palette)
        colors = generate_theme_assets.render_colors(palette)
        red = generate_theme_assets.rgb(palette["semantic"]["negative"])
        warning = generate_theme_assets.rgb(palette["semantic"]["warning"])
        self.assertIn(f"[Color1]\nColor={red}", konsole)
        self.assertIn(f"ForegroundNegative={red}", colors)
        self.assertIn(f"ForegroundNeutral={warning}", colors)
        gold = generate_theme_assets.rgb(palette["elements"]["shadowcode"]["accent"])
        self.assertIn(f"[Color3]\nColor={gold}", konsole)

    def test_there_is_exactly_one_look(self):
        palette = generate_theme_assets.load_palette()
        self.assertEqual(["shadowcode"], list(palette["elements"]))
        self.assertEqual(2, len(generate_theme_assets.generated(palette)))
        for surface in ("app-chrome", "document"):
            self.assertEqual(["shadowcode"],
                             list(palette["surfaces"][surface]["element_roles"]))

    def test_a_second_element_is_refused(self):
        palette = generate_theme_assets.load_palette()
        palette["elements"]["ice"] = dict(palette["elements"]["shadowcode"])
        with self.assertRaises(ValueError):
            generate_theme_assets.generated(palette)

    def test_display_names_say_shadowcode_and_ids_do_not_move(self):
        palette = generate_theme_assets.load_palette()
        look = palette["elements"]["shadowcode"]
        # The ids are what 4.1 configurations name; renaming them strands them.
        self.assertEqual("ShadowfetchDark", look["plasma_color_scheme"])
        self.assertEqual("ShadowfetchUmbra", look["konsole_scheme"])
        self.assertEqual("org.shadowfetch.dark", look["look_and_feel"])
        colors = generate_theme_assets.render_colors(palette)
        self.assertIn("\nName=ShadowCode\n", colors)
        self.assertIn("Description=ShadowCode\n",
                      generate_theme_assets.render_konsole(palette))

    def test_contrast_meets_the_recorded_floor(self):
        """Body text >= 7:1 and the accent >= 4.5:1 on every surface's ground."""
        def lum(hex_):
            chans = [int(hex_.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
            lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
                   for c in chans]
            return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

        def ratio(a, b):
            hi, lo = sorted((lum(a), lum(b)), reverse=True)
            return (hi + 0.05) / (lo + 0.05)

        palette = generate_theme_assets.load_palette()
        accent = palette["elements"]["shadowcode"]["accent"]
        desk = palette["surfaces"]["desktop"]["roles"]
        chrome = palette["surfaces"]["app-chrome"]
        doc = palette["surfaces"]["document"]
        pairs = [
            ("desktop text/window", desk["text"], desk["window"], 7),
            ("desktop text/ink", desk["text"], desk["ink"], 7),
            ("accent/window", accent, desk["window"], 4.5),
            ("accent/ink", accent, desk["ink"], 4.5),
            ("ink on accent (selection)", desk["ink"], accent, 4.5),
            ("chrome text/bg", chrome["roles"]["text"], chrome["roles"]["bg"], 7),
            ("chrome gold/bg", chrome["element_roles"]["shadowcode"]["gold"],
             chrome["roles"]["bg"], 4.5),
            ("document text/bg", doc["roles"]["text"],
             doc["element_roles"]["shadowcode"]["bg"], 7),
            ("document gold/bg", doc["element_roles"]["shadowcode"]["gold"],
             doc["element_roles"]["shadowcode"]["bg"], 4.5),
            ("warning/window", palette["semantic"]["warning"], desk["window"], 4.5),
        ]
        for name, fg, bg, floor in pairs:
            self.assertGreaterEqual(ratio(fg, bg), floor, name)

    def test_warning_is_not_the_accent(self):
        """A warning must not read as brand gold.

        CIE76 distance in Lab. 4.1's Fire gold and amber warning sat 8.6 apart
        and were hard to tell apart; ShadowCode gold and the 5.0 warning sit
        17.2 apart. The floor is between the two.
        """
        import math

        def lab(hex_):
            chans = [int(hex_.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
            r, g, b = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
                       for c in chans]
            x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
            y = 0.2126 * r + 0.7152 * g + 0.0722 * b
            z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883
            f = [v ** (1 / 3) if v > 0.008856 else 7.787 * v + 16 / 116 for v in (x, y, z)]
            return 116 * f[1] - 16, 500 * (f[0] - f[1]), 200 * (f[1] - f[2])

        palette = generate_theme_assets.load_palette()
        distance = math.dist(lab(palette["semantic"]["warning"]),
                             lab(palette["elements"]["shadowcode"]["accent"]))
        self.assertGreaterEqual(distance, 12)

    def test_write_then_check_is_a_fixed_point(self):
        with sandbox(PALETTE_REL, *GENERATED):
            palette = generate_theme_assets.load_palette()
            for path, text in generate_theme_assets.generated(palette).items():
                path.write_text(text, encoding="utf-8")
            self.assertEqual([], generate_theme_assets.check(palette))


class TestPaletteLiterals(unittest.TestCase):
    # These used to plant their stray colour in app.py. app.py no longer holds
    # one: the sidebar colour it spelled out is theme.SIDEBAR now, and its
    # ratchet entry went with it -- which is the tightening this check exists
    # to reward. theme.py is where the remaining unnamed colours live, so it is
    # where a stray one has to be planted for the check to have anything to
    # find.
    THEME = ("packages/shadowfetch-control-center/data/usr/share/shadowfetch/"
             "control-center/sfcc/theme.py")

    def test_a_new_unnamed_colour_is_caught(self):
        with sandbox(*PALETTE_LITERAL_RELS) as fake:
            edit(fake / self.THEME, 'SIDEBAR = "#101114"',
                 'SIDEBAR = "#123456"')
            found = drifts(drift_gate.check_palette_literals(TRUTH))
        self.assertTrue(found)
        self.assertIn("#123456", found[0].detail)

    def test_a_slack_ratchet_entry_is_caught(self):
        """Removing a stray colour must also remove its ratchet entry."""
        with sandbox(*PALETTE_LITERAL_RELS) as fake:
            text = (fake / self.THEME).read_text(encoding="utf-8")
            # Every occurrence: one left behind would keep the entry earned.
            (fake / self.THEME).write_text(
                text.replace("#101114", "#151619"), encoding="utf-8")
            found = drifts(drift_gate.check_palette_literals(TRUTH))
        self.assertTrue(any("ratchet has gone slack" in f.detail for f in found))

    def test_a_third_red_in_the_fireproof_window_is_caught(self):
        """The audit's named defect: a window inside another window, own red."""
        rel = "packages/shadowfetch-fireproof/data/usr/bin/shadowfetch-fireproof"
        with sandbox(*PALETTE_LITERAL_RELS) as fake:
            edit(fake / rel, 'RED = "#e2533b"', 'RED = "#e07a6a"')
            found = drifts(drift_gate.check_palette_literals(TRUTH))
        self.assertTrue(found)
        self.assertIn("#e07a6a", found[0].detail)

    def test_a_splash_that_keeps_the_retired_gold_is_caught(self):
        with sandbox(*PALETTE_LITERAL_RELS) as fake:
            (fake / SPLASH_REL).write_text(
                (fake / SPLASH_REL).read_text(encoding="utf-8").replace("#f2b33d", "#d8a24a"),
                encoding="utf-8")
            found = drifts(drift_gate.check_palette_literals(TRUTH))
        self.assertTrue(any("#d8a24a" in f.detail for f in found))

    def test_an_sddm_theme_on_the_retired_gold_is_caught(self):
        with sandbox(*PALETTE_LITERAL_RELS) as fake:
            edit(fake / drift_gate.THEME_CONF, "color=#F2B33D", "color=#D8A24A")
            found = drifts(drift_gate.check_palette_literals(TRUTH))
        self.assertTrue(any("SDDM accent" in f.detail for f in found))

    def test_a_retired_literal_outside_its_listed_file_is_a_stray(self):
        """RETIRED_LOOK_LITERALS excuses the files it names, nobody else."""
        with sandbox(*PALETTE_LITERAL_RELS) as fake:
            edit(fake / self.THEME, 'GOLD = "#f2b33d"', 'GOLD = "#4aa2d8"')
            found = drifts(drift_gate.check_palette_literals(TRUTH))
        self.assertTrue(any("#4aa2d8" in f.detail for f in found))


class TestLookAndFeelIdentity(unittest.TestCase):
    DARK = LNF_RELS[0]

    def test_a_package_naming_another_package_is_caught(self):
        """The 4.1 regression: a package declaring itself as a different one."""
        with sandbox(*LNF_RELS, PALETTE_REL) as fake:
            edit(fake / self.DARK, "LookAndFeelPackage=org.shadowfetch.dark",
                 "LookAndFeelPackage=org.shadowfetch.ice")
            found = drifts(drift_gate.check_lookandfeel(TRUTH))
        self.assertTrue(found)
        self.assertIn("LookAndFeelPackage", found[0].detail)

    def test_a_wrong_accent_colour_is_caught(self):
        with sandbox(*LNF_RELS, PALETTE_REL) as fake:
            edit(fake / self.DARK, "AccentColor=242,179,61", "AccentColor=216,162,74")
            found = drifts(drift_gate.check_lookandfeel(TRUTH))
        self.assertTrue(any("AccentColor" in f.detail for f in found))

    def test_the_retired_wallpaper_is_caught(self):
        with sandbox(*LNF_RELS, PALETTE_REL) as fake:
            edit(fake / self.DARK, "shadowcode-4k.jpg", "umbra-4k.jpg")
            found = drifts(drift_gate.check_lookandfeel(TRUTH))
        self.assertTrue(any("Wallpaper" in f.detail for f in found))

    def test_clean_copy_passes(self):
        with sandbox(*LNF_RELS, PALETTE_REL):
            self.assertEqual([], drifts(drift_gate.check_lookandfeel(TRUTH)))


class TestDesktopHelpers(unittest.TestCase):
    """The privileged argv is built in ONE place and delegated everywhere else.

    Both tests that used to live here planted a string W-30 had already
    removed from the tree -- an argv literal that now exists only in the
    library, and a path Welcome no longer contains -- so neither of them had
    failed on anything for some time. `edit()` asserts its anchor matches once,
    which is what finally said so.
    """

    SOFTWARE = ("packages/shadowfetch-control-center/data/usr/share/shadowfetch/"
                "control-center/sfcc/software_page.py")
    LIBRARY = drift_gate.DESKTOP_LIBRARY
    WELCOME = "packages/shadowfetch-welcome/src/shadowfetch-welcome"

    def test_a_bundle_install_argv_missing_its_verb_is_caught(self):
        """UI-ARGV-01: seven Install buttons exited 2 after the password prompt.
        The literal lives in exactly one place now, so this is where it goes."""
        with sandbox(*HELPER_RELS) as fake:
            edit(fake / self.LIBRARY,
                 'return [pkexec, helper, "install", bundle_id]',
                 'return [pkexec, helper, bundle_id]')
            found = drifts(drift_gate.check_desktop_helpers(TRUTH))
        self.assertTrue(any('without the "install" verb' in f.detail
                            for f in found), found)

    def test_a_verb_laundered_through_a_variable_is_still_caught(self):
        """The property that HELD when this check was attacked, kept when the
        regex behind it was replaced by an AST read: a verb the gate cannot
        read as the literal "install" is reported, not assumed."""
        with sandbox(*HELPER_RELS) as fake:
            edit(fake / self.LIBRARY,
                 'return [pkexec, helper, "install", bundle_id]',
                 'verb = "install"\n    return [pkexec, helper, verb, bundle_id]')
            found = drifts(drift_gate.check_desktop_helpers(TRUTH))
        self.assertTrue(any('without the "install" verb' in f.detail
                            for f in found), found)

    def test_a_comment_cannot_satisfy_the_pkexec_resolution_check(self):
        """Measured, not theorised. The check read the builder's RAW source, so
        `# trusted_program("pkexec")` in a comment satisfied it while the code
        called shutil.which("pkexec"): that plant reported 0 DRIFT findings."""
        with sandbox(*HELPER_RELS) as fake:
            edit(fake / self.LIBRARY,
                 '    pkexec = trusted_program("pkexec")\n'
                 '    helper = trusted_program("shadowfetch-bundle-install")',
                 '    # trusted_program("pkexec") -- resolved below\n'
                 '    pkexec = shutil.which("pkexec")\n'
                 '    helper = trusted_program("shadowfetch-bundle-install")')
            found = drifts(drift_gate.check_desktop_helpers(TRUTH))
        self.assertTrue(any("trusted program table" in f.detail for f in found),
                        found)

    def test_a_front_end_spelling_a_helper_path_again_is_caught(self):
        with sandbox(*HELPER_RELS) as fake:
            edit(fake / self.WELCOME,
                 'STATE_HELPER = "/usr/libexec/shadowfetch-ignition-state"',
                 'STATE_HELPER = "/usr/libexec/shadowfetch-ignition-state"\n'
                 'BUNDLE_HELPER = "/usr/libexec/shadowfetch-bundle-install"')
            found = drifts(drift_gate.check_desktop_helpers(TRUTH))
        self.assertTrue(any("bundle-install path" in f.detail for f in found),
                        found)

    def test_a_page_that_assembles_the_privileged_argv_again_is_caught(self):
        """The Install pages delegate. A page that builds the argv itself is
        the duplication W-30 removed, whether or not it happens to agree."""
        with sandbox(*HELPER_RELS) as fake:
            page = fake / self.SOFTWARE
            page.write_text(
                page.read_text(encoding="utf-8")
                + '\n\ndef _install_argv(bundle_id):\n'
                  '    return ["pkexec", "/usr/libexec/" "shadowfetch-bundle-install",\n'
                  '            "install", bundle_id]\n',
                encoding="utf-8")
            found = drifts(drift_gate.check_desktop_helpers(TRUTH))
        details = "\n".join(f.detail for f in found)
        self.assertIn("privileged argv", details)
        self.assertIn("assembles the bundle-install path", details)

    def test_clean_copy_passes(self):
        with sandbox(*HELPER_RELS):
            self.assertEqual([], drifts(drift_gate.check_desktop_helpers(TRUTH)))


class TestLookAssets(unittest.TestCase):
    RELS = (*drift_gate.LOOK_APPLIERS, PALETTE_REL)

    def test_clean_copy_passes(self):
        with sandbox(*self.RELS):
            self.assertEqual([], drift_gate.check_look_assets(TRUTH))

    def test_an_applier_that_forgets_an_asset_is_caught(self):
        with sandbox(*self.RELS) as fake:
            edit(fake / drift_gate.FIRST_LOGIN,
                 "plasma-apply-lookandfeel -a org.shadowfetch.dark || true",
                 "plasma-apply-lookandfeel -a org.kde.breezedark.desktop || true")
            found = drifts(drift_gate.check_look_assets(TRUTH))
        self.assertTrue(any("org.shadowfetch.dark" in f.detail for f in found), found)

    def test_a_retired_asset_back_in_the_tree_is_caught(self):
        with sandbox(*self.RELS) as fake:
            back = fake / ("packages/shadowfetch-themes/data/usr/share/"
                           "color-schemes/ShadowfetchIce.colors")
            back.parent.mkdir(parents=True, exist_ok=True)
            back.write_text("[General]\nName=Shadowfetch Ice\n")
            found = drifts(drift_gate.check_look_assets(TRUTH))
        self.assertTrue(any("retired Fire/Ice asset is back" in f.detail for f in found))
        self.assertTrue(any("ShadowfetchIce.colors" in f.site for f in found))

    def test_an_install_line_for_a_retired_asset_is_caught(self):
        rel = "packages/shadowfetch-branding/debian/shadowfetch-branding.install"
        with sandbox(*self.RELS, rel) as fake:
            with (fake / rel).open("a", encoding="utf-8") as fh:
                fh.write("data/usr/share/wallpapers/UmbraIce     usr/share/wallpapers/\n")
            found = drifts(drift_gate.check_look_assets(TRUTH))
        self.assertTrue(any("installs a retired" in f.detail for f in found), found)

    def test_a_look_file_naming_a_retired_asset_is_drift(self):
        with sandbox(*self.RELS) as fake:
            edit(fake / drift_gate.SKEL_LOCKRC, "shadowcode-4k.jpg", "umbra-ice-4k.jpg")
            found = drifts(drift_gate.check_look_assets(TRUTH))
        self.assertTrue(any(drift_gate.SKEL_LOCKRC in f.site for f in found), found)

    def test_another_owners_file_is_reported_not_failed(self):
        rel = "packages/shadowfetch-defaults/data/usr/bin/shadowfetch-example"
        with sandbox(*self.RELS) as fake:
            (fake / rel).parent.mkdir(parents=True, exist_ok=True)
            (fake / rel).write_text('wall="/usr/share/wallpapers/UmbraFire"\n')
            findings = drift_gate.check_look_assets(TRUTH)
        self.assertEqual([], drifts(findings))
        (blocked,) = [f for f in findings if f.kind == "BLOCKED"]
        self.assertIn(rel, blocked.detail)

    def test_the_migration_script_may_name_what_it_migrates(self):
        with sandbox(*self.RELS):
            self.assertIn("UmbraFire", (ROOT / drift_gate.LOOK_MIGRATE).read_text())
            self.assertEqual([], drift_gate.check_look_assets(TRUTH))


class TestWorkspaceNameRule(unittest.TestCase):
    def test_the_corpus_matches_the_rule_it_claims_to_encode(self):
        for name, valid, why in drift_gate.WORKSPACE_CORPUS:
            self.assertEqual(valid, drift_gate._rule_accepts(name),
                             f"{name!r} ({why})")

    def test_the_rule_is_the_firebreak_rule(self):
        """Firebreak is the authority: it decides what a sandbox may write to.

        If this fails, either Firebreak changed or the rule was weakened -- and
        a weakened rule here would silently widen every other implementation.
        """
        verdicts = drift_gate._firebreak_verdicts()
        for name, valid, why in drift_gate.WORKSPACE_CORPUS:
            if name == "":
                continue  # Firebreak reads "" as "derive from cwd", not a name
            self.assertEqual(valid, verdicts[name], f"{name!r} ({why})")

    def test_a_divergent_implementation_is_detected(self):
        """Plant an implementation that accepts a hidden name; expect a finding."""
        def permissive():
            return {name: True for name, _, _ in drift_gate.WORKSPACE_CORPUS}

        saved = drift_gate.IMPLEMENTATIONS
        drift_gate.IMPLEMENTATIONS = (("planted", permissive, "planted/module.py"),)
        try:
            findings = drift_gate.check_workspace_name(TRUTH)
        finally:
            drift_gate.IMPLEMENTATIONS = saved
        planted = [f for f in findings if f.site.startswith("planted/")]
        self.assertTrue(planted)
        self.assertTrue(any("'.ssh'" in f.detail for f in planted))
        self.assertTrue(any("'a/b'" in f.detail for f in planted))

    def test_an_unrunnable_implementation_is_drift_not_silence(self):
        def broken():
            raise ImportError("no module named anything")

        saved = drift_gate.IMPLEMENTATIONS
        drift_gate.IMPLEMENTATIONS = (("broken", broken, "broken/module.py"),)
        try:
            findings = drift_gate.check_workspace_name(TRUTH)
        finally:
            drift_gate.IMPLEMENTATIONS = saved
        self.assertTrue(any(f.kind == "DRIFT" and "could not be exercised" in f.detail
                            for f in findings))


if __name__ == "__main__":
    unittest.main(verbosity=2)
