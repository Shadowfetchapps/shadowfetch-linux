#!/usr/bin/env python3
"""Move the shipped ShadowCode to a newly published, signed upstream release.

    python3 tools/bump_shadowcode.py 0.34.0
    python3 tools/bump_shadowcode.py 0.34.0 --dry-run
    python3 tools/bump_shadowcode.py --refresh-trust --trust-commit <sha> \\
        --shadowcode-checkout <path-to-ShadowCode-clone>

A bump is three edits that must agree, made together or not at all:

  vendor/shadowcode/<version>/          the release's four signed metadata files
  tools/release/shadowcode.toml         the pin every consumer reads
  packages/shadowfetch-meta/debian/control
                                        shadowfetch-desktop's `shadow-code (>= X)`

Nothing is written until the new release has been authenticated: its
RELEASE-AUTH is downloaded with the .deb, and the vendored upstream verifier
checks the Ed25519 signature against the VENDORED trust policy and key, with
the currently pinned release as --previous-dir so a downgrade, a signing-epoch
rollback, or a changed release under an already accepted version is refused.

REFUSALS, each with the reason printed:
  * a version outside the vendored policy's authorised interval. The policy
    reviewed at the vendored commit (v1.0.0) authorises key f0c60ff8... for
    0.33.0 to 1.0.0 only, so 1.0.1 and later need a newly REVIEWED policy first --
    that is --refresh-trust, which copies release/trust/ and the two verifier
    scripts out of a named, published ShadowCode commit. It refuses an
    unpublished commit and refuses a key that is not already trusted.
  * a bad or missing signature, a digest mismatch, a tampered asset.
  * a version already vendored whose published metadata has changed.

Re-bumping to the version already pinned is a verified no-op: it
re-authenticates the release and reports that nothing changed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

# APPENDED, not prepended: tools/release/ also holds a module named `acceptance`,
# which would shadow the tools/acceptance/ package for any later import.
_RELEASE_DIR = str(Path(__file__).resolve().parent / "release")
if _RELEASE_DIR not in sys.path:
    sys.path.append(_RELEASE_DIR)
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import shadowcode  # noqa: E402
from shadowcode import Asset, ShadowCodeError  # noqa: E402
import fetch_shadowcode  # noqa: E402


@dataclass
class Layout:
    """Every path the bump reads or writes. Tests point it at a temporary tree."""

    vendor: Path = shadowcode.VENDOR
    pin_file: Path = shadowcode.PIN_FILE
    control: Path = shadowcode.META_CONTROL
    cache_root: Path = shadowcode.CACHE_ROOT
    readme: Path = field(default_factory=lambda: shadowcode.VENDOR / "README.md")

    @property
    def trust(self) -> Path:
        return self.vendor / "trust"

    @property
    def upstream(self) -> Path:
        return self.vendor / "upstream"

    @property
    def verifier(self) -> Path:
        return self.upstream / "verify-native-release.sh"


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def rewrite_floor(control_text: str, version: str) -> str:
    """Set shadowfetch-desktop's `shadow-code (>= X)` floor; refuse if absent/ambiguous."""
    pattern = re.compile(r"^ shadow-code \(>= [0-9.]+\)(,?)$", re.MULTILINE)
    matches = pattern.findall(control_text)
    if len(matches) != 1:
        raise ShadowCodeError(
            f"expected exactly one ' shadow-code (>= X)' Depends line in "
            f"shadowfetch-meta/debian/control, found {len(matches)}"
        )
    return pattern.sub(lambda m: f" shadow-code (>= {version}){m.group(1)}", control_text)


# --------------------------------------------------------------------------
# Trust refresh
# --------------------------------------------------------------------------

TRUST_SOURCES = {
    "release/trust/policy": "trust/policy",
    "scripts/verify-native-release.sh": "upstream/verify-native-release.sh",
    "scripts/native-release-auth-lib.sh": "upstream/native-release-auth-lib.sh",
}


def git(checkout: Path, *args: str) -> str:
    return subprocess.run(
        ["/usr/bin/git", "-C", str(checkout), *args],
        env=shadowcode.trusted_env(), text=True, capture_output=True, check=True,
    ).stdout


def refresh_trust(layout: Layout, checkout: Path, commit: str, *, dry_run: bool) -> str:
    """Re-vendor the trust policy + verifier from a reviewed, PUBLISHED commit."""
    if not shadowcode.COMMIT.fullmatch(commit):
        raise ShadowCodeError("--trust-commit must be a full 40-hex commit")
    git(checkout, "cat-file", "-e", f"{commit}^{{commit}}")
    published = git(checkout, "branch", "-r", "--contains", commit).strip() or \
        git(checkout, "tag", "--contains", commit).strip()
    if not published:
        raise ShadowCodeError(
            f"{commit} is on no remote-tracking branch or tag of {checkout}: an "
            "unpublished trust change cannot be reviewed by anyone else. Push it "
            "(and `git fetch`) first."
        )
    current_policy = (layout.trust / "policy").read_text(encoding="ascii")
    _, current_keys = shadowcode.parse_policy(current_policy)
    new_policy = git(checkout, "show", f"{commit}:release/trust/policy")
    _, new_keys = shadowcode.parse_policy(new_policy)
    known = {key.key_id for key in current_keys}
    unknown = sorted(key.key_id for key in new_keys if key.key_id not in known)
    if unknown:
        raise ShadowCodeError(
            "the new policy trusts a key this tree has never trusted "
            f"({', '.join(k[:12] + '...' for k in unknown)}). A new signing key is a "
            "separate, reviewed change to vendor/shadowcode/trust/, not a bump."
        )
    files = {target: git(checkout, "show", f"{commit}:{source}").encode()
             for source, target in TRUST_SOURCES.items()}
    for key in new_keys:
        pem = f"release/trust/{key.key_id}.pem"
        files[f"trust/{key.key_id}.pem"] = git(checkout, "show", f"{commit}:{pem}").encode()
        existing = layout.trust / f"{key.key_id}.pem"
        if existing.is_file() and existing.read_bytes() != files[f"trust/{key.key_id}.pem"]:
            raise ShadowCodeError(f"the public key file for {key.key_id[:12]}... changed")
    ranges = ", ".join(f"{k.key_id[:12]}... {k.minimum}-{k.maximum}" for k in new_keys)
    print(f"TRUST at {commit[:12]} (published: {published.splitlines()[0].strip()}): {ranges}")
    if not dry_run:
        for target, data in files.items():
            mode = 0o755 if target.endswith("verify-native-release.sh") else 0o644
            atomic_write(layout.vendor / target, data, mode)
        if layout.readme.is_file():
            text = layout.readme.read_text(encoding="utf-8")
            text = re.sub(r"(?m)^Reviewed trust commit: `[0-9a-f]{40}`$",
                          f"Reviewed trust commit: `{commit}`", text)
            atomic_write(layout.readme, text.encode())
    return commit


# --------------------------------------------------------------------------
# Bump
# --------------------------------------------------------------------------


def obtain_metadata(version: str, directory: Path, *, from_dir: Path | None) -> None:
    tag = f"v{version}"
    for name in shadowcode.METADATA_FILES:
        target = directory / name
        if from_dir is not None:
            directory.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(from_dir / name, target)
        else:
            target.unlink(missing_ok=True)
            fetch_shadowcode.download(shadowcode.release_url(tag, name), target)


def obtain_deb(asset: Asset, tag: str, directory: Path, *, from_dir: Path | None) -> Path:
    target = directory / asset.filename
    try:
        shadowcode.check_file(target, asset, asset.filename)
        return target
    except ShadowCodeError:
        target.unlink(missing_ok=True)
    if from_dir is not None:
        shutil.copyfile(from_dir / asset.filename, target)
    else:
        fetch_shadowcode.download(shadowcode.release_url(tag, asset.filename), target)
    shadowcode.check_file(target, asset, asset.filename)
    return target


def bump(
    version: str,
    *,
    layout: Layout | None = None,
    from_dir: Path | None = None,
    dry_run: bool = False,
    trust_commit: str | None = None,
) -> str:
    """Returns "no-op", "bumped" or "dry-run". Raises ShadowCodeError to refuse."""
    layout = layout or Layout()
    shadowcode.version_tuple(version)
    current = shadowcode.load_pin(layout.pin_file)
    shadowcode.check_in_policy(version, current.key_id, layout.trust / "policy")

    download = layout.cache_root / version / "download"
    staging = Path(tempfile.mkdtemp(prefix="shadowcode-bump-"))
    try:
        obtain_metadata(version, staging, from_dir=from_dir)
        auth = shadowcode.parse_release_auth((staging / "RELEASE-AUTH").read_text(encoding="ascii"))
        if auth.version != version:
            raise ShadowCodeError(f"the {version} release's RELEASE-AUTH names {auth.version}")
        shadowcode.check_in_policy(version, auth.key_id, layout.trust / "policy")
        download.mkdir(parents=True, exist_ok=True)
        deb = obtain_deb(auth.assets["deb"], auth.tag, download, from_dir=from_dir)
        previous = layout.vendor / current.version
        line = shadowcode.run_verifier(
            staging, deb,
            expect_version=version,
            previous_dir=previous if (previous / "RELEASE-AUTH").is_file() else None,
            trust_dir=layout.trust,
            verifier=layout.verifier,
        )
        print(f"PASS: {line}")

        target_dir = layout.vendor / version
        if target_dir.is_dir():
            changed = [name for name in shadowcode.METADATA_FILES
                       if not (target_dir / name).is_file()
                       or (target_dir / name).read_bytes() != (staging / name).read_bytes()]
            if changed:
                raise ShadowCodeError(
                    f"vendor/shadowcode/{version}/ already exists and the published "
                    f"{', '.join(changed)} differ from it: the release was changed "
                    "after it was vendored. Refusing."
                )

        ships_in = current.ships_in
        new_pin = shadowcode.render_pin(
            version=auth.version,
            commit=auth.commit,
            key_id=auth.key_id,
            key_epoch=auth.key_epoch,
            trust_commit=trust_commit or current.trust_commit,
            ships_in=ships_in,
            deb=auth.assets["deb"],
            runtime_sources=auth.assets["runtime-sources"],
        )
        control_text = layout.control.read_text(encoding="utf-8")
        new_control = rewrite_floor(control_text, version)
        old_pin = layout.pin_file.read_text(encoding="utf-8")

        vendor_same = target_dir.is_dir()
        if vendor_same and new_pin == old_pin and new_control == control_text:
            print(f"NO-OP: ShadowCode {version} is already pinned, vendored and "
                  "required by shadowfetch-desktop; the release re-verified.")
            return "no-op"
        if dry_run:
            print(f"DRY-RUN: would pin ShadowCode {current.version} -> {version} "
                  f"(commit {auth.commit[:12]}, deb sha256 {auth.assets['deb'].sha256[:16]}...)")
            return "dry-run"

        for name in shadowcode.METADATA_FILES:
            atomic_write(target_dir / name, (staging / name).read_bytes())
        # Parse what is about to be written with the same loader every consumer
        # uses, so a pin the gates would reject is never written.
        probe = staging / "pin-probe.toml"
        probe.write_text(new_pin, encoding="utf-8")
        shadowcode.load_pin(probe)
        atomic_write(layout.pin_file, new_pin.encode())
        atomic_write(layout.control, new_control.encode())
        print(f"BUMPED ShadowCode {current.version} -> {version}")
        print(f"  vendored  {target_dir}")
        print(f"  pinned    {layout.pin_file}")
        print(f"  requires  shadow-code (>= {version}) in {layout.control}")
        return "bumped"
    finally:
        shutil.rmtree(staging, ignore_errors=True)


NEXT_STEPS = """
Next (see vendor/shadowcode/README.md):
  python3 tools/fetch_shadowcode.py
  python3 tools/shadowcode_smoke.py
  make packages repo
  make package-gate
  make iso              # sudo; runs sign + iso-gate
  make vm-acceptance VM_CASE=shadowcode
  make vm-acceptance VM_CASE=shadowcode-soak VM_ACCEPTANCE_ARGS=--record
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("version", nargs="?", help="the newly published ShadowCode version, e.g. 0.34.0")
    parser.add_argument("--dry-run", action="store_true", help="verify, write nothing")
    parser.add_argument("--from-dir", type=Path,
                        help="read the release's metadata and .deb from DIR instead of GitHub")
    parser.add_argument("--refresh-trust", action="store_true",
                        help="re-vendor release/trust/ and the verifier from --trust-commit first")
    parser.add_argument("--trust-commit", help="reviewed, published ShadowCode commit (40 hex)")
    parser.add_argument("--shadowcode-checkout", type=Path,
                        help="a ShadowCode git clone containing --trust-commit")
    args = parser.parse_args(argv)
    layout = Layout()
    try:
        trust_commit = None
        if args.refresh_trust:
            if not (args.trust_commit and args.shadowcode_checkout):
                parser.error("--refresh-trust needs --trust-commit and --shadowcode-checkout")
            trust_commit = refresh_trust(layout, args.shadowcode_checkout,
                                         args.trust_commit, dry_run=args.dry_run)
            if not args.version:
                if not args.dry_run:
                    # The pin records which commit the trust files came from.
                    pin = shadowcode.load_pin()
                    text = layout.pin_file.read_text(encoding="utf-8")
                    text = text.replace(f'trust_commit = "{pin.trust_commit}"',
                                        f'trust_commit = "{trust_commit}"')
                    atomic_write(layout.pin_file, text.encode())
                print("TRUST REFRESHED; now run: python3 tools/bump_shadowcode.py <version>")
                return 0
        if not args.version:
            parser.error("a version is required")
        result = bump(args.version, layout=layout, from_dir=args.from_dir,
                      dry_run=args.dry_run, trust_commit=trust_commit)
    except (ShadowCodeError, OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", None)
        print(f"BUMP_SHADOWCODE_REFUSED: {error}" + (f"\n{detail}" if detail else ""),
              file=sys.stderr)
        return 1
    if result == "bumped":
        print(NEXT_STEPS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
