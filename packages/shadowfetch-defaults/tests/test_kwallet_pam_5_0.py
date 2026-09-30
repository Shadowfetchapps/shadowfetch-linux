#!/usr/bin/env python3
"""KWallet without a first-use prompt: kwallet-pam opens kdewallet at login.

Installed users get skel's kwalletrc (one classic wallet named kdewallet,
first-use wizard off); kwallet-pam creates and unlocks it with the SDDM login
password. The autologin live user has no login password, so its hook turns
KWallet off instead.
"""
import configparser
import subprocess
import unittest
from pathlib import Path

DEFAULTS = Path(__file__).resolve().parents[1]
ROOT = DEFAULTS.parents[1]
SKEL = DEFAULTS / "data/etc/skel/.config/kwalletrc"
HOOK = ROOT / "live-build/config/hooks/0005-create-live-user.hook.chroot"
KDE_LIST = ROOT / "live-build/config/package-lists/shadowfetch-desktop-kde.list.chroot"


class SkelKWallet(unittest.TestCase):
    def test_skel_kwalletrc_names_the_pam_wallet(self):
        config = configparser.ConfigParser(interpolation=None)
        config.optionxform = str
        config.read_string(SKEL.read_text())
        self.assertEqual({"Enabled": "true", "First Use": "false",
                          "Default Wallet": "kdewallet", "Use One Wallet": "true"},
                         dict(config["Wallet"]))

    def test_skel_kwalletrc_is_installed(self):
        install = (DEFAULTS / "debian/shadowfetch-defaults.install").read_text().split("\n")
        self.assertIn("data/etc/skel/.config/kwalletrc  etc/skel/.config/", install)

    def test_pam_module_is_in_the_image(self):
        packages = [l.strip() for l in KDE_LIST.read_text().splitlines()]
        self.assertIn("libpam-kwallet5", packages)

    def test_live_user_only_turns_kwallet_off(self):
        hook = HOOK.read_text()
        subprocess.run(["bash", "-n", str(HOOK)], check=True)
        self.assertIn("WALLETRC=/home/$USER/.config/kwalletrc", hook)
        self.assertIn("'Enabled=false'", hook)
        self.assertNotRegex(hook, r">\s*/etc/skel")


if __name__ == "__main__":
    unittest.main(verbosity=2)
