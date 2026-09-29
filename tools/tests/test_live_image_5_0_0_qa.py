"""Live-image fixes from the 5.0.0 VM qualification.

* The live session auto-locked and asked for a password nobody was told.
* The installer slideshow never advanced, and its caption covered the
  slide artwork's SHADOWFETCH LINUX wordmark.
* The installed system's GRUB fell back to desktop-base's blue background
  once an entry was chosen.
"""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LB = ROOT / "live-build/config"
HOOK = LB / "hooks/0005-create-live-user.hook.chroot"
CLEANUP = LB / "includes.chroot/usr/local/sbin/sf-remove-live-user"
SKEL_LOCKRC = ROOT / "packages/shadowfetch-defaults/data/etc/skel/.config/kscreenlockerrc"
SHOW = LB / "includes.chroot/etc/calamares/branding/debian/show.qml"
BRANDING = LB / "includes.chroot/etc/calamares/branding/debian/branding.desc"
GRUB_CFG = LB / "includes.chroot/etc/default/grub.d/10-shadowfetch.cfg"


class LiveSessionDoesNotLock(unittest.TestCase):
    def test_hook_disables_locking_for_the_live_user_only(self):
        hook = HOOK.read_text()
        subprocess.run(["bash", "-n", str(HOOK)], check=True)
        self.assertIn("LOCKRC=/home/$USER/.config/kscreenlockerrc", hook)
        for key in ("Autolock=false", "LockOnResume=false", "Timeout=0"):
            self.assertIn(f"'{key}'", hook)
        # The hook verifies its own result (set -e makes a miss fatal).
        self.assertIn('grep -qx \'Autolock=false\' "$LOCKRC"', hook)
        self.assertIn('grep -qx \'LockOnResume=false\' "$LOCKRC"', hook)
        # The live override must not land in skel, which installed users get.
        self.assertNotRegex(hook, r">\s*/etc/skel")

    def test_installed_users_still_lock(self):
        skel = SKEL_LOCKRC.read_text()
        self.assertIn("[Daemon]\nAutolock=true\n", skel)
        self.assertNotIn("LockOnResume=false", skel)

    def test_live_face_icon_is_shadowfetch(self):
        hook = HOOK.read_text()
        self.assertIn("FACE=/usr/share/sddm/themes/umbra/faces/.face.icon", hook)
        self.assertTrue((ROOT / "packages/shadowfetch-themes/data/usr/share/sddm/themes/umbra/faces/.face.icon").is_file())

    def test_installer_cleanup_removes_the_live_home(self):
        # Every live-only setting above lives under /home/shadow.
        cleanup = CLEANUP.read_text()
        self.assertIn('rm -rf -- "/home/${LIVE_USER:?}"', cleanup)
        self.assertIn('if [ -e "/home/$LIVE_USER" ];', cleanup)


class Slideshow(unittest.TestCase):
    def test_timer_runs_while_calamares_shows_the_slideshow(self):
        self.assertRegex(BRANDING.read_text(), r"(?m)^slideshowAPI:\s*2\s*$")
        timer = re.search(r"Timer\s*\{(.*?)\}", SHOW.read_text(), re.S)
        self.assertIsNotNone(timer)
        self.assertIn("running: presentation.activatedInCalamares", timer.group(1))

    def test_caption_band_is_outside_the_artwork(self):
        show = SHOW.read_text()
        # Artwork is fitted into the area above the caption, never full-bleed
        # under it.
        self.assertEqual(2, show.count("height: parent.height - presentation.captionHeight"))
        self.assertNotIn('anchors.fill: parent\n            source: "slide-shadowcode.jpg"', show)


class InstalledGrubBackground(unittest.TestCase):
    def test_grub_background_is_the_umbra_background(self):
        cfg = GRUB_CFG.read_text()
        theme = re.search(r"(?m)^GRUB_THEME=(\S+)$", cfg).group(1)
        background = re.search(r"(?m)^GRUB_BACKGROUND=(\S+)$", cfg).group(1)
        self.assertEqual(str(Path(theme).parent / "background.png"), background)
        self.assertTrue((LB / "includes.chroot" / background.lstrip("/")).is_file())


if __name__ == "__main__":
    unittest.main()
