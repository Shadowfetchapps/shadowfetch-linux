#!/usr/bin/env python3
"""shared-mok-notice.sh: one plain notice when a 4.x ISO's DKMS key survived.

Runs the real script under /bin/sh with its two fixed paths pointed at a
fixture (a copy with CERT= and LIST= rewritten; the shipped script takes no
environment override) and a notify-send stub that records its argv.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

DEFAULTS = Path(__file__).resolve().parents[1]
SCRIPT = DEFAULTS / "data/usr/lib/shadowfetch/shared-mok-notice.sh"
AUTOSTART = DEFAULTS / "data/etc/xdg/autostart/shadowfetch-shared-mok-notice.desktop"
INSTALL = DEFAULTS / "debian/shadowfetch-defaults.install"

LEAKED = b"0\x82\x03<leaked 4.x certificate bytes\x00\xff"
FRESH = b"0\x82\x03<a per-machine certificate\x00\xfe"


class SharedMokNotice(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mok-notice-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        (self.home / ".config").mkdir(parents=True)
        self.cert = self.tmp / "mok.pub"
        listing = self.tmp / "shared-dkms-mok.sha256"
        listing.write_text("# comment\n%s  4.1.0\n" % hashlib.sha256(LEAKED).hexdigest())
        text = SCRIPT.read_text()
        self.assertIn("\nCERT=/var/lib/dkms/mok.pub\n", text)
        self.assertIn("\nLIST=/usr/share/shadowfetch/security/shared-dkms-mok.sha256\n", text)
        text = text.replace("\nCERT=/var/lib/dkms/mok.pub\n", f"\nCERT={self.cert}\n")
        text = text.replace("\nLIST=/usr/share/shadowfetch/security/shared-dkms-mok.sha256\n",
                            f"\nLIST={listing}\n")
        self.script = self.tmp / "shared-mok-notice.sh"
        self.script.write_text(text)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.log"
        self.log.touch()
        self.notify(0)

    def notify(self, code: int):
        stub = self.bin / "notify-send"
        stub.write_text(textwrap.dedent(f"""\
            #!{sys.executable}
            import json, sys
            open({str(self.log)!r}, "a").write(json.dumps(sys.argv[1:]) + "\\n")
            sys.exit({code})
            """))
        stub.chmod(0o755)

    def run_script(self) -> list[list[str]]:
        start = len(self.log.read_text().splitlines())
        env = {"HOME": str(self.home), "PATH": f"{self.bin}:/usr/bin:/bin"}
        result = subprocess.run(["/bin/sh", str(self.script)], env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stderr)
        return [json.loads(line) for line in self.log.read_text().splitlines()[start:]]

    @property
    def stamp(self) -> Path:
        return self.home / ".config/shadowfetch/.shared-mok-notice"

    def test_the_shared_key_is_reported_once(self):
        self.cert.write_bytes(LEAKED)
        (call,) = self.run_script()
        message = " ".join(call)
        self.assertIn("shadowfetch-doctor", message)
        self.assertIn("5.0.0 release notes", message)
        self.assertTrue(self.stamp.is_file())
        self.assertEqual([], self.run_script())

    def test_a_per_machine_key_says_nothing(self):
        self.cert.write_bytes(FRESH)
        self.assertEqual([], self.run_script())
        self.assertTrue(self.stamp.is_file())

    def test_no_key_says_nothing_and_checks_again_later(self):
        self.assertEqual([], self.run_script())
        self.assertFalse(self.stamp.exists())

    def test_a_notification_service_not_up_yet_retries(self):
        self.cert.write_bytes(LEAKED)
        self.notify(1)
        self.assertEqual(1, len(self.run_script()))
        self.assertFalse(self.stamp.exists())
        self.notify(0)
        self.assertEqual(1, len(self.run_script()))
        self.assertTrue(self.stamp.is_file())

    def test_it_never_touches_the_key(self):
        self.cert.write_bytes(LEAKED)
        self.run_script()
        self.assertEqual(LEAKED, self.cert.read_bytes())
        self.assertNotIn("rm ", SCRIPT.read_text().split("set -u", 1)[1])
        self.assertNotIn("mokutil ", SCRIPT.read_text().split("set -u", 1)[1])


class Packaging(unittest.TestCase):
    def test_installed_and_autostarted_on_kde(self):
        install = INSTALL.read_text().split()
        for path in ("data/usr/lib/shadowfetch/shared-mok-notice.sh",
                     "data/etc/xdg/autostart/shadowfetch-shared-mok-notice.desktop",
                     "data/usr/share/shadowfetch/security/shared-dkms-mok.sha256"):
            self.assertIn(path, install)
        entry = AUTOSTART.read_text()
        self.assertIn("Exec=/usr/lib/shadowfetch/shared-mok-notice.sh\n", entry)
        self.assertIn("OnlyShowIn=KDE;\n", entry)
        self.assertTrue(SCRIPT.read_text().startswith("#!/bin/sh\n"))
        self.assertTrue(SCRIPT.stat().st_mode & 0o111)
        subprocess.run(["sh", "-n", str(SCRIPT)], check=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
