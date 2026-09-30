#!/usr/bin/env python3
"""The login keyring opens at login, so a Secret Service store never prompts.

5.0.0 QA (ISO 2abd1f6f): `secret-tool store` -- what ShadowCode does with API
keys -- popped gnome-keyring's "Choose password for new keyring". Debian's
sddm 0.21.0+git20260801-3 dropped its own "-session pam_gnome_keyring.so
auto_start" line, leaving it to libpam-gnome-keyring, whose pam-auth-update
profile still only covers password changes. shadowfetch-defaults ships the
session half as a pam-auth-update profile (installed and upgraded systems);
the autologin live user, who has no password to hand PAM, gets an unencrypted
default "login" keyring from its hook.
"""
import importlib.machinery
import importlib.util
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

DEFAULTS = Path(__file__).resolve().parents[1]
ROOT = DEFAULTS.parents[1]
PROFILE = DEFAULTS / "data/usr/share/pam-configs/shadowfetch-gnome-keyring"
POSTINST = DEFAULTS / "debian/postinst"
PRERM = DEFAULTS / "debian/prerm"
HOOK = ROOT / "live-build/config/hooks/0005-create-live-user.hook.chroot"
APPLICATIONS = DEFAULTS / "data/usr/share/applications"


def profile_fields(text):
    """pam-auth-update's format: 'Key: value', continuation lines indented."""
    fields, key = {}, None
    for line in text.splitlines():
        if line[:1].isspace() and key:
            fields[key] = (fields[key] + "\n" + line.strip()).strip()
        elif ":" in line:
            key, value = line.split(":", 1)
            fields[key] = value.strip()
    return fields


class PamProfile(unittest.TestCase):
    def test_profile_adds_only_the_interactive_session_half(self):
        fields = profile_fields(PROFILE.read_text())
        self.assertEqual("yes", fields["Default"])
        self.assertEqual("Additional", fields["Session-Type"])
        self.assertEqual("yes", fields["Session-Interactive-Only"])
        self.assertEqual(["optional", "pam_gnome_keyring.so", "auto_start"],
                         fields["Session"].split())
        # sddm keeps its own "-auth optional pam_gnome_keyring.so", and
        # gnome-keyring's own profile does the password half.
        self.assertFalse({"Auth", "Auth-Type", "Password", "Password-Type"} & set(fields))

    def test_profile_is_installed(self):
        install = (DEFAULTS / "debian/shadowfetch-defaults.install").read_text().split("\n")
        self.assertIn(
            "data/usr/share/pam-configs/shadowfetch-gnome-keyring  usr/share/pam-configs/",
            install)

    def test_postinst_applies_it_on_every_configure(self):
        # Every configure, not only fresh installs: an upgraded 4.x system's
        # sddm upgrade replaced /etc/pam.d/sddm with the line-less version.
        text = POSTINST.read_text()
        block = re.search(
            r'if \[ "\$1" = "configure" \] && command -v pam-auth-update[^\n]*\n'
            r"\s+pam-auth-update --package\n\s*fi", text)
        self.assertIsNotNone(block)
        self.assertLess(text.index("pam-auth-update --package"), text.index("#DEBHELPER#"))

    def test_prerm_removes_it_before_the_profile_goes(self):
        text = PRERM.read_text()
        self.assertTrue(os.access(PRERM, os.X_OK))
        self.assertIn('if [ "$1" = "remove" ]', text)
        self.assertIn("pam-auth-update --package --remove shadowfetch-gnome-keyring", text)
        self.assertIn("#DEBHELPER#", text)

    def test_maintainer_scripts_parse(self):
        for script in (POSTINST, PRERM):
            subprocess.run(["sh", "-n", str(script)], check=True)

    def test_no_repository_file_edits_another_packages_pam_conffile(self):
        # The fix goes through pam-auth-update; editing /etc/pam.d/sddm would
        # turn every later sddm upgrade into a conffile prompt.
        for script in (POSTINST, PRERM, HOOK):
            code = "\n".join(line for line in script.read_text().splitlines()
                             if not line.lstrip().startswith("#"))
            self.assertNotIn("/etc/pam.d/sddm", code, script)


class LiveLoginKeyring(unittest.TestCase):
    """Runs the hook's own keyring block against a temporary home."""

    def keyring_block(self):
        hook = HOOK.read_text()
        start = hook.index("KEYRINGS=/home/$USER/.local/share/keyrings")
        end = hook.index("\n", hook.index("grep -q 'Secret.Service Unlock", start))
        return hook[start:end + 1]

    def test_hook_creates_a_private_unencrypted_default_login_keyring(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / "shadow").mkdir(parents=True)
            script = "set -e\nUSER=shadow\n" + self.keyring_block().replace("/home/", f"{home}/")
            subprocess.run(["bash", "-c", script], check=True)
            keyrings = home / "shadow/.local/share/keyrings"
            self.assertEqual(0o700, stat.S_IMODE(keyrings.stat().st_mode))
            login = keyrings / "login.keyring"
            default = keyrings / "default"
            for path in (login, default):
                self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode), path)
            self.assertEqual("login", default.read_text())
            text = login.read_text()
            self.assertTrue(text.startswith("[keyring]\n"))
            self.assertIn("display-name=Login\n", text)
            self.assertIn("lock-on-idle=false\n", text)
            self.assertNotIn("GnomeKeyring", text)  # the encrypted binary format's magic
            unlock = home / "shadow/.config/autostart/shadowfetch-live-keyring.desktop"
            unlock_text = unlock.read_text()
            if shutil.which("desktop-file-validate"):
                result = subprocess.run(["desktop-file-validate", str(unlock)],
                                        capture_output=True, text=True)
                self.assertEqual("", result.stdout + result.stderr)

            # The ISO gate accepts exactly what the hook produced.
            gate_src = ROOT / "tools/release/iso_gate.py"
            import sys
            sys.path.insert(0, str(gate_src.parent))
            loader = importlib.machinery.SourceFileLoader("iso_gate_keyring_test", str(gate_src))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            iso_gate = importlib.util.module_from_spec(spec)
            loader.exec_module(iso_gate)
            inventory = {
                iso_gate.LIVE_KEYRINGS + suffix: stat.filemode(path.stat().st_mode)
                for suffix, path in (("", keyrings), ("/login.keyring", login),
                                     ("/default", default))
            }
            inventory[iso_gate.LIVE_KEYRING_UNLOCK] = stat.filemode(unlock.stat().st_mode)
            iso_gate.validate_login_keyring_contract(
                "session\toptional\tpam_gnome_keyring.so auto_start\n",
                inventory, text, default.read_text(), unlock_text)

    def test_hook_keeps_it_in_the_live_home_only(self):
        hook = HOOK.read_text()
        subprocess.run(["bash", "-n", str(HOOK)], check=True)
        self.assertNotRegex(hook, r"/etc/skel/\.local/share/keyrings")
        # The hook's final chown hands the new files to the live user.
        self.assertLess(hook.index("KEYRINGS="), hook.index("chown -R \"$USER:$PRIMARY_GROUP\" \"/home/$USER\""))


class DesktopEntries(unittest.TestCase):
    def test_exec_lines_use_no_string_escapes(self):
        # `\"` is not a valid desktop-entry string escape; KDE reported
        # shadowfetch-gpu.desktop as invalid (5.0.0 QA).
        for entry in sorted(APPLICATIONS.glob("*.desktop")):
            for line in entry.read_text().splitlines():
                if line.startswith("Exec="):
                    self.assertNotRegex(line, r'\\[^\\sntr;]', entry.name)

    def test_gpu_entry_keeps_its_prompt(self):
        text = (APPLICATIONS / "shadowfetch-gpu.desktop").read_text()
        self.assertIn(
            "Exec=konsole -e bash -c \"shadowfetch-gpu; echo; "
            "read -r -p 'Press Enter to close…' _\"\n", text)

    @unittest.skipUnless(shutil.which("desktop-file-validate"), "desktop-file-utils absent")
    def test_entries_validate(self):
        entries = sorted(str(path) for path in APPLICATIONS.glob("*.desktop"))
        result = subprocess.run(["desktop-file-validate", *entries],
                                capture_output=True, text=True)
        self.assertEqual("", result.stdout + result.stderr)
        self.assertEqual(0, result.returncode)


if __name__ == "__main__":
    unittest.main(verbosity=2)
