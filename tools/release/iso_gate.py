#!/usr/bin/env python3
"""Validate a Shadowfetch Linux release ISO as a release artifact.

ONE implementation for every release. The installed-package allowlist, the
signing fingerprint, the identity strings and the installer slideshow contract
are DATA in tools/release/versions/<version>.toml.

Every program is resolved to a trusted absolute path by gate.ProgramResolver.
This gate is the strongest place the invariant matters: gpgv decides whether
the shipped ISO is genuinely signed, sha256sum decides whether the image
contents match the manifest, and unsquashfs supplies every byte the payload
checks then judge.
"""

from __future__ import annotations

import argparse
import bz2
from contextlib import contextmanager
import gzip
import hashlib
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
from typing import Iterator

import yaml

import gate
from gate import (
    ROLE_QUALITY,
    ROLE_SECURITY,
    output,
    parse_deb822,
    parse_os_release,
    run,
    sha256,
)

from providers.validate_manifest import validate_provider_payload

import shadowcode

from drkonqi_pickup_contract import (
    DROPIN, HELPER, UPSTREAM_UNITS, UPSTREAM_VERSION,
    validate_dropin, validate_upstream_unit,
)


ROOT = gate.ROOT
MAX_SQUASHFS_BYTES = 4 * 1024 * 1024 * 1024 - 1

# gpg/gpgv decide the artifact's authenticity, sha256sum its integrity,
# unsquashfs/dpkg-deb/xorriso what it actually contains, and mount/umount/
# findmnt/sudo how it is opened. Every one of those decides a security fact
# about the shipped image, so none may be found through PATH.
REQUIRED_PROGRAMS = (
    ("dpkg-deb", ROLE_SECURITY),
    ("findmnt", ROLE_SECURITY),
    ("gpg", ROLE_SECURITY),
    ("gpgv", ROLE_SECURITY),
    ("mount", ROLE_SECURITY),
    ("sha256sum", ROLE_SECURITY),
    ("sudo", ROLE_SECURITY),
    ("umount", ROLE_SECURITY),
    ("unsquashfs", ROLE_SECURITY),
    ("xorriso", ROLE_QUALITY),
)

# Set once in main(); see the same note in package_gate.py.
RELEASE: gate.ReleaseData
PROGRAMS: dict[str, gate.TrustedProgram] = {}


def program(name: str) -> gate.TrustedProgram:
    try:
        return PROGRAMS[name]
    except KeyError:  # pragma: no cover - a programming error, not a gate failure
        raise RuntimeError(f"{name} was not resolved before use") from None


def image_packages(release: gate.ReleaseData) -> dict[str, str]:
    """Custom packages that must be installed IN the image.

    Not every published package is installed: the NVIDIA setup package is a
    post-install helper, so it is listed under packages.image_excluded and the
    installed allowlist must not expect it.

    And not every installed package is published. packages.image_only names the
    other direction -- shadowfetch-archive-keyring is built by `make iso` and
    installed into the chroot before live-build's first apt-get update, because
    it carries the key the apt sources name by path. It cannot be published
    through the repository it authenticates. Its version is the bare release
    version: the Makefile stamps its control file with $(VERSION), not with
    <version>-<revision>.
    """
    section = release.section("packages")
    excluded = set(section.get("image_excluded", ()))
    packages = {
        name: version
        for name, version in release.binary_versions.items()
        if name not in excluded
    }
    for name in section.get("image_only", ()):
        if name in packages:
            raise RuntimeError(
                f"{name} is in both packages.image_only and the published set; "
                "it is one or the other")
        packages[name] = release.version
    return packages


REQUIRED_IMAGE_FILES = {
    "boot/grub/grub.cfg",
    "boot/grub/themes/umbra/theme.txt",
    "live/filesystem.squashfs",
    "live/initrd.img",
    "live/vmlinuz",
}

WEBKIT_GENERATOR = (
    "usr/lib/systemd/user-environment-generators/"
    "60-shadowfetch-webkit-software-rendering"
)

# 5.0.0 soak (ISO 2d8a72e0): KDE's update notifier had PackageKit refresh the
# apt indexes 300s after login, and on the live medium ~350 MB of them landed
# in RAM. shadowfetch-defaults keeps it off there with a drop-in for the user
# unit systemd-xdg-autostart-generator makes from Discover's autostart entry.
# Both names are checked against each other: if Discover renames its entry,
# the drop-in silently matches nothing.
DISCOVER_NOTIFIER_AUTOSTART = "etc/xdg/autostart/org.kde.discover.notifier.desktop"
DISCOVER_NOTIFIER_DROPIN = (
    "usr/lib/systemd/user/app-org.kde.discover.notifier@autostart.service.d/"
    "10-shadowfetch-live-medium.conf"
)
# The only conditions the drop-in may carry. Conditions are ANDed, so anything
# else could also stop the notifier on installed systems, which keep it.
LIVE_MEDIUM_CONDITIONS = frozenset({
    ("ConditionPathExists", "!/run/live/medium"),
    ("ConditionPathExists", "!/run/live/rootfs"),
})

REQUIRED_ROOT_FILES = {
    HELPER,
    DROPIN,
    DISCOVER_NOTIFIER_AUTOSTART,
    DISCOVER_NOTIFIER_DROPIN,
    *UPSTREAM_UNITS,
    "etc/apt/sources.list.d/shadowfetch.list",
    "etc/calamares/branding/debian/branding.desc",
    "etc/calamares/branding/debian/show.qml",
    "etc/calamares/branding/debian/slide-shadowcode.jpg",
    "etc/calamares/branding/debian/slide-agents.jpg",
    "etc/calamares/modules/partition.conf",
    "etc/calamares/modules/shellprocess.conf",
    "etc/calamares/settings.conf",
    "etc/os-release",
    "etc/systemd/system/shadowfetch-live-nossh.service",
    "etc/systemd/system/sysinit.target.wants/shadowfetch-live-nossh.service",
    "etc/systemd/system/sshd-keygen.service.d/10-shadowfetch-hostkeys.conf",
    "etc/ufw/ufw.conf",
    "usr/bin/add-calamares-desktop-icon",
    "usr/bin/shadowfetch-missions",
    "usr/bin/shadowfetch-grok-bot",
    # Optional-agent installers only; the agents themselves are never in the image.
    "usr/bin/shadowfetch-hermes",
    "usr/bin/shadowfetch-openclaw",
    "usr/bin/shadowfetch-control",
    "usr/bin/shadowfetch-agent-network",
    "usr/bin/shadowfetch-firebreak",
    "usr/bin/shadowfetch-passport",
    "usr/bin/shadowfetch-workbench",
    "usr/bin/shadowfetch-welcome",
    "usr/lib/shadowfetch/firstboot.sh",
    "usr/lib/systemd/system/shadowfetch-migrate-2.1.3-ai.service",
    "usr/libexec/shadowfetch-migrate-2.1.3-ai",
    "usr/libexec/phoenix-apt-repair",
    "usr/local/sbin/sf-remove-live-user",
    "usr/share/applications/shadowfetch-guide.desktop",
    "usr/share/applications/shadowfetch-workbench.desktop",
    "usr/share/shadowfetch/control-center/sfcc/guide_page.py",
    "usr/share/shadowfetch/control-center/sfcc/workbench_page.py",
    "usr/share/shadowfetch/control-center/sfcc/missions_page.py",
    "usr/share/shadowfetch/control-center/sfcc/grok_bot_page.py",
    "usr/share/shadowfetch/installer-packages/grub-pc.deb",
    "usr/share/shadowfetch/installer-packages/grub-pc.deb.sha256",
    "usr/share/shadowfetch/migrations/2.1.3-ai-packages",
    "usr/share/shadowfetch/apt-recovery/KEYRING.README",
    "usr/share/shadowfetch/apt-recovery/debian.sources",
    "usr/share/shadowfetch/apt-recovery/umbra-archive-keyring.gpg",
    "usr/share/shadowfetch/apt-recovery/umbra.sources",
    "usr/share/shadowfetch/os-release.shadowfetch",
    "usr/share/shadowfetch/version",
    "usr/share/shadowfetch/workbench/profiles.json",
    "usr/share/shadowfetch/welcome/catalog/workbench-ai-lab.json",
    "usr/share/shadowfetch/welcome/catalog/workbench-creative-ai.json",
    "usr/share/shadowfetch/welcome/catalog/workbench-production-ops.json",
    "usr/share/shadowfetch/welcome/catalog/workbench-software-studio.json",
    "var/lib/dpkg/status",
    # 5.0: ShadowCode's system policy; without it the preinstalled copy nags
    # about GitHub releases that apt, not the user, must install.
    "etc/shadowcode/policy.yaml",
    # 5.0 QA blocker: without it ShadowCode (WebKitGTK) idles at 120-170% CPU
    # on machines with no DRM render node. Mode checked below: systemd skips
    # a generator it cannot execute, silently.
    WEBKIT_GENERATOR,
}

REQUIRED_EXECUTABLES = {
    HELPER,
    "usr/bin/add-calamares-desktop-icon",
    "usr/bin/shadowfetch-missions",
    "usr/bin/shadowfetch-grok-bot",
    "usr/bin/shadowfetch-hermes",
    "usr/bin/shadowfetch-openclaw",
    "usr/bin/shadowfetch-control",
    "usr/bin/shadowfetch-agent-network",
    "usr/bin/shadowfetch-firebreak",
    "usr/bin/shadowfetch-passport",
    "usr/bin/shadowfetch-workbench",
    "usr/bin/shadowfetch-welcome",
    "usr/libexec/shadowfetch-migrate-2.1.3-ai",
    "usr/libexec/phoenix-apt-repair",
    "usr/local/sbin/sf-remove-live-user",
    WEBKIT_GENERATOR,
}

RETIRED_PACKAGE = re.compile(
    r"^(?:openclaw(?:-|$)|hermes(?:-|$)|ollama(?:-|$)|"
    r"llama\.cpp(?:-|$)|libllama[0-9]*(?:-|$)|shadowfetch-ai-workspace$)",
    re.IGNORECASE,
)

PROPRIETARY_NVIDIA_PACKAGE = re.compile(
    r"^(?:nvidia-driver(?:-|$)|nvidia-kernel(?:-|$)|nvidia-open(?:-|$)|"
    r"cuda-drivers(?:-|$)|xserver-xorg-video-nvidia(?:-|$)|"
    r"libnvidia-(?:cfg|compute|decode|encode|glcore|ml)(?:[0-9]+|[-.]|$))",
    re.IGNORECASE,
)

FORBIDDEN_BUILD_TIME_PACKAGES = frozenset(
    {
        "libdvd-pkg",
        "libdvdcss2",
        "libdvdcss-dev",
        "libdvdcss2-dbgsym",
    }
)

CRITICAL_PACKAGE_PAYLOADS = {
    "shadowfetch-drkonqi-pickup": (HELPER, DROPIN),
    "shadowfetch-missions": (
        "usr/bin/shadowfetch-missions",
        "usr/lib/shadowfetch/missions/sf_missions.py",
        "usr/lib/systemd/user/shadowfetch-missions.service",
    ),
    "shadowfetch-control-center": (
        "usr/share/shadowfetch/control-center/sfcc/missions_page.py",
        "usr/share/shadowfetch/control-center/sfcc/grok_bot_page.py",
        "usr/share/shadowfetch/control-center/sfcc/mission_client.py",
    ),
    "shadowfetch-defaults": (
        "usr/bin/shadowfetch-grok-bot",
        "usr/bin/shadowfetch-hermes",
        "usr/bin/shadowfetch-openclaw",
        "usr/bin/shadowfetch-agent-network",
        "usr/bin/shadowfetch-passport",
        "usr/bin/shadowfetch-workbench",
        DISCOVER_NOTIFIER_DROPIN,
    ),
    "shadowfetch-fireline": (
        "usr/bin/shadowfetch-checkpoint",
        "usr/bin/shadowfetch-firebreak",
        "usr/bin/shadowfetch-mcp",
        "usr/lib/shadowfetch/mcp/sf_mcp.py",
    ),
}

SECRET_PATH = re.compile(
    r"^(?:root|home/[^/]+)/(?:(?:\.ssh/(?:id_[^/]+|authorized_keys))|"
    r"(?:\.aws/credentials)|(?:\.config/gcloud/application_default_credentials\.json)|"
    r"(?:\.config/gh/hosts\.yml)|(?:\.config/rclone/rclone\.conf)|"
    r"(?:\.gnupg/private-keys-v1\.d/))",
    re.IGNORECASE,
)

# 5.0.0 release scan: state the BUILD generated and every install then shared.
# /var/lib/dkms/mok.key was a Secure Boot module-signing key that shadowfetch-gpu
# asks users to enroll; the snakeoil key and SSH host keys are per-machine TLS/
# SSH identities. hooks/0100-scrub-build-state removes them; this refuses an
# image where it did not.
MACHINE_SECRET_PATH = re.compile(
    r"^(?:var/lib/dkms/mok\.(?:key|pub)"
    r"|var/lib/shim-signed/mok/MOK\.(?:priv|der|pem)"
    r"|etc/ssl/private/ssl-cert-snakeoil\.key"
    r"|etc/ssl/certs/ssl-cert-snakeoil\.pem"
    r"|etc/ssh/ssh_host_[^/]+_key"
    r"|var/lib/systemd/(?:random-seed|credential\.secret)"
    r"|etc/NetworkManager/system-connections/[^/]+)$"
)
# Content check behind the path check: any PEM/OpenSSH/PGP private key in the
# trees that hold machine or user state. Upstream packages ship none there (the
# 5.0.0 c8ea7ef0 image had exactly the two build-generated ones), so the
# allowlist is empty; add a genuine public test fixture by exact path only.
# The armor line must START a line, as RFC 7468/4880 require of a real key:
# /etc/ImageMagick-7/mime.xml quotes "-----BEGIN PGP PRIVATE KEY BLOCK-----"
# as a magic="..." attribute, which is file-type detection, not a key.
PRIVATE_KEY_SCAN_ROOTS = ("etc", "var", "root", "home")
PRIVATE_KEY_SCAN_MAX_BYTES = 1024 * 1024
PRIVATE_KEY_PEM = re.compile(
    rb"(?m)^[ \t]*-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"
)
ALLOWED_PRIVATE_KEY_PATHS: frozenset[str] = frozenset()
# Build logs recorded the builder's checkout (/home/<builder>/.../live-build/chroot).
BUILD_LOG_SCAN_ROOTS = ("var/log", "root")
BUILD_ROOT_PATH = re.compile(rb"live-build/chroot|/home/[A-Za-z0-9._-]+/")


def machine_secret_paths(inventory: dict[str, str]) -> list[str]:
    """Build-generated per-machine secrets present in the image, by path."""
    return sorted(path for path in inventory if MACHINE_SECRET_PATH.match(path))


def _walk_files(tree: Path, roots: tuple[str, ...]) -> Iterator[tuple[str, Path]]:
    """Regular files under tree/<root> (symlinks not followed), as (rel, path).

    An unreadable directory is an error, not a silent skip: a scan that did not
    look is not a pass.
    """
    def fail(error: OSError) -> None:
        raise RuntimeError(f"cannot scan extracted image tree: {error}")

    for root in roots:
        base = tree / root
        if not base.is_dir() or base.is_symlink():
            continue
        for directory, _dirs, files in os.walk(base, onerror=fail):
            for name in files:
                path = Path(directory) / name
                if path.is_symlink() or not path.is_file():
                    continue
                yield path.relative_to(tree).as_posix(), path


def private_key_files(tree: Path) -> list[str]:
    """Files under /etc, /var, /root, /home carrying a private key block."""
    found = []
    for relative, path in _walk_files(tree, PRIVATE_KEY_SCAN_ROOTS):
        if relative in ALLOWED_PRIVATE_KEY_PATHS:
            continue
        if path.stat().st_size > PRIVATE_KEY_SCAN_MAX_BYTES:
            continue
        if PRIVATE_KEY_PEM.search(path.read_bytes()):
            found.append(relative)
    return sorted(found)


def _log_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    openers = {".gz": gzip.decompress, ".xz": lzma.decompress, ".bz2": bz2.decompress}
    opener = openers.get(path.suffix)
    if opener is None:
        return data
    try:
        return opener(data)
    except (OSError, EOFError, lzma.LZMAError, ValueError):
        return data


def build_root_leaks(tree: Path) -> list[str]:
    """Files under /var/log or /root naming the build host's checkout."""
    return sorted(
        relative
        for relative, path in _walk_files(tree, BUILD_LOG_SCAN_ROOTS)
        if BUILD_ROOT_PATH.search(_log_bytes(path))
    )


def build_leak_gate(squashfs: Path, inventory: dict[str, str]) -> None:
    by_path = machine_secret_paths(inventory)
    if by_path:
        raise RuntimeError(
            "build-generated per-machine secrets are in the image: " + ", ".join(by_path)
        )
    roots = [
        root for root in PRIVATE_KEY_SCAN_ROOTS
        if root in inventory and inventory[root].startswith("d")
    ]
    with tempfile.TemporaryDirectory(
        prefix="iso-gate-leaks-", ignore_cleanup_errors=True
    ) as scratch:
        tree = Path(scratch) / "root"
        run(
            "extract " + ", ".join("/" + root for root in roots) + " for secret scan",
            program("unsquashfs").argv(
                "-no-xattrs", "-no-progress", "-quiet", "-d", str(tree),
                str(squashfs), *roots,
            ),
            capture=True,
        )
        keys = private_key_files(tree)
        if keys:
            raise RuntimeError("private keys are embedded in the image: " + ", ".join(keys))
        leaks = build_root_leaks(tree)
        if leaks:
            raise RuntimeError(
                "build logs naming the build host's checkout are in the image: "
                + ", ".join(leaks)
            )
    print(
        "PASS: no DKMS MOK, snakeoil, SSH host or other private keys under "
        "/etc /var /root /home; no build-root paths under /var/log or /root"
    )


def command_for_privilege(command: list[str]) -> list[str]:
    if os.geteuid() == 0:
        return command
    return program("sudo").argv(*command)


def calamares_exec_sequence(settings: str) -> list[str]:
    """Return the single Calamares execution phase from parsed YAML."""
    try:
        document = yaml.safe_load(settings)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Calamares settings are not valid YAML: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("sequence"), list):
        raise RuntimeError("Calamares settings have no sequence list")
    phases = [
        phase["exec"]
        for phase in document["sequence"]
        if isinstance(phase, dict) and "exec" in phase
    ]
    if len(phases) != 1 or not isinstance(phases[0], list):
        raise RuntimeError(f"expected one Calamares exec phase, found {len(phases)}")
    if not all(isinstance(module, str) for module in phases[0]):
        raise RuntimeError("Calamares exec phase contains a non-string module")
    return phases[0]


def validate_calamares_exec_sequence(settings: str) -> list[str]:
    sequence = calamares_exec_sequence(settings)
    required = ("unpackfs", "shellprocess", "users", "sources-final", "umount")
    duplicates = [module for module in required if sequence.count(module) != 1]
    if duplicates:
        raise RuntimeError(
            "Calamares exec sequence must contain each required module exactly once: "
            + ", ".join(duplicates)
        )
    positions = [sequence.index(module) for module in required]
    if positions != sorted(positions):
        raise RuntimeError(
            f"Calamares cleanup sequence is unsafe: "
            f"{dict(zip(required, positions, strict=True))}"
        )
    return sequence


LIVE_KEYRINGS = "home/shadow/.local/share/keyrings"
LIVE_KEYRING_UNLOCK = "home/shadow/.config/autostart/shadowfetch-live-keyring.desktop"
LIVE_KEYRING_UNLOCK_EXEC = (
    "Exec=busctl --user call org.freedesktop.secrets /org/freedesktop/secrets "
    "org.freedesktop.Secret.Service Unlock ao 1 /org/freedesktop/secrets/collection/login"
)


def validate_login_keyring_contract(
    common_session: str,
    inventory: dict[str, str],
    login_keyring: str,
    default: str,
    unlock_autostart: str,
) -> None:
    """No "Choose password for new keyring" on the first Secret Service store.

    Installed systems: common-session must open gnome-keyring's login keyring
    (Debian's sddm stopped doing it itself). Live session: the autologin user
    has no password for PAM to hand over, so its hook ships an unencrypted,
    private default "login" keyring, plus the autostart entry that unlocks it
    (gnome-keyring loads even a plain-text keyring locked, and ShadowCode's
    CreateItem does not ask for an Unlock) (5.0.0 QA, ISO 2abd1f6f).
    """
    if not re.search(
        r"(?m)^session\s+optional\s+pam_gnome_keyring\.so\s+auto_start\s*$",
        common_session,
    ):
        raise RuntimeError("common-session does not open the gnome-keyring login keyring")
    expected = {
        LIVE_KEYRINGS: "drwx------",
        f"{LIVE_KEYRINGS}/login.keyring": "-rw-------",
        f"{LIVE_KEYRINGS}/default": "-rw-------",
    }
    wrong = {path: inventory.get(path) for path, mode in expected.items() if inventory.get(path) != mode}
    if wrong:
        raise RuntimeError(f"live login keyring is absent or not private: {wrong}")
    if default.strip() != "login":
        raise RuntimeError(f"live default keyring is {default.strip()!r}, not 'login'")
    if not re.search(r"(?m)^\[keyring\]$", login_keyring) or re.search(
        r"(?m)^lock-on-idle=true$", login_keyring
    ):
        raise RuntimeError("live login keyring is not an unlocked plain-text keyring")
    if LIVE_KEYRING_UNLOCK not in inventory or not re.search(
        rf"(?m)^{re.escape(LIVE_KEYRING_UNLOCK_EXEC)}$", unlock_autostart
    ):
        raise RuntimeError("live session does not unlock its login keyring at login")


def xdg_autostart_unit(desktop_file: str) -> str:
    """The user unit systemd-xdg-autostart-generator makes from an autostart entry.

    app-<desktop-file id>@autostart.service, the id escaped as systemd's
    unit_name_escape() does it: '/' becomes '-', and a leading '.' and every
    byte outside [A-Za-z0-9:_.] -- '-' included -- becomes \\xNN.
    """
    name = PurePosixPath(desktop_file).name
    if name.endswith(".desktop"):
        name = name[: -len(".desktop")]
    escaped = []
    for index, byte in enumerate(name.encode("utf-8")):
        char = chr(byte)
        if char == "/":
            escaped.append("-")
        elif (index == 0 and char == ".") or not (
            byte < 0x80 and (char.isalnum() or char in ":_.")
        ):
            escaped.append(f"\\x{byte:02x}")
        else:
            escaped.append(char)
    return f"app-{''.join(escaped)}@autostart.service"


def validate_live_notifier_dropin(inventory: dict[str, str], dropin: str) -> str:
    """KDE's update notifier is skipped on the live medium, and only there.

    The drop-in must sit in the directory of the unit the image's own Discover
    autostart entry generates, and carry nothing but the live-medium
    conditions in [Unit]. Returns the unit name.
    """
    if DISCOVER_NOTIFIER_AUTOSTART not in inventory:
        raise RuntimeError(
            f"/{DISCOVER_NOTIFIER_AUTOSTART} is absent: KDE's update notifier moved "
            "or was renamed, and the live-medium drop-in may match no unit"
        )
    unit = xdg_autostart_unit(DISCOVER_NOTIFIER_AUTOSTART)
    if PurePosixPath(DISCOVER_NOTIFIER_DROPIN).parent.name != f"{unit}.d":
        raise RuntimeError(
            f"the live-medium drop-in /{DISCOVER_NOTIFIER_DROPIN} is not for {unit}, "
            f"the unit /{DISCOVER_NOTIFIER_AUTOSTART} generates"
        )
    section = None
    conditions: set[tuple[str, str]] = set()
    for raw in dropin.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        key, separator, value = line.partition("=")
        if section != "Unit" or not separator:
            raise RuntimeError(
                f"update-notifier drop-in has more than [Unit] conditions: {line!r}"
            )
        conditions.add((key.strip(), value.strip()))
    if ("ConditionPathExists", "!/run/live/medium") not in conditions:
        raise RuntimeError(
            "update-notifier drop-in does not skip the live medium "
            "(ConditionPathExists=!/run/live/medium)"
        )
    unexpected = sorted(conditions - LIVE_MEDIUM_CONDITIONS)
    if unexpected:
        raise RuntimeError(
            "update-notifier drop-in could also stop the notifier on installed "
            "systems: " + ", ".join(f"{key}={value}" for key, value in unexpected)
        )
    return unit


def validate_partition_contract(partition: str) -> dict:
    """Require firmware-native tables, one ESP, clear /boot and encrypted root."""
    try:
        document = yaml.safe_load(partition)
    except yaml.YAMLError as exc:
        raise RuntimeError(f"Calamares partition config is not valid YAML: {exc}") from exc
    if not isinstance(document, dict):
        raise RuntimeError("Calamares partition config is not an object")
    efi = document.get("efi")
    expected_efi = {
        "mountPoint": "/boot/efi",
        "recommendedSize": "512MiB",
        "minimumSize": "300MiB",
        "label": "EFI",
    }
    if not isinstance(efi, dict) or any(efi.get(key) != value for key, value in expected_efi.items()):
        raise RuntimeError(f"Calamares EFI settings mismatch: {efi!r}")
    layout = document.get("partitionLayout")
    if not isinstance(layout, list) or not all(isinstance(item, dict) for item in layout):
        raise RuntimeError("Calamares partitionLayout is not a list of objects")
    duplicate_esps = [
        item
        for item in layout
        if item.get("mountPoint") == "/boot/efi"
        or str(item.get("type", "")).lower() == "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
    ]
    if duplicate_esps:
        raise RuntimeError(f"partitionLayout duplicates Calamares' EFI partition: {duplicate_esps!r}")
    if document.get("defaultPartitionTableType") not in (None, ""):
        raise RuntimeError("Calamares must select msdos for BIOS and GPT for UEFI")
    if document.get("createHybridBootloaderLayout") is True:
        raise RuntimeError("Calamares hybrid partition layout is not firmware-native")
    bios = [item for item in layout if item.get("name") == "bios_grub"]
    boots = [item for item in layout if item.get("mountPoint") == "/boot"]
    roots = [item for item in layout if item.get("mountPoint") == "/"]
    if bios:
        raise RuntimeError(f"partitionLayout must not synthesize a GPT BIOS boot partition: {bios!r}")
    if (
        len(boots) != 1
        or boots[0].get("filesystem") != "ext4"
        or boots[0].get("noEncrypt") is not True
        or boots[0].get("size") != "2G"
    ):
        raise RuntimeError(f"partitionLayout clear /boot contract mismatch: {boots!r}")
    if (
        len(roots) != 1
        or roots[0].get("filesystem") != "btrfs"
        or roots[0].get("noEncrypt") is True
    ):
        raise RuntimeError(f"partitionLayout Btrfs root contract mismatch: {roots!r}")
    return document


def validate_grub_installer_contract(installer: str) -> None:
    """Reject mapper-unsafe disk guessing and cross-firmware GRUB installs."""
    required = (
        "physical_disk_for()",
        "node=${1%%\\[*}",
        'lsblk -s -nro NAME "$node"',
        "for mountpoint in /boot/efi /boot /; do",
        'lsblk -dnro TYPE "$disk"',
        "grub-install --target=i386-pc --recheck \"$DISK\"",
        "grub-install --target=x86_64-efi --efi-directory=/boot/efi",
        "GRUB_PC_DEB=$GRUB_PC_DIR/grub-pc.deb",
        "sha256sum --check grub-pc.deb.sha256",
        "dpkg-deb --field \"$GRUB_PC_DEB\" Version",
        "dpkg --remove grub-efi-amd64",
        "dpkg --install \"$GRUB_PC_DEB\"",
        "grub-pc grub-pc/install_devices multiselect $DISK",
        "rm -rf \"$GRUB_PC_DIR\"",
    )
    missing = [token for token in required if token not in installer]
    if missing:
        raise RuntimeError(f"GRUB installer physical-disk contract is incomplete: {missing!r}")
    forbidden = (
        "grub-probe --target=device / | sed",
        "sfdisk --part-type",
        "i386-pc bonus install",
    )
    present = [token for token in forbidden if token in installer]
    if present:
        raise RuntimeError(f"GRUB installer retains unsafe legacy logic: {present!r}")


def validate_grub_package_payload(squashfs: Path) -> None:
    """Verify the exact offline grub-pc package carried for BIOS installs."""
    package_path = "usr/share/shadowfetch/installer-packages/grub-pc.deb"
    checksum_path = package_path + ".sha256"
    package = squash_cat(squashfs, package_path, binary=True)
    checksum = squash_cat(squashfs, checksum_path)
    status_text = squash_cat(squashfs, "var/lib/dpkg/status")
    assert isinstance(package, bytes) and isinstance(checksum, str)
    assert isinstance(status_text, str)

    match = re.fullmatch(r"([0-9a-f]{64})  grub-pc\.deb\n?", checksum)
    actual = hashlib.sha256(package).hexdigest()
    if not match or match.group(1) != actual:
        raise RuntimeError("offline BIOS GRUB package checksum mismatch")

    installed = {
        record["Package"]: record
        for record in parse_deb822(status_text)
        if record.get("Status") == "install ok installed"
    }
    grub_pc_bin = installed.get("grub-pc-bin")
    if not grub_pc_bin:
        raise RuntimeError("live image does not contain grub-pc-bin")

    with tempfile.TemporaryDirectory(prefix="shadowfetch-grub-pc-") as temporary:
        local = Path(temporary) / "grub-pc.deb"
        local.write_bytes(package)
        metadata = {
            field: output(program("dpkg-deb").argv("--field", str(local), field))
            for field in ("Package", "Version", "Architecture")
        }
    expected = {
        "Package": "grub-pc",
        "Version": grub_pc_bin.get("Version"),
        "Architecture": grub_pc_bin.get("Architecture"),
    }
    if metadata != expected:
        raise RuntimeError(
            f"offline BIOS GRUB package metadata mismatch: expected={expected}, got={metadata}"
        )
    print(
        "PASS: offline BIOS GRUB package checksum and metadata match "
        f"grub-pc-bin {metadata['Version']} {metadata['Architecture']}"
    )


def artifact_gate(iso: Path, marker: Path) -> None:
    checksum = Path(str(iso) + ".sha256")
    signature = Path(str(iso) + ".asc")
    public_key = ROOT / "repo/shadowfetch.gpg.asc"
    for path in (iso, checksum, signature, public_key, marker):
        if not path.is_file():
            raise RuntimeError(f"missing release artifact: {path}")

    marker_ns = marker.stat().st_mtime_ns
    stale = [path for path in (iso, checksum, signature) if path.stat().st_mtime_ns <= marker_ns]
    if stale:
        raise RuntimeError("release artifacts predate the build marker: " + ", ".join(map(str, stale)))

    line = checksum.read_text(encoding="utf-8").strip()
    match = re.fullmatch(r"([0-9a-f]{64})  (\S+)", line)
    if not match or match.group(2) != iso.name:
        raise RuntimeError(f"malformed checksum sidecar: {line!r}")
    actual = sha256(iso)
    if actual != match.group(1):
        raise RuntimeError(f"ISO checksum mismatch: expected {match.group(1)}, got {actual}")
    print(f"PASS: ISO SHA256 {actual} size={iso.stat().st_size}")

    with tempfile.TemporaryDirectory(prefix="shadowfetch-iso-keyring-") as temporary:
        keyring = Path(temporary) / "shadowfetch.gpg"
        run(
            "dearmor release signing key",
            program("gpg").argv(
                "--batch", "--yes", "--dearmor", "--output", str(keyring), str(public_key)
            ),
        )
        fingerprints = re.findall(
            r"^fpr:+([0-9A-F]+):$",
            output(program("gpg").argv("--batch", "--with-colons", "--show-keys", str(keyring))),
            re.MULTILINE,
        )
        if RELEASE.signing_fingerprint not in fingerprints:
            raise RuntimeError(f"release key fingerprint mismatch: {fingerprints}")
        run(
            "detached ISO signature",
            program("gpgv").argv("--keyring", str(keyring), str(signature), str(iso)),
        )
    print(f"PASS: release signing fingerprint {RELEASE.signing_fingerprint}")


def boot_gate(iso: Path) -> None:
    report = run(
        "El Torito and system-area inspection",
        program("xorriso").argv(
            "-indev", str(iso), "-report_el_torito", "plain", "-report_system_area", "plain"
        ),
        capture=True,
    ).stdout or ""
    requirements = {
        "volume label": "Volume id    : 'SHADOWFETCH'",
        "El Torito": "Boot record  : El Torito",
        "BIOS boot image": "BIOS  y",
        "UEFI boot image": "UEFI  y",
        "BIOS image path": "/boot/grub/i386-pc/eltorito.img",
        "UEFI image path": "/efi.img",
        "protective MBR": "MBR protective-msdos-label",
        "GPT hybrid": "GPT",
    }
    missing = [label for label, needle in requirements.items() if needle not in report]
    if missing:
        raise RuntimeError("ISO boot structure is incomplete: " + ", ".join(missing))
    print("PASS: BIOS and UEFI hybrid boot structure")


@contextmanager
def mounted_iso(iso: Path) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="shadowfetch-iso-mount-") as temporary:
        mountpoint = Path(temporary)
        mounted = False
        try:
            run(
                "read-only ISO loop mount",
                command_for_privilege(
                    program("mount").argv(
                        "-o", "loop,ro,nosuid,nodev,noexec", str(iso), str(mountpoint)
                    )
                ),
            )
            mounted = True
            mount_info = output(
                program("findmnt").argv("-no", "FSTYPE,OPTIONS", "--target", str(mountpoint))
            )
            fields = mount_info.split(None, 1)
            if not fields or fields[0] != "iso9660" or len(fields) < 2:
                raise RuntimeError(f"unexpected ISO mount: {mount_info}")
            options = set(fields[1].split(","))
            for required in ("ro", "nosuid", "nodev", "noexec"):
                if required not in options:
                    raise RuntimeError(f"ISO mount lacks {required}: {mount_info}")
            print(f"PASS: read-only mount options {mount_info}")
            yield mountpoint
        finally:
            if mounted:
                run(
                    "ISO unmount",
                    command_for_privilege(program("umount").argv(str(mountpoint))),
                )


def internal_manifest_gate(mountpoint: Path) -> Path:
    manifest = mountpoint / "SHA256SUMS"
    if not manifest.is_file():
        raise RuntimeError("ISO has no internal SHA256SUMS")
    names: set[str] = set()
    for index, raw in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        match = re.fullmatch(r"[0-9a-f]{64}  (.+)", raw)
        if not match:
            raise RuntimeError(f"invalid internal checksum line {index}: {raw!r}")
        name = match.group(1).removeprefix("./")
        pure = PurePosixPath(name)
        if pure.is_absolute() or ".." in pure.parts or name in names:
            raise RuntimeError(f"unsafe or duplicate checksum target: {name}")
        names.add(name)
    missing = sorted(REQUIRED_IMAGE_FILES - names)
    if missing:
        raise RuntimeError("internal checksum manifest misses final image files: " + ", ".join(missing))
    run(
        f"internal ISO checksums ({len(names)} files)",
        program("sha256sum").argv("--check", "--strict", "--quiet", "SHA256SUMS"),
        cwd=mountpoint,
    )
    for name in REQUIRED_IMAGE_FILES:
        if not (mountpoint / name).is_file():
            raise RuntimeError(f"required ISO file is absent: {name}")
    print("PASS: kernel, initrd, squashfs, final GRUB config and Umbra theme are covered")
    return mountpoint / "live/filesystem.squashfs"


def squashfs_inventory(squashfs: Path) -> tuple[dict[str, str], str]:
    size = squashfs.stat().st_size
    if size > MAX_SQUASHFS_BYTES:
        raise RuntimeError(f"squashfs exceeds the 4 GiB file ceiling: {size}")
    stats = run(
        "squashfs metadata",
        program("unsquashfs").argv("-s", str(squashfs)),
        capture=True,
    ).stdout or ""
    if not re.search(r"(?im)^Compression\s+xz\s*$", stats):
        raise RuntimeError("squashfs is not xz-compressed")

    print("\n>>> squashfs path and mode inventory")
    process = subprocess.Popen(
        program("unsquashfs").argv("-lln", str(squashfs)),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    inventory: dict[str, str] = {}
    pattern = re.compile(
        r"^(\S+)\s+\d+/\d+\s+\d+\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+"
        r"squashfs-root(?:/(.*))?$"
    )
    diagnostic: list[str] = []
    for raw in process.stdout:
        line = raw.rstrip("\n")
        match = pattern.match(line)
        if not match:
            if line:
                diagnostic.append(line)
            continue
        path = (match.group(2) or "").split(" -> ", 1)[0]
        if path:
            inventory[path] = match.group(1)
    if process.wait() != 0:
        raise RuntimeError("could not inventory squashfs: " + " | ".join(diagnostic[-10:]))
    if len(inventory) < 10000:
        raise RuntimeError(f"implausibly small squashfs inventory: {len(inventory)} paths")
    print(f"PASS: squashfs size={size} headroom={MAX_SQUASHFS_BYTES - size} paths={len(inventory)}")
    return inventory, stats


def squash_cat(squashfs: Path, path: str, *, binary: bool = False) -> str | bytes:
    result = subprocess.run(
        program("unsquashfs").argv("-cat", str(squashfs), path),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if binary:
        return result.stdout
    return result.stdout.decode("utf-8")


def forbidden_build_time_packages(installed: set[str]) -> list[str]:
    return sorted(installed & FORBIDDEN_BUILD_TIME_PACKAGES)


def custom_packages(installed: dict[str, str], release: gate.ReleaseData) -> dict[str, str]:
    """The installed packages this project answers for, with their versions.

    Shadowfetch's own packages and grub-btrfs by name, plus every prebuilt
    package the release declares (ShadowCode). Selecting the prebuilt ones by
    the release data -- not by name pattern -- is what makes a missing or
    wrong-version shadow-code a mismatch against image_packages(): it is in the
    expected set through binary_versions, so it has to be in this one.
    """
    prebuilt = set(release.prebuilt)
    return {
        package: version
        for package, version in installed.items()
        if package.startswith("shadowfetch-") or package == "grub-btrfs"
        or package in prebuilt
    }


def package_gate(squashfs: Path) -> None:
    status_text = squash_cat(squashfs, "var/lib/dpkg/status")
    assert isinstance(status_text, str)
    installed = {
        record["Package"]: record["Version"]
        for record in parse_deb822(status_text)
        if record.get("Status") == "install ok installed"
    }
    custom = custom_packages(installed, RELEASE)
    expected_custom = image_packages(RELEASE)
    if custom != expected_custom:
        missing = sorted(set(expected_custom) - set(custom))
        extra = sorted(set(custom) - set(expected_custom))
        mismatched = sorted(
            package
            for package in set(custom) & set(expected_custom)
            if custom[package] != expected_custom[package]
        )
        raise RuntimeError(
            f"installed custom package mismatch; missing={missing}, extra={extra}, "
            f"versions={[(name, custom[name]) for name in mismatched]}"
        )
    if installed.get("drkonqi") != UPSTREAM_VERSION:
        raise RuntimeError("DrKonqi differs from the tested upstream package " + UPSTREAM_VERSION)
    retired = sorted(package for package in installed if RETIRED_PACKAGE.search(package))
    if retired:
        raise RuntimeError("retired runtime packages remain installed: " + ", ".join(retired))
    nvidia = sorted(package for package in installed if PROPRIETARY_NVIDIA_PACKAGE.search(package))
    if nvidia:
        raise RuntimeError("proprietary NVIDIA driver packages are preinstalled: " + ", ".join(nvidia))
    if "buzz" in installed or "shadowfetch-nvidia" in installed:
        raise RuntimeError("Buzz or the deferred NVIDIA metapackage was installed without consent")
    if "grub-efi-amd64" not in installed or "grub-pc-bin" not in installed or "grub-pc" in installed:
        raise RuntimeError("live image GRUB package baseline is not hybrid-media safe")
    build_time_packages = forbidden_build_time_packages(set(installed))
    if build_time_packages:
        raise RuntimeError(
            "unpinned build-time downloader packages are installed: "
            + ", ".join(build_time_packages)
        )
    if "systemd-timesyncd" not in installed:
        raise RuntimeError("systemd-timesyncd is not installed")
    print(
        f"PASS: installed package contract ({len(installed)} total, "
        f"{len(custom)} exact Shadowfetch packages, no retired runtime, proprietary NVIDIA driver, "
        "or build-time downloader)"
    )


SHADOWCODE_PARITY = (shadowcode.LAUNCHER, shadowcode.LLAMA_SERVER, shadowcode.DESKTOP_FILE)


def misplaced_local_runtime(inventory: dict[str, str], *, allowed: bool) -> list[str]:
    """llama.cpp / ggml runtime files the image may not carry.

    With ShadowCode shipped (`allowed`), exactly one tree may hold them:
    /usr/lib/shadowcode/, its private bundled runtime. Anywhere else -- /usr/bin,
    a system library directory, another package's tree -- they are the retired
    local-inference stack coming back, and still refused. Without ShadowCode
    there is no permitted location at all.
    """
    return sorted(
        path for path in inventory
        if shadowcode.llama_family(path)
        and not (allowed and path.startswith(shadowcode.RUNTIME_PREFIX))
    )


def shadowcode_gate(squashfs: Path, inventory: dict[str, str]) -> None:
    """ShadowCode is installed, launchable, and byte-identical to the signed .deb.

    The package gate authenticated build/shadow-code_<v>_amd64.deb against the
    upstream publisher key. Here the INSTALLED files are compared with that
    archive, after re-checking its size and SHA-256 against the pin, so a stale
    live-build cache entry or a same-version republication cannot pass.
    """
    pin = shadowcode.load_pin()
    for path in (shadowcode.LAUNCHER, shadowcode.DESKTOP_FILE, shadowcode.LLAMA_SERVER):
        if path not in inventory:
            raise RuntimeError(f"ShadowCode is not installed in the image: /{path} is absent")
    for path in (shadowcode.LAUNCHER, shadowcode.LLAMA_SERVER):
        if "x" not in inventory[path]:
            raise RuntimeError(f"ShadowCode program is not executable: /{path}")
    shadowcode.check_file(pin.build_deb, pin.deb, "ShadowCode .deb in build/")
    with tempfile.TemporaryDirectory(prefix="shadowfetch-shadowcode-parity-") as temporary:
        root = Path(temporary)
        subprocess.run(
            program("dpkg-deb").argv("--extract", str(pin.build_deb), str(root)),
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        for path in SHADOWCODE_PARITY:
            installed = squash_cat(squashfs, path, binary=True)
            assert isinstance(installed, bytes)
            if (root / path).read_bytes() != installed:
                raise RuntimeError(
                    f"installed /{path} differs from the signed ShadowCode {pin.version} archive"
                )
    print(
        f"PASS: ShadowCode {pin.version} installed; {len(SHADOWCODE_PARITY)} files match "
        "the upstream-signed archive"
    )


def critical_payload_parity_gate(squashfs: Path) -> None:
    """Reject a live-build cache hit whose version matches but payload is stale."""
    with tempfile.TemporaryDirectory(prefix="shadowfetch-package-parity-") as temporary:
        extraction_root = Path(temporary)
        checked = 0
        for package, paths in CRITICAL_PACKAGE_PAYLOADS.items():
            version = image_packages(RELEASE)[package]
            matches = sorted((ROOT / "build").glob(f"{package}_{version}_*.deb"))
            if len(matches) != 1:
                raise RuntimeError(
                    f"expected one freshly built archive for {package} {version}, found {matches}"
                )
            package_root = extraction_root / package
            subprocess.run(
                program("dpkg-deb").argv("--extract", str(matches[0]), str(package_root)),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for path in paths:
                packaged = package_root / path
                if not packaged.is_file():
                    raise RuntimeError(f"critical package payload is absent: {package}:/{path}")
                installed = squash_cat(squashfs, path, binary=True)
                assert isinstance(installed, bytes)
                if packaged.read_bytes() != installed:
                    raise RuntimeError(
                        f"installed payload differs from fresh {package} archive: /{path}"
                    )
                checked += 1
    print(f"PASS: {checked} critical installed helpers match freshly built package payloads")


def drkonqi_gate(squashfs: Path) -> None:
    dropin = squash_cat(squashfs, DROPIN)
    assert isinstance(dropin, str)
    validate_dropin(dropin)
    for path in UPSTREAM_UNITS:
        content = squash_cat(squashfs, path, binary=True)
        assert isinstance(content, bytes)
        validate_upstream_unit(path, content)
    print("PASS: pickup-only override; original upstream pickup and per-crash units intact")


def retired_path(path: str) -> bool:
    lowered = path.lower()
    exact_prefixes = (
        "etc/openclaw/",
        "etc/hermes/",
        "etc/ollama/",
        "opt/openclaw/",
        "opt/hermes/",
        "usr/share/openclaw/",
        "usr/share/ollama/",
        "usr/share/llama.cpp/",
        "var/lib/openclaw/",
        "var/lib/hermes/",
        "var/lib/ollama/",
        # 5.0: Hermes and OpenClaw are optional PER-USER installs. Seeding their
        # state into new homes, or a system-wide copy, is preinstallation.
        "etc/skel/.hermes",
        "etc/skel/.openclaw",
        "etc/skel/.npm-global",
        "etc/skel/.local/share/shadowfetch/openclaw",
        "root/.hermes",
        "root/.openclaw",
        "usr/lib/node_modules/openclaw/",
        "usr/local/lib/node_modules/openclaw/",
        "usr/local/lib/hermes-agent/",
    )
    if lowered.startswith(exact_prefixes):
        return True
    basename = PurePosixPath(lowered).name
    if lowered.startswith(("usr/bin/", "usr/sbin/", "usr/local/bin/")) and (
        basename == "openclaw"
        or basename == "hermes"
        or basename == "ollama"
        or basename.startswith("llama-")
    ):
        return True
    if lowered.startswith(("etc/systemd/", "usr/lib/systemd/")) and re.search(
        r"(?:openclaw|hermes|ollama|shadowfetch-llama|llama-server)", basename
    ):
        return True
    if lowered.startswith("usr/libexec/") and re.search(
        r"(?:openclaw|hermes|ollama|shadowfetch-llama)", basename
    ):
        return True
    return False


def payload_gate(squashfs: Path, inventory: dict[str, str]) -> None:
    missing = sorted(REQUIRED_ROOT_FILES - set(inventory))
    if missing:
        raise RuntimeError("required installed-image files are absent: " + ", ".join(missing))
    bad_modes = sorted(
        path for path in REQUIRED_EXECUTABLES if "x" not in inventory[path]
    )
    if bad_modes:
        raise RuntimeError("installed helpers are not executable: " + ", ".join(bad_modes))

    retired = sorted(path for path in inventory if retired_path(path))
    if retired:
        raise RuntimeError("retired runtime paths remain in squashfs: " + ", ".join(retired))
    ships_shadowcode = shadowcode.PACKAGE in RELEASE.prebuilt
    misplaced = misplaced_local_runtime(inventory, allowed=ships_shadowcode)
    if misplaced:
        raise RuntimeError(
            "llama.cpp/ggml runtime files outside "
            + (f"/{shadowcode.RUNTIME_PREFIX}" if ships_shadowcode else "any permitted location")
            + ": " + ", ".join(misplaced)
        )
    models = sorted(
        path for path in inventory if path.lower().endswith((".gguf", ".safetensors"))
    )
    if models:
        raise RuntimeError("model weights were embedded in the ISO: " + ", ".join(models))
    secrets = sorted(path for path in inventory if SECRET_PATH.search(path))
    if secrets:
        raise RuntimeError("private credentials or keys were embedded: " + ", ".join(secrets))

    mission_source = squash_cat(squashfs, "usr/lib/shadowfetch/missions/sf_missions.py")
    def _read(relative):
        try:
            return squash_cat(squashfs, relative)
        except Exception:
            return None
    result = validate_provider_payload(inventory, mission_source, read=_read)
    print("PASS: provider manifests validated: " + ", ".join(result["providers"]))
    # Was "local AI stack absent" -- false once ShadowCode's bundled llama.cpp
    # ships. What the checks above do establish is stated instead.
    if ships_shadowcode:
        print(
            "PASS: no Ollama/Open WebUI/llama.cpp packages, no model weights; the only "
            f"local model runtime is ShadowCode's, confined to /{shadowcode.RUNTIME_PREFIX}"
        )
    else:
        print("PASS: local AI stack absent; Codex cloud and offline media capabilities")
    passport = squash_cat(squashfs, "usr/bin/shadowfetch-passport")
    recovery_sources = squash_cat(
        squashfs, "usr/share/shadowfetch/apt-recovery/debian.sources"
    )
    guide = squash_cat(
        squashfs, "usr/share/shadowfetch/control-center/sfcc/guide_page.py"
    )
    launcher = squash_cat(squashfs, "usr/share/applications/shadowfetch-guide.desktop")
    workbench = squash_cat(squashfs, "usr/bin/shadowfetch-workbench")
    workbench_page = squash_cat(
        squashfs, "usr/share/shadowfetch/control-center/sfcc/workbench_page.py"
    )
    workbench_manifest = squash_cat(
        squashfs, "usr/share/shadowfetch/workbench/profiles.json"
    )
    assert all(isinstance(item, str) for item in (
        passport, recovery_sources, guide, launcher,
        workbench, workbench_page, workbench_manifest
    ))
    for token in (
        '"local_only": True', '"upload_performed": False',
        "privacy_issues(document)", "shadowfetch-facts", "--output",
    ):
        if token not in passport:
            raise RuntimeError(f"System Passport contract is absent: {token}")
    if "shadowfetch-passport" not in guide or "Nothing is uploaded" not in guide:
        raise RuntimeError("Guide UI is not wired to the private Passport")
    if "Exec=shadowfetch-control --page guide" not in launcher:
        raise RuntimeError("Guide launcher does not open the Guide route")
    print("PASS: Shadowfetch Guide and private System Passport are installed")
    profiles = json.loads(workbench_manifest).get("profiles", [])
    expected_profiles = list(RELEASE.section("workbench")["profiles"])
    if [profile.get("id") for profile in profiles] != expected_profiles:
        raise RuntimeError(
            f"Workbench profile allowlist differs from {RELEASE.version}"
        )
    if 'subprocess.run(["pkexec", str(helper), "install"' not in workbench:
        raise RuntimeError("Workbench bypasses the protected bundle installer")
    if "class WorkbenchPage" not in workbench_page:
        raise RuntimeError("Workbench Control Center page is absent")
    print("PASS: Workbench CLI, GUI and four-profile contract are installed")
    if "deb-src http://deb.debian.org/debian/ testing " not in recovery_sources:
        raise RuntimeError("Phoenix recovery sources differ from installed-system policy")
    print("PASS: Phoenix source-repair payload matches installed-system policy")


def identity_and_installer_gate(squashfs: Path, inventory: dict[str, str]) -> None:
    version = squash_cat(squashfs, "usr/share/shadowfetch/version")
    os_release = squash_cat(squashfs, "etc/os-release")
    canonical = squash_cat(squashfs, "usr/share/shadowfetch/os-release.shadowfetch")
    assert isinstance(version, str) and isinstance(os_release, str) and isinstance(canonical, str)
    identity = RELEASE.section("identity")
    if version.strip() != RELEASE.version:
        raise RuntimeError(f"version file reports {version.strip()!r}")
    for label, content in (("/etc/os-release", os_release), ("canonical os-release", canonical)):
        values = parse_os_release(content)
        required = {
            "NAME": identity["os_release_name"],
            "ID": identity["os_release_id"],
            "VERSION_ID": RELEASE.version,
            "VERSION_CODENAME": RELEASE.codename,
        }
        mismatched = {key: values.get(key) for key, expected in required.items() if values.get(key) != expected}
        if mismatched or RELEASE.display_codename not in values.get("PRETTY_NAME", ""):
            raise RuntimeError(f"{label} identity mismatch: {mismatched or values.get('PRETTY_NAME')}")

    apt_source = squash_cat(squashfs, "etc/apt/sources.list.d/shadowfetch.list")
    assert isinstance(apt_source, str)
    if apt_source.strip() != identity["apt_source_line"]:
        raise RuntimeError(f"unexpected installed APT source: {apt_source.strip()!r}")
    key_paths = sorted(
        path
        for path in inventory
        if re.fullmatch(r"etc/apt/trusted\.gpg\.d/shadowfetch[^/]*\.(?:gpg|key)", path)
    )
    if not key_paths:
        raise RuntimeError("installed image has no Shadowfetch APT signing key")
    fingerprints: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="shadowfetch-installed-key-") as temporary:
        for index, key_path in enumerate(key_paths):
            content = squash_cat(squashfs, key_path, binary=True)
            assert isinstance(content, bytes)
            local = Path(temporary) / f"key-{index}.gpg"
            local.write_bytes(content)
            listing = output(
                program("gpg").argv("--batch", "--with-colons", "--show-keys", str(local))
            )
            fingerprints.update(re.findall(r"^fpr:+([0-9A-F]+):$", listing, re.MULTILINE))
    if RELEASE.signing_fingerprint not in fingerprints:
        raise RuntimeError(f"installed APT key fingerprint mismatch: {sorted(fingerprints)}")

    shellprocess = squash_cat(squashfs, "etc/calamares/modules/shellprocess.conf")
    settings = squash_cat(squashfs, "etc/calamares/settings.conf")
    partition = squash_cat(squashfs, "etc/calamares/modules/partition.conf")
    branding = squash_cat(squashfs, "etc/calamares/branding/debian/branding.desc")
    slideshow = squash_cat(squashfs, "etc/calamares/branding/debian/show.qml")
    grub_installer = squash_cat(squashfs, "usr/local/sbin/sf-install-grub")
    desktop_icon = squash_cat(squashfs, "usr/bin/add-calamares-desktop-icon")
    hostkey_dropin = squash_cat(
        squashfs,
        "etc/systemd/system/sshd-keygen.service.d/10-shadowfetch-hostkeys.conf",
    )
    cleanup = squash_cat(squashfs, "usr/local/sbin/sf-remove-live-user")
    nossh = squash_cat(squashfs, "etc/systemd/system/shadowfetch-live-nossh.service")
    ufw = squash_cat(squashfs, "etc/ufw/ufw.conf")
    firstboot = squash_cat(squashfs, "usr/lib/shadowfetch/firstboot.sh")
    assert all(
        isinstance(item, str)
        for item in (
            shellprocess,
            settings,
            partition,
            branding,
            slideshow,
            grub_installer,
            desktop_icon,
            hostkey_dropin,
            cleanup,
            nossh,
            ufw,
            firstboot,
        )
    )
    for token in RELEASE.stamped_tokens("installer_slideshow"):
        if token not in slideshow:
            raise RuntimeError(f"Calamares flagship slideshow contract is absent: {token}")
    if "windowSize: 920px,640px" not in branding:
        raise RuntimeError("Calamares flagship window size is not installed")
    print(
        f"PASS: {RELEASE.version} {RELEASE.edition} installer presentation is installed"
    )
    if '"sh /usr/local/sbin/sf-remove-live-user"' not in shellprocess or "|| true" in shellprocess:
        raise RuntimeError("Calamares does not fail closed on live-account cleanup")
    validate_calamares_exec_sequence(settings)
    validate_partition_contract(partition)
    validate_grub_installer_contract(grub_installer)
    validate_grub_package_payload(squashfs)
    if "Shadowfetch replacement for Debian's live-session desktop-icon helper" not in desktop_icon:
        raise RuntimeError("the corrected Calamares desktop-icon helper is not installed")
    if not re.search(r"(?m)^ConditionFirstBoot=$", hostkey_dropin) or (
        "ConditionPathExists=!/etc/ssh/ssh_host_ed25519_key" not in hostkey_dropin
    ):
        raise RuntimeError("installed SSH host-key generation is not first-install safe")
    for required in (
        "LIVE_USER=shadow",
        'rm -rf -- "/home/${LIVE_USER:?}"',
        'grep -q "^${LIVE_USER}:" /etc/shadow',
    ):
        if required not in cleanup:
            raise RuntimeError(f"live-user cleanup lacks verification: {required}")
    if "ConditionPathExists=/run/live/medium" not in nossh or "mask ssh.service ssh.socket" not in nossh:
        raise RuntimeError("live-session SSH hardening is incomplete")
    if not inventory["etc/systemd/system/sysinit.target.wants/shadowfetch-live-nossh.service"].startswith("l"):
        raise RuntimeError("live-session SSH hardening service is not enabled")
    notifier_dropin = squash_cat(squashfs, DISCOVER_NOTIFIER_DROPIN)
    assert isinstance(notifier_dropin, str)
    notifier_unit = validate_live_notifier_dropin(inventory, notifier_dropin)
    print(f"PASS: {notifier_unit} (KDE's update notifier) is skipped on the live medium only")
    if not re.search(r"(?m)^ENABLED=yes$", ufw):
        raise RuntimeError("UFW is not enabled in the live image")
    if (
        "timedatectl set-local-rtc 0 --adjust-system-clock" not in firstboot
        or "timedatectl set-local-rtc 1" in firstboot
        or "systemctl enable --now systemd-timesyncd.service" not in firstboot
    ):
        raise RuntimeError("first boot does not enforce UTC RTC and network time")
    if "etc/sudoers.d/shadowfetch-live-shadow" not in inventory:
        raise RuntimeError("live account contract changed without updating installer cleanup QA")
    common_session = squash_cat(squashfs, "etc/pam.d/common-session")
    login_keyring, default_keyring, keyring_unlock = (
        squash_cat(squashfs, path) if path in inventory else ""
        for path in (
            f"{LIVE_KEYRINGS}/login.keyring", f"{LIVE_KEYRINGS}/default", LIVE_KEYRING_UNLOCK
        )
    )
    assert all(
        isinstance(item, str)
        for item in (common_session, login_keyring, default_keyring, keyring_unlock)
    )
    validate_login_keyring_contract(
        common_session, inventory, login_keyring, default_keyring, keyring_unlock
    )
    print(
        "PASS: version, APT trust, Calamares cleanup, UFW, live-session SSH hardening "
        "and the login keyring"
    )


def main(argv: list[str] | None = None) -> int:
    global RELEASE
    parser = argparse.ArgumentParser(description=__doc__)
    gate.add_version_argument(parser)
    parser.add_argument("--iso", type=Path, default=None)
    parser.add_argument("--marker", type=Path, default=None)
    args = parser.parse_args(argv)
    RELEASE = gate.load_release(args.version)
    iso = (args.iso or ROOT / RELEASE.iso_name).resolve()
    marker = (
        args.marker or ROOT / f"build/.live-build-{RELEASE.version}-started"
    ).resolve()
    os.environ.setdefault("LC_ALL", "C.UTF-8")

    resolver = gate.ProgramResolver()
    for resolved in resolver.require(REQUIRED_PROGRAMS):
        PROGRAMS[resolved.name] = resolved
    print(
        f"Shadowfetch Linux {RELEASE.version} ISO gate: {iso.name} "
        f"(data: {RELEASE.path.name})"
    )

    # Refuses a release the ShadowCode pin ships in whose data file does not
    # declare it, rather than skipping every ShadowCode check below.
    ships_shadowcode = shadowcode.check_release_linkage(RELEASE.version, RELEASE.document)

    artifact_gate(iso, marker)
    boot_gate(iso)
    with mounted_iso(iso) as mountpoint:
        squashfs = internal_manifest_gate(mountpoint)
        inventory, _ = squashfs_inventory(squashfs)
        package_gate(squashfs)
        critical_payload_parity_gate(squashfs)
        if ships_shadowcode:
            shadowcode_gate(squashfs, inventory)
        drkonqi_gate(squashfs)
        payload_gate(squashfs, inventory)
        build_leak_gate(squashfs, inventory)
        identity_and_installer_gate(squashfs, inventory)
    print("\nISO_GATE_PASSED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError, UnicodeDecodeError) as exc:
        print(f"ISO_GATE_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
