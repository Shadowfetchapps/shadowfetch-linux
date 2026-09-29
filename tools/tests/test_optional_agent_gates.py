"""Release gates for the 5.0 optional agents (Hermes Agent, OpenClaw).

They are no longer retired runtimes, but the gates must still stop them from
being preinstalled: named only by an explicit allowlist, never in a live-build
package list or chroot include, never a package relationship, never a path in
the image, and the helpers must carry their release pins.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
RELEASE_DIR = ROOT / "tools" / "release"
if str(RELEASE_DIR) not in sys.path:
    sys.path.insert(0, str(RELEASE_DIR))

import gate  # noqa: E402,F401  (loaded first so the gate modules resolve it)


def load(name: str):
    loader = importlib.machinery.SourceFileLoader(f"optional_agents_{name}", str(RELEASE_DIR / f"{name}.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


source_gate = load("source_gate")
package_gate = load("package_gate")
iso_gate = load("iso_gate")

DEFAULTS = ROOT / "packages/shadowfetch-defaults/data"


class SourceGate(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_the_real_tree_passes(self) -> None:
        self.assertEqual([], source_gate.optional_agent_findings(ROOT))

    def test_allowlisted_files_may_name_the_agents(self) -> None:
        for relative in sorted(source_gate.OPTIONAL_AGENT_FILES):
            self.write(relative, "Hermes and OpenClaw\n")
        self.write("packages/shadowfetch-defaults/data/usr/share/shadowfetch/openclaw/1/package-lock.json", '"openclaw"')
        self.write("packages/shadowfetch-defaults/debian/changelog", "Add optional Hermes installer\n")
        self.assertEqual([], source_gate.optional_agent_findings(self.root))

    def test_a_package_list_or_chroot_include_fails(self) -> None:
        self.write("live-build/config/package-lists/agents.list.chroot", "openclaw\n")
        self.write("live-build/config/includes.chroot/etc/skel/.hermes/config.yaml", "model: x\n")
        self.write("live-build/config/includes.chroot/etc/motd", "try hermes\n")
        findings = source_gate.optional_agent_findings(self.root)
        self.assertEqual(2, len(findings), findings)
        self.assertTrue(all("preinstall list or chroot include" in item for item in findings))

    def test_any_other_image_file_fails(self) -> None:
        self.write("packages/shadowfetch-defaults/data/usr/lib/shadowfetch/first-login.sh", "hermes setup\n")
        self.assertEqual(["packages/shadowfetch-defaults/data/usr/lib/shadowfetch/first-login.sh (not allowlisted)"],
                         source_gate.optional_agent_findings(self.root))

    def test_a_package_relationship_fails(self) -> None:
        self.write("packages/shadowfetch-meta/debian/control",
                   "Source: shadowfetch-meta\n\nPackage: shadowfetch-desktop\n"
                   "# a comment: hermes\nDepends: foo,\n bar\nRecommends: openclaw\n")
        findings = source_gate.optional_agent_findings(self.root)
        self.assertIn("packages/shadowfetch-meta/debian/control: shadowfetch-desktop Recommends", findings)

    def test_competing_runtimes_stay_retired(self) -> None:
        for word in ("ollama", "open-webui", "llama.cpp", "llama-server"):
            self.assertTrue(source_gate.RETIRED_RUNTIME.search(word), word)
        for word in ("openclaw", "hermes"):
            self.assertIsNone(source_gate.RETIRED_RUNTIME.search(word), word)


class FakeRelease:
    def __init__(self, pins: dict[str, list[str]]):
        self.pins = pins

    def section(self, name: str):
        assert name == "pinned_artifacts"
        return self.pins


class PackageGate(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.extracted = Path(self.tmp.name)
        helper = (DEFAULTS / "usr/bin/shadowfetch-openclaw").read_text(encoding="utf-8")
        self.version = package_gate._literal(helper, "OPENCLAW_VERSION")
        self.owners: dict[str, list[str]] = {}
        for relative, owner in (
            ("usr/bin/shadowfetch-hermes", "shadowfetch-defaults"),
            ("usr/bin/shadowfetch-openclaw", "shadowfetch-defaults"),
            (f"usr/share/shadowfetch/openclaw/{self.version}/package-lock.json", "shadowfetch-defaults"),
            (f"usr/share/shadowfetch/openclaw/{self.version}/package.json", "shadowfetch-defaults"),
        ):
            target = self.extracted / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(DEFAULTS / relative, target)
            self.owners[relative] = [owner]
        self.owners["usr/share/shadowfetch/control-center/sfcc/optional_agents_page.py"] = ["shadowfetch-control-center"]
        pins = {
            "hermes": [f'HERMES_VERSION = "{self.hermes("HERMES_VERSION")}"',
                       f'HERMES_COMMIT = "{self.hermes("HERMES_COMMIT")}"',
                       f'INSTALLER_SHA256 = "{self.hermes("INSTALLER_SHA256")}"'],
            "openclaw": [f'OPENCLAW_VERSION = "{self.version}"',
                         f'OPENCLAW_INTEGRITY = "{package_gate._literal(helper, "OPENCLAW_INTEGRITY")}"',
                         f'LOCKFILE_SHA256 = "{package_gate._literal(helper, "LOCKFILE_SHA256")}"'],
        }
        self.previous = getattr(package_gate, "RELEASE", None)
        package_gate.RELEASE = FakeRelease(pins)
        self.addCleanup(setattr, package_gate, "RELEASE", self.previous)

    def hermes(self, name: str) -> str:
        return package_gate._literal((DEFAULTS / "usr/bin/shadowfetch-hermes").read_text(encoding="utf-8"), name)

    def test_the_shipped_helpers_and_lockfile_pass(self) -> None:
        package_gate.check_optional_agent_payload(self.owners, self.extracted)

    def test_a_missing_pin_fails(self) -> None:
        package_gate.RELEASE.pins["hermes"].append('HERMES_VERSION = "9.9.9"')
        with self.assertRaisesRegex(RuntimeError, "release pin is absent"):
            package_gate.check_optional_agent_payload(self.owners, self.extracted)

    def test_a_tampered_lockfile_fails(self) -> None:
        lock = self.extracted / f"usr/share/shadowfetch/openclaw/{self.version}/package-lock.json"
        data = json.loads(lock.read_text())
        data["packages"]["node_modules/openclaw"]["integrity"] = "sha512-x"
        lock.write_text(json.dumps(data))
        with self.assertRaisesRegex(RuntimeError, "LOCKFILE_SHA256"):
            package_gate.check_optional_agent_payload(self.owners, self.extracted)

    def test_a_packaged_agent_fails(self) -> None:
        self.owners["usr/lib/node_modules/openclaw/package.json"] = ["shadowfetch-defaults"]
        with self.assertRaisesRegex(RuntimeError, "must not be packaged"):
            package_gate.check_optional_agent_payload(self.owners, self.extracted)

    def test_payload_text_allowlist(self) -> None:
        self.assertTrue(package_gate.optional_agent_payload_allowed("usr/bin/shadowfetch-hermes", ["shadowfetch-defaults"]))
        self.assertFalse(package_gate.optional_agent_payload_allowed("usr/bin/shadowfetch-hermes", ["shadowfetch-meta"]))
        self.assertFalse(package_gate.optional_agent_payload_allowed("usr/lib/shadowfetch/first-login.sh"))
        self.assertTrue(package_gate.optional_agent_payload_allowed("usr/share/doc/shadowfetch/WORKBENCH.md"))

    def test_relationship_fields_are_checked(self) -> None:
        self.assertEqual(["shadowfetch-desktop Recommends"], package_gate.optional_agent_relationships(
            "shadowfetch-desktop", {"Depends": "shadowfetch-defaults", "Recommends": "openclaw (>= 1)"}))
        self.assertEqual([], package_gate.optional_agent_relationships("shadowfetch-desktop", {"Depends": "curl"}))

    def test_competing_runtimes_stay_retired(self) -> None:
        self.assertTrue(package_gate.RETIRED_RUNTIME.search(b"ollama serve"))
        self.assertIsNone(package_gate.RETIRED_RUNTIME.search(b"shadowfetch-hermes"))


class IsoGate(unittest.TestCase):
    def test_the_helpers_are_required_executables(self) -> None:
        for helper in ("usr/bin/shadowfetch-hermes", "usr/bin/shadowfetch-openclaw"):
            self.assertIn(helper, iso_gate.REQUIRED_ROOT_FILES)
            self.assertIn(helper, iso_gate.REQUIRED_EXECUTABLES)
            self.assertIn(helper, iso_gate.CRITICAL_PACKAGE_PAYLOADS["shadowfetch-defaults"])
            self.assertFalse(iso_gate.retired_path(helper))

    def test_preinstalled_agent_paths_are_rejected(self) -> None:
        for path in ("etc/skel/.hermes/config.yaml", "etc/skel/.openclaw/openclaw.json", "etc/skel/.npm-global/bin/openclaw",
                     "usr/bin/hermes", "usr/local/bin/openclaw", "usr/lib/node_modules/openclaw/openclaw.mjs",
                     "usr/local/lib/hermes-agent/run_agent.py", "usr/lib/systemd/user/openclaw-gateway.service",
                     "root/.hermes/.env"):
            with self.subTest(path=path):
                self.assertTrue(iso_gate.retired_path(path))
        self.assertFalse(iso_gate.retired_path("usr/share/shadowfetch/openclaw/2026.9.6/package-lock.json"))

    def test_the_installed_package_ban_is_kept(self) -> None:
        for package in ("openclaw", "hermes-agent", "ollama"):
            self.assertTrue(iso_gate.RETIRED_PACKAGE.search(package), package)
        self.assertIsNone(iso_gate.RETIRED_PACKAGE.search("shadowfetch-defaults"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
