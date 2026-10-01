#!/usr/bin/env python3
"""Publish the accepted 4.0 artifacts; preserve every previous release object.

Run on the Linux publisher with AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY and
SHADOWFETCH_R2_ENDPOINT in the process environment. Credentials are never
written into the source tree. A plan is the default; --apply performs uploads.
The current-release pointer is the final object written, after the ISO's own
bytes have been streamed back and checked; the signed APT InRelease is last
among the objects that precede it. Both orderings are the same rule: nothing
that DIRECTS a reader is written before the thing it directs them to is present
and proven.

--apt-only publishes a point update that ships no ISO: the APT repository and
nothing else -- never the ISO, its sidecars, the evidence files or
releases/CURRENT.json. It is selected by `delivery = "apt-only"` in the release
data or by the flag, and has its own preconditions (apt_only_acceptance,
apt_only_plan).
"""
from __future__ import annotations
import argparse
import bz2
from dataclasses import dataclass
import datetime
import email.utils
import gzip
import hashlib
import importlib.util
import json
import lzma
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

BUCKET = "shadowfetch-linux"
# The one authorized publishing tree: ~/projects/shadowfetch-4.0.0 of the
# account running the publisher (derived from $HOME, not a named user).
PUBLISHER = Path.home().resolve() / "projects" / "shadowfetch-4.0.0"
ROOT = Path(__file__).resolve().parents[1]
FINGERPRINT = "8F13CE1535EE1F4A2916A1F73C5C900B7BE80CA1"
# WHICH RELEASE THIS IS, is not written down here. A publisher carrying its own
# copy of that is how a stamped 4.1.0 tree comes to upload a pointer naming
# 4.0.0 as live: measured on this tree, `make publish` after the 4.1.0 stamp
# still verified qa/4.0.0/acceptance.json, still looked in work/release-4.0.0,
# and would still have written releases/CURRENT.json saying 4.0.0 -- past a
# preflight that passed, because everything it checked was 4.0.0 too.
#
# tools/release/versions/<v>.toml is the one authority. load_release(None)
# selects the single non-historical file and REFUSES rather than guesses when
# there are two, which is the same refusal the drift gate makes.
#
# The `_4_0_0` in this file's NAME is now a legacy label and decides nothing.
# It is not renamed because ARCHITECTURE_AUDIT.md, ARCHITECTURE_DECISIONS.md,
# STAGE_VERIFICATION_CORRECTIONS.md, tools/truth/release.json and the artifact
# worker's README each name this exact path as the thing they examined;
# renaming it would silently falsify all five.
sys.path.insert(0, str(ROOT / "tools/release"))
import gate  # noqa: E402
# The ShadowCode pin and its signed-release verifier: what decides whether a
# file under pool/third-party-source/ is the signed upstream bytes.
import shadowcode  # noqa: E402
RELEASE = gate.load_release(None)
VERSION = RELEASE.version
# The pointer's schema, its validation and its key live with the worker that
# READS it. Importing that module rather than restating the document here is
# the whole point: a writer with its own idea of the schema is how a reader
# comes to refuse what a writer produced.
sys.path.insert(0, str(ROOT / "web/shadowfetch-linux-worker/tools"))
import release_pointer  # noqa: E402
ISO = f"shadowfetch-{VERSION}-amd64.iso"
EVIDENCE = (
    f"dossier-{VERSION}.md", f"packages-{VERSION}.manifest",
    f"sbom-{VERSION}.cdx.json", f"sbom-sources-{VERSION}.txt",
    f"release-facts-{VERSION}.json", f"release-evidence-{VERSION}.sha256",
    f"evidence-bundle-{VERSION}.tar.gz", f"evidence-bundle-{VERSION}.contents",
)
# The repository signing key, at the bucket root (the worker also serves it
# under /linux/apt/). Immutable: an update signed by another key is not a
# packages-only matter, and existing_matches refuses to replace it.
REPOSITORY_KEY = "shadowfetch.gpg.asc"

# -- packages-only (--apt-only) point updates ---------------------------------
#
# [release].delivery in the release data is "iso" -- the default, and what
# every historical file means by saying nothing -- or "apt-only". An apt-only
# release ships no image: installed systems take it with `sudo apt update;
# fireproof update`, and the previous ISO stays the download.
DELIVERY_ISO = "iso"
APT_ONLY = "apt-only"
DELIVERIES = (DELIVERY_ISO, APT_ONLY)
# The acceptance FLOOR for an update that ships no image: the cases whose
# subject is the packages themselves -- the source they were built from
# (SRC-01), the exact signed binary and source inventory and a clean install of
# it (PKG-01), and what an APT-only update IS, a published system taking it
# (UPGRADE-01). The other cases are about an image this update does not
# produce: ISO-01, INSTALL-01, VISUAL-01 and EVIDENCE-01 describe its bytes, its
# installer and its screenshots. Release data may ADD cases
# ([apt_only].acceptance) when an update touches what they measure; nothing can
# take one of these three away. tools/release/acceptance.py verify -- the gate
# an ISO release passes -- is neither used nor changed by this mode: an ISO
# release still needs every required case.
APT_ONLY_FLOOR = ("SRC-01", "PKG-01", "UPGRADE-01")
# The packages under test: the manifest field, and the index under
# dists/<codename>/ whose digest it must be. The binary and source indices pin
# every .deb and source file by SHA-256, so these two digests are the packages.
PACKAGE_INDICES = (
    ("apt_packages_sha256", "main/binary-amd64/Packages"),
    ("apt_sources_sha256", "main/source/Sources"),
)
# How long the signed index must stay valid when it is published: the same
# floor pre_release_check.sh is given, read from the text the signature covers.
MIN_VALID_FOR = datetime.timedelta(days=7)
# Corresponding source of a prebuilt package, published beside the pool rather
# than in main/source (vendor/shadowcode/README.md).
THIRD_PARTY_SOURCE = "pool/third-party-source"
CLEARSIGN_BEGIN = "-----BEGIN PGP SIGNED MESSAGE-----"
SIGNATURE_BEGIN = "-----BEGIN PGP SIGNATURE-----"
SIGNATURE_END = "-----END PGP SIGNATURE-----"
# Compressed forms apt may download in place of an index. Each must decompress
# to the uncompressed file beside it, which is the one every check here reads.
DECOMPRESSORS = {".gz": gzip.open, ".xz": lzma.open, ".lzma": lzma.open, ".bz2": bz2.open}
UNREADABLE_COMPRESSIONS = (".zst", ".lz4")

@dataclass(frozen=True)
class Object:
    path: Path
    key: str
    sha256: str
    size: int
    mutable: bool = False

def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()

def object_for(path, key, mutable=False):
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"Missing, empty or symbolic-link release file: {path}")
    if key.startswith("/") or ".." in key.split("/"):
        raise ValueError("Object key escapes its release prefix")
    return Object(path, key, digest(path), path.stat().st_size, mutable)

def repository_objects(root, codename=None):
    """The APT repository in write order: key, pool, indices, signed index last.

    Shared by both modes, so an ISO release and a packages-only update cannot
    come to disagree about what "the repository" is or in which order it is
    written.
    """
    codename = codename or RELEASE.codename
    objects = [object_for(root / "repo/shadowfetch.gpg.asc", REPOSITORY_KEY)]
    pool = root / "repo/pool"
    pool_files = sorted(path for path in pool.rglob("*") if path.is_file())
    if not pool_files:
        raise ValueError("APT package/source pool is empty")
    objects.extend(object_for(path, "apt/pool/" + path.relative_to(pool).as_posix()) for path in pool_files)
    dists = root / "repo/dists"
    metadata = [object_for(path, "apt/dists/" + path.relative_to(dists).as_posix(), True) for path in dists.rglob("*") if path.is_file()]
    # Indices first, detached metadata next, atomic signed index last.
    terminal = {f"apt/dists/{codename}/Release.gpg": 1, f"apt/dists/{codename}/Release": 2, f"apt/dists/{codename}/InRelease": 3}
    def order(item):
        return terminal.get(item.key, 0), item.key
    metadata.sort(key=order)
    if not metadata or metadata[-1].key != f"apt/dists/{codename}/InRelease":
        raise ValueError("APT signed InRelease is absent")
    objects.extend(metadata)
    return objects

def publication_plan(root):
    manifest = json.loads((root / f"qa/{VERSION}/acceptance.json").read_text())
    artifact = manifest.get("artifact", {})
    iso = object_for(root / ISO, "releases/" + ISO)
    if iso.sha256 != artifact.get("iso_sha256") or iso.size != artifact.get("iso_size_bytes"):
        raise ValueError("ISO differs from the accepted artifact")
    release = root / f"work/release-{VERSION}"
    objects = [iso]
    objects.extend(object_for(root / name, "releases/" + name) for name in (ISO + ".sha256", ISO + ".asc"))
    objects.extend(object_for(release / name, "releases/" + name) for name in EVIDENCE)
    bundle = next(item for item in objects if item.path.name == f"evidence-bundle-{VERSION}.tar.gz")
    if bundle.sha256 != artifact.get("evidence_bundle_sha256"):
        raise ValueError("Evidence bundle differs from the accepted artifact")
    objects.extend(repository_objects(root))
    if len({item.key for item in objects}) != len(objects):
        raise ValueError("Duplicate publication object key")
    return objects


def pointer_object(root, iso, published=None):
    """The current-release pointer, built from the ISO that is on disk.

    `published` defaults to the ISO's own modification time rather than to
    "now": re-running the publisher then produces a byte-identical document, so
    a second run reports UNCHANGED instead of rewriting the one object every
    reader consults. A publisher who wants to state a different moment passes
    --published.
    """
    stamp = published or datetime.datetime.fromtimestamp(
        (root / ISO).stat().st_mtime, datetime.timezone.utc
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    document = release_pointer.build(
        VERSION, root / ISO, published=stamp,
        fingerprint=FINGERPRINT, sha256=iso.sha256)
    path = root / f"work/release-{VERSION}/CURRENT.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    return object_for(path, "releases/CURRENT.json", True)

def remote_digest(client, key):
    response = client.get_object(Bucket=BUCKET, Key=key)
    checksum = hashlib.sha256()
    try:
        for chunk in response["Body"].iter_chunks(chunk_size=8 * 1024**2):
            checksum.update(chunk)
    finally:
        response["Body"].close()
    return checksum.hexdigest()

def existing_matches(client, item):
    try:
        head = client.head_object(Bucket=BUCKET, Key=item.key)
    except Exception as error:
        if str(getattr(error, "response", {}).get("Error", {}).get("Code")) in ("404", "NoSuchKey", "NotFound"):
            return False
        raise
    if head["ContentLength"] == item.size:
        actual = head.get("Metadata", {}).get("sha256") or remote_digest(client, item.key)
        if actual == item.sha256:
            return True
    if not item.mutable:
        raise ValueError(f"Refusing to replace a different immutable object: {item.key}")
    return False

def transfer_config():
    from boto3.s3.transfer import TransferConfig
    return TransferConfig(multipart_threshold=64 * 1024**2, multipart_chunksize=64 * 1024**2, max_concurrency=4)

def media_type(item):
    media = "application/x-iso9660-image" if item.path.name.endswith(".iso") else mimetypes.guess_type(item.path.name)[0] or "application/octet-stream"
    if item.path.name.endswith(".asc"):
        media = "application/pgp-signature" if item.path.name != "shadowfetch.gpg.asc" else "application/pgp-keys"
    return media

def upload_object(client, item, config):
    """Upload one object, then read its size and recorded digest back."""
    print(f"UPLOAD {item.key} {item.size} bytes", flush=True)
    client.upload_file(str(item.path), BUCKET, item.key, Config=config, ExtraArgs={
        "ContentType": media_type(item),
        "CacheControl": "public, max-age=0, must-revalidate" if item.mutable else "public, max-age=3600",
        "Metadata": {"release": VERSION, "sha256": item.sha256},
    })
    head = client.head_object(Bucket=BUCKET, Key=item.key)
    if head["ContentLength"] != item.size or head.get("Metadata", {}).get("sha256") != item.sha256:
        raise ValueError("Uploaded object readback failed: " + item.key)

def publish(client, objects, pointer=None):
    config = transfer_config()
    # Resolve collisions across the entire plan before the first upload.
    matches = {item.key: existing_matches(client, item) for item in objects}
    for item in objects:
        if matches[item.key]:
            print("UNCHANGED " + item.key, flush=True)
            continue
        upload_object(client, item, config)
    # Independently stream the large object back; metadata alone is not proof.
    iso = next(item for item in objects if item.path.name == ISO)
    if remote_digest(client, iso.key) != iso.sha256:
        raise ValueError("R2 ISO bytes do not match the accepted artifact")
    print("R2_RELEASE_BYTES_VERIFIED", flush=True)
    if pointer is not None:
        # LAST, AND ONLY NOW. This is the object that tells every reader which
        # release is live, so it is written after the bytes it names have been
        # uploaded AND streamed back. Written earlier it would, for the length
        # of an upload, advertise an image the bucket did not hold.
        print(f"UPLOAD {pointer.key} {pointer.size} bytes", flush=True)
        client.upload_file(str(pointer.path), BUCKET, pointer.key, ExtraArgs={
            "ContentType": "application/json",
            "CacheControl": "public, max-age=0, must-revalidate",
            "Metadata": {"release": VERSION, "sha256": pointer.sha256},
        })
        head = client.head_object(Bucket=BUCKET, Key=pointer.key)
        if head.get("Metadata", {}).get("sha256") != pointer.sha256:
            raise ValueError("Pointer readback failed: " + pointer.key)
        print("R2_CURRENT_POINTER_WRITTEN", flush=True)

# These three decide whether the shipped ISO's signature and digest are
# genuine. They were invoked by bare name, so PATH decided which program
# answered -- and on the build host ~/.local/bin precedes /usr/bin and is
# writable by the builder. Absolute paths, and TRUSTED_SUBPROCESS_PATH so the
# programs' own helpers cannot be swapped either.
GPG = "/usr/bin/gpg"
GPGV = "/usr/bin/gpgv"
SHA256SUM = "/usr/bin/sha256sum"
TRUSTED_SUBPROCESS_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"


def trusted_env(**extra):
    return dict(os.environ, PATH=TRUSTED_SUBPROCESS_PATH, **extra)


def release_keyring(root, directory):
    """Dearmor repo/shadowfetch.gpg.asc into `directory`, once it is shown to
    be the official release key."""
    key = root / "repo/shadowfetch.gpg.asc"
    fingerprints = subprocess.check_output([GPG, "--batch", "--with-colons", "--show-keys", str(key)], text=True, env=trusted_env())
    if FINGERPRINT not in [row.split(":")[9] for row in fingerprints.splitlines() if row.startswith("fpr:")]:
        raise ValueError("Repository key differs from the official release fingerprint")
    keyring = Path(directory) / "release.gpg"
    subprocess.run([GPG, "--batch", "--yes", "--dearmor", "--output", str(keyring), str(key)], check=True, env=trusted_env())
    return keyring

def verify_signatures(root):
    with tempfile.TemporaryDirectory(prefix="shadowfetch-publication-key-") as temporary:
        keyring = release_keyring(root, temporary)
        subprocess.run([GPGV, "--keyring", str(keyring), str(root / (ISO + ".asc")), str(root / ISO)], check=True, env=trusted_env())
        subprocess.run([GPGV, "--keyring", str(keyring), str(root / "repo/dists/umbra/InRelease")], check=True, env=trusted_env())

def verify_repository_signatures(root, codename=None):
    """Both signed forms of the index verify under the release key.

    Returns the text InRelease's signature covers, as gpgv emits it: what is
    then compared with the files is what was signed, not whatever else the
    InRelease file might also carry.
    """
    dists = root / "repo/dists" / (codename or RELEASE.codename)
    with tempfile.TemporaryDirectory(prefix="shadowfetch-publication-key-") as temporary:
        keyring = release_keyring(root, temporary)
        signed = subprocess.run([GPGV, "--keyring", str(keyring), "--output", "-", str(dists / "InRelease")], check=True, env=trusted_env(), stdout=subprocess.PIPE).stdout
        subprocess.run([GPGV, "--keyring", str(keyring), str(dists / "Release.gpg"), str(dists / "Release")], check=True, env=trusted_env())
    return signed.decode("utf-8")

# -- packages-only (--apt-only) ------------------------------------------------

def delivery(release=None):
    """How this release reaches users: "iso" unless the data says "apt-only"."""
    release = release or RELEASE
    value = release.release.get("delivery", DELIVERY_ISO)
    if value not in DELIVERIES:
        raise ValueError(f"{release.path.name}: [release].delivery is {value!r}; expected one of {', '.join(DELIVERIES)}")
    return value

def publication_mode(apt_only_flag, release=None):
    """APT-only when the release data says so or the operator asked; else ISO.

    Never inferred from what is on disk: a missing ISO in an ISO release is a
    refusal (publication_plan), not a quiet fall back to packages only.
    """
    declared = delivery(release)  # validated even when the flag decides
    return APT_ONLY if apt_only_flag or declared == APT_ONLY else DELIVERY_ISO

def _version_key(version):
    return tuple(int(part) for part in version.split("."))

def apt_only_table(release=None):
    release = release or RELEASE
    table = release.document.get("apt_only", {})
    if not isinstance(table, dict):
        raise ValueError(f"{release.path.name}: [apt_only] must be a table")
    return table

def apt_only_cases(release=None):
    """The floor, plus whatever the release data adds. Never fewer."""
    extra = apt_only_table(release).get("acceptance", [])
    if not isinstance(extra, list) or not all(isinstance(case, str) and case for case in extra):
        raise ValueError("[apt_only].acceptance must be a list of case ids")
    return tuple(dict.fromkeys([*APT_ONLY_FLOOR, *extra]))

def base_release(release=None):
    """The ISO release this update is applied on top of.

    [apt_only].base_release names it; otherwise it is the newest earlier
    release whose data says it shipped an image. That image is still the
    download, so it is what an installed system taking this update started from.
    """
    release = release or RELEASE
    directory = release.path.parent
    named = apt_only_table(release).get("base_release")
    if named is None:
        earlier = [
            version for version in gate.available_versions(directory)
            if _version_key(version) < _version_key(release.version)
            and delivery(gate.load_release(version, directory=directory)) == DELIVERY_ISO
        ]
        if not earlier:
            raise ValueError(f"No earlier ISO release in {directory} for {release.version} to update")
        named = max(earlier, key=_version_key)
    base = gate.load_release(named, directory=directory)
    if _version_key(base.version) >= _version_key(release.version) or delivery(base) != DELIVERY_ISO:
        raise ValueError(f"Base release {base.version} is not an earlier ISO release")
    return base

def base_manifest(root, base):
    """qa/<base>/acceptance.json: the base release's own accepted manifest."""
    path = root / f"qa/{base.version}/acceptance.json"
    return path, json.loads(path.read_text(encoding="utf-8"))

def base_image_digest(root, base):
    """The base image's digest, as that release's own accepted manifest records it."""
    manifest, data = base_manifest(root, base)
    value = (data.get("artifact") or {}).get("iso_sha256")
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError(f"{manifest} records no artifact.iso_sha256 for the base image")
    return value.lower()

@dataclass(frozen=True)
class BaseRecord:
    """What the base release accepted, so it cannot be accepted again as this.

    Every receipt of the base release is bound to the same image digest an
    APT-only update's evidence is bound to, so that binding cannot tell the
    two apart; the package-index stamps can (evidence_errors). These are the
    belt to those braces: a file the base release recorded, a reason it gave
    for a waiver, and the directory it kept its evidence in.
    """
    version: str
    evidence: dict  # sha256 -> "CASE-ID path"
    waivers: dict  # case id -> reason
    evidence_root: Path | None

def base_record(root, base, acceptance):
    manifest, data = base_manifest(root, base)
    evidence, waivers = {}, {}
    for case in data.get("cases") or []:
        if not isinstance(case, dict):
            continue
        for item in case.get("evidence") or []:
            if isinstance(item, dict) and isinstance(item.get("sha256"), str):
                evidence.setdefault(item["sha256"].lower(), f"{case.get('id')} {item.get('path')}")
        reason = (case.get("waiver") or {}).get("reason") if isinstance(case.get("waiver"), dict) else None
        if isinstance(reason, str) and reason.strip():
            waivers[case.get("id")] = reason
    named = data.get("evidence_root")
    evidence_root = acceptance.evidence_root_for(manifest, data) if isinstance(named, str) and named.strip() else None
    return BaseRecord(base.version, evidence, waivers, evidence_root)

def _inside(path, directory):
    return directory is not None and (path == directory or directory in path.parents)

def release_acceptance():
    """tools/release/acceptance.py -- its evidence floors, not a copy of them.

    Loaded by path because `import acceptance` binds the VM harness PACKAGE
    (tools/acceptance/): gate.py puts tools/ ahead of tools/release/.
    """
    name = "sf_release_acceptance"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, gate.RELEASE_DIR / "acceptance.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]

def package_binding_errors(label, item, packages):
    """`item` (an evidence entry or a waiver) carries the digests of the indices
    being published, as `acceptance.py record` stamps them from the manifest."""
    errors = []
    for field, index in PACKAGE_INDICES:
        stamped, published = item.get(field), packages.get(field)
        if published is None:
            continue  # the index itself is missing; reported once, above
        if not isinstance(stamped, str) or stamped.lower() != published:
            against = f"recorded against {index} {stamped[:16]}..." if isinstance(stamped, str) \
                else f"carries no {field}, so it was recorded against no particular {index}"
            errors.append(
                f"{label}: {against}, not the one being published ({published[:16]}...). Re-run it "
                f"against this repository and re-record it once artifact.{field} names it: "
                "`acceptance.py record` stamps the digest from there")
    return errors

def evidence_errors(acceptance, case, evidence_root, bound_to, packages, base):
    """One passing case's evidence: present, unchanged, usable, and bound to
    the base image AND to the packages being published -- never the base
    release's own."""
    case_id = case["id"]
    if not case["evidence"]:
        return [f"{case_id}: passing case has no evidence"]
    errors = []
    for index, item in enumerate(case["evidence"]):
        label = f"{case_id}.evidence[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{label}: entry must be an object")
            continue
        kind, value, recorded = item.get("kind"), item.get("path"), item.get("sha256")
        if kind not in acceptance.VALID_KINDS or not isinstance(value, str) or not value \
                or not isinstance(recorded, str) or len(recorded) != 64:
            errors.append(f"{label}: needs a valid kind, a path and a 64-character sha256")
            continue
        try:
            path = acceptance.resolve_evidence(evidence_root, value)
        except ValueError as error:
            errors.append(f"{label}: {error}")
            continue
        if _inside(path, base.evidence_root):
            errors.append(f"{label}: {value} is in {base.version}'s evidence directory; an update's acceptance is re-run, not inherited")
            continue
        if not path.is_file():
            errors.append(f"{label}: missing file {path}")
            continue
        if acceptance.sha256_file(path) != recorded.lower():
            errors.append(f"{label}: SHA-256 mismatch for {value}")
        if recorded.lower() in base.evidence:
            errors.append(f"{label}: {value} is {base.version}'s own evidence ({base.evidence[recorded.lower()]} in qa/{base.version}/acceptance.json); an update's acceptance is re-run, not inherited")
        bound = item.get("artifact_sha256")
        if not isinstance(bound, str) or bound.lower() != bound_to:
            errors.append(f"{label}: recorded against {str(bound)[:16]}..., not the image this update is applied to ({bound_to[:16]}...)")
        errors.extend(package_binding_errors(label, item, packages))
        errors.extend(f"{label}: {error}" for error in acceptance.evidence_quality_errors(path, kind))
        if kind == "screenshot":
            try:
                width, height = acceptance.png_size(path)
            except ValueError as error:
                errors.append(f"{label}: {error}")
            else:
                if width < 1280 or height < 720:
                    errors.append(f"{label}: screenshot is {width}x{height}, below 1280x720")
    return errors

def waiver_errors(case, packages, base):
    """A waived case's decision was taken about these packages, for this release."""
    waiver = case["waiver"]  # approver and reason: acceptance.validate_manifest
    errors = package_binding_errors(f"{case['id']}.waiver", waiver, packages)
    if base.waivers.get(case["id"], "").strip() == waiver["reason"].strip():
        errors.append(f"{case['id']}.waiver: the reason is {base.version}'s waiver of {case['id']}, word for word; a waiver is argued again for this release, not inherited")
    return errors

def apt_only_acceptance_errors(root, release=None):
    """The packages-only acceptance subset of qa/<v>/acceptance.json.

    Beyond the manifest being structurally valid for this release:
      * every case in apt_only_cases() is present, still required and
        prepublish, and is pass (with bound, unchanged, usable evidence) or
        waived (with an approver and a reason);
      * artifact.iso_sha256 is the BASE image -- the shipped ISO this update is
        applied on top of, which stays the download -- and passing evidence is
        bound to it, which is the digest the recorder stamps;
      * artifact.apt_packages_sha256 and artifact.apt_sources_sha256 are the
        digests of the binary and source indices being published, which pin
        every .deb and source file by SHA-256: acceptance recorded against
        another build of the packages does not describe these;
      * and, because the base image digest is ALSO what every receipt of the
        base release is bound to, each evidence entry and each waiver of the
        subset carries those two index digests itself (`acceptance.py record`
        stamps them from the manifest, as it stamps artifact_sha256). Setting
        the manifest-level digests after the fact binds nothing that was
        recorded before them. On top of that, no evidence file of the subset
        is one qa/<base>/acceptance.json records, none lies in the base
        release's evidence directory, and no waiver repeats the base release's
        reason for waiving that case: the base release's acceptance is not
        this update's;
      * no case anywhere in the manifest is recorded as `fail`. A case outside
        the subset may be pending; a recorded failure is not "not required".
    """
    release = release or RELEASE
    acceptance = release_acceptance()
    manifest = root / f"qa/{release.version}/acceptance.json"
    try:
        data = acceptance.load_manifest(manifest)
    except (OSError, ValueError) as error:
        return [f"{manifest}: {error}"]
    errors = acceptance.validate_manifest(data, release)
    if errors:
        return errors
    artifact = data["artifact"] if isinstance(data.get("artifact"), dict) else {}
    base = base_release(release)
    bound_to = base_image_digest(root, base)
    declared = str(artifact.get("iso_sha256") or "").lower()
    if declared != bound_to:
        errors.append(f"artifact.iso_sha256 must name {base.iso_name} ({bound_to[:16]}...), the image this update is applied to; it names {declared[:16] or 'nothing'}")
    dists = root / "repo/dists" / release.codename
    packages = {}
    for field, index in PACKAGE_INDICES:
        path = dists / index
        if not path.is_file():
            errors.append(f"{path} is missing")
            continue
        packages[field] = digest(path)
        if artifact.get(field) != packages[field]:
            errors.append(f"artifact.{field} is {artifact.get(field)!r} but the {index} being published is {packages[field]}: the subset was not accepted against these packages")
    evidence_root = acceptance.evidence_root_for(manifest, data)
    base_accepted = base_record(root, base, acceptance)
    if _inside(evidence_root, base_accepted.evidence_root):
        errors.append(f"evidence_root {evidence_root} is {base.version}'s evidence directory; this update's evidence is its own")
    cases = {case["id"]: case for case in data["cases"]}
    for case_id in apt_only_cases(release):
        case = cases.get(case_id)
        if case is None:
            errors.append(f"{case_id}: absent from {manifest.name}")
            continue
        if case["required"] is not True or case["phase"] != "prepublish":
            errors.append(f"{case_id}: must stay a required prepublish case")
        if case["status"] == "pass":
            errors.extend(evidence_errors(acceptance, case, evidence_root, bound_to, packages, base_accepted))
        elif case["status"] == "waived":
            errors.extend(waiver_errors(case, packages, base_accepted))
        else:
            errors.append(f"{case_id}: required status is {case['status']}, not pass or waived")
    errors.extend(f"{case['id']}: recorded as fail" for case in data["cases"] if case["status"] == "fail")
    return errors

def apt_only_acceptance(root, release=None):
    release = release or RELEASE
    errors = apt_only_acceptance_errors(root, release)
    if errors:
        raise ValueError("APT-only acceptance refused:\n  - " + "\n  - ".join(errors))
    required = apt_only_cases(release)
    data = json.loads((root / f"qa/{release.version}/acceptance.json").read_text(encoding="utf-8"))
    for case in data["cases"]:
        state = "REQUIRED" if case["id"] in required else "NOT_REQUIRED"
        print(f"{state} {case['id']} {case['status']}", flush=True)
    print(f"APT_ONLY_ACCEPTANCE_PASSED required={','.join(required)}", flush=True)

def repository_errors(repo, release=None):
    """The indices name exactly this release's packages, and the pool holds them."""
    release = release or RELEASE
    dists = repo / "dists" / release.codename
    packages, sources = dists / "main/binary-amd64/Packages", dists / "main/source/Sources"
    if not packages.is_file() or not sources.is_file():
        return [f"missing {packages} or {sources}"]
    errors = []
    binary = gate.parse_deb822(packages.read_text(encoding="utf-8"))
    published = {record.get("Package"): record.get("Version") for record in binary}
    expected = release.binary_versions
    if len(published) != len(binary):
        errors.append("the binary index lists a package more than once")
    for name in sorted(set(published) | set(expected), key=str):
        if published.get(name) != expected.get(name):
            errors.append(f"{name}: the binary index has {published.get(name)}, the release data says {expected.get(name)}")
    for record in binary:
        pooled = repo / record.get("Filename", "")
        if not pooled.is_file() or digest(pooled) != record.get("SHA256") or pooled.stat().st_size != int(record.get("Size", -1)):
            errors.append(f"{record.get('Filename')}: missing from the pool or not the bytes the binary index names")
    third_party = release.document["packages"].get("third_party", {})
    source_records = gate.parse_deb822(sources.read_text(encoding="utf-8"))
    names = {record.get("Package") for record in source_records}
    if names != release.source_packages:
        errors.append(f"source index mismatch: missing={sorted(release.source_packages - names)}, extra={sorted(names - release.source_packages, key=str)}")
    indexed = {record.get("Filename") for record in binary}
    for record in source_records:
        want = third_party.get(record.get("Package"), f"{release.version}-{release.revision}")
        if record.get("Version") != want:
            errors.append(f"source {record.get('Package')}: the source index has {record.get('Version')}, the release data says {want}")
        for line in record.get("Checksums-Sha256", "").splitlines():
            fields = line.split()
            if len(fields) == 3:
                path = repo / record.get("Directory", "") / fields[2]
                indexed.add(path.relative_to(repo).as_posix())
                if not path.is_file() or digest(path) != fields[0]:
                    errors.append(f"{path.relative_to(repo)}: missing from the pool or not the bytes the source index names")
    # Everything else in the pool would be uploaded as a permanent object that
    # no index names. pool/third-party-source/ is the one deliberate exception:
    # it is the corresponding source of a prebuilt package (ShadowCode), and
    # every file in it is checked on its own terms below.
    stray = sorted(
        relative for relative in (path.relative_to(repo).as_posix() for path in (repo / "pool").rglob("*") if path.is_file())
        if relative not in indexed and not relative.startswith(THIRD_PARTY_SOURCE + "/"))
    if stray:
        errors.append(f"pool files no index names: {', '.join(stray)}")
    errors.extend(third_party_source_errors(repo, binary))
    return errors

def third_party_source_errors(repo, binary_records):
    """Every file under pool/third-party-source/ is checked source of a package
    being published.

    Uploaded as permanent objects, so nothing goes up here on the strength of
    its directory name. A file is accepted only at <package>/<version>/<file>
    for a (Package, Version) the binary index lists -- the fetch tool removes
    other versions when it stages a new pin -- and only when something has
    verified it: named in that directory's SOURCE-SHA256SUMS with those bytes,
    or, for ShadowCode, one of the pin's signed assets passing the upstream
    verifier, one of its signed metadata files byte-identical to the vendored
    copy that verifier authenticates, or the README the fetch tool writes.
    A partial download, a stray version directory or anything else is refused.
    """
    top = repo / THIRD_PARTY_SOURCE
    if not top.exists():
        return []
    listed = {(record.get("Package"), record.get("Version")) for record in binary_records}
    errors, directories = [], {}
    for path in sorted(top.rglob("*")):
        relative = path.relative_to(repo).as_posix()
        if path.is_symlink():
            errors.append(f"{relative}: a symbolic link, not a file")
            continue
        if path.is_dir():
            continue
        parts = path.relative_to(top).parts
        if len(parts) != 3:
            errors.append(f"{relative}: not at {THIRD_PARTY_SOURCE}/<package>/<version>/<file>")
            continue
        name, version, filename = parts
        if (name, version) not in listed:
            errors.append(f"{relative}: the binary index lists no {name} {version}, so this is the source of nothing being published")
            continue
        directories.setdefault((name, version), set()).add(filename)
    for (name, version), files in sorted(directories.items()):
        verified, problems = verified_third_party_files(top / name / version, name, version)
        errors.extend(problems)
        errors.extend(
            f"{THIRD_PARTY_SOURCE}/{name}/{version}/{filename}: named in neither {shadowcode.SOURCE_SUMS} nor the signed release metadata, so nothing has verified it"
            for filename in sorted(files - verified))
    return errors

def verified_third_party_files(directory, name, version):
    """The files in one <package>/<version>/ directory that a check vouches for."""
    label = f"{THIRD_PARTY_SOURCE}/{name}/{version}"
    verified, errors = set(), []
    sums = directory / shadowcode.SOURCE_SUMS
    if sums.is_file():
        sound = True
        for line in sums.read_text(encoding="utf-8", errors="replace").splitlines():
            match = re.fullmatch(r"([0-9a-f]{64})  ([^/\s][^/]*)", line)
            if not match:
                errors.append(f"{label}/{shadowcode.SOURCE_SUMS}: unreadable line {line[:80]!r}")
                sound = False
                continue
            path = directory / match.group(2)
            if path.is_symlink() or not path.is_file() or digest(path) != match.group(1):
                errors.append(f"{label}/{match.group(2)}: missing or not the bytes {shadowcode.SOURCE_SUMS} names")
                sound = False
            else:
                verified.add(match.group(2))
        if sound:
            verified.add(shadowcode.SOURCE_SUMS)
    if name == shadowcode.PACKAGE:
        verified |= signed_shadowcode_files(directory, version, label, errors)
    return verified, errors

def signed_shadowcode_files(directory, version, label, errors):
    """ShadowCode's signed release files, verified the way every consumer does."""
    pin = shadowcode.load_pin()
    if version != pin.version:
        errors.append(f"{label}: the ShadowCode pin is {pin.version}, not {version}")
        return set()
    verified = set()
    for name in shadowcode.METADATA_FILES:
        path, vendored = directory / name, pin.vendor_dir / name
        if not path.is_file():
            continue
        if vendored.is_file() and path.read_bytes() == vendored.read_bytes():
            verified.add(name)
        else:
            errors.append(f"{label}/{name}: not the signed metadata vendored at vendor/shadowcode/{version}/{name}")
    for role, asset in (("deb", pin.deb), ("runtime-sources", pin.runtime_sources)):
        path = directory / asset.filename
        if not path.is_file():
            continue
        try:
            shadowcode.verify_pinned_artifact(pin, path, role)
        except shadowcode.ShadowCodeError as error:
            errors.append(f"{label}/{asset.filename}: {error}")
        else:
            verified.add(asset.filename)
    readme = directory / shadowcode.SOURCE_README
    if readme.is_file():
        if readme.read_bytes() == shadowcode.source_readme(pin).encode("utf-8"):
            verified.add(shadowcode.SOURCE_README)
        else:
            errors.append(f"{label}/{shadowcode.SOURCE_README}: not the README tools/fetch_shadowcode.py stages for {version}")
    return verified

def pool_build_errors(repo, build):
    """Every .deb in the pool is byte-identical to the one this tree built."""
    debs = sorted((repo / "pool").rglob("*.deb"))
    if not debs:
        return ["repo/pool holds no .deb"]
    errors = []
    for deb in debs:
        built = build / deb.name
        if not built.is_file():
            errors.append(f"{deb.relative_to(repo)}: build/ has no {deb.name}")
        elif digest(built) != digest(deb):
            errors.append(f"{deb.relative_to(repo)} differs from build/{deb.name}")
    return errors

def signed_index_errors(dists, signed_text, codename, now=None):
    """The verified InRelease covers every index file exactly, and Release says the same.

    apt rejects an index whose bytes differ from what InRelease lists (Hash Sum
    mismatch), so a stale or hand-edited file in dists/ would break `apt update`
    on every installed system -- for an APT-only update, the whole release.
    Nothing unsigned is uploaded beside it either: InRelease is exactly one
    clearsigned message, and its dates are read from the text the signature
    covers, never from the file around it.
    """
    errors = inrelease_framing_errors(dists / "InRelease", codename)
    fields = dict(line.split(": ", 1) for line in signed_text.splitlines() if ": " in line and not line.startswith(" "))
    if fields.get("Codename") != codename:
        errors.append(f"the signed index is for {fields.get('Codename')!r}, not {codename!r}")
    errors.extend(signed_date_errors(fields, codename, now))
    listed, section = {}, None
    for line in signed_text.splitlines():
        if not line.startswith(" "):
            section = line[:-1] if line.endswith(":") else None
        elif section == "SHA256":
            sha, size, name = line.split()
            listed[name] = (sha, int(size))
    if not listed:
        errors.append("the signed index lists no SHA256 entries")
    for name, (sha, size) in sorted(listed.items()):
        path = dists / name
        if not path.is_file() or path.stat().st_size != size or digest(path) != sha:
            errors.append(f"dists/{codename}/{name}: missing or not the bytes the signed index names")
    present = {path.relative_to(dists).as_posix() for path in dists.rglob("*") if path.is_file()}
    unsigned = sorted(present - set(listed) - {"InRelease", "Release", "Release.gpg"})
    if unsigned:
        errors.append(f"files in dists/{codename} that the signed index does not cover: {', '.join(unsigned)}")
    release_file = dists / "Release"
    if not release_file.is_file() or release_file.read_text(encoding="utf-8") != signed_text:
        errors.append(f"dists/{codename}/Release is not the text InRelease signs")
    errors.extend(compressed_index_errors(dists, codename))
    return errors

def inrelease_framing_errors(path, codename):
    """InRelease is one clearsigned message and nothing else.

    gpgv verifies a clearsigned message with unsigned text before its header
    (or after its signature) and reports a good signature; apt refuses such a
    file. Text outside the signature is also what a line-oriented reader --
    pre_release_check.sh's Valid-Until grep -- would have believed.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        return [f"dists/{codename}/InRelease is unreadable: {error}"]
    errors = []
    if not lines or lines[0] != CLEARSIGN_BEGIN:
        errors.append(f"dists/{codename}/InRelease does not begin with {CLEARSIGN_BEGIN!r}: text before it is not covered by the signature")
    if not lines or lines[-1] != SIGNATURE_END:
        errors.append(f"dists/{codename}/InRelease does not end with {SIGNATURE_END!r}: text after it is not covered by the signature")
    for marker in (CLEARSIGN_BEGIN, SIGNATURE_BEGIN, SIGNATURE_END):
        if lines.count(marker) != 1:
            errors.append(f"dists/{codename}/InRelease holds {lines.count(marker)} {marker!r} lines, not one")
    return errors

def _rfc1123(value):
    try:
        moment = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)

def signed_date_errors(fields, codename, now=None):
    """Date and Valid-Until, as the signature covers them, are ones apt accepts
    now and will go on accepting for MIN_VALID_FOR."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    errors, moments = [], {}
    for name in ("Date", "Valid-Until"):
        raw = fields.get(name)
        moments[name] = _rfc1123(raw) if raw else None
        if moments[name] is None:
            errors.append(f"the signed index for {codename} has {'no' if raw is None else 'an unreadable'} {name}{'' if raw is None else ': ' + raw}")
    date, valid_until = moments["Date"], moments["Valid-Until"]
    if date is not None and date > now:
        errors.append(f"the signed index's Date {fields['Date']} is in the future; apt refuses it as not valid yet")
    if valid_until is not None:
        if date is not None and valid_until <= date:
            errors.append(f"the signed index's Valid-Until {fields['Valid-Until']} is not after its Date {fields['Date']}")
        if valid_until - now < MIN_VALID_FOR:
            state = "expired" if valid_until <= now else f"{(valid_until - now).days} days remaining"
            errors.append(
                f"the signed index's Valid-Until is {fields['Valid-Until']} ({state}); publishing needs "
                f"{MIN_VALID_FOR.days} days. Re-sign it (make refresh-index) and re-run")
    return errors

def compressed_index_errors(dists, codename):
    """Every compressed index is the uncompressed one beside it.

    apt downloads Packages.gz or Sources.gz in place of the plain file whenever
    the signed index lists it, while every check here -- versions, pool bytes,
    the acceptance digests -- reads the plain file. A stale or hand-made
    compressed copy would put something no check read in front of every
    installed system.
    """
    errors = []
    for path in sorted(dists.rglob("*")):
        if not path.is_file():
            continue
        relative = f"dists/{codename}/{path.relative_to(dists).as_posix()}"
        if path.suffix in UNREADABLE_COMPRESSIONS:
            errors.append(f"{relative}: no decompressor here to compare it with its uncompressed index, so apt would read an index nothing checked")
            continue
        opener = DECOMPRESSORS.get(path.suffix)
        if opener is None:
            continue
        plain = path.with_suffix("")
        if not plain.is_file():
            errors.append(f"{relative}: there is no uncompressed {plain.name} beside it, which is the index every check reads")
            continue
        expected = plain.read_bytes()
        try:
            with opener(path, "rb") as stream:
                data = stream.read(len(expected) + 1)
        except (OSError, EOFError, lzma.LZMAError, ValueError) as error:
            errors.append(f"{relative}: does not decompress ({error})")
            continue
        if data != expected:
            errors.append(f"{relative}: does not decompress to {plain.name}, the index every check reads")
    return errors

def dists_scope_errors(repo, codename):
    """Nothing under repo/dists but dists/<codename>/, which the signature covers.

    repository_objects uploads all of repo/dists, and an index file outside
    the signed suite would be published -- and would replace whatever the
    bucket holds at that key -- with nothing having checked it.
    """
    dists = repo / "dists"
    outside = sorted(
        path.relative_to(repo).as_posix() for path in dists.rglob("*")
        if (path.is_file() or path.is_symlink()) and codename != path.relative_to(dists).parts[0])
    if outside:
        return [f"files under repo/dists outside the signed suite dists/{codename}/: {', '.join(outside)}"]
    return []

def check_apt_only_scope(objects, codename=None):
    """What an APT-only publication may write, and in which order. Raises.

    Run when the plan is made and again immediately before the first network
    call, so no later change to the plan can slip the ISO, an evidence file or
    releases/CURRENT.json into a packages-only update.
    """
    codename = codename or RELEASE.codename
    terminal = (f"apt/dists/{codename}/Release.gpg", f"apt/dists/{codename}/Release", f"apt/dists/{codename}/InRelease")
    rank = 0
    for item in objects:
        if item.key == REPOSITORY_KEY:
            stage = 0
        elif item.key.startswith("apt/pool/"):
            stage = 1
        elif item.key in terminal:
            stage = 3
        elif item.key.startswith(f"apt/dists/{codename}/"):
            stage = 2
        else:
            # Including apt/dists/ outside the signed suite: an index there is
            # one no signature covers, replacing what the bucket holds.
            raise ValueError(f"An APT-only publication may not write {item.key}")
        if item.mutable != item.key.startswith("apt/dists/"):
            raise ValueError(f"Only APT index files may be replaced; {item.key} is marked {'mutable' if item.mutable else 'immutable'}")
        if stage < rank:
            raise ValueError(f"{item.key} would be written after something that directs a reader to it")
        rank = stage
    if not objects or objects[-1].key != terminal[-1]:
        raise ValueError("The signed InRelease must be the last object written")
    if len({item.key for item in objects}) != len(objects):
        raise ValueError("Duplicate publication object key")

def apt_only_plan(root, signed_text, release=None):
    """The APT repository objects, once the repository is shown to be this release.

    The ISO, its sidecars, the evidence files and releases/CURRENT.json are
    never read here, so whatever the tree holds they cannot be planned.
    """
    release = release or RELEASE
    repo = root / "repo"
    objects = repository_objects(root, release.codename)
    errors = [
        *repository_errors(repo, release),
        *pool_build_errors(repo, root / "build"),
        *dists_scope_errors(repo, release.codename),
        *signed_index_errors(repo / "dists" / release.codename, signed_text, release.codename),
    ]
    if errors:
        raise ValueError("APT-only publication refused:\n  - " + "\n  - ".join(errors))
    check_apt_only_scope(objects, release.codename)
    return objects

def publish_apt_only(client, objects):
    """Upload the repository, proving each object's bytes before the next write.

    Every object -- uploaded now or already present -- is streamed back and
    hashed before anything after it is written, so no index names a package,
    and InRelease names no index, that the bucket was not shown to hold.
    Nothing is deleted, and an immutable object that differs refuses the whole
    plan before the first upload (existing_matches).
    """
    check_apt_only_scope(objects)
    config = transfer_config()
    matches = {item.key: existing_matches(client, item) for item in objects}
    for item in objects:
        if matches[item.key]:
            print("UNCHANGED " + item.key, flush=True)
        else:
            upload_object(client, item, config)
        if remote_digest(client, item.key) != item.sha256:
            raise ValueError("R2 bytes do not match the release file: " + item.key)
    print(f"R2_APT_BYTES_VERIFIED objects={len(objects)}", flush=True)
    print("R2_APT_ONLY_PUBLISHED no ISO, evidence or releases/CURRENT.json was written", flush=True)

def credentialed_client():
    if sys.platform != "linux" or ROOT != PUBLISHER or os.geteuid() == 0:
        raise ValueError("Release publication must run from the authorized Linux 4.0 source tree, as a non-root user")
    endpoint = os.environ.get("SHADOWFETCH_R2_ENDPOINT", "")
    if not re.fullmatch(r"https://[a-f0-9]{32}\.r2\.cloudflarestorage\.com", endpoint):
        raise ValueError("Set the account's HTTPS R2 endpoint")
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        if not os.environ.get(name):
            raise ValueError("Missing process credential: " + name)
    import boto3
    return boto3.client("s3", endpoint_url=endpoint, region_name="auto")

def pre_release_check():
    # REPO_DIR and CODENAME are set, not inherited: exported in the operator's
    # shell, either would point the check at a repository other than the one
    # being published.
    subprocess.run([str(ROOT / "tools/pre_release_check.sh")], check=True, env=dict(
        os.environ, ROOT=str(ROOT), REPO_DIR=str(ROOT / "repo"), CODENAME=RELEASE.codename,
        REPO_MIN_VALID_FOR_SECONDS=str(int(MIN_VALID_FOR.total_seconds()))))

def main_apt_only(args):
    if args.published is not None:
        raise ValueError("--published dates releases/CURRENT.json, which an APT-only update never writes")
    selected_by = "release data" if delivery() == APT_ONLY else "--apt-only"
    print(f"PUBLICATION_MODE apt-only version={VERSION} selected_by={selected_by}", flush=True)
    apt_only_acceptance(ROOT)
    pre_release_check()
    plan = apt_only_plan(ROOT, verify_repository_signatures(ROOT))
    if not args.apply:
        print(json.dumps([{"key": item.key, "bytes": item.size, "sha256": item.sha256, "mutable": item.mutable} for item in plan], indent=2))
        return 0
    publish_apt_only(credentialed_client(), plan)
    return 0

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--published", default=None,
                        help="Publication timestamp for releases/CURRENT.json. "
                             "Defaults to the ISO's own mtime, so re-running "
                             "this rewrites nothing.")
    parser.add_argument("--apt-only", action="store_true",
                        help="Publish only the APT repository: a point update "
                             "that ships no ISO. Implied when the release data "
                             "says delivery = \"apt-only\".")
    args = parser.parse_args(argv)
    if publication_mode(args.apt_only) == APT_ONLY:
        return main_apt_only(args)
    subprocess.run(
        [sys.executable, str(ROOT / "tools/release/acceptance.py"),
         "--version", VERSION, "verify"],
        check=True,
    )
    pre_release_check()
    subprocess.run([SHA256SUM, "--check", ISO + ".sha256"], cwd=ROOT, check=True, env=trusted_env())
    verify_signatures(ROOT)
    plan = publication_plan(ROOT)
    iso = next(item for item in plan if item.path.name == ISO)
    pointer = pointer_object(ROOT, iso, args.published)
    if not args.apply:
        print(json.dumps([{"key": item.key, "bytes": item.size, "sha256": item.sha256, "mutable": item.mutable} for item in [*plan, pointer]], indent=2))
        return 0
    publish(credentialed_client(), plan, pointer)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
