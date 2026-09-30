"""Unit tests for the live ISO gate (tools/release/iso_gate.py).

These cases existed before Stage Q, but only as byte-identical pairs targeting
tools/iso_gate_2_1_4.py and tools/iso_gate_2_1_5.py -- archived copies. The ISO
gate that actually ran for 4.0.0 had NO test. They now run against the one live
implementation, which is the point of the consolidation.
"""

import gzip
import importlib.machinery
import importlib.util
import inspect
import lzma
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
RELEASE_DIR = ROOT / "tools" / "release"
if str(RELEASE_DIR) not in sys.path:
    sys.path.insert(0, str(RELEASE_DIR))

import gate  # noqa: E402

SRC = RELEASE_DIR / "iso_gate.py"
_loader = importlib.machinery.SourceFileLoader("release_iso_gate_test", str(SRC))
_spec = importlib.util.spec_from_loader("release_iso_gate_test", _loader)
iso_gate = importlib.util.module_from_spec(_spec)
_loader.exec_module(iso_gate)


class ImagePackageAllowlistTests(unittest.TestCase):
    """The installed allowlist is derived from version data, not transcribed."""

    def setUp(self) -> None:
        self.release = gate.load_release("4.0.0")

    def test_every_installed_package_carries_the_release_version(self) -> None:
        packages = iso_gate.image_packages(self.release)
        self.assertEqual("4.0.0-1", packages["shadowfetch-missions"])
        self.assertEqual("4.14-2", packages["grub-btrfs"])

    def test_the_nvidia_setup_package_is_published_but_not_installed(self) -> None:
        """It is a post-install helper: expecting it in the image would fail the gate."""
        self.assertIn("shadowfetch-nvidia", self.release.binary_versions)
        self.assertNotIn("shadowfetch-nvidia", iso_gate.image_packages(self.release))

    def test_the_image_allowlist_is_otherwise_the_published_set(self) -> None:
        excluded = set(self.release.section("packages")["image_excluded"])
        self.assertEqual(
            set(self.release.binary_versions) - excluded,
            set(iso_gate.image_packages(self.release)),
        )


class IsoGateTests(unittest.TestCase):
    PARTITION_CONFIG = """
efi:
  mountPoint: /boot/efi
  recommendedSize: 512MiB
  minimumSize: 300MiB
  label: EFI
partitionLayout:
  - name: boot
    filesystem: ext4
    noEncrypt: true
    mountPoint: /boot
    size: 2G
  - name: root
    filesystem: btrfs
    mountPoint: /
    size: 100%
"""

    def test_calamares_order_uses_exec_phase_not_show_phase(self):
        settings = """
sequence:
  - show:
      - welcome
      - users
      - summary
  - exec:
      - partition
      - unpackfs
      - shellprocess
      - users
      - sources-final
      - umount
  - show:
      - finished
"""
        self.assertEqual(
            [
                "partition",
                "unpackfs",
                "shellprocess",
                "users",
                "sources-final",
                "umount",
            ],
            iso_gate.validate_calamares_exec_sequence(settings),
        )

    def test_calamares_rejects_cleanup_after_user_creation(self):
        settings = """
sequence:
  - exec:
      - unpackfs
      - users
      - shellprocess
      - sources-final
      - umount
"""
        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            iso_gate.validate_calamares_exec_sequence(settings)

    def test_calamares_requires_one_of_each_safety_boundary(self):
        settings = """
sequence:
  - exec:
      - unpackfs
      - shellprocess
      - users
      - sources-final
"""
        with self.assertRaisesRegex(RuntimeError, "exactly once"):
            iso_gate.validate_calamares_exec_sequence(settings)

    def test_partition_contract_has_one_calamares_managed_esp(self):
        document = iso_gate.validate_partition_contract(self.PARTITION_CONFIG)
        self.assertEqual("512MiB", document["efi"]["recommendedSize"])

    def test_partition_contract_rejects_duplicate_layout_esp(self):
        config = self.PARTITION_CONFIG.replace(
            "  - name: root",
            "  - name: EFI\n"
            "    type: c12a7328-f81f-11d2-ba4b-00a0c93ec93b\n"
            "    filesystem: fat32\n"
            "    mountPoint: /boot/efi\n"
            "  - name: root",
        )
        with self.assertRaisesRegex(RuntimeError, "duplicates"):
            iso_gate.validate_partition_contract(config)

    def test_partition_contract_rejects_wrong_efi_size(self):
        config = self.PARTITION_CONFIG.replace("512MiB", "300MiB")
        with self.assertRaisesRegex(RuntimeError, "EFI settings mismatch"):
            iso_gate.validate_partition_contract(config)

    def test_partition_contract_requires_unencrypted_boot_partition(self):
        config = self.PARTITION_CONFIG.replace("    noEncrypt: true\n", "")
        with self.assertRaisesRegex(RuntimeError, "clear /boot contract mismatch"):
            iso_gate.validate_partition_contract(config)

    def test_partition_contract_rejects_forced_gpt(self):
        config = "defaultPartitionTableType: gpt\n" + self.PARTITION_CONFIG
        with self.assertRaisesRegex(RuntimeError, "msdos for BIOS"):
            iso_gate.validate_partition_contract(config)

    def test_partition_contract_rejects_custom_bios_grub(self):
        config = self.PARTITION_CONFIG.replace(
            "  - name: boot",
            "  - name: bios_grub\n"
            "    filesystem: unformatted\n"
            "    noEncrypt: true\n"
            "    size: 2M\n"
            "  - name: boot",
        )
        with self.assertRaisesRegex(RuntimeError, "must not synthesize"):
            iso_gate.validate_partition_contract(config)

    def test_grub_installer_resolves_mapper_stack_without_uuid_sed(self):
        installer = (ROOT / "live-build/config/includes.chroot/usr/local/sbin/sf-install-grub").read_text()
        iso_gate.validate_grub_installer_contract(installer)
        with self.assertRaisesRegex(RuntimeError, "unsafe legacy logic"):
            iso_gate.validate_grub_installer_contract(
                installer + "\ngrub-probe --target=device / | sed -E 's/p?[0-9]+$//'\n"
            )
        with self.assertRaisesRegex(RuntimeError, "incomplete"):
            iso_gate.validate_grub_installer_contract(
                installer.replace(
                    'dpkg --install "$GRUB_PC_DEB"',
                    "false",
                )
            )

    def test_grub_cache_hook_pins_and_verifies_the_live_binary_version(self):
        hook = (
            ROOT / "live-build/config/hooks/0014-cache-bios-grub-package.hook.chroot"
        ).read_text()
        for required in (
            "VERSION=$(dpkg-query -W -f='${Version}' grub-pc-bin)",
            'apt-get download "grub-pc=$VERSION"',
            'PACKAGE=$(dpkg-deb --field "$1" Package)',
            'PACKAGE_VERSION=$(dpkg-deb --field "$1" Version)',
            'PACKAGE_ARCHITECTURE=$(dpkg-deb --field "$1" Architecture)',
            "sha256sum grub-pc.deb > grub-pc.deb.sha256",
        ):
            with self.subTest(required=required):
                self.assertIn(required, hook)

    def test_nvidia_gate_catches_abi_suffixed_driver_libraries(self):
        rejected = (
            "nvidia-driver",
            "nvidia-open-610",
            "libnvidia-ml1",
            "libnvidia-cfg1",
            "libnvidia-compute-550",
        )
        for package in rejected:
            with self.subTest(package=package):
                self.assertRegex(package, iso_gate.PROPRIETARY_NVIDIA_PACKAGE)
        allowed = (
            "nvidia-detect",
            "nvidia-alternative",
            "nvidia-installer-cleanup",
            "glx-alternative-nvidia",
        )
        for package in allowed:
            with self.subTest(package=package):
                self.assertNotRegex(package, iso_gate.PROPRIETARY_NVIDIA_PACKAGE)

    def test_build_time_downloader_gate_catches_libdvd_packages(self):
        installed = {
            "bash",
            "libdvd-pkg",
            "libdvdcss2",
            "libdvdcss-dev",
            "libdvdcss2-dbgsym",
        }
        self.assertEqual(
            [
                "libdvd-pkg",
                "libdvdcss-dev",
                "libdvdcss2",
                "libdvdcss2-dbgsym",
            ],
            iso_gate.forbidden_build_time_packages(installed),
        )


class RequiredPayloadTests(unittest.TestCase):
    """Files the 5.0.0 VM qualification showed the image cannot do without."""

    def test_webkit_generator_is_required_and_executable(self) -> None:
        path = ("usr/lib/systemd/user-environment-generators/"
                "60-shadowfetch-webkit-software-rendering")
        self.assertEqual(path, iso_gate.WEBKIT_GENERATOR)
        self.assertIn(path, iso_gate.REQUIRED_ROOT_FILES)
        # systemd silently skips a generator without the execute bit.
        self.assertIn(path, iso_gate.REQUIRED_EXECUTABLES)
        source = ROOT / "packages/shadowfetch-defaults/data" / path
        self.assertTrue(source.is_file())
        install = (ROOT / "packages/shadowfetch-defaults/debian/"
                   "shadowfetch-defaults.install").read_text().split()
        self.assertIn(f"data/{path}", install)

    def test_every_required_executable_is_a_required_file(self) -> None:
        self.assertLessEqual(iso_gate.REQUIRED_EXECUTABLES, iso_gate.REQUIRED_ROOT_FILES)


class LoginKeyringGateTests(unittest.TestCase):
    """5.0.0 QA (ISO 2abd1f6f): the first Secret Service store prompted for a keyring."""

    SESSION = "session\trequired\tpam_unix.so \nsession\toptional\tpam_gnome_keyring.so auto_start\n"
    KEYRING = "[keyring]\ndisplay-name=Login\nctime=0\nmtime=0\nlock-on-idle=false\nlock-after=false\n"
    UNLOCK = "[Desktop Entry]\nType=Application\n" + iso_gate.LIVE_KEYRING_UNLOCK_EXEC + "\n"

    def inventory(self, **overrides: str) -> dict[str, str]:
        base = iso_gate.LIVE_KEYRINGS
        inventory = {
            base: "drwx------",
            f"{base}/login.keyring": "-rw-------",
            f"{base}/default": "-rw-------",
            iso_gate.LIVE_KEYRING_UNLOCK: "-rw-r--r--",
        }
        for name, mode in overrides.items():
            key = base if name == "dir" else f"{base}/{name.replace('_', '.')}"
            inventory[key] = mode
        return inventory

    def test_accepts_the_contract(self) -> None:
        iso_gate.validate_login_keyring_contract(
            self.SESSION, self.inventory(), self.KEYRING, "login", self.UNLOCK)

    def test_refuses_a_session_stack_without_the_keyring(self) -> None:
        for session in ("session\trequired\tpam_unix.so\n",
                        "session\toptional\tpam_gnome_keyring.so\n",
                        "#session\toptional\tpam_gnome_keyring.so auto_start\n"):
            with self.assertRaisesRegex(RuntimeError, "common-session"):
                iso_gate.validate_login_keyring_contract(
                    session, self.inventory(), self.KEYRING, "login", self.UNLOCK)

    def test_refuses_missing_or_readable_keyring_files(self) -> None:
        for inventory in (
            {},
            self.inventory(dir="drwxr-xr-x"),
            self.inventory(login_keyring="-rw-r--r--"),
        ):
            with self.assertRaisesRegex(RuntimeError, "not private"):
                iso_gate.validate_login_keyring_contract(
                    self.SESSION, inventory, self.KEYRING, "login", self.UNLOCK)

    def test_refuses_another_default_or_a_locking_keyring(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "default keyring"):
            iso_gate.validate_login_keyring_contract(
                self.SESSION, self.inventory(), self.KEYRING, "session", self.UNLOCK)
        with self.assertRaisesRegex(RuntimeError, "plain-text"):
            iso_gate.validate_login_keyring_contract(
                self.SESSION, self.inventory(), "GnomeKeyring\n\r\x00\n", "login", self.UNLOCK)
        with self.assertRaisesRegex(RuntimeError, "plain-text"):
            iso_gate.validate_login_keyring_contract(
                self.SESSION, self.inventory(),
                self.KEYRING.replace("lock-on-idle=false", "lock-on-idle=true"), "login",
                self.UNLOCK)

    def test_refuses_a_live_session_that_leaves_the_keyring_locked(self) -> None:
        inventory = self.inventory()
        del inventory[iso_gate.LIVE_KEYRING_UNLOCK]
        for inv, unlock in (
            (inventory, self.UNLOCK),
            (self.inventory(), "[Desktop Entry]\nExec=true\n"),
            (self.inventory(), "#" + self.UNLOCK.split("\n", 2)[2]),
        ):
            with self.assertRaisesRegex(RuntimeError, "unlock its login keyring"):
                iso_gate.validate_login_keyring_contract(
                    self.SESSION, inv, self.KEYRING, "login", unlock)

    def test_gate_runs_the_contract(self) -> None:
        source = inspect.getsource(iso_gate.identity_and_installer_gate)
        self.assertIn("validate_login_keyring_contract(", source)


class BuildLeakGateTests(unittest.TestCase):
    """5.0.0 release scan: build-generated secrets and build-host paths.

    The c8ea7ef0 candidate (and 4.1.0) shipped /var/lib/dkms/mok.key, the
    ssl-cert snakeoil key and a bootstrap.log naming the builder's checkout.
    """

    RSA = b"-----BEGIN " + b"RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA\n-----END " + b"RSA PRIVATE KEY-----\n"
    PKCS8 = b"-----BEGIN " + b"PRIVATE KEY-----\nMIIEvQIBADANBgkq\n-----END " + b"PRIVATE KEY-----\n"
    # Armor is assembled at run time so secret scanners see no key in this file.
    OPENSSH = (b"-----BEGIN " + b"OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXkt\n"
               b"-----END " + b"OPENSSH PRIVATE KEY-----\n")
    PGP = (b"-----BEGIN " + b"PGP PRIVATE KEY BLOCK-----\n\nlQOYBF\n-----END "
           + b"PGP PRIVATE KEY BLOCK-----\n")

    def setUp(self) -> None:
        self._scratch = tempfile.TemporaryDirectory()
        self.tree = Path(self._scratch.name)

    def tearDown(self) -> None:
        self._scratch.cleanup()

    def put(self, relative: str, data: bytes) -> Path:
        path = self.tree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_path_check_names_every_build_generated_secret(self) -> None:
        inventory = {path: "-rw-------" for path in (
            "var/lib/dkms/mok.key",
            "var/lib/dkms/mok.pub",
            "etc/ssl/private/ssl-cert-snakeoil.key",
            "etc/ssl/certs/ssl-cert-snakeoil.pem",
            "etc/ssh/ssh_host_ed25519_key",
            "etc/ssh/ssh_host_rsa_key",
            "var/lib/systemd/random-seed",
            "var/lib/systemd/credential.secret",
            "var/lib/shim-signed/mok/MOK.priv",
            "etc/NetworkManager/system-connections/home.nmconnection",
        )}
        self.assertEqual(sorted(inventory), iso_gate.machine_secret_paths(inventory))

    def test_path_check_leaves_the_legitimate_neighbours_alone(self) -> None:
        inventory = {path: "-rw-r--r--" for path in (
            "var/lib/dkms/v4l2loopback/0.15.4/source/dkms.conf",
            "etc/ssh/ssh_host_ed25519_key.pub",
            "etc/ssh/sshd_config",
            "etc/ssl/certs/ca-certificates.crt",
            "etc/NetworkManager/system-connections",
            "etc/dkms/framework.conf",
            "usr/share/doc/dkms/mok.key.example",
        )}
        self.assertEqual([], iso_gate.machine_secret_paths(inventory))

    def test_content_scan_finds_pem_openssh_and_pgp_private_keys(self) -> None:
        self.put("etc/ssl/private/other.key", self.RSA)
        self.put("var/lib/dkms/mok.key", self.PKCS8)
        self.put("home/shadow/.ssh/id_ed25519", self.OPENSSH)
        self.put("root/secret.asc", b"notes\n" + self.PGP)
        self.assertEqual(
            ["etc/ssl/private/other.key", "home/shadow/.ssh/id_ed25519",
             "root/secret.asc", "var/lib/dkms/mok.key"],
            iso_gate.private_key_files(self.tree),
        )

    def test_content_scan_ignores_quoted_markers_public_keys_and_other_trees(self) -> None:
        # ImageMagick's mime.xml names the PGP armor as a magic string.
        self.put(
            "etc/ImageMagick-7/mime.xml",
            b'<mime type="application/pgp-keys" offset="0" '
            b'magic="-----BEGIN PGP PRIVATE KEY BLOCK-----" priority="50" />\n',
        )
        self.put("etc/ssl/certs/ca.pem", b"-----BEGIN CERTIFICATE-----\nMIIB\n")
        self.put("etc/ssh/ssh_host_ed25519_key.pub", b"ssh-ed25519 AAAAC3Nz host\n")
        self.put("usr/share/doc/example/test.key", self.RSA)
        outside = self.put("usr/share/doc/example/linked.key", self.RSA)
        (self.tree / "etc/linked.key").symlink_to(outside)
        self.assertEqual([], iso_gate.private_key_files(self.tree))

    def test_content_scan_honours_an_exact_path_allowlist_only(self) -> None:
        self.put("etc/fixture/test.key", self.RSA)
        self.put("etc/fixture/test2.key", self.RSA)
        with mock.patch.object(
            iso_gate, "ALLOWED_PRIVATE_KEY_PATHS", frozenset({"etc/fixture/test.key"})
        ):
            self.assertEqual(["etc/fixture/test2.key"], iso_gate.private_key_files(self.tree))

    def test_build_root_scan_reads_plain_and_compressed_logs(self) -> None:
        self.put(
            "var/log/bootstrap.log",
            b"I: Retrieving InRelease\n"
            b"/home/builder/projects/shadowfetch/live-build/chroot/debootstrap\n",
        )
        self.put(
            "var/log/apt/eipp.log.xz",
            lzma.compress(b"Dir: /srv/x/live-build/chroot/var/cache/apt\n"),
        )
        self.put("var/log/installer/syslog.1.gz",
                 gzip.compress(b"cwd=/home/builder/src\n"))
        self.put("root/.bash_history", b"cd /home/builder/work\n")
        self.put("var/log/dpkg.log", b"2026-09-29 status installed dkms:all 3.2.2-1\n")
        self.put("root/.bashrc", b"# ~/.bashrc: executed by bash(1)\n")
        # Outside /var/log and /root the rule does not apply (ucf's smb.conf
        # copy legitimately says /home/samba).
        self.put("var/lib/ucf/cache/:etc:samba:smb.conf", b"path = /home/samba/\n")
        self.assertEqual(
            ["root/.bash_history", "var/log/apt/eipp.log.xz",
             "var/log/bootstrap.log", "var/log/installer/syslog.1.gz"],
            iso_gate.build_root_leaks(self.tree),
        )

    def test_gate_refuses_path_hits_before_extracting_anything(self) -> None:
        inventory = {"etc": "drwxr-xr-x", "var/lib/dkms/mok.key": "-rw-------"}
        with mock.patch.object(iso_gate, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "var/lib/dkms/mok.key"):
                iso_gate.build_leak_gate(Path("/nonexistent.squashfs"), inventory)
        run.assert_not_called()

    def test_gate_fails_on_content_found_in_the_extracted_tree(self) -> None:
        inventory = {"etc": "drwxr-xr-x", "var": "drwxr-xr-x", "root": "drwx------"}

        def extract(label, argv, **_kwargs):
            destination = Path(argv[argv.index("-d") + 1])
            self.assertEqual(["etc", "var", "root"], argv[-3:])
            leaked = destination / "var/log/bootstrap.log"
            leaked.parent.mkdir(parents=True)
            leaked.write_bytes(b"/home/builder/x/live-build/chroot\n")

        with mock.patch.object(iso_gate, "run", side_effect=extract), \
                mock.patch.object(iso_gate, "program") as program:
            program.return_value.argv.side_effect = lambda *a: ["unsquashfs", *a]
            with self.assertRaisesRegex(RuntimeError, "var/log/bootstrap.log"):
                iso_gate.build_leak_gate(Path("/x.squashfs"), inventory)

    def test_gate_runs_in_main(self) -> None:
        source = inspect.getsource(iso_gate.main)
        self.assertIn("build_leak_gate(squashfs, inventory)", source)


class ScrubHookTests(unittest.TestCase):
    HOOKS = ROOT / "live-build/config/hooks"
    HOOK = HOOKS / "0100-scrub-build-state.hook.chroot"

    def test_scrub_hook_is_the_last_executable_chroot_hook(self) -> None:
        hooks = sorted(path.name for path in self.HOOKS.glob("*.chroot"))
        self.assertEqual(self.HOOK.name, hooks[-1])
        self.assertTrue(os.access(self.HOOK, os.X_OK))

    def test_scrub_hook_removes_keys_and_logs_and_verifies(self) -> None:
        hook = self.HOOK.read_text()
        for required in (
            "rm -f /var/lib/dkms/mok.key /var/lib/dkms/mok.pub",
            "rm -f /etc/ssl/private/ssl-cert-snakeoil.key /etc/ssl/certs/ssl-cert-snakeoil.pem",
            "rm -f /etc/ssh/ssh_host_*_key",
            "find /var/log -xdev \\( -type f -o -type l \\) -delete",
            "FATAL: $leftover survived the scrub",
        ):
            with self.subTest(required=required):
                self.assertIn(required, hook)
        # Directories stay: only files are removed from /var/log.
        self.assertNotIn("rm -rf /var/log", hook)
        self.assertNotIn("rm -rf /var/lib/dkms", hook)

    def test_installed_system_regenerates_its_own_snakeoil(self) -> None:
        firstboot = (
            ROOT / "packages/shadowfetch-defaults/data/usr/lib/shadowfetch/firstboot.sh"
        ).read_text()
        self.assertIn("make-ssl-cert generate-default-snakeoil --force-overwrite", firstboot)
        self.assertIn("ssl-cert", firstboot.split("make-ssl-cert generate")[0])
        self.assertLess(
            firstboot.index("generate-default-snakeoil"), firstboot.index('touch "$STAMP"')
        )

    def test_dkms_signing_key_is_left_to_dkms_defaults(self) -> None:
        """DKMS only regenerates /var/lib/dkms/mok.* when nothing overrides it."""
        for path in (ROOT / "live-build/config").rglob("*"):
            if path.is_file() and "dkms" in path.as_posix():
                self.assertNotIn("mok_signing_key", path.read_text(errors="replace"), path)


if __name__ == "__main__":
    unittest.main()
