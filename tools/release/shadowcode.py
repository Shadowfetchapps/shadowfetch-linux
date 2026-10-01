#!/usr/bin/env python3
"""ShadowCode: the one prebuilt third-party package Shadowfetch ships.

Every other package in the image is built here from a debian/ tree. ShadowCode
is not: its .deb is built by upstream CI (Shadowfetchapps/ShadowCode) and signed
there, and Shadowfetch republishes those exact bytes. So what this release can
honestly assert about it is not "we built it" but "these are the bytes the
upstream publisher signed, for the version we pinned", and every consumer --
the fetch tool, the bump tool, the package gate, the ISO gate, the smoke check
and the VM acceptance case -- asserts exactly that, through this module.

ONE SOURCE OF TRUTH. tools/release/shadowcode.toml holds the pin (version,
commit, per-asset size and SHA-256, signing key). Nothing else in the tree
repeats those values; the release data file only *names* the pin file under
[packages.prebuilt], and the shadowfetch-desktop dependency floor is rewritten
by tools/bump_shadowcode.py in the same step that rewrites the pin.

TRUST. Authenticity is decided by the upstream verifier
(vendor/shadowcode/upstream/verify-native-release.sh), copied byte for byte
from the reviewed ShadowCode commit recorded in vendor/shadowcode/README.md,
against the vendored trust policy and public key in vendor/shadowcode/trust/.
It checks the Ed25519 signature over RELEASE-AUTH, the key's authorised
version interval, the manifest and checksum digests, and the artifact's size
and SHA-256. This module then re-checks, independently and in Python, that the
authenticated RELEASE-AUTH says what the pin says. Two readers of the same
signed document have to agree.

Stdlib only: the gates, the fetch tool and the acceptance harness all import it.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import json
import re
import shutil
import subprocess
import tempfile
import tomllib
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[2]
PIN_FILE = ROOT / "tools/release/shadowcode.toml"
VENDOR = ROOT / "vendor/shadowcode"
TRUST_DIR = VENDOR / "trust"
UPSTREAM_DIR = VENDOR / "upstream"
VERIFIER = UPSTREAM_DIR / "verify-native-release.sh"
META_CONTROL = ROOT / "packages/shadowfetch-meta/debian/control"
CACHE_ROOT = ROOT / "build/cache/shadowcode"
BUILD_DIR = ROOT / "build"

PACKAGE = "shadow-code"
REPOSITORY = "Shadowfetchapps/ShadowCode"
REPOSITORY_ID = "1377099349"
OWNER_ID = "209457103"
TARGET = "x86_64-unknown-linux-gnu"
CHANNEL = "stable"

# The four signed metadata files a release publishes next to its assets. They
# are what gets vendored per version; the assets themselves never enter Git.
METADATA_FILES = ("RELEASE-AUTH", "RELEASE-AUTH.sig", "SHA256SUMS", "RELEASE-MANIFEST.json")

# Where the bundled local-model runtime lives inside the package, and the only
# place in the image the ISO gate lets llama.cpp / ggml files exist.
RUNTIME_PREFIX = "usr/lib/shadowcode/"
LAUNCHER = "usr/bin/shadowcode"
DESKTOP_FILE = "usr/share/applications/com.shadowfetch.shadowcode.desktop"
LLAMA_SERVER = RUNTIME_PREFIX + "llama-server"
LLAMA_CLI = RUNTIME_PREFIX + "llama-cli"

# Same PATH the release gates give their subprocesses (gate.SUBPROCESS_PATH).
# The upstream verifier resolves openssl, sha256sum, dd and stat through PATH,
# so PATH is part of what decides the verdict and must not be the builder's.
TRUSTED_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"

HEX64 = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
VERSION = re.compile(r"^(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})$")


class ShadowCodeError(RuntimeError):
    """A ShadowCode pin, signature or payload fact does not hold."""


def version_tuple(value: str) -> tuple[int, int, int]:
    match = VERSION.fullmatch(value)
    if not match:
        raise ShadowCodeError(f"not a stable x.y.z version: {value!r}")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def asset_names(version: str) -> dict[str, str]:
    """role -> upstream asset basename, exactly as the upstream verifier derives them."""
    return {
        "appimage": f"ShadowCode_{version}_amd64.AppImage",
        "deb": f"ShadowCode_{version}_amd64.deb",
        "runtime-sources": f"ShadowCode_{version}_appimage-runtime-sources.tar.gz",
    }


def release_url(tag: str, name: str) -> str:
    return f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"


# --------------------------------------------------------------------------
# The pin
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Asset:
    filename: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class Pin:
    path: Path
    package: str
    version: str
    tag: str
    commit: str
    key_id: str
    key_epoch: int
    trust_commit: str
    ships_in: tuple[str, ...]
    deb: Asset
    runtime_sources: Asset

    @property
    def vendor_dir(self) -> Path:
        return VENDOR / self.version

    @property
    def cache_dir(self) -> Path:
        return CACHE_ROOT / self.version

    @property
    def build_deb_name(self) -> str:
        """Debian's canonical file name, which is what build/ and the pool use."""
        return f"{self.package}_{self.version}_amd64.deb"

    @property
    def build_deb(self) -> Path:
        return BUILD_DIR / self.build_deb_name

    @property
    def dependency(self) -> str:
        return f"{self.package} (>= {self.version})"


def _asset(table: Any, label: str, path: Path) -> Asset:
    if not isinstance(table, dict):
        raise ShadowCodeError(f"{path}: [{label}] is missing")
    filename, digest, size = table.get("filename"), table.get("sha256"), table.get("bytes")
    if not isinstance(filename, str) or "/" in filename or not filename:
        raise ShadowCodeError(f"{path}: [{label}].filename must be a basename")
    if not isinstance(digest, str) or not HEX64.fullmatch(digest):
        raise ShadowCodeError(f"{path}: [{label}].sha256 must be 64 lower-case hex")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise ShadowCodeError(f"{path}: [{label}].bytes must be a positive integer")
    return Asset(filename, digest, size)


def load_pin(path: Path | None = None) -> Pin:
    path = path or PIN_FILE
    with path.open("rb") as handle:
        document = tomllib.load(handle)
    package = document.get("package")
    version = document.get("version")
    if package != PACKAGE:
        raise ShadowCodeError(f"{path}: package must be {PACKAGE!r}, got {package!r}")
    if not isinstance(version, str):
        raise ShadowCodeError(f"{path}: version is missing")
    version_tuple(version)
    table = document.get("shadowcode")
    if not isinstance(table, dict):
        raise ShadowCodeError(f"{path}: [shadowcode] is missing")
    tag, commit = table.get("tag"), table.get("commit")
    key_id, key_epoch = table.get("key_id"), table.get("key_epoch")
    trust_commit = table.get("trust_commit")
    ships_in = table.get("ships_in")
    if tag != f"v{version}":
        raise ShadowCodeError(f"{path}: tag {tag!r} does not name version {version}")
    for label, value in (("commit", commit), ("trust_commit", trust_commit)):
        if not isinstance(value, str) or not COMMIT.fullmatch(value):
            raise ShadowCodeError(f"{path}: {label} must be a full 40-hex commit")
    if not isinstance(key_id, str) or not HEX64.fullmatch(key_id):
        raise ShadowCodeError(f"{path}: key_id must be 64 lower-case hex")
    if not isinstance(key_epoch, int) or isinstance(key_epoch, bool) or key_epoch < 1:
        raise ShadowCodeError(f"{path}: key_epoch must be a positive integer")
    if not isinstance(ships_in, list) or not all(isinstance(v, str) for v in ships_in):
        raise ShadowCodeError(f"{path}: ships_in must be a list of release versions")
    names = asset_names(version)
    deb = _asset(table.get("deb"), "shadowcode.deb", path)
    runtime = _asset(table.get("runtime_sources"), "shadowcode.runtime_sources", path)
    if deb.filename != names["deb"] or runtime.filename != names["runtime-sources"]:
        raise ShadowCodeError(
            f"{path}: asset file names do not match version {version}: "
            f"{deb.filename}, {runtime.filename}"
        )
    return Pin(
        path=path,
        package=package,
        version=version,
        tag=tag,
        commit=commit,
        key_id=key_id,
        key_epoch=key_epoch,
        trust_commit=trust_commit,
        ships_in=tuple(ships_in),
        deb=deb,
        runtime_sources=runtime,
    )


PIN_TEMPLATE = '''\
# ShadowCode pin -- the ONE place the shipped ShadowCode version lives.
#
# WRITTEN BY tools/bump_shadowcode.py. Do not hand-edit a value: every field
# below is copied out of an upstream RELEASE-AUTH whose Ed25519 signature the
# bump verified against vendor/shadowcode/trust/, and the gates re-verify that
# signature and compare it with this file on every run. A hand edit that does
# not match the signed document fails the package gate.
#
# Consumers: tools/fetch_shadowcode.py (make packages / make repo),
# tools/release/package_gate.py, tools/release/iso_gate.py,
# tools/shadowcode_smoke.py and the shadowcode VM acceptance cases. The release
# data file only names this file ([packages.prebuilt] in versions/<v>.toml).

package = "{package}"
version = "{version}"

[shadowcode]
tag = "{tag}"
commit = "{commit}"
key_id = "{key_id}"
key_epoch = {key_epoch}
# ShadowCode commit the vendored trust policy, public key and verifier scripts
# were copied from. See vendor/shadowcode/README.md.
trust_commit = "{trust_commit}"
# Shadowfetch releases that preinstall this pin. A gate for a release named
# here refuses to run unless that release's data file declares the package.
ships_in = [{ships_in}]

[shadowcode.deb]
filename = "{deb_filename}"
sha256 = "{deb_sha256}"
bytes = {deb_bytes}

# The AppImage runtime's corresponding source. Fetched, verified and published
# beside the APT repository; see vendor/shadowcode/README.md for why it is not
# in main/source and why it is not the .deb's source.
[shadowcode.runtime_sources]
filename = "{rs_filename}"
sha256 = "{rs_sha256}"
bytes = {rs_bytes}
'''


def render_pin(
    *,
    version: str,
    commit: str,
    key_id: str,
    key_epoch: int,
    trust_commit: str,
    ships_in: Iterable[str],
    deb: Asset,
    runtime_sources: Asset,
) -> str:
    return PIN_TEMPLATE.format(
        package=PACKAGE,
        version=version,
        tag=f"v{version}",
        commit=commit,
        key_id=key_id,
        key_epoch=key_epoch,
        trust_commit=trust_commit,
        ships_in=", ".join(f'"{item}"' for item in ships_in),
        deb_filename=deb.filename,
        deb_sha256=deb.sha256,
        deb_bytes=deb.bytes,
        rs_filename=runtime_sources.filename,
        rs_sha256=runtime_sources.sha256,
        rs_bytes=runtime_sources.bytes,
    )


# --------------------------------------------------------------------------
# Signed documents
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseAuth:
    version: str
    tag: str
    commit: str
    key_id: str
    key_epoch: int
    manifest_sha256: str
    checksums_sha256: str
    assets: dict[str, Asset]  # role -> asset


def parse_release_auth(text: str) -> ReleaseAuth:
    """Structural parse of RELEASE-AUTH. Authenticity is the verifier's job."""
    rows = text.split("\n")
    if not text.endswith("\n") or rows[-1] != "":
        raise ShadowCodeError("RELEASE-AUTH needs a final newline")
    rows = rows[:-1]
    if len(rows) != 16 or rows[0] != "ShadowCode-Release-Auth-v1":
        raise ShadowCodeError("RELEASE-AUTH schema/field count")
    fields: dict[str, str] = {}
    order = ("repository", "repository-id", "owner-id", "version", "tag", "commit",
             "target", "channel", "key-id", "key-epoch", "manifest-sha256",
             "checksums-sha256")
    for index, name in enumerate(order, start=1):
        prefix = name + "="
        if not rows[index].startswith(prefix):
            raise ShadowCodeError(f"RELEASE-AUTH: expected {name} on line {index + 1}")
        fields[name] = rows[index][len(prefix):]
    expected = {
        "repository": REPOSITORY, "repository-id": REPOSITORY_ID,
        "owner-id": OWNER_ID, "target": TARGET, "channel": CHANNEL,
    }
    for name, value in expected.items():
        if fields[name] != value:
            raise ShadowCodeError(f"RELEASE-AUTH: {name} is {fields[name]!r}, not {value!r}")
    version = fields["version"]
    version_tuple(version)
    if fields["tag"] != f"v{version}":
        raise ShadowCodeError("RELEASE-AUTH: version/tag mismatch")
    names = asset_names(version)
    assets: dict[str, Asset] = {}
    for row, role in zip(rows[13:16], ("appimage", "deb", "runtime-sources")):
        if not row.startswith("asset="):
            raise ShadowCodeError("RELEASE-AUTH: missing asset line")
        parts = row[len("asset="):].split("\t")
        if len(parts) != 4 or parts[0] != role or parts[1] != names[role]:
            raise ShadowCodeError(f"RELEASE-AUTH: malformed {role} asset line")
        if not parts[2].isdigit() or not HEX64.fullmatch(parts[3]):
            raise ShadowCodeError(f"RELEASE-AUTH: malformed {role} size/digest")
        assets[role] = Asset(parts[1], parts[3], int(parts[2]))
    for name in ("commit",):
        if not COMMIT.fullmatch(fields[name]):
            raise ShadowCodeError("RELEASE-AUTH: invalid commit")
    for name in ("key-id", "manifest-sha256", "checksums-sha256"):
        if not HEX64.fullmatch(fields[name]):
            raise ShadowCodeError(f"RELEASE-AUTH: invalid {name}")
    return ReleaseAuth(
        version=version,
        tag=fields["tag"],
        commit=fields["commit"],
        key_id=fields["key-id"],
        key_epoch=int(fields["key-epoch"]),
        manifest_sha256=fields["manifest-sha256"],
        checksums_sha256=fields["checksums-sha256"],
        assets=assets,
    )


@dataclass(frozen=True)
class TrustKey:
    epoch: int
    key_id: str
    minimum: str
    maximum: str


def parse_policy(text: str) -> tuple[str, list[TrustKey]]:
    """(minimum-version, keys) from a ShadowCode-Release-Trust-v1 policy."""
    rows = text.rstrip("\n").split("\n")
    if not rows or rows[0] != "ShadowCode-Release-Trust-v1":
        raise ShadowCodeError("trust policy schema")
    values: dict[str, str] = {}
    keys: list[TrustKey] = []
    for row in rows[1:]:
        name, _, value = row.partition("=")
        if name == "key":
            epoch, key_id, minimum, maximum = value.split("\t")
            version_tuple(minimum)
            version_tuple(maximum)
            keys.append(TrustKey(int(epoch), key_id, minimum, maximum))
        else:
            values[name] = value
    if "minimum-version" not in values:
        raise ShadowCodeError("trust policy has no minimum-version")
    return values["minimum-version"], keys


def authorized_range(key_id: str, policy_path: Path | None = None) -> tuple[str, str]:
    """The version interval the vendored policy authorises key_id to sign."""
    policy_path = policy_path or TRUST_DIR / "policy"
    floor, keys = parse_policy(policy_path.read_text(encoding="ascii"))
    for key in keys:
        if key.key_id == key_id:
            low = max(version_tuple(floor), version_tuple(key.minimum))
            return ".".join(map(str, low)), key.maximum
    raise ShadowCodeError(f"key {key_id} is not in the vendored trust policy")


def check_in_policy(version: str, key_id: str, policy_path: Path | None = None) -> None:
    """Refuse, with the remedy, a version the vendored policy does not authorise.

    The upstream verifier refuses these too ("above key version interval"); this
    runs first so the refusal names what has to happen next instead.
    """
    low, high = authorized_range(key_id, policy_path)
    if not version_tuple(low) <= version_tuple(version) <= version_tuple(high):
        raise ShadowCodeError(
            f"ShadowCode {version} is outside the vendored trust policy, which "
            f"authorises key {key_id[:12]}... for {low} through {high} only. "
            "Extending that interval is a trust decision, not a bump: review the "
            "upstream release/trust/policy change at a PUBLISHED ShadowCode commit "
            "and re-vendor it with tools/bump_shadowcode.py --refresh-trust "
            "--trust-commit <sha> --shadowcode-checkout <path>, then bump."
        )


def pin_problems(auth: ReleaseAuth, pin: Pin) -> list[str]:
    """Every way an authenticated RELEASE-AUTH disagrees with the pin."""
    problems: list[str] = []
    pairs = (
        ("version", auth.version, pin.version),
        ("tag", auth.tag, pin.tag),
        ("commit", auth.commit, pin.commit),
        ("key id", auth.key_id, pin.key_id),
        ("key epoch", auth.key_epoch, pin.key_epoch),
    )
    for label, signed, pinned in pairs:
        if signed != pinned:
            problems.append(f"{label}: signed {signed!r}, pinned {pinned!r}")
    for role, pinned in (("deb", pin.deb), ("runtime-sources", pin.runtime_sources)):
        signed = auth.assets[role]
        if signed != pinned:
            problems.append(f"{role}: signed {signed}, pinned {pinned}")
    return problems


def check_file(path: Path, asset: Asset, label: str) -> None:
    """Independent size + SHA-256 check, in Python, of one artifact on disk."""
    if not path.is_file() or path.is_symlink():
        raise ShadowCodeError(f"{label}: {path} is not a regular file")
    size = path.stat().st_size
    if size != asset.bytes:
        raise ShadowCodeError(f"{label}: {path} is {size} bytes, expected {asset.bytes}")
    actual = sha256_file(path)
    if actual != asset.sha256:
        raise ShadowCodeError(f"{label}: {path} sha256 {actual}, expected {asset.sha256}")


def trusted_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("BASH_FUNC_")}
    env["PATH"] = TRUSTED_PATH
    env["LC_ALL"] = "C"
    env.pop("BASH_ENV", None)
    env.pop("ENV", None)
    return env


def run_verifier(
    metadata_dir: Path,
    artifact: Path,
    *,
    expect_version: str,
    expect_commit: str | None = None,
    previous_dir: Path | None = None,
    trust_dir: Path | None = None,
    verifier: Path | None = None,
    bash: str = "/bin/bash",
) -> str:
    """Run the vendored upstream verifier over metadata_dir + artifact.

    The verifier wants one bundle directory holding the metadata and the
    artifact under its upstream basename, and snapshots every input before it
    reads it. A private bundle is assembled here from copies, so a symlinked or
    concurrently replaced input can never be what it verifies. Returns the
    verifier's success line; raises ShadowCodeError with its refusal otherwise.
    """
    trust_dir = trust_dir or TRUST_DIR
    verifier = verifier or VERIFIER
    with tempfile.TemporaryDirectory(prefix="shadowcode-verify-") as temporary:
        bundle = Path(temporary) / "bundle"
        bundle.mkdir()
        for name in METADATA_FILES:
            source = metadata_dir / name
            if not source.is_file() or source.is_symlink():
                raise ShadowCodeError(f"missing signed metadata: {source}")
            shutil.copyfile(source, bundle / name)
        name = artifact.name
        shutil.copyfile(artifact, bundle / name)
        argv = [
            bash, str(verifier),
            "--bundle-dir", str(bundle),
            "--trust-dir", str(trust_dir),
            "--artifact", name,
            "--stage-dir", str(Path(temporary) / "stage"),
            "--expect-version", expect_version,
        ]
        if expect_commit:
            argv += ["--expect-commit", expect_commit]
        if previous_dir is not None:
            argv += ["--previous-dir", str(previous_dir)]
        result = subprocess.run(
            argv, env=trusted_env(), text=True, capture_output=True, check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
            raise ShadowCodeError(f"upstream verifier refused {name}: {detail}")
        return result.stdout.strip()


def verify_pinned_artifact(pin: Pin, artifact: Path, role: str, *, bash: str = "/bin/bash") -> str:
    """The full check every consumer runs: signature, pin agreement, bytes.

    `artifact` may carry any file name (build/ uses Debian's canonical one);
    it is verified under the upstream basename the signature names.
    """
    auth_path = pin.vendor_dir / "RELEASE-AUTH"
    if not auth_path.is_file():
        raise ShadowCodeError(f"no vendored signed metadata for {pin.version}: {pin.vendor_dir}")
    auth = parse_release_auth(auth_path.read_text(encoding="ascii"))
    problems = pin_problems(auth, pin)
    if problems:
        raise ShadowCodeError(
            f"{pin.path.name} disagrees with the signed RELEASE-AUTH: " + "; ".join(problems)
        )
    asset = pin.deb if role == "deb" else pin.runtime_sources
    check_file(artifact, asset, f"ShadowCode {role}")
    upstream_name = asset.filename
    if artifact.name != upstream_name:
        with tempfile.TemporaryDirectory(prefix="shadowcode-rename-") as temporary:
            staged = Path(temporary) / upstream_name
            shutil.copyfile(artifact, staged)
            return run_verifier(pin.vendor_dir, staged, expect_version=pin.version,
                                expect_commit=pin.commit, bash=bash)
    return run_verifier(pin.vendor_dir, artifact, expect_version=pin.version,
                        expect_commit=pin.commit, bash=bash)


# --------------------------------------------------------------------------
# Release data linkage
# --------------------------------------------------------------------------


def declared_in(release_document: dict[str, Any]) -> bool:
    prebuilt = release_document.get("packages", {}).get("prebuilt", {})
    return isinstance(prebuilt, dict) and PACKAGE in prebuilt


def check_release_linkage(release_version: str, release_document: dict[str, Any],
                          pin: Pin | None = None) -> bool:
    """True when this release ships ShadowCode. Refuses a half-declared state.

    A gate for a release the pin says ships ShadowCode must not silently skip
    every ShadowCode check because the release data file has not caught up.
    """
    pin = pin or load_pin()
    declared = declared_in(release_document)
    listed = release_version in pin.ships_in
    if listed and not declared:
        raise ShadowCodeError(
            f"{pin.path.name} says ShadowCode ships in {release_version}, but "
            f"versions/{release_version}.toml does not declare it. Add:\n"
            "    [packages.prebuilt]\n"
            f'    {PACKAGE} = "tools/release/shadowcode.toml"'
        )
    if declared and not listed:
        raise ShadowCodeError(
            f"versions/{release_version}.toml declares {PACKAGE} but "
            f"{pin.path.name} ships_in does not name {release_version}"
        )
    return declared


def meta_floor(control_text: str) -> str | None:
    """The version in shadowfetch-desktop's `shadow-code (>= X)` Depends, if any."""
    match = re.search(r"^ shadow-code \(>= ([0-9.]+)\),?$", control_text, re.MULTILINE)
    return match.group(1) if match else None


def llama_family(path: str) -> bool:
    """A llama.cpp / ggml runtime file, by name. Used by the ISO gate."""
    base = path.rsplit("/", 1)[-1].lower()
    return (
        base.startswith(("llama-", "libllama", "libggml", "libmtmd"))
        or base in {"llama-server", "llama-cli", "llama.cpp"}
        or "/llama.cpp/" in "/" + path.lower() + "/"
    )


# ---- corresponding source of the .deb (see tools/fetch_shadowcode.py) -----

GIT = "/usr/bin/git"
SHADOWCODE_GIT = f"https://github.com/{REPOSITORY}.git"
SPIRV_HEADERS_GIT = "https://github.com/KhronosGroup/SPIRV-Headers.git"
SOURCE_SUMS = "SOURCE-SHA256SUMS"


def source_archive_name(name: str, commit: str) -> str:
    return f"{name}-{commit}.tar.gz"


def source_inputs(pin: "Pin") -> list[tuple[str, str, str]]:
    """(name, git url, commit) for every source the pinned .deb is built from."""
    manifest = json.loads((pin.vendor_dir / "RELEASE-MANIFEST.json").read_text(encoding="utf-8"))
    if manifest.get("commit") != pin.commit:
        raise ShadowCodeError(
            f"RELEASE-MANIFEST.json names commit {manifest.get('commit')}, the pin {pin.commit}")
    runtime = dict(line.split("=", 1) for line in manifest["runtime_pin"].splitlines() if "=" in line)
    inputs = [("shadowcode", SHADOWCODE_GIT, pin.commit),
              ("llama.cpp", runtime["url"], runtime["commit"])]
    if runtime.get("spirv_headers_commit"):
        inputs.append(("spirv-headers", SPIRV_HEADERS_GIT, runtime["spirv_headers_commit"]))
    for name, url, commit in inputs:
        if not COMMIT.match(commit):
            raise ShadowCodeError(f"{name} commit {commit!r} is not a full 40-hex id")
        if not url.startswith("https://"):
            raise ShadowCodeError(f"{name} source url {url!r} is not https")
    return inputs


def archive_commit_id(path: Path) -> str:
    unpacked = subprocess.run(["/usr/bin/gzip", "-dc", str(path)], capture_output=True)
    if unpacked.returncode != 0:
        return ""
    # cwd="/": git 2.43's get-tar-commit-id fails when the current directory's
    # path is long (about 128 characters), which made the result depend on
    # where the caller happened to be standing.
    result = subprocess.run([GIT, "get-tar-commit-id"], input=unpacked.stdout,
                            capture_output=True, env=trusted_env(), cwd="/")
    return result.stdout.decode().strip() if result.returncode == 0 else ""
