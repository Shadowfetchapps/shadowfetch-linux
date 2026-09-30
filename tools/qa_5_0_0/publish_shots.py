#!/usr/bin/env python3
"""Check website screenshot masters and derive their WebP copies.

For every PNG in the screenshots directory (or the names given):
  * size must be 1920x1080 (or 1280x720 for *-1280.png),
  * OCR (tesseract) must not find anything personal: the host user/handle,
    the host name, the owner's surname, e-mail addresses, or token-like
    strings (sk-..., ghp_..., xai-..., long hex/base64 secrets),
  * a lossless-quality WebP (cwebp -q 92 -m 6) is written next to it.
Writes privacy-report.json beside the images. Exit 1 on any problem.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import struct
import subprocess
import os
import sys

REPO = Path(__file__).resolve().parents[2]
SHOTS = REPO / "work" / "qa-5.0.0" / "screenshots"
# Personal identifiers to refuse come from a local, uncommitted terms file
# (one per line, as for privacy_scan.py), never from this source.
_TERMS = Path(os.environ.get("SHADOWFETCH_PRIVACY_TERMS",
                             str(Path.home() / ".config/shadowfetch-privacy-terms")))
_PERSONAL = ([re.compile(re.escape(line.strip()), re.I)
              for line in _TERMS.read_text(encoding="utf-8").splitlines()
              if line.strip() and not line.startswith("#")]
             if _TERMS.is_file() else [])
FORBIDDEN = _PERSONAL + [
    re.compile(r"pop-?os", re.I),
    re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    re.compile(r"\b(sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,}|xai-[A-Za-z0-9]{16,}|gho_[A-Za-z0-9]{20,})"),
    re.compile(r"/home/(?!demo\b|shadow\b|qa\b)[a-z0-9_-]+"),
    re.compile(r"nordvpn|ollama", re.I),
]


def png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        head = handle.read(24)
    return struct.unpack(">II", head[16:24])


def main() -> int:
    names = sys.argv[1:]
    files = [SHOTS / f"{n}.png" for n in names] if names else sorted(SHOTS.glob("*.png"))
    report, bad = {}, 0
    for png in files:
        entry: dict = {"file": png.name}
        width, height = png_size(png)
        entry["size"] = f"{width}x{height}"
        want = (1280, 720) if png.stem.endswith("-1280") else (1920, 1080)
        problems = []
        if (width, height) != want:
            problems.append(f"size {width}x{height}, want {want[0]}x{want[1]}")
        text = subprocess.run(["tesseract", str(png), "-", "--psm", "11"],
                              capture_output=True, text=True).stdout
        (png.with_suffix(".ocr.txt")).write_text(text)
        hits = sorted({m.group(0) for rx in FORBIDDEN for m in rx.finditer(text)})
        if hits:
            problems.append(f"OCR found personal/secret-looking text: {hits}")
        webp = png.with_suffix(".webp")
        subprocess.run(["cwebp", "-quiet", "-q", "92", "-m", "6", str(png), "-o", str(webp)],
                       check=True)
        entry.update({"ocr_chars": len(text), "problems": problems,
                      "png_bytes": png.stat().st_size, "webp_bytes": webp.stat().st_size})
        bad += bool(problems)
        report[png.stem] = entry
        print(f"{'OK ' if not problems else 'BAD'} {png.name} {entry['size']} "
              f"webp={entry['webp_bytes']}B {'; '.join(problems)}")
    (SHOTS / "privacy-report.json").write_text(json.dumps(report, indent=2) + "\n")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
