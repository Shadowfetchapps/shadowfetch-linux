"""Unit tests for tools/pre_release_check.sh (W-03: no silent skip without git)."""

from __future__ import annotations

import datetime
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "pre_release_check.sh"


def http_date(moment: datetime.datetime) -> str:
    return moment.strftime("%a, %d %b %Y %H:%M:%S UTC")


def clearsigned(signed_text: str) -> str:
    """InRelease's shape: one clearsigned message. The signature is not real;
    gpgv is the publisher's job, this script reads the signed text."""
    return (
        "-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\n"
        + signed_text
        + "-----BEGIN PGP SIGNATURE-----\n\niHUEARYKAB0WIQQfixture\n=abcd\n"
        "-----END PGP SIGNATURE-----\n"
    )


@unittest.skipUnless(shutil.which("git"), "git is required")
class PreReleaseCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        distribution = self.root / "repo" / "dists" / "umbra"
        (distribution / "main" / "binary-amd64").mkdir(parents=True)
        (distribution / "main" / "source").mkdir(parents=True)
        self.now = datetime.datetime.now(datetime.timezone.utc)
        self.inrelease = distribution / "InRelease"
        self.inrelease.write_text(
            clearsigned(
                "Origin: Shadowfetch\n"
                f"Valid-Until: {http_date(self.now + datetime.timedelta(days=30))}\n"
            ),
            encoding="utf-8",
        )
        (distribution / "main" / "binary-amd64" / "Packages").write_text(
            "Package: shadowfetch-defaults\nVersion: 4.0.0-1\n\n", encoding="utf-8"
        )
        (distribution / "main" / "source" / "Sources").write_text(
            "Package: shadowfetch-defaults\nVersion: 4.0.0-1\n\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def check(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(SCRIPT)],
            env={**os.environ, "ROOT": str(self.root)},
            capture_output=True,
            text=True,
        )

    def test_unusable_git_is_a_hard_failure(self) -> None:
        """The tree is not a repository: the credential-state check cannot run."""
        result = self.check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("PRE_RELEASE_CHECK_FAILED", result.stderr)
        self.assertIn("tracked-credential-state check cannot run", result.stderr)
        self.assertNotIn("PRE_RELEASE_CHECK_PASSED", result.stdout)

    def test_missing_git_executable_is_a_hard_failure(self) -> None:
        empty_path = self.root / "no-tools"
        empty_path.mkdir()
        for name in ("bash", "grep", "date", "find", "awk", "comm", "sort", "paste"):
            source = shutil.which(name)
            if source:
                (empty_path / name).symlink_to(source)
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            env={"ROOT": str(self.root), "PATH": str(empty_path)},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("git is not installed", result.stderr)

    def test_healthy_git_tree_still_passes(self) -> None:
        """Invariant: with a working git and a clean tree the check passes as before."""
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PRE_RELEASE_CHECK_PASSED", result.stdout)

    def _third_party(self, tamper: bool = False) -> None:
        """A republished binary whose source ships as third-party archives."""
        import hashlib
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        packages = self.root / "repo/dists/umbra/main/binary-amd64/Packages"
        packages.write_text(packages.read_text() + "Package: shadow-code\nVersion: 1.0.0\n\n")
        folder = self.root / "repo/pool/third-party-source/shadow-code/1.0.0"
        folder.mkdir(parents=True)
        archive = folder / "shadowcode-abc.tar.gz"
        archive.write_bytes(b"source")
        digest = hashlib.sha256(b"source").hexdigest()
        (folder / "SOURCE-SHA256SUMS").write_text(f"{digest}  shadowcode-abc.tar.gz\n")
        if tamper:
            archive.write_bytes(b"changed")

    def test_verified_third_party_source_satisfies_the_offer(self) -> None:
        self._third_party()
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("shadow-code 1.0.0 source published as verified third-party archives", result.stdout)

    def test_tampered_third_party_source_still_fails(self) -> None:
        self._third_party(tamper=True)
        result = self.check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("no corresponding source in main/source: shadow-code", result.stderr)

    def test_a_token_file_is_named_relative_to_a_root_with_glob_characters(self) -> None:
        """$ROOT is stripped as a literal prefix, not matched as a pattern."""
        root = self.root / "release[x]"
        (root / "sub").mkdir(parents=True)
        (root / "sub" / ".write-token.txt").write_text("token\n", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(SCRIPT)],
            env={**os.environ, "ROOT": str(root)},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("present in release tree: sub/.write-token.txt\n", result.stderr)

    def test_valid_until_outside_the_signed_text_is_not_believed(self) -> None:
        """gpgv verifies a clearsigned message with unsigned lines before its
        header; apt does not. The signed text here expired in 2024, and an
        unsigned line above the header says 2027 -- a first-match grep of
        the file read that one and passed."""
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        expired = clearsigned("Origin: Shadowfetch\nValid-Until: Mon, 01 Jan 2024 00:00:00 UTC\n")
        later = f"Valid-Until: {http_date(self.now + datetime.timedelta(days=180))}\n"
        for name, text in (("before the header", later + expired), ("after the signature", expired + later)):
            with self.subTest(name):
                self.inrelease.write_text(text, encoding="utf-8")
                result = self.check()
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("is not exactly one clearsigned message", result.stderr)
        self.inrelease.write_text(expired, encoding="utf-8")
        result = self.check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("expired Valid-Until", result.stderr)

    def test_two_clearsigned_messages_are_refused(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        text = self.inrelease.read_text(encoding="utf-8")
        self.inrelease.write_text(text + text, encoding="utf-8")
        result = self.check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("is not exactly one clearsigned message", result.stderr)

    def test_valid_until_missing_from_the_signed_text_fails(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.inrelease.write_text(clearsigned("Origin: Shadowfetch\n"), encoding="utf-8")
        result = self.check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("missing Valid-Until in the signed text", result.stderr)

    def test_tracked_wrangler_state_still_fails(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        cache = self.root / ".wrangler" / "state.json"
        cache.parent.mkdir()
        cache.write_text("{}\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "-f", ".wrangler/state.json"], cwd=self.root, check=True
        )
        result = self.check()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("tracked Wrangler cache/state files", result.stderr)


if __name__ == "__main__":
    unittest.main()
