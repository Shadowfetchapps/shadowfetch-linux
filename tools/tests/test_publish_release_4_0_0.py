import contextlib
import dataclasses
import gzip
import hashlib
import importlib.util
import io
import json
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

def build_repository(root, release=RELEASE, version_override=None):
    """repo/ and build/ as `make repo` leaves them, derived from the release data.

    Returns the text a signature over InRelease would cover. Nothing here is
    signed: signature verification is gpgv's job and is exercised on the real
    repository; these tests are about what is published, and in which order.
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
    write(repo / "pool/third-party-source/shadow-code/1.0.0/source.tar.gz", archive)
    write(repo / "pool/third-party-source/shadow-code/1.0.0/SOURCE-SHA256SUMS", f"{sha(archive)}  source.tar.gz\n")
    dists = repo / "dists" / release.codename
    packages = "\n".join(records).encode()
    source_index = "\n".join(sources).encode()
    indices = {
        "main/binary-amd64/Packages": packages,
        "main/binary-amd64/Packages.gz": gzip.compress(packages, mtime=0),
        "main/binary-amd64/Release": b"Component: main\nArchitecture: amd64\n",
        "main/source/Sources": source_index,
        "main/source/Sources.gz": gzip.compress(source_index, mtime=0),
        "main/source/Release": b"Component: main\nArchitecture: source\n",
    }
    for name, data in indices.items():
        write(dists / name, data)
    signed = (
        f"Origin: Shadowfetch\nCodename: {release.codename}\n"
        "Valid-Until: Mon, 29 Mar 2027 19:14:18 UTC\nSHA256:\n"
        + "".join(f" {sha(data)} {len(data)} {name}\n" for name, data in sorted(indices.items()))
    )
    write(dists / "Release", signed)
    write(dists / "Release.gpg", "-----BEGIN PGP SIGNATURE-----\nfixture\n")
    write(dists / "InRelease", "-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n" + signed + "-----BEGIN PGP SIGNATURE-----\nfixture\n-----END PGP SIGNATURE-----\n")
    return signed

BASE_IMAGE = "2d" * 32
EVIDENCE_TEXT = "{case} recorded by the harness against the update: every check held, exit 0.\n"

def write_acceptance(root, release=RELEASE, cases=None, artifact=None, bind_to=BASE_IMAGE):
    """qa/<v>/acceptance.json for an APT-only update, with real evidence files.

    `cases` maps a case id to its status; pass cases get one bound log each.
    """
    base = publisher.base_release(release)
    write(root / f"qa/{base.version}/acceptance.json", json.dumps({"artifact": {"iso_sha256": BASE_IMAGE}}))
    write(root / "Makefile", "fixture\n")
    (root / "packages").mkdir(exist_ok=True)
    evidence_root = root / f"work/qa-{release.version}/evidence"
    statuses = {case: "pass" for case in publisher.APT_ONLY_FLOOR}
    statuses.update({"ISO-01": "pending", "INSTALL-01": "pending", "VISUAL-01": "pending", "DURABLE-01": "pending"})
    statuses.update(cases or {})
    entries = []
    for case_id, status in statuses.items():
        case = {"id": case_id, "phase": "prepublish", "required": True, "status": status, "evidence": []}
        if status == "pass":
            data = EVIDENCE_TEXT.format(case=case_id).encode()
            write(evidence_root / f"{case_id}.log", data)
            case["evidence"] = [{"kind": "log", "path": f"{case_id}.log", "sha256": sha(data), "artifact_sha256": bind_to}]
        if status == "waived":
            case["waiver"] = {"approver": "release owner", "reason": "fixture waiver with a written reason"}
        entries.append(case)
    dists = root / "repo/dists" / release.codename
    document = {
        "schema_version": 1,
        "release": publisher.release_acceptance().expected_release(release),
        "evidence_root": f"work/qa-{release.version}/evidence",
        "artifact": {
            "iso_path": base.iso_name,
            "iso_sha256": BASE_IMAGE,
            "apt_packages_sha256": publisher.digest(dists / "main/binary-amd64/Packages"),
            "apt_sources_sha256": publisher.digest(dists / "main/source/Sources"),
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


class AptOnlyScopeTests(unittest.TestCase):
    def objects(self, *keys):
        return [publisher.Object(Path(key), key, "a" * 64, 1, key.startswith("apt/dists/")) for key in keys]

    def test_nothing_outside_the_repository_may_be_written(self):
        for key in ("releases/CURRENT.json", "releases/" + publisher.ISO, f"releases/evidence-bundle-{V}.tar.gz"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "may not write"):
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
