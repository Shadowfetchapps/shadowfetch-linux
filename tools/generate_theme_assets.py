#!/usr/bin/env python3
"""Generate the desktop colour assets from tools/truth/palette.json (ADR-0009).

Why this exists
---------------
ARCHITECTURE_AUDIT.md:246 counted five hand-maintained copies of the Umbra
palette.  Two of them -- the Plasma colour schemes and the Konsole schemes --
are pure colour data with no layout, no logic and no reason to be typed by
hand.  They are now DERIVED.  Edit tools/truth/palette.json and re-run:

    python3 tools/generate_theme_assets.py --write

`--check` (the default) renders in memory and diffs against the tree, so
tools/drift_gate.py can fail the moment a generated file is hand-edited.

One element
-----------
5.0.0 collapsed the Fire and Ice pair into ONE look, ShadowCode.  The palette
has exactly one entry under "elements" (ELEMENT below) and this generator
refuses any other shape, so a second element cannot creep back in through a
palette edit without the generator, the drift gate and their tests changing
with it.  The file names are unchanged (ShadowfetchDark.colors,
ShadowfetchUmbra.colorscheme) so installed configurations that name them keep
working across the upgrade; only their display names say "ShadowCode".

The defect this caught on its first run (4.1.0)
-----------------------------------------------
The old Ice assets had been produced by applying the Fire->Ice R/B mirror to
EVERY value, semantic ones included, so error text and ANSI red rendered blue.
palette.json still declares semantic roles element-invariant, and this
generator still takes them from "semantic", never from the brand.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
PALETTE = ROOT / "tools/truth/palette.json"
COLOR_SCHEME_DIR = ROOT / "packages/shadowfetch-themes/data/usr/share/color-schemes"
KONSOLE_DIR = ROOT / "packages/shadowfetch-themes/data/usr/share/konsole"


def load_palette(path: Path = PALETTE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


ELEMENT = "shadowcode"


def brand(palette: dict) -> dict:
    """The one element's brand node; any other palette shape is an error."""
    elements = palette["elements"]
    if list(elements) != [ELEMENT]:
        raise ValueError(
            f"palette.json must declare exactly one element, {ELEMENT!r}; "
            f"found {sorted(elements)}")
    return elements[ELEMENT]


def rgb(value: str) -> str:
    """'#F2B33D' -> '242,179,61', the only spelling KDE config files accept."""
    text = value.lstrip("#")
    if len(text) != 6:
        raise ValueError(f"not a #rrggbb colour: {value!r}")
    return "{},{},{}".format(*(int(text[i:i + 2], 16) for i in (0, 2, 4)))


def _roles(palette: dict) -> dict[str, str]:
    """Every colour the desktop surface names, resolved."""
    surface = palette["surfaces"]["desktop"]["roles"]
    look = brand(palette)
    semantic = {k: v for k, v in palette["semantic"].items() if not k.startswith("_")}
    resolved = {}
    resolved.update({k: rgb(v) for k, v in surface.items()})
    resolved.update({k: rgb(v) for k, v in semantic.items()})
    resolved.update({k: rgb(look[k])
                     for k in ("accent", "accent_bright", "accent_deep", "silver")})
    return resolved


# The ten Foreground*/Decoration* lines every Colors:* group except Selection
# repeats verbatim.  One definition, five uses -- the file itself was the
# duplication this generator removes.
def _standard_group(c: dict[str, str]) -> list[str]:
    return [
        f"DecorationFocus={c['accent']}",
        f"DecorationHover={c['accent_bright']}",
        f"ForegroundActive={c['accent']}",
        f"ForegroundInactive={c['mist']}",
        f"ForegroundLink={c['link']}",
        f"ForegroundNegative={c['negative']}",
        f"ForegroundNeutral={c['warning']}",
        f"ForegroundNormal={c['text']}",
        f"ForegroundPositive={c['positive']}",
        f"ForegroundVisited={c['accent_deep']}",
    ]


def render_colors(palette: dict) -> str:
    c = _roles(palette)
    look = brand(palette)
    lines: list[str] = []

    lines += [
        "[ColorEffects:Disabled]",
        f"Color={c['slate']}",
        "ColorAmount=0",
        "ColorEffect=0",
        "ContrastAmount=0.65",
        "ContrastEffect=1",
        "IntensityAmount=0.1",
        "IntensityEffect=2",
        "",
        "[ColorEffects:Inactive]",
        "ChangeSelectionColor=true",
        f"Color={c['steel']}",
        "ColorAmount=0.025",
        "ColorEffect=2",
        "ContrastAmount=0.1",
        "ContrastEffect=2",
        "Enable=false",
        "IntensityAmount=0",
        "IntensityEffect=0",
        "",
        "[Colors:Button]",
        f"BackgroundAlternate={c['steel_raised']}",
        f"BackgroundNormal={c['steel']}",
        *_standard_group(c),
        "",
        # Selection inverts: the accent becomes the background, so every
        # foreground here is read AGAINST the accent, not against the window.
        "[Colors:Selection]",
        f"BackgroundAlternate={c['accent_deep']}",
        f"BackgroundNormal={c['accent']}",
        f"DecorationFocus={c['accent_bright']}",
        f"DecorationHover={c['accent_bright']}",
        f"ForegroundActive={c['ink']}",
        f"ForegroundInactive={c['ink']}",
        f"ForegroundLink={c['ink']}",
        f"ForegroundNegative={c['negative_dim']}",
        f"ForegroundNeutral={c['warning_dim']}",
        f"ForegroundNormal={c['ink']}",
        f"ForegroundPositive={c['positive_dim']}",
        f"ForegroundVisited={c['ink']}",
        "",
        "[Colors:Tooltip]",
        f"BackgroundAlternate={c['ink_raised']}",
        f"BackgroundNormal={c['ink']}",
        *_standard_group(c),
        "",
        "[Colors:View]",
        f"BackgroundAlternate={c['ink_raised']}",
        f"BackgroundNormal={c['ink']}",
        *_standard_group(c),
        "",
        "[Colors:Window]",
        f"BackgroundAlternate={c['steel_raised']}",
        f"BackgroundNormal={c['window']}",
        *_standard_group(c),
        "",
        "[Colors:Header]",
        f"BackgroundAlternate={c['steel_raised']}",
        f"BackgroundNormal={c['header']}",
        *_standard_group(c),
        "",
        "[Colors:Header][Inactive]",
        f"BackgroundAlternate={c['steel']}",
        f"BackgroundNormal={c['window']}",
        "",
        "[General]",
        f"ColorScheme={look['plasma_color_scheme']}",
        f"Name={look['plasma_scheme_name']}",
        "shadeSortColumn=true",
        "",
        "[KDE]",
        "contrast=4",
        "",
        "[WM]",
        f"activeBackground={c['window']}",
        f"activeBlend={c['accent']}",
        f"activeForeground={c['text']}",
        f"inactiveBackground={c['window']}",
        f"inactiveBlend={c['graphite']}",
        f"inactiveForeground={c['mist']}",
    ]
    return "\n".join(lines) + "\n"


def render_konsole(palette: dict) -> str:
    c = _roles(palette)
    look = brand(palette)
    # ANSI slot 3 ("yellow") is deliberately the brand accent, ShadowCode gold.
    # Slots 1/2 (red/green) are semantic and come from "semantic", never brand.
    pairs = [
        ("Background", c["ink"]),
        ("BackgroundIntense", c["abyss"]),
        ("Color0", c["window"]),
        ("Color0Intense", c["slate"]),
        ("Color1", c["negative"]),
        ("Color1Intense", c["negative_intense"]),
        ("Color2", c["positive"]),
        ("Color2Intense", c["positive_intense"]),
        ("Color3", c["accent"]),
        ("Color3Intense", c["accent_bright"]),
        ("Color4", c["ansi_blue"]),
        ("Color4Intense", c["ansi_blue_intense"]),
        ("Color5", c["ansi_magenta"]),
        ("Color5Intense", c["ansi_magenta_intense"]),
        ("Color6", c["link"]),
        ("Color6Intense", c["link_intense"]),
        ("Color7", c["ansi_white"]),
        ("Color7Intense", c["ansi_white_intense"]),
        ("Foreground", c["terminal_foreground"]),
        ("ForegroundIntense", c["accent_bright"]),
    ]
    lines: list[str] = []
    for name, value in pairs:
        lines += [f"[{name}]", f"Color={value}"]
    lines += [
        "[General]",
        "Blur=true",
        "ColorRandomization=false",
        f"Description={look['konsole_description']}",
        "Opacity=0.96",
        "Wallpaper=",
    ]
    return "\n".join(lines) + "\n"


def generated(palette: dict) -> dict[Path, str]:
    """Every file this generator owns, keyed by absolute path."""
    look = brand(palette)
    return {
        COLOR_SCHEME_DIR / f"{look['plasma_color_scheme']}.colors": render_colors(palette),
        KONSOLE_DIR / f"{look['konsole_scheme']}.colorscheme": render_konsole(palette),
    }


def check(palette: dict) -> list[tuple[Path, str]]:
    """[(path, reason)] for every generated file the tree disagrees with."""
    problems = []
    for path, want in generated(palette).items():
        try:
            have = path.read_text(encoding="utf-8")
        except OSError as exc:
            problems.append((path, f"unreadable: {exc}"))
            continue
        if have != want:
            want_lines = want.splitlines()
            have_lines = have.splitlines()
            diffs = [
                f"line {n + 1}: tree {h!r} != generated {w!r}"
                for n, (h, w) in enumerate(zip(have_lines, want_lines))
                if h != w
            ]
            if len(have_lines) != len(want_lines):
                diffs.append(
                    f"length: tree {len(have_lines)} lines, generated {len(want_lines)}")
            problems.append((path, "; ".join(diffs[:6]) or "content differs"))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true",
                      help="write the generated files into the tree")
    mode.add_argument("--check", action="store_true",
                      help="diff the tree against the palette (the default)")
    args = parser.parse_args(argv)
    palette = load_palette()

    if args.write:
        for path, text in generated(palette).items():
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path.relative_to(ROOT)}")
        return 0

    problems = check(palette)
    for path, reason in problems:
        print(f"DRIFT {path.relative_to(ROOT)}: {reason}", file=sys.stderr)
    if problems:
        print("\nRe-run with --write, or fix tools/truth/palette.json.", file=sys.stderr)
        return 1
    print(f"OK: {len(generated(palette))} generated theme assets match the palette")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
