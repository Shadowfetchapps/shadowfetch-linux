#!/usr/bin/env python3
"""Fetch, authenticate and stage the pinned ShadowCode release for the build.

    python3 tools/fetch_shadowcode.py                 # what `make packages` runs
    python3 tools/fetch_shadowcode.py --offline       # refuse to touch the network
    python3 tools/fetch_shadowcode.py --print-build-deb
    python3 tools/fetch_shadowcode.py --stage-sources repo/pool/third-party-source

WHAT IT DOES, IN ORDER, AND WHAT IT REFUSES
-------------------------------------------
1. Reads the pin, tools/release/shadowcode.toml -- the only place the version,
   commit and per-asset size/SHA-256 live -- and refuses a version the vendored
   trust policy (vendor/shadowcode/trust/policy) does not authorise.
2. Downloads the .deb and the runtime-sources tarball from the GitHub release
   into build/cache/shadowcode/<version>/download/, unless a cached copy already
   has the pinned size and SHA-256. It also fetches the release's four signed
   metadata files and refuses if they differ by one byte from the copies
   vendored under vendor/shadowcode/<version>/: a republished release under an
   accepted version is exactly what pinning exists to notice.
3. Runs the vendored upstream verifier on each asset -- Ed25519 over
   RELEASE-AUTH against the vendored key, the key's authorised interval, the
   manifest/checksum digests, and the asset's size and SHA-256 -- then checks,
   in Python, that the signed RELEASE-AUTH agrees with the pin field by field
   and that dpkg-deb reads the pinned Package/Version/Architecture.
5. Archives the .deb's corresponding source -- ShadowCode at the signed commit
   plus the llama.cpp and SPIRV-Headers commits the signed manifest names --
   into build/cache/shadowcode/<version>/source/, and with --stage-sources
   publishes them next to the runtime sources.
4. Copies the .deb to build/shadow-code_<version>_amd64.deb (Debian's canonical
   name, which is what the package gate, reprepro and the ISO gate's payload
   parity check expect), removing any other shadow-code_*.deb from build/.

Idempotent: a second run downloads nothing and re-verifies everything. Offline:
works with no network once the cache holds the assets, because verification
reads only vendored metadata and local bytes. Nothing is trusted because it was
verified last time -- there is no "verified" marker to forge.

Exit 0 on success; 1 with FETCH_SHADOWCODE_FAILED: <reason> on any refusal.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

# APPENDED, not prepended: tools/release/ also holds a module named `acceptance`,
# which would shadow the tools/acceptance/ package for any later import.
_RELEASE_DIR = str(Path(__file__).resolve().parent / "release")
if _RELEASE_DIR not in sys.path:
    sys.path.append(_RELEASE_DIR)

import shadowcode  # noqa: E402
from shadowcode import ShadowCodeError  # noqa: E402


USER_AGENT = "shadowfetch-build/fetch_shadowcode"
GIT = shadowcode.GIT


def download(url: str, destination: Path, *, attempts: int = 3) -> None:
    """Stream url to destination through a .part file; never leaves a partial file."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as out:
                shutil.copyfileobj(response, out, 1024 * 1024)
            os.replace(partial, destination)
            return
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            last = error
            partial.unlink(missing_ok=True)
            if attempt < attempts:
                time.sleep(2 * attempt)
    raise ShadowCodeError(f"could not download {url}: {last}")


def cached_asset(pin: shadowcode.Pin, asset: shadowcode.Asset, *, offline: bool) -> Path:
    path = pin.cache_dir / "download" / asset.filename
    try:
        shadowcode.check_file(path, asset, asset.filename)
        print(f"CACHED {asset.filename} ({asset.bytes} bytes, sha256 {asset.sha256[:16]}...)")
        return path
    except ShadowCodeError as problem:
        if path.exists():
            print(f"DISCARD cached {asset.filename}: {problem}")
            path.unlink()
    if offline:
        raise ShadowCodeError(
            f"--offline and no verified cached copy of {asset.filename} in {path.parent}"
        )
    url = shadowcode.release_url(pin.tag, asset.filename)
    print(f"DOWNLOAD {url}")
    download(url, path)
    shadowcode.check_file(path, asset, asset.filename)
    return path


def check_published_metadata(pin: shadowcode.Pin, *, offline: bool) -> None:
    """The release's signed metadata must still be byte-identical to what we vendored."""
    directory = pin.cache_dir / "download"
    for name in shadowcode.METADATA_FILES:
        vendored = pin.vendor_dir / name
        cached = directory / name
        if not cached.is_file():
            if offline:
                print(f"SKIPPED republication check for {name} (--offline, not cached)")
                continue
            download(shadowcode.release_url(pin.tag, name), cached)
        if cached.read_bytes() != vendored.read_bytes():
            raise ShadowCodeError(
                f"the published {name} for {pin.tag} differs from "
                f"vendor/shadowcode/{pin.version}/{name}: the release was changed "
                "after it was pinned. Do not use it; ask upstream why."
            )
    print(f"PASS: published signed metadata matches vendor/shadowcode/{pin.version}/")


def dpkg_fields(deb: Path) -> dict[str, str]:
    fields = {}
    for name in ("Package", "Version", "Architecture"):
        fields[name] = subprocess.run(
            ["/usr/bin/dpkg-deb", "-f", str(deb), name],
            env=shadowcode.trusted_env(), text=True, check=True, capture_output=True,
        ).stdout.strip()
    return fields


def install_build_deb(pin: shadowcode.Pin, verified: Path) -> Path:
    target = pin.build_deb
    target.parent.mkdir(parents=True, exist_ok=True)
    for stale in target.parent.glob(f"{pin.package}_*.deb"):
        if stale != target:
            print(f"REMOVE stale {stale.name}")
            stale.unlink()
    try:
        shadowcode.check_file(target, pin.deb, "build copy")
        print(f"UNCHANGED {target}")
    except ShadowCodeError:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".shadowcode-", delete=False) as handle:
            temporary = Path(handle.name)
        try:
            shutil.copyfile(verified, temporary)
            os.chmod(temporary, 0o644)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        shadowcode.check_file(target, pin.deb, "build copy")
        print(f"STAGED {target}")
    return target


# ---- corresponding source of the .deb --------------------------------------
#
# Upstream publishes no source tarball for the .deb, so this makes one per
# input the .deb is built from: ShadowCode at the signed commit, and the
# llama.cpp and SPIRV-Headers commits the signed RELEASE-MANIFEST.json names.
# Each is `git archive` of a commit fetched by its id. Integrity comes from git,
# not from a pinned tarball digest: the fetched object must BE the named commit,
# and `git get-tar-commit-id` re-reads that id out of an archive before reuse.
# Rust and npm dependencies are not vendored; the archives carry the lockfiles
# the signed manifest hashes.

def _git(*args: str, cwd: Path | None = None, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run([GIT, *args], cwd=cwd, env=shadowcode.trusted_env(),
                          check=True, **kwargs)



def archive_commit(name: str, url: str, commit: str, *, git_cache: Path,
                   out_dir: Path, offline: bool) -> Path:
    """A reproducible .tar.gz of exactly `commit`, fetched from `url` by id."""
    target = out_dir / shadowcode.source_archive_name(name, commit)
    if target.is_file() and shadowcode.archive_commit_id(target) == commit:
        return target
    repo = git_cache / f"{name}.git"
    if not repo.is_dir():
        repo.mkdir(parents=True)
        _git("init", "--quiet", "--bare", str(repo))
    present = subprocess.run([GIT, "cat-file", "-e", f"{commit}^{{commit}}"], cwd=repo,
                             env=shadowcode.trusted_env(), capture_output=True).returncode == 0
    if not present:
        if offline:
            raise ShadowCodeError(f"{name} {commit[:12]} is not cached and --offline was given")
        _git("fetch", "--quiet", "--depth", "1", "--no-tags", url, commit, cwd=repo)
    resolved = _git("rev-parse", "--verify", f"{commit}^{{commit}}", cwd=repo,
                    capture_output=True, text=True).stdout.strip()
    if resolved != commit:
        raise ShadowCodeError(f"{name}: fetched {resolved}, expected {commit}")
    out_dir.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".part")
    with temporary.open("wb") as handle:
        archive = subprocess.Popen([GIT, "archive", "--format=tar", f"--prefix={name}-{commit}/", commit],
                                   cwd=repo, env=shadowcode.trusted_env(), stdout=subprocess.PIPE)
        subprocess.run(["/usr/bin/gzip", "-n", "-9"], stdin=archive.stdout, stdout=handle, check=True)
        archive.stdout.close()
        if archive.wait() != 0:
            raise ShadowCodeError(f"git archive of {name} {commit[:12]} failed")
    if shadowcode.archive_commit_id(temporary) != commit:
        temporary.unlink(missing_ok=True)
        raise ShadowCodeError(f"{name} archive does not carry commit {commit}")
    os.replace(temporary, target)
    return target



def deb_sources(pin: shadowcode.Pin, *, offline: bool) -> list[Path]:
    out_dir = pin.cache_dir / "source"
    git_cache = shadowcode.CACHE_ROOT / "git"
    archives = []
    for name, url, commit in shadowcode.source_inputs(pin):
        path = archive_commit(name, url, commit, git_cache=git_cache, out_dir=out_dir, offline=offline)
        print(f"PASS: {name} {commit[:12]} source archived ({path.stat().st_size} bytes)")
        archives.append(path)
    return archives


def stage_sources(pin: shadowcode.Pin, runtime: Path, destination: Path,
                  sources: list[Path] = ()) -> Path:
    """Publish the .deb's source archives, the runtime-sources tarball and the
    signed metadata beside the repo."""
    root = destination / pin.package
    target = root / pin.version
    if root.is_dir():
        for other in root.iterdir():
            if other.name != pin.version:
                shutil.rmtree(other)
    target.mkdir(parents=True, exist_ok=True)
    for name in shadowcode.METADATA_FILES:
        shutil.copyfile(pin.vendor_dir / name, target / name)
    shadowcode.check_file(runtime, pin.runtime_sources, "runtime sources")
    published = target / pin.runtime_sources.filename
    try:
        shadowcode.check_file(published, pin.runtime_sources, "published runtime sources")
    except ShadowCodeError:
        shutil.copyfile(runtime, published)
        shadowcode.check_file(published, pin.runtime_sources, "published runtime sources")
    sums = []
    for archive in sources:
        published_archive = target / archive.name
        shutil.copyfile(archive, published_archive)
        sums.append(f"{shadowcode.sha256_file(published_archive)}  {archive.name}\n")
    if sums:
        (target / shadowcode.SOURCE_SUMS).write_text("".join(sums), encoding="utf-8")
    (target / "README").write_text(
        f"ShadowCode {pin.version} ({pin.tag}, commit {pin.commit})\n"
        "\n"
        f"{pin.runtime_sources.filename} is the corresponding source of the\n"
        "AppImage runtime upstream publishes with this release, verified against\n"
        "the Ed25519-signed RELEASE-AUTH beside it (vendor/shadowcode/README.md\n"
        "in the Shadowfetch source tree says how). It is republished here because\n"
        "Shadowfetch redistributes the release; it is NOT the source of the\n"
        f"{pin.package} .deb.\n"
        "\n"
        f"The .deb's source is the other archives here: {shadowcode.REPOSITORY} at the\n"
        "commit above, and the llama.cpp and SPIRV-Headers commits the signed\n"
        "RELEASE-MANIFEST.json names. Each is `git archive` of that exact commit;\n"
        "`gzip -dc <file> | git get-tar-commit-id` prints the commit it holds.\n"
        "Rust and npm dependencies are pinned by the Cargo.lock and ui lockfile in\n"
        "the ShadowCode archive, whose hashes the signed manifest records.\n",
        encoding="utf-8",
    )
    print(f"STAGED sources {target}")
    return target


def fetch(*, offline: bool, stage: Path | None) -> Path:
    pin = shadowcode.load_pin()
    shadowcode.check_in_policy(pin.version, pin.key_id)
    for name in shadowcode.METADATA_FILES:
        if not (pin.vendor_dir / name).is_file():
            raise ShadowCodeError(
                f"vendor/shadowcode/{pin.version}/{name} is missing; run "
                f"tools/bump_shadowcode.py {pin.version} first"
            )
    print(f"ShadowCode {pin.version} ({pin.commit[:12]}), key {pin.key_id[:12]}...")
    deb = cached_asset(pin, pin.deb, offline=offline)
    runtime = cached_asset(pin, pin.runtime_sources, offline=offline)
    check_published_metadata(pin, offline=offline)
    for role, path in (("deb", deb), ("runtime-sources", runtime)):
        line = shadowcode.verify_pinned_artifact(pin, path, role)
        print(f"PASS: {line}")
    fields = dpkg_fields(deb)
    expected = {"Package": pin.package, "Version": pin.version, "Architecture": "amd64"}
    if fields != expected:
        raise ShadowCodeError(f"control fields {fields} differ from the pin {expected}")
    print(f"PASS: {pin.package} {pin.version} amd64 control fields match the pin")
    target = install_build_deb(pin, deb)
    # Every fetch prepares the .deb's source archives, so the offline
    # `make repo` run finds them cached and only re-checks their commit ids.
    sources = deb_sources(pin, offline=offline)
    if stage is not None:
        stage_sources(pin, runtime, stage, sources)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--offline", action="store_true",
                        help="never download; verify only what the cache holds")
    parser.add_argument("--print-build-deb", action="store_true",
                        help="print the build/ path of the pinned .deb and exit")
    parser.add_argument("--stage-sources", type=Path, metavar="DIR",
                        help="also publish the verified runtime-sources tarball and "
                        "signed metadata under DIR/shadow-code/<version>/")
    args = parser.parse_args(argv)
    try:
        if args.print_build_deb:
            print(shadowcode.load_pin().build_deb)
            return 0
        target = fetch(offline=args.offline, stage=args.stage_sources)
    except (ShadowCodeError, OSError, subprocess.CalledProcessError) as error:
        print(f"FETCH_SHADOWCODE_FAILED: {error}", file=sys.stderr)
        return 1
    print(f"\nFETCH_SHADOWCODE_PASSED {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
