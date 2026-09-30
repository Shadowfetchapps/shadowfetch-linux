"""Shipped Python must not reference undefined names (5.0.0 QA).

Two NameErrors reached release candidates because nothing exercised the code
path: guide_page.py used busutil without importing it (Mission Control aborted
on Guide), and Welcome's failure page used a bare PHOENIX_RESTORE. ruff's
pyflakes rules find both statically. Skipped when ruff is not installed.
"""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RULES = "F821,F822,F823"


def shipped_python() -> list[str]:
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "packages", "live-build/config"],
        check=True, capture_output=True, text=True).stdout.splitlines()
    files = []
    for name in tracked:
        if "/tests/" in name:
            continue
        path = ROOT / name
        if name.endswith(".py"):
            files.append(name)
        elif path.is_file() and not path.is_symlink():
            with path.open("rb") as handle:
                first = handle.readline(128)
            if first.startswith(b"#!") and b"python" in first:
                files.append(name)
    return files


@unittest.skipUnless(shutil.which("ruff"), "ruff is not installed")
class ShippedPythonNames(unittest.TestCase):
    def test_no_undefined_names(self):
        files = shipped_python()
        self.assertGreater(len(files), 50)
        result = subprocess.run(
            ["ruff", "check", "--no-cache", "--isolated", "--select", RULES,
             "--output-format", "concise", *files],
            cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
