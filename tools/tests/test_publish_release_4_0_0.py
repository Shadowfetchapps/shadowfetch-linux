import argparse
import contextlib
import dataclasses
import datetime
import gzip
import hashlib
import importlib.util
import io
import json
import lzma
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location("publisher4", Path(__file__).resolve().parents[1] / "publish_release_4_0_0.py")
publisher = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = publisher
spec.loader.exec_module(publisher)

# The version the PUBLISHER derived, not one written down here. These
# fixtures used to be built at a hard-coded qa/{V}, which was silently
# correct only while the tool carried the same literal; the day the tool
# started reading tools/release/versions/, that made the tests version
# sites that had been hiding behind the defect they should have caught.
V = publisher.VERSION
RELEASE = publisher.RELEASE
CODENAME = RELEASE.codename
DISTS = f"apt/dists/{CODENAME}/"
SIGNED_TRIO = [DISTS + "Release.gpg", DISTS + "Release", DISTS + "InRelease"]

class PublisherTests(unittest.TestCase):
    def test_different_immutable_object_is_never_overwritten(self):
        item = publisher.Object(Path("candidate"), "apt/pool/existing.deb", "a" * 64, 100)
        client = Mock()
        client.head_object.return_value = {"ContentLength": 200}
        with self.assertRaisesRegex(ValueError, "Refusing to replace"):
            publisher.existing_matches(client, item)
        client.upload_file.assert_not_called()
        client.delete_object.assert_not_called()

    def test_auth_failure_is_not_treated_as_missing_object(self):
        class Denied(Exception):
            response = {"Error": {"Code": "403"}}
        client = Mock()
        client.head_object.side_effect = Denied()
        with self.assertRaises(Denied):
            publisher.existing_matches(client, publisher.Object(Path("candidate"), "releases/image.iso", "a" * 64, 100))

    def test_plan_rejects_wrong_iso_before_network(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / publisher.ISO).write_bytes(b"different image")
            (root / f"qa/{V}").mkdir(parents=True)
            (root / f"qa/{V}/acceptance.json").write_text(json.dumps({"artifact": {"iso_sha256": "0" * 64, "iso_size_bytes": 15}}))
            with self.assertRaisesRegex(ValueError, "ISO differs"):
                publisher.publication_plan(root)

    def test_plan_publishes_all_artifacts_before_signed_apt_switch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [publisher.ISO, publisher.ISO + ".asc", publisher.ISO + ".sha256", "repo/shadowfetch.gpg.asc", "repo/pool/main/test.deb", "repo/dists/umbra/main/binary-amd64/Packages", "repo/dists/umbra/Release.gpg", "repo/dists/umbra/Release", "repo/dists/umbra/InRelease"]
            paths += [f"work/release-{V}/" + name for name in publisher.EVIDENCE]
            for name in paths:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(name.encode())
            (root / f"qa/{V}").mkdir(parents=True)
            artifact = {"iso_sha256": publisher.digest(root / publisher.ISO), "iso_size_bytes": (root / publisher.ISO).stat().st_size, "evidence_bundle_sha256": publisher.digest(root / f"work/release-{V}/evidence-bundle-{V}.tar.gz")}
            (root / f"qa/{V}/acceptance.json").write_text(json.dumps({"artifact": artifact}))
            plan = publisher.publication_plan(root)
            self.assertEqual("apt/dists/umbra/InRelease", plan[-1].key)
            self.assertTrue(all(not item.mutable for item in plan if item.key.startswith(("releases/", "apt/pool/"))))


# -- fixtures ------------------------------------------------------------------

def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    return path

def sha(data):
    return hashlib.sha256(data).hexdigest()

def build_full_tree(root):
    """An ISO release's tree: image, sidecars, evidence, and a repository."""
    for name in [publisher.ISO, publisher.ISO + ".asc", publisher.ISO + ".sha256"]:
        write(root / name, name)
    for name in publisher.EVIDENCE:
        write(root / f"work/release-{V}" / name, name)
    signed = build_repository(root)
    artifact = {"iso_sha256": publisher.digest(root / publisher.ISO), "iso_size_bytes": (root / publisher.ISO).stat().st_size, "evidence_bundle_sha256": publisher.digest(root / f"work/release-{V}/evidence-bundle-{V}.tar.gz")}
    write(root / f"qa/{V}/acceptance.json", json.dumps({"artifact": artifact}))
    return signed

def http_date(moment):
    return moment.strftime("%a, %d %b %Y %H:%M:%S UTC")

NOW = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
DATED = f"Date: {http_date(NOW - datetime.timedelta(hours=1))}\nValid-Until: {http_date(NOW + datetime.timedelta(days=180))}\n"
SHADOWCODE = publisher.shadowcode.PACKAGE

def build_repository(root, release=RELEASE, version_override=None, dates=DATED,
                     compressions=(".gz",), extra_indices=None):
    """repo/ and build/ as `make repo` leaves them, derived from the release data.

    Returns the text a signature over InRelease would cover. Nothing here is
    signed: signature verification is gpgv's job and is exercised on the real
    repository; these tests are about what is published, and in which order.
    `dates` is the Date/Valid-Until block of that text; `extra_indices` adds
    or replaces files under dists/<codename>/ before it is "signed".
    """
    version_override = version_override or {}
    repo, build = root / "repo", root / "build"
    write(repo / "shadowfetch.gpg.asc", "-----BEGIN PGP PUBLIC KEY BLOCK-----\nfixture key\n")
    records = []
    for name, version in release.binary_versions.items():
        version = version_override.get(name, version)
        data = f"{name} {version} payload\n".encode()
        filename = f"pool/main/{name[0]}/{name}/{name}_{version}_all.deb"
        write(repo / filename, data)
        write(build / Path(filename).name, data)
        records.append(f"Package: {name}\nVersion: {version}\nArchitecture: all\nFilename: {filename}\nSize: {len(data)}\nSHA256: {sha(data)}\n")
    third_party = release.document["packages"].get("third_party", {})
    sources = []
    for name in sorted(release.source_packages):
        version = third_party.get(name, f"{release.version}-{release.revision}")
        directory = f"pool/main/{name[0]}/{name}"
        data = f"Source: {name}\nVersion: {version}\n".encode()
        write(repo / directory / f"{name}_{version}.dsc", data)
        sources.append(f"Package: {name}\nVersion: {version}\nDirectory: {directory}\nChecksums-Sha256: \n {sha(data)} {len(data)} {name}_{version}.dsc\n")
    archive = b"third-party source archive\n"
    third_party = repo / "pool/third-party-source" / SHADOWCODE / version_override.get(SHADOWCODE, release.binary_versions[SHADOWCODE])
    write(third_party / "source.tar.gz", archive)
    write(third_party / "SOURCE-SHA256SUMS", f"{sha(archive)}  source.tar.gz\n")
    dists = repo / "dists" / release.codename
    packages = "\n".join(records).encode()
    source_index = "\n".join(sources).encode()
    compress = {".gz": lambda data: gzip.compress(data, mtime=0), ".xz": lzma.compress}
    indices = {
        "main/binary-amd64/Release": b"Component: main\nArchitecture: amd64\n",
        "main/source/Release": b"Component: main\nArchitecture: source\n",
    }
    for name, data in (("main/binary-amd64/Packages", packages), ("main/source/Sources", source_index)):
        indices[name] = data
        indices.update({name + suffix: compress[suffix](data) for suffix in compressions})
    indices.update(extra_indices or {})
    for name, data in indices.items():
        write(dists / name, data)
    signed = (
        f"Origin: Shadowfetch\nCodename: {release.codename}\n{dates}SHA256:\n"
        + "".join(f" {sha(data)} {len(data)} {name}\n" for name, data in sorted(indices.items()))
    )
    write(dists / "Release", signed)
    write(dists / "Release.gpg", "-----BEGIN PGP SIGNATURE-----\nfixture\n")
    write(dists / "InRelease", "-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n" + signed + "-----BEGIN PGP SIGNATURE-----\nfixture\n-----END PGP SIGNATURE-----\n")
    return signed

BASE_IMAGE = "2d" * 32
EVIDENCE_TEXT = "{case} recorded by the harness against the update: every check held, exit 0.\n"

def packages_under_test(root, release=RELEASE):
    dists = root / "repo/dists" / release.codename
    return {field: publisher.digest(dists / index) for field, index in publisher.PACKAGE_INDICES}

def write_base_manifest(root, release=RELEASE, cases=(), evidence_root=None):
    """qa/<base>/acceptance.json: the base release's own accepted manifest."""
    base = publisher.base_release(release)
    document = {"artifact": {"iso_sha256": BASE_IMAGE}, "cases": list(cases)}
    if evidence_root is not None:
        document["evidence_root"] = evidence_root
    write(root / f"qa/{base.version}/acceptance.json", json.dumps(document))
    return base

def write_acceptance(root, release=RELEASE, cases=None, artifact=None, bind_to=BASE_IMAGE, stamp=None):
    """qa/<v>/acceptance.json for an APT-only update, with real evidence files.

    `cases` maps a case id to its status; pass cases get one bound log each,
    stamped -- as `acceptance.py record` stamps it -- with the digests of the
    indices on disk, or with `stamp` when a test says otherwise ({} for none).
    Waivers are stamped the same way.
    """
    base = write_base_manifest(root, release)
    write(root / "Makefile", "fixture\n")
    (root / "packages").mkdir(exist_ok=True)
    evidence_root = root / f"work/qa-{release.version}/evidence"
    statuses = {case: "pass" for case in publisher.APT_ONLY_FLOOR}
    statuses.update({"ISO-01": "pending", "INSTALL-01": "pending", "VISUAL-01": "pending", "DURABLE-01": "pending"})
    statuses.update(cases or {})
    stamp = packages_under_test(root, release) if stamp is None else stamp
    entries = []
    for case_id, status in statuses.items():
        case = {"id": case_id, "phase": "prepublish", "required": True, "status": status, "evidence": []}
        if status == "pass":
            data = EVIDENCE_TEXT.format(case=case_id).encode()
            write(evidence_root / f"{case_id}.log", data)
            case["evidence"] = [{"kind": "log", "path": f"{case_id}.log", "sha256": sha(data), "artifact_sha256": bind_to, **stamp}]
        if status == "waived":
            case["waiver"] = {"approver": "release owner", "reason": "fixture waiver with a written reason", **stamp}
        entries.append(case)
    document = {
        "schema_version": 1,
        "release": publisher.release_acceptance().expected_release(release),
        "evidence_root": f"work/qa-{release.version}/evidence",
        "artifact": {
            "iso_path": base.iso_name,
            "iso_sha256": BASE_IMAGE,
            **packages_under_test(root, release),
            **(artifact or {}),
        },
        "cases": entries,
    }
    write(root / f"qa/{release.version}/acceptance.json", json.dumps(document, indent=2))
    return document

def with_data(base=RELEASE, fields=None, **tables):
    """The release data with [release] `fields` and whole tables replaced."""
    document = json.loads(json.dumps(base.document))
    document["release"].update(fields or {})
    document.update(tables)
    return dataclasses.replace(base, document=document)

APT_ONLY_RELEASE = with_data(fields={"delivery": "apt-only"})


class Missing(Exception):
    response = {"Error": {"Code": "NoSuchKey"}}

class Body:
    def __init__(self, data):
        self.data = data
    def iter_chunks(self, chunk_size):
        for start in range(0, len(self.data), chunk_size):
            yield self.data[start:start + chunk_size]
    def close(self):
        pass

class Bucket:
    """An in-memory R2 bucket that records every write and every read-back."""
    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.writes, self.streamed = [], []
        self.corrupt = set()
    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise Missing()
        data, metadata = self.objects[Key]
        return {"ContentLength": len(data), "Metadata": dict(metadata)}
    def upload_file(self, filename, bucket, key, Config=None, ExtraArgs=None):
        data = Path(filename).read_bytes()
        if key in self.corrupt:
            data = data[:-1] + bytes([data[-1] ^ 1])
        self.objects[key] = (data, dict((ExtraArgs or {}).get("Metadata", {})))
        self.writes.append(key)
    def get_object(self, Bucket, Key):
        self.streamed.append(Key)
        return {"Body": Body(self.objects[Key][0])}
    def delete_object(self, **arguments):
        raise AssertionError("a publication never deletes")

def quietly(function, *arguments, **keywords):
    with contextlib.redirect_stdout(io.StringIO()):
        return function(*arguments, **keywords)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.dists = self.root / "repo/dists" / CODENAME
        transfer = mock.patch.object(publisher, "transfer_config", return_value=None)
        transfer.start()
        self.addCleanup(transfer.stop)


# -- the ISO release is unchanged ---------------------------------------------

class FullReleaseUnchangedTests(Fixture):
    def test_full_plan_order_is_exactly_what_it_was(self):
        """Image, sidecars, evidence, key, pool, indices, signed trio -- the
        order the full publisher has always had, now that the repository part
        is shared with the packages-only mode."""
        build_full_tree(self.root)
        plan = publisher.publication_plan(self.root)
        repo = self.root / "repo"
        pool = ["apt/pool/" + path.relative_to(repo / "pool").as_posix() for path in sorted((repo / "pool").rglob("*")) if path.is_file()]
        indices = sorted("apt/dists/" + path.relative_to(repo / "dists").as_posix() for path in (repo / "dists").rglob("*") if path.is_file())
        indices = [key for key in indices if key not in SIGNED_TRIO]
        expected = [
            "releases/" + publisher.ISO, "releases/" + publisher.ISO + ".sha256", "releases/" + publisher.ISO + ".asc",
            *("releases/" + name for name in publisher.EVIDENCE),
            publisher.REPOSITORY_KEY, *pool, *indices, *SIGNED_TRIO,
        ]
        self.assertEqual(expected, [item.key for item in plan])
        self.assertEqual({key for key in expected if key.startswith("apt/dists/")}, {item.key for item in plan if item.mutable})

    def test_full_publish_writes_the_pointer_last_after_the_iso_is_streamed_back(self):
        build_full_tree(self.root)
        plan = publisher.publication_plan(self.root)
        pointer = publisher.pointer_object(self.root, plan[0])
        bucket = Bucket()
        quietly(publisher.publish, bucket, plan, pointer)
        self.assertEqual([item.key for item in plan] + ["releases/CURRENT.json"], bucket.writes)
        self.assertIn("releases/" + publisher.ISO, bucket.streamed)

    def test_an_iso_release_without_an_image_is_refused_not_degraded(self):
        """A missing ISO is never read as "so publish the packages only"."""
        build_full_tree(self.root)
        (self.root / publisher.ISO).unlink()
        self.assertEqual(publisher.DELIVERY_ISO, publisher.publication_mode(False, with_data()))
        with self.assertRaisesRegex(ValueError, "Missing, empty or symbolic-link"):
            publisher.publication_plan(self.root)

    def test_the_full_mode_still_starts_at_the_full_acceptance_gate(self):
        stop = RuntimeError("full acceptance gate reached")
        with mock.patch.object(publisher, "RELEASE", with_data()), \
                mock.patch.object(publisher, "main_apt_only") as apt_only, \
                mock.patch.object(publisher.subprocess, "run", side_effect=stop) as run:
            with self.assertRaises(RuntimeError):
                publisher.main([])
        apt_only.assert_not_called()
        self.assertEqual("verify", run.call_args.args[0][-1])


# -- mode selection --------------------------------------------------------------

class ModeTests(unittest.TestCase):
    def test_release_data_or_flag_selects_apt_only(self):
        iso = with_data(fields={"delivery": "iso"})
        self.assertEqual("iso", publisher.publication_mode(False, iso))
        self.assertEqual("iso", publisher.publication_mode(False, with_data()))
        self.assertEqual("apt-only", publisher.publication_mode(True, iso))
        self.assertEqual("apt-only", publisher.publication_mode(False, APT_ONLY_RELEASE))
        self.assertEqual("apt-only", publisher.publication_mode(True, APT_ONLY_RELEASE))

    def test_an_unknown_delivery_is_refused(self):
        with self.assertRaisesRegex(ValueError, "delivery"):
            publisher.publication_mode(True, with_data(fields={"delivery": "packages"}))

    def test_release_data_marking_routes_main_to_the_packages_only_mode(self):
        with mock.patch.object(publisher, "RELEASE", APT_ONLY_RELEASE), \
                mock.patch.object(publisher, "main_apt_only", return_value=0) as apt_only, \
                mock.patch.object(publisher.subprocess, "run", side_effect=AssertionError("full path")):
            self.assertEqual(0, publisher.main([]))
        apt_only.assert_called_once()

    def test_published_is_refused_because_no_pointer_is_written(self):
        with mock.patch.object(publisher, "apt_only_acceptance", side_effect=AssertionError("checked")):
            with self.assertRaisesRegex(ValueError, "never writes"):
                publisher.main(["--apt-only", "--published", "2026-10-01T00:00:00Z"])

    def test_the_expiry_check_examines_this_repository_whatever_the_shell_exports(self):
        exported = {"REPO_DIR": "/elsewhere/repo", "CODENAME": "elsewhere", "REPO_MIN_VALID_FOR_SECONDS": "1"}
        with mock.patch.dict(publisher.os.environ, exported), mock.patch.object(publisher.subprocess, "run") as run:
            publisher.pre_release_check()
        environment = run.call_args.kwargs["env"]
        self.assertEqual(str(publisher.ROOT / "repo"), environment["REPO_DIR"])
        self.assertEqual(RELEASE.codename, environment["CODENAME"])
        self.assertEqual(str(7 * 86400), environment["REPO_MIN_VALID_FOR_SECONDS"])

    def test_the_floor_cannot_be_removed_only_extended(self):
        self.assertEqual(publisher.APT_ONLY_FLOOR, publisher.apt_only_cases(with_data(apt_only={"acceptance": []})))
        self.assertEqual(
            (*publisher.APT_ONLY_FLOOR, "DURABLE-01"),
            publisher.apt_only_cases(with_data(apt_only={"acceptance": ["SRC-01", "DURABLE-01"]})))

    def test_the_base_is_the_newest_earlier_iso_release(self):
        base = publisher.base_release(APT_ONLY_RELEASE)
        self.assertLess(publisher._version_key(base.version), publisher._version_key(V))
        self.assertEqual("iso", publisher.delivery(base))
        with self.assertRaisesRegex(ValueError, "not an earlier ISO release"):
            publisher.base_release(with_data(apt_only={"base_release": V}))


# -- the packages-only plan --------------------------------------------------------

class AptOnlyPlanTests(Fixture):
    def plan(self, signed=None):
        signed = signed if signed is not None else self.signed
        return publisher.apt_only_plan(self.root, signed, APT_ONLY_RELEASE)

    def setUp(self):
        super().setUp()
        self.signed = build_repository(self.root)

    def test_key_then_pool_then_indices_then_the_signed_trio_last(self):
        plan = self.plan()
        keys = [item.key for item in plan]
        self.assertEqual(publisher.REPOSITORY_KEY, keys[0])
        pool = [index for index, key in enumerate(keys) if key.startswith("apt/pool/")]
        indices = [index for index, key in enumerate(keys) if key.startswith("apt/dists/")]
        self.assertTrue(pool and indices)
        self.assertLess(max(pool), min(indices), "an index would name a package not yet written")
        self.assertEqual(SIGNED_TRIO, keys[-3:])
        self.assertEqual(len(keys), 1 + len(pool) + len(indices))
        for item in plan:
            self.assertEqual(item.key.startswith("apt/dists/"), item.mutable, item.key)

    def test_the_iso_its_sidecars_the_evidence_and_the_pointer_are_never_planned(self):
        """Even when every one of them is sitting in the tree."""
        build_full_tree(self.root)
        write(self.root / f"work/release-{V}/CURRENT.json", "{}\n")
        keys = [item.key for item in self.plan(build_repository(self.root))]
        self.assertFalse([key for key in keys if key.startswith("releases/") or key.endswith((".iso", ".iso.asc", ".iso.sha256", "CURRENT.json"))])
        self.assertTrue(all(key == publisher.REPOSITORY_KEY or key.startswith("apt/") for key in keys))

    def test_a_pool_package_that_differs_from_build_is_refused(self):
        deb = next((self.root / "build").glob("shadowfetch-missions_*.deb"))
        deb.write_bytes(b"rebuilt after the repository was made\n")
        with self.assertRaisesRegex(ValueError, "differs from build/shadowfetch-missions_"):
            self.plan()

    def test_a_pool_package_this_tree_did_not_build_is_refused(self):
        next((self.root / "build").glob("shadowfetch-welcome_*.deb")).unlink()
        with self.assertRaisesRegex(ValueError, "build/ has no shadowfetch-welcome_"):
            self.plan()

    def test_a_repository_of_another_version_is_refused(self):
        stale = f"{V}-0"
        signed = build_repository(self.root, version_override={"shadowfetch-missions": stale})
        with self.assertRaisesRegex(ValueError, f"shadowfetch-missions: the binary index has {stale}, the release data says {V}-"):
            self.plan(signed)

    def test_an_index_file_the_signature_does_not_cover_is_refused(self):
        write(self.dists / "main/binary-amd64/Packages.xz", b"not signed")
        with self.assertRaisesRegex(ValueError, "does not cover: main/binary-amd64/Packages.xz"):
            self.plan()

    def test_an_index_that_is_not_the_signed_bytes_is_refused(self):
        write(self.dists / "main/source/Sources.gz", b"changed after signing")
        with self.assertRaisesRegex(ValueError, "main/source/Sources.gz: missing or not the bytes the signed index names"):
            self.plan()

    def test_a_release_file_that_is_not_the_signed_text_is_refused(self):
        write(self.dists / "Release", self.signed + "Extra: field\n")
        with self.assertRaisesRegex(ValueError, "Release is not the text InRelease signs"):
            self.plan()

    def test_a_source_file_that_is_not_the_indexed_bytes_is_refused(self):
        dsc = next((self.root / "repo/pool").rglob("shadowfetch-defaults_*.dsc"))
        dsc.write_bytes(b"edited source descriptor\n")
        with self.assertRaisesRegex(ValueError, "not the bytes the source index names"):
            self.plan()

    def test_a_signed_index_for_another_suite_is_refused(self):
        with self.assertRaisesRegex(ValueError, "the signed index is for"):
            self.plan(self.signed.replace(f"Codename: {CODENAME}", "Codename: elsewhere"))

    # -- what the signature covers, and only that ----------------------------

    def test_unsigned_text_around_the_clearsigned_message_is_refused(self):
        """gpgv reports a good signature over a clearsigned message with
        unsigned lines before its header or after its signature; apt refuses
        such a file. Here the signed text has expired and an unsigned line
        claims otherwise -- the line pre_release_check.sh's grep used to read."""
        expired = "Date: Sun, 31 Dec 2023 00:00:00 UTC\nValid-Until: Mon, 01 Jan 2024 00:00:00 UTC\n"
        signed = build_repository(self.root, dates=expired)
        inrelease = (self.dists / "InRelease").read_text()
        claim = f"Valid-Until: {http_date(NOW + datetime.timedelta(days=180))}\n"
        for where, text in (("begin with", claim + inrelease), ("end with", inrelease + claim)):
            with self.subTest(where):
                write(self.dists / "InRelease", text)
                with self.assertRaises(ValueError) as refused:
                    self.plan(signed)
                self.assertIn(f"InRelease does not {where}", str(refused.exception))
                self.assertIn("Valid-Until is Mon, 01 Jan 2024 00:00:00 UTC (expired)", str(refused.exception))

    def test_two_clearsigned_messages_in_one_inrelease_are_refused(self):
        inrelease = (self.dists / "InRelease").read_text()
        write(self.dists / "InRelease", inrelease + inrelease)
        with self.assertRaisesRegex(ValueError, "holds 2 '-----BEGIN PGP SIGNED MESSAGE-----' lines, not one"):
            self.plan()

    def test_valid_until_comes_from_the_signed_text_and_must_last_a_week(self):
        cases = {
            "expired": (NOW - datetime.timedelta(days=1), r"\(expired\)"),
            "three days": (NOW + datetime.timedelta(days=3), r"\(2 days remaining\); publishing needs 7 days"),
        }
        for name, (until, message) in cases.items():
            with self.subTest(name):
                signed = build_repository(self.root, dates=f"Date: {http_date(NOW - datetime.timedelta(days=30))}\nValid-Until: {http_date(until)}\n")
                with self.assertRaisesRegex(ValueError, message):
                    self.plan(signed)
        self.plan(build_repository(self.root, dates=f"Date: {http_date(NOW)}\nValid-Until: {http_date(NOW + datetime.timedelta(days=8))}\n"))

    def test_signed_dates_apt_would_refuse_are_refused(self):
        later = NOW + datetime.timedelta(days=180)
        cases = {
            "no Valid-Until": (f"Date: {http_date(NOW)}\n", "has no Valid-Until"),
            "no Date": (f"Valid-Until: {http_date(later)}\n", "has no Date"),
            "unreadable": (f"Date: yesterday\nValid-Until: {http_date(later)}\n", "unreadable Date: yesterday"),
            "future Date": (f"Date: {http_date(NOW + datetime.timedelta(days=1))}\nValid-Until: {http_date(later)}\n", "is in the future"),
        }
        for name, (dates, message) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(ValueError, message):
                self.plan(build_repository(self.root, dates=dates))

    def test_a_dists_file_outside_the_signed_suite_is_refused(self):
        """repository_objects uploads all of repo/dists; only dists/<codename>/
        is signed, so anything beside it would go up unchecked and replace
        what the bucket holds at that key."""
        write(self.root / "repo/dists/README", "not signed\n")
        write(self.root / "repo/dists/stable/InRelease", "not this suite\n")
        with self.assertRaisesRegex(ValueError, f"outside the signed suite dists/{CODENAME}/: dists/README, dists/stable/InRelease"):
            self.plan()

    def test_a_compressed_index_that_is_not_its_plain_index_is_refused(self):
        """Signed, so apt would accept it -- and download it in place of the
        Packages file every check here reads."""
        stale = gzip.compress(b"Package: shadowfetch-missions\nVersion: 5.0.0-1\nFilename: pool/main/s/gone.deb\n", mtime=0)
        signed = build_repository(self.root, extra_indices={"main/binary-amd64/Packages.gz": stale})
        with self.assertRaisesRegex(ValueError, "main/binary-amd64/Packages.gz: does not decompress to Packages"):
            self.plan(signed)
        signed = build_repository(self.root, extra_indices={"main/source/Sources.gz": b"not gzip at all"})
        with self.assertRaisesRegex(ValueError, "main/source/Sources.gz: does not decompress"):
            self.plan(signed)

    def test_every_compressed_form_is_compared_and_one_that_cannot_be_is_refused(self):
        self.plan(build_repository(self.root, compressions=(".gz", ".xz")))
        signed = build_repository(self.root, extra_indices={"main/binary-amd64/Packages.zst": b"(zstd frame)"})
        with self.assertRaisesRegex(ValueError, "Packages.zst: no decompressor"):
            self.plan(signed)
        signed = build_repository(self.root, extra_indices={"main/binary-amd64/Contents.gz": gzip.compress(b"x", mtime=0)})
        with self.assertRaisesRegex(ValueError, "Contents.gz: there is no uncompressed Contents beside it"):
            self.plan(signed)


class ThirdPartySourceTests(Fixture):
    """pool/third-party-source/ goes up as permanent objects; each file must
    be source of a package being published, and verified."""
    def setUp(self):
        super().setUp()
        self.signed = build_repository(self.root)
        self.pin = publisher.shadowcode.load_pin()
        self.folder = self.root / "repo/pool/third-party-source" / SHADOWCODE / RELEASE.binary_versions[SHADOWCODE]

    def plan(self):
        return publisher.apt_only_plan(self.root, self.signed, APT_ONLY_RELEASE)

    def test_the_fixture_tree_is_accepted(self):
        keys = [item.key for item in self.plan()]
        self.assertIn(f"apt/pool/third-party-source/{SHADOWCODE}/{self.folder.name}/source.tar.gz", keys)

    def test_another_version_directory_is_refused(self):
        write(self.root / f"repo/pool/third-party-source/{SHADOWCODE}/0.9.9/anything.bin", "stray\n")
        with self.assertRaisesRegex(ValueError, f"{SHADOWCODE}/0.9.9/anything.bin: the binary index lists no {SHADOWCODE} 0.9.9"):
            self.plan()

    def test_a_package_the_index_does_not_list_is_refused(self):
        write(self.root / "repo/pool/third-party-source/unlisted/1.0/source.tar.gz", "stray\n")
        with self.assertRaisesRegex(ValueError, "the binary index lists no unlisted 1.0"):
            self.plan()

    def test_a_partial_download_is_refused(self):
        write(self.folder / "source.tar.gz.partial", "half an archive\n")
        with self.assertRaisesRegex(ValueError, "source.tar.gz.partial: named in neither SOURCE-SHA256SUMS nor the signed release metadata"):
            self.plan()

    def test_a_file_outside_a_package_version_directory_is_refused(self):
        write(self.root / "repo/pool/third-party-source/README", "loose\n")
        write(self.folder / "nested/source.tar.gz", "deeper\n")
        with self.assertRaises(ValueError) as refused:
            self.plan()
        self.assertIn("pool/third-party-source/README: not at pool/third-party-source/<package>/<version>/<file>", str(refused.exception))
        self.assertIn("nested/source.tar.gz: not at", str(refused.exception))

    def test_an_archive_that_is_not_its_listed_bytes_is_refused(self):
        write(self.folder / "source.tar.gz", "replaced after it was summed\n")
        with self.assertRaisesRegex(ValueError, "source.tar.gz: missing or not the bytes SOURCE-SHA256SUMS names"):
            self.plan()

    def test_a_symbolic_link_is_refused(self):
        (self.folder / "link.tar.gz").symlink_to(self.folder / "source.tar.gz")
        with self.assertRaisesRegex(ValueError, "symbolic-link release file: .*link.tar.gz"):
            self.plan()
        repo = self.root / "repo"
        records = publisher.gate.parse_deb822((self.dists / "main/binary-amd64/Packages").read_text())
        self.assertIn(f"pool/third-party-source/{SHADOWCODE}/{self.folder.name}/link.tar.gz: a symbolic link, not a file",
                      publisher.third_party_source_errors(repo, records))

    def test_signed_metadata_and_the_readme_are_accepted_only_as_vendored_and_staged(self):
        for name in publisher.shadowcode.METADATA_FILES:
            write(self.folder / name, (self.pin.vendor_dir / name).read_bytes())
        write(self.folder / "README", publisher.shadowcode.source_readme(self.pin))
        self.plan()
        write(self.folder / "RELEASE-MANIFEST.json", (self.pin.vendor_dir / "RELEASE-MANIFEST.json").read_bytes() + b" ")
        write(self.folder / "README", publisher.shadowcode.source_readme(self.pin) + "edited\n")
        with self.assertRaises(ValueError) as refused:
            self.plan()
        self.assertIn("RELEASE-MANIFEST.json: not the signed metadata vendored at", str(refused.exception))
        self.assertIn("README: not the README tools/fetch_shadowcode.py stages", str(refused.exception))

    def test_a_signed_asset_is_accepted_only_through_the_upstream_verifier(self):
        runtime = write(self.folder / self.pin.runtime_sources.filename, "runtime sources\n")
        with mock.patch.object(publisher.shadowcode, "verify_pinned_artifact", return_value="VERIFIED") as verifier:
            self.plan()
        verifier.assert_called_once_with(self.pin, runtime, "runtime-sources")
        refusal = publisher.shadowcode.ShadowCodeError("upstream verifier refused it: bad signature")
        with mock.patch.object(publisher.shadowcode, "verify_pinned_artifact", side_effect=refusal), \
                self.assertRaisesRegex(ValueError, f"{self.pin.runtime_sources.filename}: upstream verifier refused it"):
            self.plan()


class AptOnlyScopeTests(unittest.TestCase):
    def objects(self, *keys):
        return [publisher.Object(Path(key), key, "a" * 64, 1, key.startswith("apt/dists/")) for key in keys]

    def test_nothing_outside_the_repository_may_be_written(self):
        for key in ("releases/CURRENT.json", "releases/" + publisher.ISO, f"releases/evidence-bundle-{V}.tar.gz"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "may not write"):
                publisher.check_apt_only_scope(self.objects("apt/pool/a.deb", key, *SIGNED_TRIO))

    def test_only_the_signed_suite_may_be_written_under_dists(self):
        for key in ("apt/dists/README", "apt/dists/stable/InRelease", f"apt/dists/{CODENAME}-proposed/Release"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "may not write " + key):
                publisher.check_apt_only_scope(self.objects("apt/pool/a.deb", key, *SIGNED_TRIO))

    def test_only_index_files_may_be_replaced(self):
        objects = self.objects("apt/pool/a.deb", *SIGNED_TRIO)
        objects[0] = dataclasses.replace(objects[0], mutable=True)
        with self.assertRaisesRegex(ValueError, "Only APT index files may be replaced"):
            publisher.check_apt_only_scope(objects)

    def test_nothing_is_written_after_what_directs_a_reader_to_it(self):
        with self.assertRaisesRegex(ValueError, "directs a reader"):
            publisher.check_apt_only_scope(self.objects(DISTS + "main/binary-amd64/Packages", "apt/pool/a.deb", *SIGNED_TRIO))
        with self.assertRaisesRegex(ValueError, "directs a reader"):
            publisher.check_apt_only_scope(self.objects("apt/pool/a.deb", DISTS + "InRelease", DISTS + "main/source/Sources"))
        with self.assertRaisesRegex(ValueError, "last object"):
            publisher.check_apt_only_scope(self.objects("apt/pool/a.deb", DISTS + "InRelease", DISTS + "Release"))


# -- the packages-only acceptance subset ---------------------------------------------

class AptOnlyAcceptanceTests(Fixture):
    def setUp(self):
        super().setUp()
        build_repository(self.root)

    def errors(self, release=APT_ONLY_RELEASE, **manifest):
        write_acceptance(self.root, release, **manifest)
        return publisher.apt_only_acceptance_errors(self.root, release)

    def test_the_floor_passing_is_enough_and_image_cases_may_stay_pending(self):
        self.assertEqual([], self.errors())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            publisher.apt_only_acceptance(self.root, APT_ONLY_RELEASE)
        self.assertIn("NOT_REQUIRED ISO-01 pending", output.getvalue())
        self.assertIn("APT_ONLY_ACCEPTANCE_PASSED required=SRC-01,PKG-01,UPGRADE-01", output.getvalue())

    def test_a_waived_floor_case_with_an_approver_and_reason_is_accepted(self):
        self.assertEqual([], self.errors(cases={"UPGRADE-01": "waived"}))

    def test_every_floor_case_must_be_pass_or_waived(self):
        for case in publisher.APT_ONLY_FLOOR:
            for status in ("pending", "blocked", "fail"):
                with self.subTest(case=case, status=status):
                    errors = self.errors(cases={case: status})
                    self.assertIn(f"{case}: required status is {status}, not pass or waived", errors)
                    with self.assertRaisesRegex(ValueError, "APT-only acceptance refused"):
                        quietly(publisher.apt_only_acceptance, self.root, APT_ONLY_RELEASE)

    def test_a_floor_case_cannot_be_made_optional_in_the_manifest(self):
        document = write_acceptance(self.root, APT_ONLY_RELEASE)
        next(case for case in document["cases"] if case["id"] == "PKG-01")["required"] = False
        write(self.root / f"qa/{V}/acceptance.json", json.dumps(document))
        self.assertIn("PKG-01: must stay a required prepublish case", publisher.apt_only_acceptance_errors(self.root, APT_ONLY_RELEASE))

    def test_a_recorded_failure_outside_the_subset_still_refuses(self):
        self.assertIn("ISO-01: recorded as fail", self.errors(cases={"ISO-01": "fail"}))

    def test_cases_the_release_data_adds_are_required_too(self):
        release = with_data(APT_ONLY_RELEASE, apt_only={"acceptance": ["DURABLE-01"]})
        self.assertIn("DURABLE-01: required status is pending, not pass or waived", self.errors(release))
        self.assertEqual([], self.errors(release, cases={"DURABLE-01": "pass"}))

    def test_evidence_about_another_image_is_refused(self):
        errors = self.errors(bind_to="0" * 64)
        self.assertTrue([error for error in errors if "SRC-01.evidence[0]: recorded against 0000000000000000" in error], errors)

    def test_the_manifest_must_name_the_base_image(self):
        errors = self.errors(artifact={"iso_sha256": "1" * 64})
        self.assertTrue([error for error in errors if error.startswith("artifact.iso_sha256 must name")], errors)

    def test_evidence_changed_after_recording_is_refused(self):
        write_acceptance(self.root, APT_ONLY_RELEASE)
        write(self.root / f"work/qa-{V}/evidence/UPGRADE-01.log", "rewritten after it was recorded, by hand\n")
        errors = publisher.apt_only_acceptance_errors(self.root, APT_ONLY_RELEASE)
        self.assertIn("UPGRADE-01.evidence[0]: SHA-256 mismatch for UPGRADE-01.log", errors)

    def test_acceptance_of_other_packages_does_not_describe_these(self):
        errors = self.errors(artifact={"apt_packages_sha256": "3" * 64})
        self.assertTrue([error for error in errors if error.startswith("artifact.apt_packages_sha256")], errors)
        write_acceptance(self.root, APT_ONLY_RELEASE)
        build_repository(self.root, version_override={"shadowfetch-missions": f"{V}-9"})
        errors = publisher.apt_only_acceptance_errors(self.root, APT_ONLY_RELEASE)
        self.assertTrue([error for error in errors if "the subset was not accepted against these packages" in error], errors)

    def test_a_manifest_for_another_release_is_refused(self):
        document = write_acceptance(self.root, APT_ONLY_RELEASE)
        document["release"]["version"] = "0.0.1"
        write(self.root / f"qa/{V}/acceptance.json", json.dumps(document))
        self.assertIn(f"release.version must be {V!r}", publisher.apt_only_acceptance_errors(self.root, APT_ONLY_RELEASE))

    # -- the base release's acceptance is not this update's ---------------------

    def matching(self, errors, *fragments):
        return [error for error in errors if all(fragment in error for fragment in fragments)]

    def test_the_base_releases_own_acceptance_is_not_this_updates(self):
        """The review's reproduction. Every receipt of the base release is bound
        to the base image -- the digest this update's evidence is bound to as
        well -- so its SRC-01 and PKG-01 logs and its UPGRADE-01 waiver, copied
        in with the manifest-level index digests set, used to pass."""
        base = publisher.base_release(APT_ONLY_RELEASE)
        base_root = f"work/qa-{base.version}/evidence"
        base_cases = []
        for case_id in ("SRC-01", "PKG-01"):
            data = f"{case_id} gate for {base.version}: every package at {base.version}-1, exit 0\n".encode()
            write(self.root / base_root / f"gates/{case_id}.log", data)
            base_cases.append({"id": case_id, "status": "pass", "evidence": [
                {"kind": "log", "path": f"gates/{case_id}.log", "sha256": sha(data), "artifact_sha256": BASE_IMAGE}]})
        base_cases.append({"id": "UPGRADE-01", "status": "waived", "evidence": [],
                           "waiver": {"approver": "release owner", "reason": f"{base.version}: the upgrade VM ran out of time"}})
        document = write_acceptance(self.root, APT_ONLY_RELEASE, cases={"UPGRADE-01": "waived"})
        write_base_manifest(self.root, APT_ONLY_RELEASE, base_cases, base_root)
        copied = {case["id"]: json.loads(json.dumps(case)) for case in base_cases}
        for case in document["cases"]:
            if case["id"] in copied:
                case.update({key: copied[case["id"]][key] for key in ("status", "evidence", "waiver") if key in copied[case["id"]]})
                for item in case["evidence"]:
                    write(self.root / f"work/qa-{V}/evidence" / item["path"], (self.root / base_root / item["path"]).read_bytes())
        write(self.root / f"qa/{V}/acceptance.json", json.dumps(document))
        errors = publisher.apt_only_acceptance_errors(self.root, APT_ONLY_RELEASE)
        for case_id in ("SRC-01", "PKG-01"):
            self.assertTrue(self.matching(errors, f"{case_id}.evidence[0]: carries no apt_packages_sha256"), errors)
            self.assertTrue(self.matching(errors, f"{case_id}.evidence[0]: gates/{case_id}.log is {base.version}'s own evidence"), errors)
        self.assertTrue(self.matching(errors, "UPGRADE-01.waiver: carries no apt_sources_sha256"), errors)
        self.assertTrue(self.matching(errors, f"UPGRADE-01.waiver: the reason is {base.version}'s waiver of UPGRADE-01, word for word"), errors)
        # Stamping the copies by hand does not make them this update's.
        for case in document["cases"]:
            for item in [*case["evidence"], *([case["waiver"]] if "waiver" in case else [])]:
                item.update(packages_under_test(self.root))
        write(self.root / f"qa/{V}/acceptance.json", json.dumps(document))
        errors = publisher.apt_only_acceptance_errors(self.root, APT_ONLY_RELEASE)
        self.assertFalse(self.matching(errors, "carries no"), errors)
        self.assertEqual(3, len(self.matching(errors, f"{base.version}'s")), errors)

    def test_evidence_recorded_against_an_earlier_build_of_the_packages_is_refused(self):
        """The manifest names this repository; the entries were recorded before
        it was rebuilt. Setting the manifest-level digest binds nothing."""
        errors = self.errors(stamp={"apt_packages_sha256": "3" * 64, "apt_sources_sha256": "4" * 64})
        for case_id in publisher.APT_ONLY_FLOOR:
            self.assertTrue(self.matching(errors, f"{case_id}.evidence[0]: recorded against main/binary-amd64/Packages 3333333333333333..., not the one being published"), errors)
            self.assertTrue(self.matching(errors, f"{case_id}.evidence[0]: recorded against main/source/Sources 4444444444444444..."), errors)
        self.assertFalse([error for error in errors if error.startswith("artifact.apt_")], errors)

    def test_a_waiver_is_bound_to_the_packages_it_was_decided_about(self):
        errors = self.errors(cases={"UPGRADE-01": "waived"}, stamp={})
        self.assertTrue(self.matching(errors, "UPGRADE-01.waiver: carries no apt_packages_sha256"), errors)

    def test_the_base_releases_evidence_directory_is_not_this_updates(self):
        base = publisher.base_release(APT_ONLY_RELEASE)
        document = write_acceptance(self.root, APT_ONLY_RELEASE)
        write_base_manifest(self.root, APT_ONLY_RELEASE, [], f"work/qa-{base.version}/evidence")
        for evidence_root, prefix in ((f"work/qa-{base.version}/evidence", ""), ("work", f"qa-{base.version}/evidence/")):
            with self.subTest(evidence_root=evidence_root):
                moved = json.loads(json.dumps(document))
                moved["evidence_root"] = evidence_root
                for case in moved["cases"]:
                    for item in case["evidence"]:
                        write(self.root / f"work/qa-{base.version}/evidence" / item["path"], (self.root / f"work/qa-{V}/evidence" / item["path"]).read_bytes())
                        item["path"] = prefix + item["path"]
                write(self.root / f"qa/{V}/acceptance.json", json.dumps(moved))
                errors = publisher.apt_only_acceptance_errors(self.root, APT_ONLY_RELEASE)
                self.assertEqual(3, len(self.matching(errors, f"is in {base.version}'s evidence directory")), errors)
                self.assertEqual(not prefix, bool(self.matching(errors, f"evidence_root {self.root.resolve() / evidence_root} is {base.version}'s evidence directory")), errors)

    def test_what_the_recorder_writes_is_what_the_publisher_accepts(self):
        """acceptance.py record stamps the index digests from the manifest; a
        rebuilt repository is refused until the subset is recorded again."""
        acceptance = publisher.release_acceptance()
        manifest = self.root / f"qa/{V}/acceptance.json"

        def record_floor():
            for case_id in publisher.APT_ONLY_FLOOR:
                log = write(self.root / f"work/qa-{V}/evidence/{case_id}-run.log", EVIDENCE_TEXT.format(case=case_id) + publisher.digest(self.dists / "main/binary-amd64/Packages") + "\n")
                quietly(acceptance.record, argparse.Namespace(
                    manifest=manifest, release=APT_ONLY_RELEASE, case_id=case_id, status="pass", evidence=[log],
                    kind="log", notes=None, waiver_approver=None, waiver_reason=None, clear_evidence=False))

        write_acceptance(self.root, APT_ONLY_RELEASE, cases={case: "pending" for case in publisher.APT_ONLY_FLOOR})
        record_floor()
        self.assertEqual([], publisher.apt_only_acceptance_errors(self.root, APT_ONLY_RELEASE))
        build_repository(self.root, version_override={"shadowfetch-missions": f"{V}-9"})
        document = json.loads(manifest.read_text())
        document["artifact"].update(packages_under_test(self.root))
        write(manifest, json.dumps(document))
        errors = publisher.apt_only_acceptance_errors(self.root, APT_ONLY_RELEASE)
        self.assertEqual(3, len(self.matching(errors, "recorded against main/binary-amd64/Packages", "not the one being published")), errors)
        self.assertFalse(self.matching(errors, "main/source/Sources"), "the rebuild changed no source package")
        record_floor()
        self.assertEqual([], publisher.apt_only_acceptance_errors(self.root, APT_ONLY_RELEASE))


# -- the packages-only upload --------------------------------------------------------

class AptOnlyPublishTests(Fixture):
    def setUp(self):
        super().setUp()
        self.plan = publisher.apt_only_plan(self.root, build_repository(self.root), APT_ONLY_RELEASE)

    def test_written_in_plan_order_each_proven_before_the_next_and_inrelease_last(self):
        bucket = Bucket()
        quietly(publisher.publish_apt_only, bucket, self.plan)
        keys = [item.key for item in self.plan]
        self.assertEqual(keys, bucket.writes)
        self.assertEqual(DISTS + "InRelease", bucket.writes[-1])
        self.assertEqual(keys, bucket.streamed, "every object is streamed back, in order")
        self.assertFalse([key for key in bucket.objects if key.startswith("releases/")])

    def test_previous_release_objects_are_kept_and_only_indices_replaced(self):
        old_deb = "apt/pool/main/s/shadowfetch-missions/shadowfetch-missions_5.0.0-1_all.deb"
        old_index = DISTS + "main/binary-amd64/Packages"
        bucket = Bucket({
            old_deb: (b"previous release", {"sha256": sha(b"previous release")}),
            old_index: (b"previous index", {"sha256": sha(b"previous index")}),
            "releases/CURRENT.json": (b"{}", {"sha256": sha(b"{}")}),
        })
        quietly(publisher.publish_apt_only, bucket, self.plan)
        self.assertEqual(b"previous release", bucket.objects[old_deb][0])
        self.assertEqual(b"{}", bucket.objects["releases/CURRENT.json"][0])
        self.assertEqual((self.root / "repo/dists" / CODENAME / "main/binary-amd64/Packages").read_bytes(), bucket.objects[old_index][0])

    def test_a_different_immutable_object_refuses_before_the_first_upload(self):
        pooled = next(item for item in self.plan if item.key.startswith("apt/pool/") and item.key.endswith(".deb"))
        bucket = Bucket({pooled.key: (b"x" * pooled.size, {"sha256": "f" * 64})})
        with self.assertRaisesRegex(ValueError, "Refusing to replace a different immutable object"):
            quietly(publisher.publish_apt_only, bucket, self.plan)
        self.assertEqual([], bucket.writes)

    def test_bytes_that_do_not_stream_back_stop_before_any_index_is_written(self):
        bucket = Bucket()
        corrupted = next(item.key for item in self.plan if item.key.startswith("apt/pool/"))
        bucket.corrupt.add(corrupted)
        with self.assertRaisesRegex(ValueError, "R2 bytes do not match the release file: " + corrupted):
            quietly(publisher.publish_apt_only, bucket, self.plan)
        self.assertEqual(corrupted, bucket.writes[-1])
        self.assertFalse([key for key in bucket.writes if key.startswith("apt/dists/")])

    def test_a_second_run_writes_nothing(self):
        bucket = Bucket()
        quietly(publisher.publish_apt_only, bucket, self.plan)
        bucket.writes.clear()
        quietly(publisher.publish_apt_only, bucket, self.plan)
        self.assertEqual([], bucket.writes)

    def test_a_plan_that_strays_outside_the_repository_is_refused_before_the_network(self):
        stray = publisher.Object(self.root / "repo/shadowfetch.gpg.asc", "releases/CURRENT.json", "a" * 64, 1, True)
        bucket = Mock()
        with self.assertRaisesRegex(ValueError, "may not write releases/CURRENT.json"):
            publisher.publish_apt_only(bucket, [stray, *self.plan])
        self.assertEqual([], bucket.mock_calls)


class AptOnlyMainTests(Fixture):
    """main() in packages-only mode, with the gates that need gpg and git stubbed."""
    def run_main(self, *arguments, client=None):
        signed = build_repository(self.root)
        write_acceptance(self.root, APT_ONLY_RELEASE)
        output = io.StringIO()
        with mock.patch.object(publisher, "ROOT", self.root), \
                mock.patch.object(publisher, "RELEASE", APT_ONLY_RELEASE), \
                mock.patch.object(publisher, "pre_release_check") as check, \
                mock.patch.object(publisher, "verify_repository_signatures", return_value=signed) as signatures, \
                mock.patch.object(publisher, "credentialed_client", return_value=client) as credentials, \
                contextlib.redirect_stdout(output):
            code = publisher.main(list(arguments))
        check.assert_called_once()
        signatures.assert_called_once()
        return code, output.getvalue(), credentials

    def test_plan_mode_prints_only_repository_objects_and_needs_no_credentials(self):
        code, output, credentials = self.run_main()
        self.assertEqual(0, code)
        credentials.assert_not_called()
        self.assertIn("PUBLICATION_MODE apt-only", output)
        plan = json.loads(output[output.index("\n[") + 1:])
        self.assertEqual(DISTS + "InRelease", plan[-1]["key"])
        self.assertFalse([item for item in plan if item["key"].startswith("releases/")])
        self.assertFalse(list((self.root / f"work/release-{V}").glob("CURRENT.json")))

    def test_apply_writes_the_repository_through_the_same_credential_path(self):
        bucket = Bucket()
        code, output, credentials = self.run_main("--apply", client=bucket)
        self.assertEqual(0, code)
        credentials.assert_called_once_with()
        self.assertEqual(DISTS + "InRelease", bucket.writes[-1])
        self.assertIn("R2_APT_ONLY_PUBLISHED", output)


if __name__ == "__main__":
    unittest.main()
