#!/usr/bin/env python3
"""Validate a Shadowfetch Linux release's packages and signed APT repository.

ONE implementation for every release. The package allowlist, the source set,
the container smoke commands and every version-stamped literal are DATA in
tools/release/versions/<version>.toml -- before Stage Q each of those lived as
17 hand-edited "<version>-1" strings inside a copied module.

Every program is resolved to a trusted absolute path by gate.ProgramResolver:
gpgv decides whether the repository signature is genuine and dpkg-deb decides
what the packages contain, so neither may come from a PATH lookup.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile

import gate
from gate import ROLE_QUALITY, ROLE_SECURITY, output, run, sha256

from providers.validate_manifest import validate_provider_payload

import ship_list_check

import shadowcode

from drkonqi_pickup_contract import (
    DROPIN, HELPER, PACKAGE as PICKUP_PACKAGE, validate_dropin, validate_package_paths,
)


ROOT = gate.ROOT
BUILD = ROOT / "build"
REPO = ROOT / "repo"
# Where `make repo` publishes the prebuilt packages' upstream source material.
# Under pool/ so the publisher (which uploads every pool file) carries it.
THIRD_PARTY_SOURCE = REPO / "pool/third-party-source"

# dpkg-deb reads the payload the gate then judges, dpkg-source reproduces the
# corresponding source, and gpg/gpgv decide whether the index and every .dsc are
# genuinely signed. lintian and podman decide quality and installability facts.
# bash runs the vendored upstream ShadowCode verifier and openssl is what that
# verifier checks the Ed25519 publisher signature with.
REQUIRED_PROGRAMS = (
    ("bash", ROLE_SECURITY),
    ("dpkg-deb", ROLE_SECURITY),
    ("dpkg-source", ROLE_SECURITY),
    ("gpg", ROLE_SECURITY),
    ("gpgv", ROLE_SECURITY),
    ("openssl", ROLE_SECURITY),
    ("desktop-file-validate", ROLE_QUALITY),
    ("lintian", ROLE_QUALITY),
    ("podman", ROLE_QUALITY),
)

# Set once in main(). The gate functions below read the release through these
# rather than taking eight parameters each; the copied modules used file-scope
# constants for exactly this data, so this keeps the call sites unchanged.
RELEASE: gate.ReleaseData
PROGRAMS: dict[str, gate.TrustedProgram] = {}


def program(name: str) -> gate.TrustedProgram:
    try:
        return PROGRAMS[name]
    except KeyError:  # pragma: no cover - a programming error, not a gate failure
        raise RuntimeError(f"{name} was not resolved before use") from None


RETIRED_RUNTIME = re.compile(
    rb"\bollama\b|open[- ]?webui|llama\.cpp|llama-server",
    re.IGNORECASE,
)
# 5.0: Hermes Agent and OpenClaw are optional, per-user installs made by two
# helpers after consent. They may be NAMED only by the files below, and no
# package may carry them or pull them in.
OPTIONAL_AGENT = re.compile(rb"openclaw|\bhermes\b", re.IGNORECASE)
OPTIONAL_AGENT_PAYLOAD = {
    "usr/bin/shadowfetch-hermes": "shadowfetch-defaults",
    "usr/bin/shadowfetch-openclaw": "shadowfetch-defaults",
    "usr/bin/shadowfetch-agent-network": "shadowfetch-defaults",
    "usr/lib/shadowfetch/desktop/sf_desktop.py": "shadowfetch-defaults",
    "usr/share/shadowfetch/control-center/sfcc/optional_agents_page.py": "shadowfetch-control-center",
    "usr/share/shadowfetch/control-center/sfcc/pages.py": "shadowfetch-control-center",
    "usr/bin/shadowfetch-welcome": "shadowfetch-welcome",
}
OPTIONAL_AGENT_PAYLOAD_PREFIXES = (
    "usr/share/shadowfetch/openclaw/",   # the distro-generated OpenClaw lockfiles
    "usr/share/doc/",
)
PACKAGE_RELATIONSHIP_FIELDS = ("Depends", "Pre-Depends", "Recommends", "Suggests", "Enhances", "Provides")


def optional_agent_payload_allowed(relative: str, owners: list[str] | None = None) -> bool:
    expected = OPTIONAL_AGENT_PAYLOAD.get(relative)
    if expected is not None:
        return owners is None or owners == [expected]
    if relative.startswith("usr/share/shadowfetch/openclaw/"):
        return owners is None or owners == ["shadowfetch-defaults"]
    return relative.startswith(OPTIONAL_AGENT_PAYLOAD_PREFIXES)


def optional_agent_relationships(package: str, fields: dict[str, str]) -> list[str]:
    """Relationship fields of one binary package that name Hermes or OpenClaw."""
    return [f"{package} {field}" for field in PACKAGE_RELATIONSHIP_FIELDS
            if OPTIONAL_AGENT.search(fields.get(field, "").encode())]


def _literal(text: str, name: str) -> str:
    match = re.search(rf'^{name} = "([^"]+)"$', text, re.MULTILINE)
    if not match:
        raise RuntimeError(f"optional-agent helper has no {name} pin")
    return match.group(1)


def check_optional_agent_payload(owners: dict[str, list[str]], extracted: Path) -> None:
    """The two helpers ship with their release pins, and nothing else carries the agents."""
    required = {
        "usr/bin/shadowfetch-hermes": "shadowfetch-defaults",
        "usr/bin/shadowfetch-openclaw": "shadowfetch-defaults",
        "usr/share/shadowfetch/control-center/sfcc/optional_agents_page.py": "shadowfetch-control-center",
    }
    for path, owner in required.items():
        if owners.get(path) != [owner]:
            raise RuntimeError(f"optional-agent payload missing or wrong owner: {path}")
    pins = RELEASE.section("pinned_artifacts")
    for helper, key in (("usr/bin/shadowfetch-hermes", "hermes"), ("usr/bin/shadowfetch-openclaw", "openclaw")):
        text = (extracted / helper).read_text(encoding="utf-8")
        for token in pins[key]:
            if token not in text:
                raise RuntimeError(f"{helper} release pin is absent: {token}")
        # Nothing in either helper may ask for administrator rights.
        for forbidden in ("/usr/bin/sudo", "/usr/bin/pkexec", "/usr/bin/doas", "/usr/bin/run0"):
            if forbidden in text:
                raise RuntimeError(f"{helper} contains an escalation path: {forbidden}")
    openclaw = (extracted / "usr/bin/shadowfetch-openclaw").read_text(encoding="utf-8")
    version = _literal(openclaw, "OPENCLAW_VERSION")
    folder = f"usr/share/shadowfetch/openclaw/{version}"
    for name, pin in (("package-lock.json", "LOCKFILE_SHA256"), ("package.json", "PACKAGE_JSON_SHA256")):
        path = f"{folder}/{name}"
        if owners.get(path) != ["shadowfetch-defaults"]:
            raise RuntimeError(f"OpenClaw lockfile payload missing or wrong owner: {path}")
        if sha256(extracted / path) != _literal(openclaw, pin):
            raise RuntimeError(f"{path} does not match the helper's {pin}")
    lock = json.loads((extracted / folder / "package-lock.json").read_text(encoding="utf-8"))
    entry = (lock.get("packages") or {}).get("node_modules/openclaw") or {}
    if entry.get("version") != version or entry.get("integrity") != _literal(openclaw, "OPENCLAW_INTEGRITY"):
        raise RuntimeError("the shipped OpenClaw lockfile does not pin the helper's version and integrity")
    stray = sorted(path for path, packages in owners.items()
                   if OPTIONAL_AGENT.search(path.encode()) and not optional_agent_payload_allowed(path, packages))
    if stray:
        raise RuntimeError("optional agents must not be packaged: " + ", ".join(stray))
MIGRATION_MANIFEST_PATH = (
    "usr/share/shadowfetch/migrations/2.1.3-ai-packages"
)
EXPECTED_MIGRATION_MANIFEST = b"""shadowfetch-ai-workspace
llama.cpp
llama.cpp-services
llama.cpp-tools
llama.cpp-tools-extra
libllama0
whisper.cpp
libwhisper1
whisper.cpp-tools
"""


def parse_deb822(path: Path) -> list[dict[str, str]]:
    return gate.parse_deb822(path.read_text(encoding="utf-8"))


def container_script(release: gate.ReleaseData) -> str:
    """The Debian 13 install script. Pure, so a test can read it without podman."""
    codename = release.codename
    binaries = release.binary_versions
    smoke = release.smoke_install
    all_packages = " ".join(sorted(binaries))
    smoke_packages = " ".join(smoke)
    candidate_checks = " ".join(
        f"{package}={version}" for package, version in sorted(binaries.items())
    )
    installed_checks = " ".join(f"{package}={binaries[package]}" for package in smoke)
    # STAGE W. Everything else in this script names every package explicitly,
    # so none of it can notice a metapackage that does not install the product.
    # This solve is given ONE name and --no-install-recommends, and the real apt
    # resolver decides: through 4.0.0 it would have planned a Plasma desktop
    # with no Mission Control, no Phoenix, no Fireproof and no Firewatch,
    # because only live-build's package list ever named them.
    # A prebuilt package the release declares (ShadowCode) is in the product
    # for the same reason a pillar is: shadowfetch-desktop has to pull it in,
    # or `apt install shadowfetch-desktop` off the ISO quietly lacks it.
    pillars = " ".join(sorted({*ship_list_check.REQUIRED_PILLARS, *release.prebuilt}))
    container_smoke = release.section("packages")["container_smoke"]
    present = "\n".join(f"{command} >/dev/null" for command in container_smoke["present"])
    absent = "\n".join(f"[ ! -e {path} ]" for path in container_smoke["absent"])
    return f"""
set -eux
export DEBIAN_FRONTEND=noninteractive
# The disposable slim container excludes translations/docs by default. Install
# complete packages here so the upstream DrKonqi integrity check is meaningful.
rm -f /etc/dpkg/dpkg.cfg.d/docker
apt-get update
apt-get install -y --no-install-recommends ca-certificates gnupg
gpg --batch --dearmor --output /usr/share/keyrings/shadowfetch.gpg /repo/shadowfetch.gpg.asc
printf '%s\n' 'deb [signed-by=/usr/share/keyrings/shadowfetch.gpg] file:/repo {codename} main' > /etc/apt/sources.list.d/shadowfetch.list
apt-get update
for item in {candidate_checks}; do
    package=${{item%%=*}}
    expected=${{item#*=}}
    candidate=$(apt-cache policy "$package" | awk '/Candidate:/ {{print $2; exit}}')
    [ "$candidate" = "$expected" ] || {{ echo "$package: expected candidate $expected, got $candidate" >&2; exit 1; }}
done
apt-get --simulate install {all_packages}
apt-get --simulate --no-install-recommends install shadowfetch-desktop > /tmp/desktop-plan
for pillar in {pillars}; do
    grep -q "^Inst $pillar " /tmp/desktop-plan || {{ echo "shadowfetch-desktop does not install $pillar" >&2; exit 1; }}
done
apt-get install -y --no-install-recommends {smoke_packages}
for item in {installed_checks}; do
    package=${{item%%=*}}
    expected=${{item#*=}}
    actual=$(dpkg-query -W -f='${{Version}}' "$package")
    [ "$actual" = "$expected" ] || {{ echo "$package: expected installed $expected, got $actual" >&2; exit 1; }}
done
{present}
{absent}
[ -z "$(dpkg --verify drkonqi)" ]
dpkg --audit
echo DEBIAN13_PACKAGE_INSTALL_PASS
"""


def package_inventory() -> dict[str, Path]:
    dpkg_deb = program("dpkg-deb")
    expected_binaries = RELEASE.binary_versions
    package_paths: dict[str, Path] = {}
    for deb in sorted(BUILD.glob("*.deb")):
        package = output(dpkg_deb.argv("-f", str(deb), "Package"))
        version = output(dpkg_deb.argv("-f", str(deb), "Version"))
        architecture = output(dpkg_deb.argv("-f", str(deb), "Architecture"))
        if package in package_paths:
            raise RuntimeError(f"duplicate binary package artifact: {package}")
        if architecture not in {"all", "amd64"}:
            raise RuntimeError(f"{package}: unexpected architecture {architecture}")
        package_paths[package] = deb
        print(f"PACKAGE {package} {version} {architecture} sha256={sha256(deb)}")
    if set(package_paths) != set(expected_binaries):
        missing = sorted(set(expected_binaries) - set(package_paths))
        extra = sorted(set(package_paths) - set(expected_binaries))
        raise RuntimeError(f"binary allowlist mismatch; missing={missing}, extra={extra}")
    for package, expected in expected_binaries.items():
        actual = output(dpkg_deb.argv("-f", str(package_paths[package]), "Version"))
        if actual != expected:
            raise RuntimeError(f"{package}: expected {expected}, got {actual}")
    print(f"PASS: exact binary inventory ({len(package_paths)} packages)")
    return package_paths


def prebuilt_owned(owners: dict[str, list[str]], prebuilt: set[str]) -> set[str]:
    """Payload paths owned ONLY by declared prebuilt packages (exempt from the
    retired-runtime text scan; see payload_gate)."""
    return {
        relative for relative, packages in owners.items()
        if packages and set(packages) <= prebuilt
    }


def shadowcode_prebuilt_gate(package_paths: dict[str, Path]) -> None:
    """Re-authenticate the ShadowCode .deb in build/ -- never trust the fetch.

    The fetch tool verified these bytes when it staged them; this gate does not
    take its word for it. The vendored upstream verifier checks the Ed25519
    publisher signature over RELEASE-AUTH against the vendored key and the
    key's authorised version interval, then the asset's size and SHA-256; this
    module then checks that the signed document agrees with the pin, and that
    shadowfetch-desktop's floor names the pinned version.
    """
    pin = shadowcode.load_pin()
    shadowcode.check_in_policy(pin.version, pin.key_id)
    deb = package_paths[shadowcode.PACKAGE]
    if deb.resolve() != pin.build_deb.resolve():
        raise RuntimeError(f"ShadowCode .deb is {deb.name}, expected {pin.build_deb.name}")
    line = shadowcode.verify_pinned_artifact(pin, deb, "deb", bash=str(program("bash").path))
    print(f"PASS: {line}")
    floor = shadowcode.meta_floor(shadowcode.META_CONTROL.read_text(encoding="utf-8"))
    if floor != pin.version:
        raise RuntimeError(
            f"shadowfetch-desktop requires shadow-code (>= {floor}) but the pin is "
            f"{pin.version}; run tools/bump_shadowcode.py {pin.version}"
        )
    print(f"PASS: ShadowCode {pin.version} signed by {pin.key_id[:12]}..., pinned, "
          f"and required by shadowfetch-desktop")


def shadowcode_payload_gate(owners: dict[str, list[str]], extracted: Path) -> None:
    """ShadowCode's own payload, and the ONE place a local model runtime may live."""
    package = shadowcode.PACKAGE
    required = (shadowcode.LAUNCHER, shadowcode.DESKTOP_FILE,
                shadowcode.LLAMA_SERVER, shadowcode.LLAMA_CLI)
    wrong = [path for path in required if owners.get(path) != [package]]
    if wrong:
        raise RuntimeError("ShadowCode payload missing or wrong owner: " + ", ".join(wrong))
    misplaced = sorted(
        f"{path} ({'+'.join(packages)})"
        for path, packages in owners.items()
        if shadowcode.llama_family(path)
        and (packages != [package] or not path.startswith(shadowcode.RUNTIME_PREFIX))
    )
    if misplaced:
        raise RuntimeError(
            "llama.cpp/ggml runtime files outside ShadowCode's private runtime "
            f"directory /{shadowcode.RUNTIME_PREFIX}: " + ", ".join(misplaced)
        )
    entry = (extracted / shadowcode.DESKTOP_FILE).read_text(encoding="utf-8")
    if "\nExec=shadowcode" not in "\n" + entry:
        raise RuntimeError("ShadowCode desktop entry does not launch /usr/bin/shadowcode")
    print(f"PASS: ShadowCode payload; its llama.cpp runtime is confined to "
          f"/{shadowcode.RUNTIME_PREFIX}")


def payload_gate(package_paths: dict[str, Path], extracted: Path) -> None:
    owners: dict[str, list[str]] = defaultdict(list)
    built: dict[str, set[str]] = defaultdict(set)
    executable_candidates: list[tuple[str, int]] = []
    for package, deb in sorted(package_paths.items()):
        process = subprocess.Popen(
            program("dpkg-deb").argv("--fsys-tarfile", str(deb)),
            stdout=subprocess.PIPE,
        )
        assert process.stdout is not None
        with tarfile.open(fileobj=process.stdout, mode="r|*") as archive:
            for member in archive:
                relative = member.name.removeprefix("./").rstrip("/")
                if not relative:
                    continue
                if member.isfile() or member.issym() or member.islnk():
                    owners[relative].append(package)
                    built[package].add(relative)
                if member.isfile() and (
                    relative.startswith(("usr/bin/", "usr/sbin/", "usr/libexec/"))
                    or "/usr/libexec/" in "/" + relative
                ):
                    executable_candidates.append((relative, member.mode))
        if process.wait() != 0:
            raise RuntimeError(f"could not inspect payload for {package}")
        subprocess.run(program("dpkg-deb").argv("-x", str(deb), str(extracted)), check=True)

    # STAGE W. The ship lists are a description of the packages until they are
    # checked against the ones that were built. A .deb carrying a file no
    # debian/*.install names, or a ship list naming a file the build dropped,
    # fails here -- the source-side reverse manifest in check_all() cannot see
    # either, because it reads the .install text and not the artifact.
    ship_list_check.check_built_payload(ship_list_check.build_ship_lists(), dict(built))

    duplicates = {path: value for path, value in owners.items() if len(value) > 1}
    if duplicates:
        sample = ", ".join(f"{path}={value}" for path, value in sorted(duplicates.items())[:10])
        raise RuntimeError(f"duplicate package file ownership: {sample}")
    print(f"PASS: unique file ownership ({len(owners)} payload paths)")

    bad_modes = [path for path, mode in executable_candidates if not mode & 0o111]
    if bad_modes:
        raise RuntimeError("non-executable program payloads: " + ", ".join(sorted(bad_modes)))
    print(f"PASS: executable modes ({len(executable_candidates)} program payloads)")

    prebuilt = set(RELEASE.prebuilt)
    if shadowcode.PACKAGE in prebuilt:
        shadowcode_payload_gate(dict(owners), extracted)

    pickup_paths = [path for path, packages in owners.items() if PICKUP_PACKAGE in packages]
    validate_package_paths(pickup_paths)
    for path in (HELPER, DROPIN):
        if owners.get(path) != [PICKUP_PACKAGE]:
            raise RuntimeError("Pickup correction has missing or wrong file owner: " + path)
        if (extracted / path).is_symlink() or not (extracted / path).is_file():
            raise RuntimeError("Pickup correction must contain a regular payload file: " + path)
    validate_dropin((extracted / DROPIN).read_text())
    if (extracted / HELPER).read_bytes()[:4] != b"\x7fELF":
        raise RuntimeError("DrKonqi pickup helper must be a compiled ELF executable")
    print("PASS: compiled pickup helper owns only its narrow service override")

    release_payload = {
        "usr/bin/shadowfetch-missions": "shadowfetch-missions",
        "usr/lib/shadowfetch/missions/sf_missions.py": "shadowfetch-missions",
        "usr/lib/systemd/user/shadowfetch-missions.service": "shadowfetch-missions",
        "usr/bin/shadowfetch-grok-bot": "shadowfetch-defaults",
        "usr/share/shadowfetch/grok-bot/release.json": "shadowfetch-defaults",
        "usr/share/applications/shadowfetch-mission-control.desktop": "shadowfetch-control-center",
        "usr/share/applications/shadowfetch-grok-bot-setup.desktop": "shadowfetch-control-center",
        "usr/share/kio/servicemenus/shadowfetch-mission.desktop": "shadowfetch-control-center",
        "usr/share/shadowfetch/control-center/sfcc/missions_page.py": "shadowfetch-control-center",
        "usr/share/shadowfetch/control-center/sfcc/grok_bot_page.py": "shadowfetch-control-center",
    }
    for path, owner in release_payload.items():
        if owners.get(path) != [owner]:
            raise RuntimeError(
                f"{RELEASE.version} package payload missing or wrong owner: {path}"
            )
    def _read(relative):
        try:
            return (extracted / relative).read_text(encoding="utf-8")
        except OSError:
            return None
    result = validate_provider_payload(owners, (extracted / "usr/lib/shadowfetch/missions/sf_missions.py").read_text(), read=_read)
    print("PASS: provider manifests validated: " + ", ".join(result["providers"]))
    # 5.0.0: this used to say "local AI stack absent", which stops being true
    # the moment ShadowCode ships its bundled llama.cpp runtime. What IS still
    # true -- and asserted by the retired-runtime scan below and by
    # shadowcode_payload_gate -- is that no Shadowfetch-built package carries a
    # local model runtime; the only one is inside the prebuilt ShadowCode tree.
    print("PASS: Mission Control/Grok payload ownership; no Shadowfetch-built "
          "package carries a local model runtime")

    required_guide_payload = {
        "usr/bin/shadowfetch-passport",
        "usr/share/applications/shadowfetch-guide.desktop",
        "usr/share/shadowfetch/control-center/sfcc/guide_page.py",
    }
    missing_guide = sorted(required_guide_payload - set(owners))
    if missing_guide:
        raise RuntimeError(
            "Shadowfetch Guide package payload is incomplete: "
            + ", ".join(missing_guide)
        )
    passport = (extracted / "usr/bin/shadowfetch-passport").read_text(
        encoding="utf-8"
    )
    for token in ('"local_only": True', '"upload_performed": False',
                  "privacy_issues(document)"):
        if token not in passport:
            raise RuntimeError(f"System Passport contract is absent: {token}")
    print("PASS: Shadowfetch Guide package payload and privacy contract")

    check_optional_agent_payload(owners, extracted)
    relationships: list[str] = []
    for package, deb in sorted(package_paths.items()):
        fields = {field: output(program("dpkg-deb").argv("-f", str(deb), field))
                  for field in PACKAGE_RELATIONSHIP_FIELDS}
        relationships.extend(optional_agent_relationships(package, fields))
    if relationships:
        raise RuntimeError("a package relationship pulls in an optional agent: " + ", ".join(relationships))
    print("PASS: Hermes/OpenClaw pinned helpers; no package depends on, recommends or carries them")

    required_workbench_payload = {
        "usr/bin/shadowfetch-workbench",
        "usr/share/applications/shadowfetch-workbench.desktop",
        "usr/share/doc/shadowfetch/WORKBENCH.md",
        "usr/share/shadowfetch/workbench/profiles.json",
        "usr/share/shadowfetch/control-center/sfcc/workbench_page.py",
        "usr/share/shadowfetch/welcome/catalog/workbench-software-studio.json",
        "usr/share/shadowfetch/welcome/catalog/workbench-ai-lab.json",
        "usr/share/shadowfetch/welcome/catalog/workbench-production-ops.json",
        "usr/share/shadowfetch/welcome/catalog/workbench-creative-ai.json",
    }
    # dh_compress gzips /usr/share/doc files over ~4KB (WORKBENCH.md and
    # GROK-BOT.md both cross it), so a doc's shipped path may be either <name>
    # or <name>.gz. Both mean "the doc is shipped"; requiring the uncompressed
    # path made the gate fail the day a doc grew past the threshold.
    def _shipped(path):
        return path in owners or (
            path.startswith("usr/share/doc/") and path + ".gz" in owners)
    missing_workbench = sorted(p for p in required_workbench_payload
                               if not _shipped(p))
    if missing_workbench:
        raise RuntimeError(
            "Workbench package payload is incomplete: "
            + ", ".join(missing_workbench)
        )
    manifest = json.loads(
        (extracted / "usr/share/shadowfetch/workbench/profiles.json").read_text(
            encoding="utf-8"
        )
    )
    profiles = manifest.get("profiles", [])
    expected_profiles = list(RELEASE.section("workbench")["profiles"])
    if [profile.get("id") for profile in profiles] != expected_profiles:
        raise RuntimeError(
            f"Workbench profile allowlist differs from {RELEASE.version}"
        )
    workbench = (extracted / "usr/bin/shadowfetch-workbench").read_text(
        encoding="utf-8"
    )
    for token in (
        'subprocess.run(["pkexec", str(helper), "install"',
        '"network_default": network_default',
        'if target.exists() or target.is_symlink()',
    ):
        if token not in workbench:
            raise RuntimeError(f"Workbench safety contract is absent: {token}")
    print("PASS: Workbench payload, profiles and privilege boundary")

    mcp = (extracted / "usr/lib/shadowfetch/mcp/sf_mcp.py").read_text(encoding="utf-8")
    for token in RELEASE.stamped_tokens("mcp_server"):
        if token not in mcp:
            raise RuntimeError(
                f"Fireline MCP protocol is not stamped for {RELEASE.version}: {token}"
            )
    print("PASS: Fireline MCP version; local model ignition absent")

    required_recovery_payload = {
        "usr/libexec/phoenix-apt-repair",
        "usr/share/shadowfetch/apt-recovery/KEYRING.README",
        "usr/share/shadowfetch/apt-recovery/debian.sources",
        "usr/share/shadowfetch/apt-recovery/umbra-archive-keyring.gpg",
        "usr/share/shadowfetch/apt-recovery/umbra.sources",
    }
    missing_recovery = sorted(required_recovery_payload - set(owners))
    if missing_recovery:
        raise RuntimeError(
            "Phoenix source-repair payload is incomplete: "
            + ", ".join(missing_recovery)
        )
    recovery_sources = (
        extracted / "usr/share/shadowfetch/apt-recovery/debian.sources"
    ).read_text(encoding="utf-8")
    if "deb-src http://deb.debian.org/debian/ testing " not in recovery_sources:
        raise RuntimeError("Phoenix Debian recovery sources omit installer source entries")
    print("PASS: Phoenix source-repair helper and recovery payload")

    # The retired-runtime text scan covers every package Shadowfetch BUILDS.
    # The one exemption is a file owned by a declared prebuilt package: the
    # ShadowCode .deb names its bundled llama.cpp runtime in its notices,
    # metainfo and COMMIT record, those bytes are fixed by the upstream
    # signature, and shadowcode_payload_gate has already confined the runtime
    # itself to usr/lib/shadowcode/. Exempt by OWNER, never by path pattern, so
    # a Shadowfetch package cannot launder a hit by writing under a ShadowCode
    # directory.
    exempt_owned = prebuilt_owned(owners, set(RELEASE.prebuilt))
    retired: list[str] = []
    optional: list[str] = []
    for path in extracted.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 4 * 1024 * 1024:
            continue
        relative = path.relative_to(extracted).as_posix()
        if relative in exempt_owned:
            continue
        try:
            content = path.read_bytes()
        except OSError:
            continue
        if b"\0" in content[:4096]:
            continue
        if relative == MIGRATION_MANIFEST_PATH:
            if content != EXPECTED_MIGRATION_MANIFEST:
                raise RuntimeError(
                    "2.1.3 migration manifest differs from the reviewed package set"
                )
            continue
        if RETIRED_RUNTIME.search(content):
            retired.append(relative)
        if OPTIONAL_AGENT.search(content) and not optional_agent_payload_allowed(relative, owners.get(relative)):
            optional.append(relative)
    if retired:
        raise RuntimeError("retired runtime residue in packages: " + ", ".join(sorted(retired)))
    if optional:
        raise RuntimeError("Hermes/OpenClaw named outside their optional-install allowlist: "
                           + ", ".join(sorted(optional)))
    print("PASS: retired runtime payload scan, optional-agent allowlist and exact migration manifest")

    desktop_files = [
        path
        for path in extracted.rglob("*.desktop")
        if "applications" in path.parts or "autostart" in path.parts
    ]
    run(
        f"desktop entry validation ({len(desktop_files)} files)",
        program("desktop-file-validate").argv(*map(str, desktop_files)),
    )


def repository_gate() -> list[Path]:
    codename = RELEASE.codename
    expected_binaries = RELEASE.binary_versions
    expected_sources = RELEASE.source_packages
    packages_index = REPO / f"dists/{codename}/main/binary-amd64/Packages"
    sources_index = REPO / f"dists/{codename}/main/source/Sources"
    inrelease = REPO / f"dists/{codename}/InRelease"
    for path in (packages_index, sources_index, inrelease, REPO / "shadowfetch.gpg.asc"):
        if not path.is_file():
            raise RuntimeError(f"missing repository artifact: {path}")

    binary_records = parse_deb822(packages_index)
    binary_versions = {record["Package"]: record["Version"] for record in binary_records}
    if binary_versions != expected_binaries:
        raise RuntimeError(f"repository binary index mismatch: {binary_versions}")
    source_records = parse_deb822(sources_index)
    sources = {record["Package"] for record in source_records}
    if sources != expected_sources:
        raise RuntimeError(
            f"repository source index mismatch; missing={sorted(expected_sources - sources)}, "
            f"extra={sorted(sources - expected_sources)}"
        )

    valid_line = next(
        (line for line in inrelease.read_text(encoding="utf-8").splitlines() if line.startswith("Valid-Until: ")),
        None,
    )
    if not valid_line:
        raise RuntimeError("InRelease has no Valid-Until")
    valid_until = parsedate_to_datetime(valid_line.split(": ", 1)[1]).astimezone(timezone.utc)
    remaining = (valid_until - datetime.now(timezone.utc)).total_seconds()
    if remaining < 7 * 24 * 60 * 60:
        raise RuntimeError(f"repository expires too soon: {valid_until.isoformat()}")

    dscs = sorted(BUILD.glob("src/*.dsc"))
    if len(dscs) != len(expected_sources):
        raise RuntimeError(f"expected {len(expected_sources)} dsc files, got {len(dscs)}")

    with tempfile.TemporaryDirectory(prefix="shadowfetch-keyring-") as temporary:
        keyring = Path(temporary) / "shadowfetch.gpg"
        run(
            "dearmor repository signing key",
            program("gpg").argv(
                "--batch", "--yes", "--dearmor",
                "--output", str(keyring), str(REPO / "shadowfetch.gpg.asc"),
            ),
        )
        run(
            "InRelease signature verification",
            program("gpgv").argv("--keyring", str(keyring), str(inrelease)),
        )
        for dsc in dscs:
            run(
                f"source descriptor signature {dsc.name}",
                program("gpgv").argv("--keyring", str(keyring), str(dsc)),
            )
    print(
        f"PASS: signed APT index ({len(binary_records)} binary, {len(source_records)} source, "
        f"valid_until={valid_until.isoformat()})"
    )

    with tempfile.TemporaryDirectory(prefix="shadowfetch-sources-") as temporary:
        destination = Path(temporary)
        extracted_sources: set[str] = set()
        for index, dsc in enumerate(dscs):
            source = next(
                line.split(":", 1)[1].strip()
                for line in dsc.read_text(encoding="utf-8").splitlines()
                if line.startswith("Source:")
            )
            extracted_sources.add(source)
            run(
                f"extract source {source}",
                program("dpkg-source").argv(
                    "-x", str(dsc), str(destination / f"{index:02d}-{source}")
                ),
            )
        if extracted_sources != expected_sources:
            raise RuntimeError(f"extracted source set mismatch: {extracted_sources}")
    print(f"PASS: corresponding source extraction ({len(dscs)} packages)")
    if shadowcode.PACKAGE in RELEASE.prebuilt:
        shadowcode_repository_gate(binary_records)
    return dscs


def shadowcode_repository_gate(binary_records: list[dict[str, str]]) -> None:
    """The repository serves the signed upstream bytes, and their source beside them.

    ShadowCode has no .dsc -- nothing in this tree builds it -- so it is absent
    from main/source by construction (source_packages never names it). What
    `make repo` publishes instead is the release's runtime-sources tarball and
    its signed metadata under pool/third-party-source/, re-verified here.
    """
    pin = shadowcode.load_pin()
    record = next(r for r in binary_records if r["Package"] == shadowcode.PACKAGE)
    if record.get("SHA256") != pin.deb.sha256 or int(record.get("Size", "0")) != pin.deb.bytes:
        raise RuntimeError(
            f"repository serves {shadowcode.PACKAGE} sha256={record.get('SHA256')} "
            f"size={record.get('Size')}, not the pinned signed upstream bytes"
        )
    pooled = REPO / record["Filename"]
    shadowcode.check_file(pooled, pin.deb, "pooled ShadowCode .deb")
    published = THIRD_PARTY_SOURCE / shadowcode.PACKAGE / pin.version
    tarball = published / pin.runtime_sources.filename
    for name in shadowcode.METADATA_FILES:
        if (published / name).read_bytes() != (pin.vendor_dir / name).read_bytes():
            raise RuntimeError(f"published {name} differs from vendor/shadowcode/{pin.version}")
    line = shadowcode.verify_pinned_artifact(
        pin, tarball, "runtime-sources", bash=str(program("bash").path)
    )
    print(f"PASS: {line}")
    # The .deb's own corresponding source: one git archive per input the signed
    # manifest names, each holding exactly that commit, and summed.
    sums = (published / shadowcode.SOURCE_SUMS).read_text(encoding="utf-8").splitlines()
    listed = {entry.split("  ", 1)[1]: entry.split("  ", 1)[0] for entry in sums if "  " in entry}
    for name, _url, commit in shadowcode.source_inputs(pin):
        archive = published / shadowcode.source_archive_name(name, commit)
        if shadowcode.archive_commit_id(archive) != commit:
            raise RuntimeError(f"{archive.name} is missing or does not hold {name} commit {commit}")
        if listed.get(archive.name) != shadowcode.sha256_file(archive):
            raise RuntimeError(f"{archive.name} is not the archive {shadowcode.SOURCE_SUMS} records")
    print(f"PASS: repository serves the pinned ShadowCode bytes; the .deb's source "
          f"and the runtime sources are published at {published.relative_to(REPO)}")


def prebuilt_lintian_file(name: str) -> Path:
    if name != shadowcode.PACKAGE:
        raise RuntimeError(f"no lintian exception list is defined for prebuilt {name}")
    return shadowcode.load_pin().vendor_dir / "lintian-accepted"


def prebuilt_lintian_accepted(name: str) -> set[str]:
    """Reviewed lintian error lines for a prebuilt package; empty if none."""
    try:
        text = prebuilt_lintian_file(name).read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    return {line for line in text.splitlines() if line.startswith("E: ")}


def container_install_gate() -> None:
    run(
        "Debian 13 dependency solve and runtime package install",
        program("podman").argv(
            "run",
            "--rm",
            "--volume",
            f"{REPO}:/repo:ro",
            "docker.io/library/debian:trixie-slim",
            "sh",
            "-c",
            container_script(RELEASE),
        ),
    )


def main(argv: list[str] | None = None) -> int:
    global RELEASE
    parser = argparse.ArgumentParser(description=__doc__)
    gate.add_version_argument(parser)
    parser.add_argument("--skip-container", action="store_true")
    args = parser.parse_args(argv)
    RELEASE = gate.load_release(args.version)
    os.environ.setdefault("LC_ALL", "C.UTF-8")

    # Resolve every program before any package is opened: a gate that discovers
    # halfway through that it cannot verify a signature has already printed a
    # page of PASS lines.
    resolver = gate.ProgramResolver()
    required = REQUIRED_PROGRAMS
    if args.skip_container:
        required = tuple(item for item in required if item[0] != "podman")
    for resolved in resolver.require(required):
        PROGRAMS[resolved.name] = resolved
    print(
        f"Shadowfetch Linux {RELEASE.version} package gate "
        f"(data: {RELEASE.path.name}, suite: {RELEASE.codename})"
    )

    # STAGE W. The package graph must describe the product: every pillar
    # reachable from shadowfetch-desktop through Depends, no live-build package
    # list quietly supplying one, no undeclared cross-package module drop, and
    # every payload file either shipped or declared unshipped. Cheapest check in
    # the gate and the one with the widest blast radius, so it runs first.
    ship_list_check.check_all()

    # A release the ShadowCode pin says it ships in must declare it, and vice
    # versa: otherwise every ShadowCode check below would be silently skipped.
    shadowcode.check_release_linkage(RELEASE.version, RELEASE.document)

    package_paths = package_inventory()
    if shadowcode.PACKAGE in RELEASE.prebuilt:
        shadowcode_prebuilt_gate(package_paths)
    with tempfile.TemporaryDirectory(prefix="shadowfetch-packages-") as temporary:
        payload_gate(package_paths, Path(temporary))
    # Lintian judges packages this tree builds. A prebuilt package's bytes are
    # fixed by its upstream signature, so its errors are fixed upstream; here
    # each one must be listed, as a reviewed exception, in
    # vendor/shadowcode/<version>/lintian-accepted, and any other error fails.
    prebuilt = set(RELEASE.prebuilt)
    run(
        "Lintian binary error gate",
        program("lintian").argv(
            "--display-level=error",
            *(str(path) for name, path in package_paths.items() if name not in prebuilt),
        ),
    )
    for name in sorted(prebuilt):
        report = subprocess.run(
            program("lintian").argv("--display-level=error", str(package_paths[name])),
            env=gate.trusted_env(), text=True, capture_output=True, check=False,
        )
        errors = [line for line in report.stdout.splitlines() if line.startswith("E: ")]
        accepted = prebuilt_lintian_accepted(name)
        unreviewed = sorted(set(errors) - accepted)
        print(f"\n>>> Lintian (prebuilt upstream bytes) {name}: {len(errors)} errors, "
              f"{len(errors) - len(unreviewed)} reviewed exceptions")
        for line in sorted(accepted - set(errors)):
            print(f"  fixed upstream, drop from the exception list: {line}")
        if unreviewed:
            for line in unreviewed:
                print(f"  UNREVIEWED {line}")
            raise RuntimeError(
                f"{name} has {len(unreviewed)} lintian error(s) nobody reviewed; fix them "
                f"upstream or list each in {prebuilt_lintian_file(name).relative_to(ROOT)}")
    repository_gate()
    if not args.skip_container:
        container_install_gate()
    print("\nPACKAGE_GATE_PASSED")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        print(f"PACKAGE_GATE_FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)
