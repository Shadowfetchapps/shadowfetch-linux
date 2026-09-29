#!/usr/bin/env python3
"""The WebKit software-rendering environment generator (5.0.0 QA blocker).

ShadowCode (WebKitGTK) burned 122-174% CPU idle on VMs without a DRM render
node; WEBKIT_DISABLE_DMABUF_RENDERER=1 dropped it to 3-4%. shadowfetch-defaults
ships a systemd user environment generator that sets the variable for the
session, only when /dev/dri has no renderD* node. The generator runs here under
/bin/sh (and dash) against a temporary directory standing in for /dev/dri.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

DEFAULTS = Path(__file__).resolve().parents[1]
REL = "usr/lib/systemd/user-environment-generators/60-shadowfetch-webkit-software-rendering"
GENERATOR = DEFAULTS / "data" / REL
INSTALL = DEFAULTS / "debian/shadowfetch-defaults.install"


def shells() -> list[str]:
    found = ["/bin/sh"]
    if shutil.which("dash"):
        found.append(shutil.which("dash"))
    return found


class Generator(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-dri-"))
        self.dri = self.tmp / "dri"
        self.dri.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_generator(self, *args: str, env: dict[str, str] | None = None) -> list[str]:
        outputs = []
        for shell in shells():
            result = subprocess.run(
                [shell, str(GENERATOR), *args],
                env={"PATH": "/usr/bin:/bin", **(env or {})},
                capture_output=True, text=True, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual("", result.stderr)
            outputs.append(result.stdout)
        self.assertEqual(1, len(set(outputs)), f"shells disagree: {outputs!r}")
        return outputs[0].splitlines()

    def test_no_dri_directory_disables_the_dmabuf_renderer(self):
        self.assertEqual(["WEBKIT_DISABLE_DMABUF_RENDERER=1"],
                         self.run_generator(str(self.tmp / "absent")))

    def test_card_node_without_render_node_disables_it(self):
        # QEMU std-VGA/virtio without virgl: a KMS card, no render node.
        (self.dri / "card0").touch()
        (self.dri / "by-path").mkdir()
        self.assertEqual(["WEBKIT_DISABLE_DMABUF_RENDERER=1"],
                         self.run_generator(str(self.dri)))

    def test_a_render_node_keeps_hardware_rendering(self):
        (self.dri / "card0").touch()
        (self.dri / "renderD128").touch()
        self.assertEqual([], self.run_generator(str(self.dri)))

    def test_a_value_the_user_set_wins(self):
        self.assertEqual([], self.run_generator(
            str(self.dri), env={"WEBKIT_DISABLE_DMABUF_RENDERER": "0"}))

    def test_output_is_only_assignments(self):
        # systemd rejects the whole generator output on a malformed line.
        for line in self.run_generator(str(self.dri)):
            self.assertRegex(line, r"^[A-Z_][A-Z0-9_]*=[^\s]*$")


class Packaging(unittest.TestCase):
    def test_generator_is_executable_posix_sh(self):
        self.assertTrue(GENERATOR.read_text().startswith("#!/bin/sh\n"))
        self.assertTrue(os.access(GENERATOR, os.X_OK))
        for shell in shells():
            subprocess.run([shell, "-n", str(GENERATOR)], check=True)

    def test_generator_is_installed_where_systemd_looks(self):
        lines = [line.split() for line in INSTALL.read_text().splitlines() if line.strip()]
        self.assertIn([f"data/{REL}", "usr/lib/systemd/user-environment-generators/"], lines)

    def test_the_real_dev_dri_is_the_default(self):
        self.assertIn("${1:-/dev/dri}", GENERATOR.read_text())

    def test_nothing_disables_plasma_systemd_boot(self):
        # The generator reaches apps only through the systemd-managed Plasma
        # session; a startkderc with systemdBoot=false would bypass it.
        root = DEFAULTS.parents[1]
        for base in (DEFAULTS / "data", root / "live-build/config/includes.chroot",
                     root / "live-build/config/hooks"):
            for path in base.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    if path.name == "startkderc":
                        self.fail(f"{path} may disable Plasma's systemd boot")
                    try:
                        text = path.read_text()
                    except (UnicodeDecodeError, OSError):
                        continue
                    self.assertNotRegex(
                        text, r"(?im)^\s*systemdBoot\s*=\s*false|--key\s+systemdBoot", str(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
