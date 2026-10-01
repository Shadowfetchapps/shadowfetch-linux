"""ShadowCode: pin, bump, gate and acceptance-registry tests.

ShadowCode is the one prebuilt package the release republishes, so the facts
worth testing are refusals: a version outside the vendored trust policy, a
tampered signature or asset, a downgrade, a same-version republication, a
release that ships ShadowCode without declaring it, a llama.cpp file anywhere
but /usr/lib/shadowcode/, and a SHADOWCODE-01 recording without its companion.

The bump tests sign synthetic releases with an EPHEMERAL Ed25519 key and run the
REAL vendored upstream verifier against them, so what is exercised is the
verifier that will run tonight, not a stand-in. They need OpenSSL 3; without
it they skip rather than pass.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock


TOOLS = Path(__file__).resolve().parents[1]
REPO_ROOT = TOOLS.parent
RELEASE_DIR = TOOLS / "release"
for entry in (str(TOOLS), str(RELEASE_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import gate  # noqa: E402
import shadowcode  # noqa: E402
from shadowcode import ShadowCodeError  # noqa: E402
import bump_shadowcode  # noqa: E402


def _load(name: str, path: Path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


package_gate = _load("shadowcode_test_package_gate", RELEASE_DIR / "package_gate.py")
iso_gate = _load("shadowcode_test_iso_gate", RELEASE_DIR / "iso_gate.py")

OPENSSL = shutil.which("openssl", path="/usr/bin:/bin")


def _openssl3() -> bool:
    if not OPENSSL:
        return False
    version = subprocess.run([OPENSSL, "version"], capture_output=True, text=True).stdout
    return version.startswith("OpenSSL 3.")


def with_prebuilt(release: gate.ReleaseData, table: dict) -> gate.ReleaseData:
    document = dict(release.document)
    document["packages"] = {**release.document["packages"], "prebuilt": table}
    return gate.ReleaseData(version=release.version, path=release.path, document=document)


# --------------------------------------------------------------------------
# The real pin, as committed
# --------------------------------------------------------------------------


class PinTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pin = shadowcode.load_pin()

    def test_the_pin_agrees_with_the_vendored_signed_document(self) -> None:
        auth = shadowcode.parse_release_auth(
            (self.pin.vendor_dir / "RELEASE-AUTH").read_text(encoding="ascii")
        )
        self.assertEqual([], shadowcode.pin_problems(auth, self.pin))

    def test_every_vendored_metadata_file_is_present(self) -> None:
        for name in shadowcode.METADATA_FILES:
            with self.subTest(name=name):
                self.assertTrue((self.pin.vendor_dir / name).is_file())

    def test_the_desktop_floor_is_the_pinned_version(self) -> None:
        control = shadowcode.META_CONTROL.read_text(encoding="utf-8")
        self.assertEqual(self.pin.version, shadowcode.meta_floor(control))

    def test_the_vendored_policy_covers_the_pin_and_nothing_past_it(self) -> None:
        shadowcode.check_in_policy(self.pin.version, self.pin.key_id)
        low, high = shadowcode.authorized_range(self.pin.key_id)
        above = high.split(".")
        above[2] = str(int(above[2]) + 1)
        with self.assertRaises(ShadowCodeError) as caught:
            shadowcode.check_in_policy(".".join(above), self.pin.key_id)
        self.assertIn("--refresh-trust", str(caught.exception))

    def test_versions_past_the_reviewed_policy_are_refused(self) -> None:
        # The policy reviewed at v1.0.0 authorises key f0c60ff8... for 0.33.0
        # through 1.0.0; the next minor and major need --refresh-trust first,
        # and a version below the floor is never accepted.
        for version in ("1.1.0", "2.0.0", "0.32.9"):
            with self.subTest(version=version):
                with self.assertRaises(ShadowCodeError) as caught:
                    shadowcode.check_in_policy(version, self.pin.key_id)
                self.assertIn("outside the vendored trust policy", str(caught.exception))

    def test_the_build_file_name_is_debians_canonical_one(self) -> None:
        self.assertEqual(f"shadow-code_{self.pin.version}_amd64.deb", self.pin.build_deb_name)

    def test_a_tampered_field_in_the_signed_document_is_a_pin_problem(self) -> None:
        text = (self.pin.vendor_dir / "RELEASE-AUTH").read_text(encoding="ascii")
        forged = text.replace(self.pin.deb.sha256, "0" * 64)
        auth = shadowcode.parse_release_auth(forged)
        problems = shadowcode.pin_problems(auth, self.pin)
        self.assertTrue(any(problem.startswith("deb:") for problem in problems))

    def test_a_foreign_repository_is_refused_by_the_parser(self) -> None:
        text = (self.pin.vendor_dir / "RELEASE-AUTH").read_text(encoding="ascii")
        with self.assertRaises(ShadowCodeError):
            shadowcode.parse_release_auth(
                text.replace("repository=Shadowfetchapps/ShadowCode", "repository=someone/else")
            )


@unittest.skipUnless(_openssl3(), "OpenSSL 3 is required by the upstream verifier")
class VendoredVerifierAgainstTheRealCache(unittest.TestCase):
    """Only when build/ holds the fetched .deb; a fresh clone skips this."""

    def test_the_staged_deb_verifies_end_to_end(self) -> None:
        pin = shadowcode.load_pin()
        if not pin.build_deb.is_file():
            self.skipTest(f"{pin.build_deb} not fetched on this host")
        line = shadowcode.verify_pinned_artifact(pin, pin.build_deb, "deb")
        self.assertIn(f"version={pin.version}", line)


# --------------------------------------------------------------------------
# Bump, against synthetic releases signed with an ephemeral key
# --------------------------------------------------------------------------


COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


class SyntheticUpstream:
    """A throwaway ShadowCode publisher: key, trust policy and signed releases."""

    def __init__(self, root: Path, maximum: str = "1.1.0") -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.key = root / "key.pem"
        self.pub = root / "pub.pem"
        subprocess.run([OPENSSL, "genpkey", "-algorithm", "ed25519", "-out", str(self.key)],
                       check=True, capture_output=True)
        subprocess.run([OPENSSL, "pkey", "-in", str(self.key), "-pubout", "-out", str(self.pub)],
                       check=True, capture_output=True)
        der = subprocess.run([OPENSSL, "pkey", "-pubin", "-in", str(self.pub), "-outform", "DER"],
                             check=True, capture_output=True).stdout
        self.key_id = hashlib.sha256(der).hexdigest()
        self.policy = (
            "ShadowCode-Release-Trust-v1\n"
            "repository=Shadowfetchapps/ShadowCode\n"
            "repository-id=1377099349\n"
            "owner-id=209457103\n"
            "target=x86_64-unknown-linux-gnu\n"
            "channel=stable\n"
            "minimum-epoch=1\n"
            "minimum-version=1.0.0\n"
            "keys=ed25519-spki-sha256\n"
            f"key=1\t{self.key_id}\t1.0.0\t{maximum}\n"
        )

    def release(self, version: str, *, deb: bytes | None = None, commit: str = COMMIT_A) -> Path:
        directory = self.root / f"release-{version}-{hashlib.sha256(deb or b'').hexdigest()[:8]}"
        directory.mkdir()
        names = shadowcode.asset_names(version)
        payloads = {
            "appimage": b"appimage " + version.encode(),
            "deb": deb if deb is not None else b"deb " + version.encode(),
            "runtime-sources": b"sources " + version.encode(),
        }
        (directory / names["deb"]).write_bytes(payloads["deb"])
        rows = [(role, names[role], len(data), hashlib.sha256(data).hexdigest())
                for role, data in payloads.items()]
        sums = "".join(f"{digest}  {name}\n" for _, name, _, digest in rows)
        manifest = '{"schema": 1, "runtime_pin": "commit=' + "c" * 40 + '"}\n'
        (directory / "SHA256SUMS").write_text(sums)
        (directory / "RELEASE-MANIFEST.json").write_text(manifest)
        auth = (
            "ShadowCode-Release-Auth-v1\n"
            "repository=Shadowfetchapps/ShadowCode\n"
            "repository-id=1377099349\n"
            "owner-id=209457103\n"
            f"version={version}\n"
            f"tag=v{version}\n"
            f"commit={commit}\n"
            "target=x86_64-unknown-linux-gnu\n"
            "channel=stable\n"
            f"key-id={self.key_id}\n"
            "key-epoch=1\n"
            f"manifest-sha256={hashlib.sha256(manifest.encode()).hexdigest()}\n"
            f"checksums-sha256={hashlib.sha256(sums.encode()).hexdigest()}\n"
            + "".join(f"asset={role}\t{name}\t{size}\t{digest}\n" for role, name, size, digest in rows)
        )
        (directory / "RELEASE-AUTH").write_text(auth)
        subprocess.run(
            [OPENSSL, "pkeyutl", "-sign", "-rawin", "-inkey", str(self.key),
             "-in", str(directory / "RELEASE-AUTH"), "-out", str(directory / "RELEASE-AUTH.sig")],
            check=True, capture_output=True,
        )
        return directory


@unittest.skipUnless(_openssl3(), "OpenSSL 3 is required by the upstream verifier")
class BumpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="shadowcode-bump-test-")
        root = Path(self.temporary.name)
        self.upstream = SyntheticUpstream(root / "upstream-publisher")
        self.upstream.root.mkdir(exist_ok=True)
        vendor = root / "vendor"
        (vendor / "trust").mkdir(parents=True)
        (vendor / "trust/policy").write_text(self.upstream.policy)
        shutil.copyfile(self.upstream.pub, vendor / "trust" / f"{self.upstream.key_id}.pem")
        shutil.copytree(shadowcode.UPSTREAM_DIR, vendor / "upstream")
        self.layout = bump_shadowcode.Layout(
            vendor=vendor,
            pin_file=root / "shadowcode.toml",
            control=root / "control",
            cache_root=root / "cache",
            readme=root / "README.md",
        )
        # Start from a vendored, pinned 1.0.0.
        self.r100 = self.upstream.release("1.0.0")
        (vendor / "1.0.0").mkdir()
        for name in shadowcode.METADATA_FILES:
            shutil.copyfile(self.r100 / name, vendor / "1.0.0" / name)
        auth = shadowcode.parse_release_auth((self.r100 / "RELEASE-AUTH").read_text())
        self.layout.pin_file.write_text(shadowcode.render_pin(
            version="1.0.0", commit=auth.commit, key_id=auth.key_id, key_epoch=1,
            trust_commit=COMMIT_B, ships_in=["5.0.0"],
            deb=auth.assets["deb"], runtime_sources=auth.assets["runtime-sources"],
        ))
        self.layout.control.write_text(
            "Package: shadowfetch-desktop\nDepends:\n shadowfetch-phoenix (= ${binary:Version}),\n"
            " shadow-code (>= 1.0.0),\n kde-plasma-desktop,\n"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def snapshot(self) -> dict[str, bytes]:
        paths = [self.layout.pin_file, self.layout.control,
                 *sorted(self.layout.vendor.rglob("*"))]
        return {str(p): p.read_bytes() for p in paths if p.is_file()}

    def test_rebumping_the_pinned_version_is_a_verified_no_op(self) -> None:
        before = self.snapshot()
        result = bump_shadowcode.bump("1.0.0", layout=self.layout, from_dir=self.r100)
        self.assertEqual("no-op", result)
        self.assertEqual(before, self.snapshot())

    def test_an_authorised_bump_rewrites_all_three_places_together(self) -> None:
        r110 = self.upstream.release("1.1.0", commit=COMMIT_B)
        self.assertEqual("bumped", bump_shadowcode.bump("1.1.0", layout=self.layout, from_dir=r110))
        pin = shadowcode.load_pin(self.layout.pin_file)
        self.assertEqual(("1.1.0", COMMIT_B), (pin.version, pin.commit))
        self.assertEqual("1.1.0", shadowcode.meta_floor(self.layout.control.read_text()))
        for name in shadowcode.METADATA_FILES:
            self.assertEqual((r110 / name).read_bytes(),
                             (self.layout.vendor / "1.1.0" / name).read_bytes())
        # ...and doing it again changes nothing.
        before = self.snapshot()
        self.assertEqual("no-op", bump_shadowcode.bump("1.1.0", layout=self.layout, from_dir=r110))
        self.assertEqual(before, self.snapshot())

    def test_dry_run_verifies_and_writes_nothing(self) -> None:
        r110 = self.upstream.release("1.1.0")
        before = self.snapshot()
        self.assertEqual("dry-run", bump_shadowcode.bump(
            "1.1.0", layout=self.layout, from_dir=r110, dry_run=True))
        self.assertEqual(before, self.snapshot())

    def assertRefusedUnchanged(self, version: str, release: Path, needle: str) -> None:
        before = self.snapshot()
        with self.assertRaises(ShadowCodeError) as caught:
            bump_shadowcode.bump(version, layout=self.layout, from_dir=release)
        self.assertIn(needle, str(caught.exception))
        self.assertEqual(before, self.snapshot(), "a refused bump must write nothing")

    def test_a_version_outside_the_policy_is_refused(self) -> None:
        self.assertRefusedUnchanged(
            "1.2.0", self.upstream.release("1.2.0"), "outside the vendored trust policy")

    def test_a_tampered_signed_document_is_refused(self) -> None:
        r110 = self.upstream.release("1.1.0")
        auth = r110 / "RELEASE-AUTH"
        auth.write_text(auth.read_text().replace(f"commit={COMMIT_A}", f"commit={COMMIT_B}"))
        self.assertRefusedUnchanged("1.1.0", r110, "invalid release signature")

    def test_a_tampered_asset_is_refused(self) -> None:
        r110 = self.upstream.release("1.1.0")
        deb = r110 / shadowcode.asset_names("1.1.0")["deb"]
        deb.write_bytes(deb.read_bytes()[:-1] + b"X")
        self.assertRefusedUnchanged("1.1.0", r110, "sha256")

    def test_a_signature_by_an_untrusted_key_is_refused(self) -> None:
        stranger = SyntheticUpstream(Path(self.temporary.name) / "stranger")
        stranger.root.mkdir(exist_ok=True)
        # Same version interval, different key: the policy does not name it.
        self.assertRefusedUnchanged(
            "1.1.0", stranger.release("1.1.0"), "is not in the vendored trust policy")

    def test_a_downgrade_is_refused(self) -> None:
        r110 = self.upstream.release("1.1.0")
        bump_shadowcode.bump("1.1.0", layout=self.layout, from_dir=r110)
        self.assertRefusedUnchanged("1.0.0", self.r100, "release downgrade")

    def test_a_changed_release_under_the_pinned_version_is_refused(self) -> None:
        republished = self.upstream.release("1.0.0", deb=b"different bytes, same version")
        self.assertRefusedUnchanged("1.0.0", republished, "changed release under accepted version")

    def test_the_desktop_floor_must_be_present_exactly_once(self) -> None:
        with self.assertRaises(ShadowCodeError):
            bump_shadowcode.rewrite_floor("Depends:\n kde-plasma-desktop,\n", "1.1.0")
        text = " shadow-code (>= 1.0.0),\n shadow-code (>= 1.0.0),\n"
        with self.assertRaises(ShadowCodeError):
            bump_shadowcode.rewrite_floor(text, "1.1.0")


# --------------------------------------------------------------------------
# Release data linkage and the gates
# --------------------------------------------------------------------------


class ReleaseLinkageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = gate.load_release("4.0.0")
        self.pin = shadowcode.load_pin()
        self.declared = with_prebuilt(self.base, {"shadow-code": "tools/release/shadowcode.toml"})
        # The release that ships this pin, read from the release data rather
        # than spelled as a literal: the pin's ships_in names it, and that
        # release's own data file must declare the package. A ShadowCode bump
        # moves ships_in, and these tests follow it.
        self.assertTrue(self.pin.ships_in, "shadowcode.toml ships_in names no release")
        self.shipping = self.pin.ships_in[-1]
        self.assertTrue(
            shadowcode.declared_in(gate.load_release(self.shipping).document),
            f"versions/{self.shipping}.toml does not declare shadow-code",
        )
        self.assertNotIn(self.base.version, self.pin.ships_in)

    def test_prebuilt_version_comes_from_the_pin_not_the_release_data(self) -> None:
        self.assertEqual(self.pin.version, self.declared.binary_versions["shadow-code"])
        self.assertNotIn("shadow-code", self.base.binary_versions)

    def test_a_prebuilt_package_has_no_source_package(self) -> None:
        self.assertNotIn("shadow-code", self.declared.source_packages)

    def test_declaring_a_package_twice_is_refused(self) -> None:
        document = dict(self.declared.document)
        document["packages"] = {**document["packages"],
                                "third_party": {"grub-btrfs": "4.14-2", "shadow-code": "0.33.1"}}
        twice = gate.ReleaseData(version="4.0.0", path=self.base.path, document=document)
        with self.assertRaises(gate.MissingReleaseData):
            twice.binary_versions

    def test_a_pin_file_naming_another_package_is_refused(self) -> None:
        wrong = with_prebuilt(self.base, {"not-shadow-code": "tools/release/shadowcode.toml"})
        with self.assertRaises(gate.MissingReleaseData):
            wrong.binary_versions

    def test_a_release_the_pin_ships_in_must_declare_it(self) -> None:
        with self.assertRaises(ShadowCodeError) as caught:
            shadowcode.check_release_linkage(self.shipping, self.base.document, self.pin)
        self.assertIn("[packages.prebuilt]", str(caught.exception))

    def test_a_release_may_not_declare_what_the_pin_does_not_ship_in(self) -> None:
        with self.assertRaises(ShadowCodeError):
            shadowcode.check_release_linkage("4.0.0", self.declared.document, self.pin)

    def test_consistent_linkage(self) -> None:
        self.assertFalse(shadowcode.check_release_linkage("4.0.0", self.base.document, self.pin))
        self.assertTrue(shadowcode.check_release_linkage(self.shipping, self.declared.document, self.pin))


class PackageGateShadowCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        base = gate.load_release("4.0.0")
        self.release = with_prebuilt(base, {"shadow-code": "tools/release/shadowcode.toml"})
        package_gate.RELEASE = self.release

    def test_the_container_pins_and_solves_shadow_code_through_the_desktop(self) -> None:
        script = package_gate.container_script(self.release)
        version = shadowcode.load_pin().version
        self.assertIn(f"shadow-code={version}", script)
        pillars = next(line for line in script.splitlines() if line.startswith("for pillar in"))
        self.assertIn("shadow-code", pillars.split())

    def test_releases_without_shadowcode_do_not_require_it(self) -> None:
        script = package_gate.container_script(gate.load_release("4.0.0"))
        self.assertNotIn("shadow-code", script)

    def test_the_text_scan_exemption_is_by_sole_owner(self) -> None:
        owners = {
            "usr/lib/shadowcode/COMMIT": ["shadow-code"],
            "usr/share/doc/shadowcode/notices/README.txt": ["shadow-code"],
            "usr/bin/shadowfetch-grok-bot": ["shadowfetch-defaults"],
            # A Shadowfetch package writing into ShadowCode's tree is NOT exempt.
            "usr/lib/shadowcode/sneaky": ["shadowfetch-defaults"],
        }
        exempt = package_gate.prebuilt_owned(owners, {"shadow-code"})
        self.assertEqual(
            {"usr/lib/shadowcode/COMMIT", "usr/share/doc/shadowcode/notices/README.txt"}, exempt)
        self.assertEqual(set(), package_gate.prebuilt_owned(owners, set()))

    def test_the_retired_runtime_scan_still_applies_to_shadowfetch_packages(self) -> None:
        self.assertTrue(package_gate.RETIRED_RUNTIME.search(b"exec llama-server --port 1"))

    def payload(self, owners: dict[str, list[str]]) -> None:
        with tempfile.TemporaryDirectory() as directory:
            entry = Path(directory) / shadowcode.DESKTOP_FILE
            entry.parent.mkdir(parents=True)
            entry.write_text("[Desktop Entry]\nExec=shadowcode\nType=Application\n")
            package_gate.shadowcode_payload_gate(owners, Path(directory))

    def good_owners(self) -> dict[str, list[str]]:
        return {path: ["shadow-code"] for path in (
            shadowcode.LAUNCHER, shadowcode.DESKTOP_FILE, shadowcode.LLAMA_SERVER,
            shadowcode.LLAMA_CLI, "usr/lib/shadowcode/libggml.so.0",
            "usr/share/doc/shadowcode/notices/upstream/llama.cpp-18f9f7b/llama.cpp-LICENSE",
        )}

    def test_the_shipped_layout_passes(self) -> None:
        self.payload(self.good_owners())

    def test_a_runtime_file_outside_usr_lib_shadowcode_fails(self) -> None:
        for path, owner in (
            ("usr/bin/llama-server", "shadow-code"),
            ("usr/lib/x86_64-linux-gnu/libggml.so.0", "shadow-code"),
            ("usr/lib/shadowfetch/llama-cli", "shadowfetch-defaults"),
            ("usr/lib/shadowcode/libllama.so", "shadowfetch-defaults"),
        ):
            with self.subTest(path=path, owner=owner):
                owners = self.good_owners()
                owners[path] = [owner]
                with self.assertRaises(RuntimeError):
                    self.payload(owners)

    def test_a_missing_launcher_fails(self) -> None:
        owners = self.good_owners()
        del owners[shadowcode.LAUNCHER]
        with self.assertRaises(RuntimeError):
            self.payload(owners)


class IsoGateShadowCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = gate.load_release("4.0.0")
        self.release = with_prebuilt(self.base, {"shadow-code": "tools/release/shadowcode.toml"})
        self.version = shadowcode.load_pin().version

    def test_shadow_code_is_expected_installed_at_exactly_the_pin(self) -> None:
        self.assertEqual(self.version, iso_gate.image_packages(self.release)["shadow-code"])

    def test_the_installed_selection_sees_shadow_code(self) -> None:
        installed = {"shadow-code": self.version, "shadowfetch-desktop": "4.0.0-1",
                     "git": "1:2.47"}
        self.assertEqual({"shadow-code": self.version, "shadowfetch-desktop": "4.0.0-1"},
                         iso_gate.custom_packages(installed, self.release))
        self.assertNotIn("shadow-code", iso_gate.custom_packages(installed, self.base))

    def test_a_wrong_or_missing_shadow_code_is_a_mismatch(self) -> None:
        expected = iso_gate.image_packages(self.release)
        for installed_version in ("0.1.0", None):
            with self.subTest(installed=installed_version):
                installed = dict(expected)
                if installed_version is None:
                    del installed["shadow-code"]
                else:
                    installed["shadow-code"] = installed_version
                self.assertNotEqual(expected, iso_gate.custom_packages(installed, self.release))

    def test_the_runtime_is_allowed_only_under_usr_lib_shadowcode(self) -> None:
        inventory = {
            "usr/lib/shadowcode/llama-server": "-rwxr-xr-x",
            "usr/lib/shadowcode/libggml-base.so.0.25.0": "-rwxr-xr-x",
            "usr/lib/shadowcode/libllama.so.0": "lrwxrwxrwx",
            "usr/share/doc/shadowcode/notices/upstream/llama.cpp-18f9f7b/llama.cpp-LICENSE": "-rw-r--r--",
            "usr/bin/shadowcode": "-rwxr-xr-x",
        }
        self.assertEqual([], iso_gate.misplaced_local_runtime(inventory, allowed=True))
        self.assertEqual(
            ["usr/lib/shadowcode/libggml-base.so.0.25.0", "usr/lib/shadowcode/libllama.so.0",
             "usr/lib/shadowcode/llama-server"],
            iso_gate.misplaced_local_runtime(inventory, allowed=False),
        )
        for stray in ("usr/bin/llama-server", "usr/local/bin/llama-cli",
                      "usr/lib/x86_64-linux-gnu/libggml.so.0", "usr/lib/x86_64-linux-gnu/libllama.so",
                      "opt/llama.cpp/build/bin/main", "usr/libexec/libmtmd.so"):
            with self.subTest(path=stray):
                self.assertEqual(
                    [stray], iso_gate.misplaced_local_runtime({stray: "-rwxr-xr-x"}, allowed=True))

    def test_retired_packages_are_still_refused(self) -> None:
        # NOTE: "libllama0" (Debian's actual library package name) does NOT
        # match RETIRED_PACKAGE's `libllama(?:-|$)`; reported, not changed here.
        for name in ("ollama", "llama.cpp", "llama.cpp-tools", "libllama"):
            with self.subTest(package=name):
                self.assertTrue(iso_gate.RETIRED_PACKAGE.search(name))
        self.assertIsNone(iso_gate.RETIRED_PACKAGE.search("shadow-code"))


# --------------------------------------------------------------------------
# VM acceptance registry
# --------------------------------------------------------------------------


class AcceptanceRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        from acceptance.cases import CASES, Blocked, Context
        self.CASES, self.Blocked, self.Context = CASES, Blocked, Context

    def test_both_cases_are_registered(self) -> None:
        self.assertIn("shadowcode", self.CASES)
        self.assertIn("shadowcode-soak", self.CASES)

    def test_shadowcode_01_is_recorded_only_from_the_soak_with_its_companion(self) -> None:
        soak = self.CASES["shadowcode-soak"]
        self.assertEqual("SHADOWCODE-01", soak.manifest_case)
        self.assertIsNone(soak.manifest_gap)
        self.assertIn({"case": "shadowcode"}, soak.required_runs)
        self.assertIsNone(self.CASES["shadowcode"].manifest_case)

    def test_an_unbootable_artifact_blocks_and_proves_nothing(self) -> None:
        for name in ("shadowcode", "shadowcode-soak"):
            with self.subTest(case=name), tempfile.TemporaryDirectory() as directory:
                ctx = self.Context(
                    name=name,
                    repo_root=REPO_ROOT,
                    run_dir=Path(directory) / "run",
                    evidence=None,
                    artifact={"path": str(Path(directory) / "missing.iso"), "sha256": "b" * 64},
                    options={"version": "5.0.0"},
                )
                with self.assertRaises(self.Blocked):
                    self.CASES[name].run(ctx)
                self.assertEqual([], ctx.checks)
                self.assertEqual(shadowcode.load_pin().version,
                                 ctx.observations["shadowcode_pin"]["version"])

    def test_the_soak_will_not_record_without_a_passing_shadowcode_run(self) -> None:
        from acceptance import release_link, vm_acceptance
        from acceptance.ledger import Ledger

        release = release_link.load_release(os.environ.get("SHADOWFETCH_RELEASE_VERSION"))
        receipt = {
            "verdict": "PASS", "run_id": "unit-run", "receipt_sha256": "c" * 64,
            "harness": {"digest": "d" * 64},
            "artifact": {"sha256": "e" * 64, "name": "unit.iso"},
            "checks": [{"name": "x", "state": "PASSED", "detail": ""}],
            "evidence": [{"relative_path": "Makefile", "sha256": "f" * 64}],
        }
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(Path(directory) / "ledger.jsonl")
            code = vm_acceptance._record(
                REPO_ROOT, release, self.CASES["shadowcode-soak"], receipt,
                Path(directory) / "receipt.json", ledger,
            )
        self.assertEqual(vm_acceptance.EXIT_BLOCKED, code)


fetch_tool = _load("shadowcode_test_fetch", TOOLS / "fetch_shadowcode.py")


class DebSourceTests(unittest.TestCase):
    """The .deb's corresponding source: exact commits, reproducible, offline-safe."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sc-src-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        upstream = self.tmp / "upstream"
        upstream.mkdir()
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                   GIT_COMMITTER_EMAIL="t@t", GIT_AUTHOR_DATE="2026-01-01T00:00:00Z",
                   GIT_COMMITTER_DATE="2026-01-01T00:00:00Z")
        subprocess.run(["git", "init", "-q", str(upstream)], check=True, env=env)
        (upstream / "main.rs").write_text("fn main() {}\n")
        subprocess.run(["git", "-C", str(upstream), "add", "."], check=True, env=env)
        subprocess.run(["git", "-C", str(upstream), "commit", "-qm", "x"], check=True, env=env)
        subprocess.run(["git", "-C", str(upstream), "config", "uploadpack.allowAnySHA1InWant", "true"], check=True)
        self.url = upstream.as_uri()
        self.commit = subprocess.run(["git", "-C", str(upstream), "rev-parse", "HEAD"], check=True,
                                     capture_output=True, text=True).stdout.strip()

    def archive(self, **kwargs):
        options = dict(git_cache=self.tmp / "git", out_dir=self.tmp / "out", offline=False)
        options.update(kwargs)
        return fetch_tool.archive_commit("demo", self.url, self.commit, **options)

    def test_the_archive_holds_exactly_the_named_commit_and_is_reproducible(self):
        first = self.archive()
        self.assertEqual(self.commit, shadowcode.archive_commit_id(first))
        digest = shadowcode.sha256_file(first)
        first.unlink()
        self.assertEqual(digest, shadowcode.sha256_file(self.archive(offline=True)))

    def test_a_tampered_archive_is_rebuilt_not_reused(self):
        path = self.archive()
        path.write_bytes(b"not an archive")
        self.assertEqual(self.commit, shadowcode.archive_commit_id(self.archive(offline=True)))

    def test_offline_with_nothing_cached_refuses(self):
        with self.assertRaisesRegex(ShadowCodeError, "not cached"):
            self.archive(offline=True)

    def test_the_pinned_release_names_three_https_sources_from_signed_data(self):
        pin = shadowcode.load_pin()
        names = [name for name, url, commit in shadowcode.source_inputs(pin)]
        self.assertEqual(["shadowcode", "llama.cpp", "spirv-headers"], names)
        self.assertEqual(pin.commit, shadowcode.source_inputs(pin)[0][2])



class PrebuiltLintianTests(unittest.TestCase):
    def test_an_exception_list_names_only_shadow_code_errors(self):
        # A clean release (0.34.2 and 1.0.0: no lintian errors) needs no list at all.
        accepted = package_gate.prebuilt_lintian_accepted(shadowcode.PACKAGE)
        self.assertTrue(all(line.startswith("E: shadow-code: ") for line in accepted))

    def test_a_version_without_a_list_accepts_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            with unittest.mock.patch.object(package_gate, "prebuilt_lintian_file",
                                            return_value=Path(folder) / "lintian-accepted"):
                self.assertEqual(set(), package_gate.prebuilt_lintian_accepted(shadowcode.PACKAGE))


if __name__ == "__main__":
    unittest.main(verbosity=2)
