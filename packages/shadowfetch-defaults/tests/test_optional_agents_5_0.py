#!/usr/bin/env python3
"""Hermes Agent and OpenClaw optional installers: pins, consent, refusals, verification.

No test downloads, installs or runs upstream code. curl, git, npm, node, konsole
and the Firebreak launcher are stubs in a private directory that the helpers'
own TOOL_DIRS search reaches first; the "upstream installer" is a fake written
by the test.
"""
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "data/usr/bin"
DATA = ROOT / "data/usr/share/shadowfetch/openclaw"
INSTALL = ROOT / "debian/shadowfetch-defaults.install"


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


HERMES = load("shadowfetch_hermes", BIN / "shadowfetch-hermes")
OPENCLAW = load("shadowfetch_openclaw", BIN / "shadowfetch-openclaw")


def write_exec(path, text):
    path.write_text(textwrap.dedent(text).lstrip())
    path.chmod(0o755)
    return path


def git_blob(data):
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class Sandbox:
    """A throwaway HOME plus a stub directory the helpers search first."""

    def __init__(self, case, module):
        self.case = case
        self.module = module
        self.tmp = tempfile.TemporaryDirectory()
        case.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.stubs = self.root / "stubs"
        self.stubs.mkdir()
        self.log = self.stubs / "calls.log"
        self.log.touch()
        env = {"HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"),
               "XDG_STATE_HOME": str(self.home / ".local/state"), "XDG_DATA_HOME": str(self.home / ".local/share"),
               "SHADOWFETCH_AGENT_NETWORK": "online", "SHADOWFETCH_ELEMENT": "",
               "SHADOWFETCH_AGENT_WORKSPACES": str(self.home / "Workspaces")}
        for name in ("HERMES_HOME", "XDG_CACHE_HOME"):
            env[name] = ""
        patches = [patch.dict(os.environ, env),
                   patch.object(module, "TOOL_DIRS", (str(self.stubs), "/usr/bin", "/bin")),
                   patch.object(module, "AGENT_NETWORK", self.root / "no-agent-network"),
                   patch.object(module, "SYSTEM_CONFIG", self.root / "etc"),
                   patch.object(module.sys.stdin, "isatty", return_value=False)]
        for item in patches:
            item.start()
            case.addCleanup(item.stop)
        os.environ.pop("HERMES_HOME", None)

    def calls(self):
        return self.log.read_text().splitlines()

    def wait_call(self, prefix, timeout=10.0):
        """A detached launch (konsole) logs asynchronously."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = [line for line in self.calls() if line.startswith(prefix)]
            if found:
                return found[0]
            time.sleep(0.05)
        raise AssertionError(f"no {prefix!r} call in {self.calls()}")

    def run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.module.main(list(argv))
        return code, out.getvalue() + err.getvalue()


CURL = """
#!/usr/bin/python3
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "calls.log"), "a") as log:
    log.write("curl " + " ".join(sys.argv[1:]) + "\\n")
url = [a for a in sys.argv[1:] if a.startswith("https://")][-1]
routes = json.load(open(os.path.join(here, "routes.json")))
route = routes.get(url)
if route is None:
    sys.stderr.write("no route for " + url + "\\n"); sys.exit(22)
data = open(route["file"], "rb").read() if "file" in route else route["text"].encode()
if "--output" in sys.argv:
    open(sys.argv[sys.argv.index("--output") + 1], "wb").write(data)
else:
    sys.stdout.buffer.write(data)
"""

GIT = """
#!/bin/sh
echo "git $*" >> "$(dirname "$0")/calls.log"
if [ "$1" = "-C" ] && [ "$3" = "rev-parse" ] && [ -f "$2/.git/HEAD_SHA" ]; then cat "$2/.git/HEAD_SHA"; exit 0; fi
exit 1
"""


class HermesCase(unittest.TestCase):
    COMMIT_NEW = "a" * 40

    def setUp(self):
        self.box = Sandbox(self, HERMES)
        write_exec(self.box.stubs / "curl", CURL)
        write_exec(self.box.stubs / "git", GIT)
        write_exec(self.box.stubs / "konsole", '#!/bin/sh\necho "konsole $*" >> "$(dirname "$0")/calls.log"\n')
        write_exec(self.box.stubs / "hermes-real", """
            #!/bin/sh
            echo "hermes $*" >> "$(dirname "$0")/calls.log"
            [ "$1" = "update" ] && [ "$2" = "--help" ] && echo "usage: hermes update [--yes] [--branch NAME]"
            exit 0
        """)
        self.routes = {}
        self.install_dir = self.box.home / ".hermes/hermes-agent"

    def fake_installer(self, commit_written):
        text = f"""#!/bin/bash
# fake Hermes installer: parses --commit, --non-interactive, --force-commit, --skip-setup; repo hermes-agent
echo "installer $* uid=$(id -u)" >> "{self.box.log}"
if sudo true 2>/dev/null; then echo SUDO_WORKED >> "{self.box.log}"; else echo SUDO_REFUSED >> "{self.box.log}"; fi
while [ $# -gt 0 ]; do case "$1" in --dir) DIR="$2"; shift 2;; *) shift;; esac; done
mkdir -p "$DIR/.git" "$HOME/.local/bin"
printf '[remote "origin"]\\n url = https://github.com/NousResearch/hermes-agent.git\\n' > "$DIR/.git/config"
echo {commit_written} > "$DIR/.git/HEAD_SHA"
printf '#!/bin/sh\\n# %s\\nexec {self.box.stubs}/hermes-real "$@"\\n' "$DIR" > "$HOME/.local/bin/hermes"
chmod +x "$HOME/.local/bin/hermes"
"""
        path = self.box.root / f"install-{commit_written[:6]}.sh"
        path.write_text(text)
        return path

    def pin_to(self, installer, commit=HERMES.HERMES_COMMIT):
        data = installer.read_bytes()
        release = dict(HERMES.PINNED_RELEASE, commit=commit, installer_sha256=hashlib.sha256(data).hexdigest(),
                       installer_git_blob=git_blob(data), installer_bytes=len(data))
        self.routes[release["installer_url"]] = {"file": str(installer)}
        self.save_routes()
        patcher = patch.object(HERMES, "PINNED_RELEASE", release)
        patcher.start()
        self.addCleanup(patcher.stop)
        return release

    def save_routes(self):
        (self.box.stubs / "routes.json").write_text(json.dumps(self.routes))

    def installed(self):
        installer = self.fake_installer(HERMES.HERMES_COMMIT)
        self.pin_to(installer)
        code, output = self.box.run("setup", "--yes", "--no-open")
        self.assertEqual(0, code, output)

    # ---- pins -------------------------------------------------------------

    def test_release_pins_are_literal_lines(self):
        source = (BIN / "shadowfetch-hermes").read_text()
        self.assertRegex(source, r'(?m)^HERMES_VERSION = "\d+\.\d+\.\d+"$')
        self.assertRegex(source, r'(?m)^HERMES_COMMIT = "[0-9a-f]{40}"$')
        self.assertRegex(source, r'(?m)^INSTALLER_SHA256 = "[0-9a-f]{64}"$')
        self.assertRegex(source, r'(?m)^INSTALLER_GIT_BLOB = "[0-9a-f]{40}"$')
        # The installer comes from the release commit, so its bytes cannot move under the pin.
        self.assertEqual(f"https://raw.githubusercontent.com/NousResearch/hermes-agent/{HERMES.HERMES_COMMIT}/scripts/install.sh",
                         HERMES.INSTALLER_URL)
        self.assertIn("data/usr/bin/shadowfetch-hermes", INSTALL.read_text())

    def test_status_json_reports_network_and_is_offline_safe(self):
        os.environ["SHADOWFETCH_AGENT_NETWORK"] = "offline"
        code, output = self.box.run("status", "--json")
        self.assertEqual(0, code)
        data = json.loads(output)
        self.assertEqual(("offline", True, False, "not-installed"),
                         (data["agent_network"], data["blocked_by_offline"], data["launchable"], data["state"]))
        self.assertFalse(data["runs_as_root"])
        self.assertEqual([], self.box.calls())

    # ---- refusals ---------------------------------------------------------

    def test_offline_refuses_setup_update_and_open_before_any_network(self):
        os.environ["SHADOWFETCH_AGENT_NETWORK"] = "offline"
        for argv in (("setup", "--yes", "--no-open"), ("update", "--yes"), ("update", "--check"), ("open",)):
            with self.subTest(argv=argv):
                code, output = self.box.run(*argv)
                self.assertEqual(1, code)
                self.assertIn("offline", output)
        self.assertEqual([], self.box.calls())

    def test_legacy_ice_setting_counts_as_offline(self):
        os.environ["SHADOWFETCH_AGENT_NETWORK"] = ""
        (self.box.home / ".config/shadowfetch").mkdir(parents=True)
        (self.box.home / ".config/shadowfetch/element").write_text("ice\n")
        self.assertEqual("offline", HERMES.agent_network())

    def test_a_broken_agent_network_tool_fails_closed(self):
        tool = write_exec(self.box.root / "agent-network", "#!/bin/sh\necho maybe\n")
        with patch.object(HERMES, "AGENT_NETWORK", tool):
            self.assertEqual("offline", HERMES.agent_network())

    def test_root_is_refused_for_every_mutating_command(self):
        with patch.object(HERMES.os, "geteuid", return_value=0):
            for argv in (("setup", "--yes", "--no-open"), ("update", "--yes"), ("open",), ("uninstall", "--yes")):
                with self.subTest(argv=argv):
                    code, output = self.box.run(*argv)
                    self.assertEqual(1, code)
                    self.assertIn("never as root", output)
        self.assertEqual([], self.box.calls())

    def test_no_download_without_consent(self):
        self.pin_to(self.fake_installer(HERMES.HERMES_COMMIT))
        code, output = self.box.run("setup", "--no-open")
        self.assertEqual(1, code)
        self.assertIn("--yes", output)
        self.assertIn("Hermes Agent is an optional", output)
        self.assertFalse(any(line.startswith("curl") for line in self.box.calls()))

    # ---- verification -----------------------------------------------------

    def test_bad_installer_sha_installs_nothing(self):
        installer = self.fake_installer(HERMES.HERMES_COMMIT)
        self.routes[HERMES.INSTALLER_URL] = {"file": str(installer)}
        self.save_routes()
        code, output = self.box.run("setup", "--yes", "--no-open")
        self.assertEqual(1, code)
        self.assertIn("size", output)  # the real pin's byte count is checked first
        with patch.object(HERMES, "PINNED_RELEASE", dict(HERMES.PINNED_RELEASE, installer_bytes=None)):
            code, output = self.box.run("setup", "--yes", "--no-open")
        self.assertEqual(1, code)
        self.assertIn("SHA-256", output)
        self.assertFalse(any(line.startswith("installer") for line in self.box.calls()))
        self.assertFalse(self.install_dir.exists())

    def test_wrong_commit_after_install_is_a_failure_without_receipt(self):
        self.pin_to(self.fake_installer("b" * 40))
        code, output = self.box.run("setup", "--yes", "--no-open")
        self.assertEqual(1, code)
        self.assertIn("not the verified", output)
        self.assertFalse(HERMES.receipt_path().exists())

    def test_setup_runs_pinned_non_interactive_user_install_without_root(self):
        self.installed()
        installer_call = next(line for line in self.box.calls() if line.startswith("installer"))
        for flag in ("--commit " + HERMES.HERMES_COMMIT, "--non-interactive", "--skip-setup", "--force-commit",
                     "--skip-browser", "--skip-computer-use", f"--dir {self.install_dir}"):
            self.assertIn(flag, installer_call)
        self.assertNotIn("uid=0", installer_call)
        self.assertIn("SUDO_REFUSED", self.box.calls())
        receipt = json.loads(HERMES.receipt_path().read_text())
        self.assertEqual((HERMES.HERMES_COMMIT, False), (receipt["commit"], receipt["credentials_stored_by_shadowfetch"]))
        self.assertTrue(receipt["update_channel"].startswith("main"))  # this release predates channels
        status = HERMES.status()
        self.assertEqual(("ready", True, True), (status["state"], status["verified"], status["launchable"]))
        self.assertEqual(stat.S_IMODE(HERMES.receipt_path().stat().st_mode), 0o600)
        self.assertTrue(HERMES.launcher_path().is_file())

    def test_opt_in_flags_reach_the_installer(self):
        self.pin_to(self.fake_installer(HERMES.HERMES_COMMIT))
        code, output = self.box.run("setup", "--yes", "--no-open", "--with-browser", "--with-computer-use")
        self.assertEqual(0, code, output)
        call = next(line for line in self.box.calls() if line.startswith("installer"))
        self.assertNotIn("--skip-browser", call)
        self.assertNotIn("--skip-computer-use", call)

    def test_stable_channel_is_set_when_hermes_supports_it(self):
        write_exec(self.box.stubs / "hermes-real", """
            #!/bin/sh
            echo "hermes $*" >> "$(dirname "$0")/calls.log"
            [ "$2" = "--help" ] && echo "--set-channel CHANNEL"
            exit 0
        """)
        self.installed()
        self.assertIn("hermes update --set-channel stable", self.box.calls())
        self.assertEqual("stable", json.loads(HERMES.receipt_path().read_text())["update_channel"])

    # ---- update -----------------------------------------------------------

    def latest_routes(self, tag="v2026.10.1", name="Hermes Agent v0.22.0 (v2026.10.1)", compare="ahead"):
        installer = self.fake_installer(self.COMMIT_NEW)
        data = installer.read_bytes()
        api = HERMES.API
        self.routes.update({
            f"{api}/releases/latest": {"text": json.dumps({"tag_name": tag, "name": name, "draft": False,
                                                            "prerelease": False, "published_at": "2026-10-01T00:00:00Z"})},
            f"{api}/commits/{tag}": {"text": self.COMMIT_NEW + "\n"},
            f"{api}/compare/{self.COMMIT_NEW}...main": {"text": json.dumps({"status": compare})},
            f"{api}/contents/scripts/install.sh?ref={self.COMMIT_NEW}": {"text": json.dumps({"sha": git_blob(data), "size": len(data)})},
            f"{HERMES.RAW}/{self.COMMIT_NEW}/scripts/install.sh": {"file": str(installer)},
        })
        self.save_routes()

    def test_update_resolves_and_installs_the_latest_release_after_consent(self):
        self.installed()
        self.latest_routes()
        code, output = self.box.run("update", "--check", "--json")
        self.assertEqual(0, code, output)
        report = json.loads(output)
        self.assertEqual(("0.22.0", "v2026.10.1", self.COMMIT_NEW, True),
                         (report["latest_version"], report["latest_tag"], report["latest_commit"], report["update_available"]))
        code, output = self.box.run("update")
        self.assertEqual(1, code)  # no tty and no --yes: consent required
        self.assertIn("0.21.5", output)
        self.assertIn("0.22.0", output)
        code, output = self.box.run("update", "--yes", "--expect", "0.23.0")
        self.assertEqual(1, code)
        self.assertIn("changed", output)
        code, output = self.box.run("update", "--yes", "--expect", "0.22.0")
        self.assertEqual(0, code, output)
        receipt = json.loads(HERMES.receipt_path().read_text())
        self.assertEqual(("0.22.0", self.COMMIT_NEW, "github-latest-release"),
                         (receipt["version"], receipt["commit"], receipt["source"]))
        self.assertEqual("ready", HERMES.status()["state"])

    def test_update_refuses_a_release_commit_off_main(self):
        self.installed()
        self.latest_routes(compare="diverged")
        code, output = self.box.run("update", "--yes")
        self.assertEqual(1, code)
        self.assertIn("not on Hermes's main branch", output)

    def test_update_refuses_an_installer_github_does_not_record(self):
        self.installed()
        self.latest_routes()
        contents = f"{HERMES.API}/contents/scripts/install.sh?ref={self.COMMIT_NEW}"
        size = json.loads(self.routes[contents]["text"])["size"]
        self.routes[contents] = {"text": json.dumps({"sha": "0" * 40, "size": size})}
        self.save_routes()
        code, output = self.box.run("update", "--yes")
        self.assertEqual(1, code)
        self.assertIn("does not match the file GitHub records", output)
        self.assertEqual(HERMES.HERMES_COMMIT, json.loads(HERMES.receipt_path().read_text())["commit"])

    def test_hermes_update_onto_main_is_reported_as_drift(self):
        self.installed()
        (self.install_dir / ".git/HEAD_SHA").write_text("c" * 40 + "\n")
        self.assertEqual(("drifted", False), (HERMES.status()["state"], HERMES.status()["verified"]))

    # ---- open / uninstall -------------------------------------------------

    def test_open_uses_a_terminal_and_the_owned_launcher(self):
        self.installed()
        code, output = self.box.run("open")
        self.assertEqual(0, code, output)
        self.assertEqual(f"konsole --workdir {self.box.home} -e {self.box.home}/.local/bin/hermes",
                         self.box.wait_call("konsole"))

    def test_uninstall_delegates_then_removes_only_what_hermes_owns(self):
        self.installed()
        (self.box.home / ".hermes/config.yaml").write_text("keep: me\n")
        (self.box.home / ".local/bin/other-tool").write_text("#!/bin/sh\n")
        os.environ["SHADOWFETCH_AGENT_NETWORK"] = "offline"  # uninstall is local and still allowed
        code, output = self.box.run("uninstall", "--yes")
        self.assertEqual(0, code, output)
        self.assertIn("hermes uninstall --yes", self.box.calls())
        self.assertFalse(self.install_dir.exists())
        self.assertFalse((self.box.home / ".local/bin/hermes").exists())
        self.assertTrue((self.box.home / ".hermes/config.yaml").exists())
        self.assertTrue((self.box.home / ".local/bin/other-tool").exists())
        self.assertFalse(HERMES.state_dir().exists())
        self.assertFalse(HERMES.launcher_path().exists())

    def test_purge_data_removes_hermes_home(self):
        self.installed()
        code, output = self.box.run("uninstall", "--yes", "--purge-data")
        self.assertEqual(0, code, output)
        self.assertIn("hermes uninstall --yes --full", self.box.calls())
        self.assertFalse((self.box.home / ".hermes").exists())


NPM = """
#!/usr/bin/python3
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(here, "npm.json")))
with open(os.path.join(here, "calls.log"), "a") as log:
    log.write("npm " + " ".join(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
cmd = args[0] if args else ""
if cmd == "--version":
    print(cfg.get("npm_version", "10.9.2")); sys.exit(0)
if cmd == "view":
    key = " ".join(a for a in args[1:] if not a.startswith("-") and not a.startswith("http") and not a.startswith("/"))
    print(json.dumps(cfg["view"][key])); sys.exit(0)
if cmd == "install" and "--package-lock-only" in args:
    open("package-lock.json", "w").write(json.dumps(cfg["generated_lock"])); sys.exit(0)
if cmd == "ci":
    lock = json.load(open("package-lock.json"))
    packages = {k: v for k, v in lock["packages"].items() if k in ("", "node_modules/openclaw", "node_modules/ms")}
    if cfg.get("tamper"):
        packages["node_modules/openclaw"] = dict(packages["node_modules/openclaw"], integrity="sha512-TAMPERED")
    os.makedirs("node_modules/openclaw", exist_ok=True)
    json.dump({"lockfileVersion": 3, "packages": packages}, open("node_modules/.package-lock.json", "w"))
    version = lock["packages"]["node_modules/openclaw"]["version"]
    json.dump({"name": "openclaw", "version": version}, open("node_modules/openclaw/package.json", "w"))
    open("node_modules/openclaw/openclaw.mjs", "w").write("// stub\\n")
    sys.exit(cfg.get("ci_rc", 0))
if cmd == "audit":
    sys.exit(cfg.get("audit_rc", 0))
if cmd == "rebuild":
    sys.exit(0)
sys.exit(1)
"""

NODE = """
#!/usr/bin/python3
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(here, "npm.json")))
with open(os.path.join(here, "calls.log"), "a") as log:
    log.write("node " + " ".join(sys.argv[1:]) + "\\n")
args = sys.argv[1:]
if args == ["--version"]:
    print(cfg.get("node_version", "v24.21.0")); sys.exit(0)
if args[:1] == ["-p"]:
    print(cfg.get("sqlite", "3.53.4")); sys.exit(0)
if len(args) >= 2 and args[1] == "--version":
    print(json.load(open(os.path.join(os.path.dirname(args[0]), "package.json")))["version"]); sys.exit(0)
sys.exit(0)
"""


class OpenClawCase(unittest.TestCase):
    NEW = "2026.10.1"
    NEW_INTEGRITY = "sha512-" + "N" * 86 + "=="

    def setUp(self):
        self.box = Sandbox(self, OPENCLAW)
        write_exec(self.box.stubs / "npm", NPM)
        write_exec(self.box.stubs / "node", NODE)
        write_exec(self.box.stubs / "konsole", '#!/bin/sh\necho "konsole $*" >> "$(dirname "$0")/calls.log"\n')
        self.firebreak = write_exec(self.box.stubs / "shadowfetch-firebreak", '#!/bin/sh\necho "firebreak $*" >> "$(dirname "$0")/calls.log"\n')
        for item in (patch.object(OPENCLAW, "DATA_DIR", DATA), patch.object(OPENCLAW, "FIREBREAK", self.firebreak)):
            item.start()
            self.addCleanup(item.stop)
        self.cfg = {"view": {}}
        self.save()

    def save(self):
        (self.box.stubs / "npm.json").write_text(json.dumps(self.cfg))

    def npm_calls(self):
        return [line for line in self.box.calls() if line.startswith("npm ")]

    def installed(self):
        code, output = self.box.run("setup", "--yes", "--no-open")
        self.assertEqual(0, code, output)

    # ---- pins -------------------------------------------------------------

    def test_release_pins_match_the_shipped_lockfile(self):
        source = (BIN / "shadowfetch-openclaw").read_text()
        for name in ("OPENCLAW_VERSION", "OPENCLAW_INTEGRITY", "LOCKFILE_SHA256", "PACKAGE_JSON_SHA256"):
            self.assertRegex(source, rf'(?m)^{name} = "[^"]+"$')
        folder = DATA / OPENCLAW.OPENCLAW_VERSION
        self.assertEqual(OPENCLAW.LOCKFILE_SHA256, hashlib.sha256((folder / "package-lock.json").read_bytes()).hexdigest())
        self.assertEqual(OPENCLAW.PACKAGE_JSON_SHA256, hashlib.sha256((folder / "package.json").read_bytes()).hexdigest())
        packages = OPENCLAW.check_lock(folder / "package-lock.json", OPENCLAW.OPENCLAW_VERSION, OPENCLAW.OPENCLAW_INTEGRITY)
        self.assertGreater(len(packages), 100)
        install = INSTALL.read_text()
        for name in ("package.json", "package-lock.json"):
            self.assertIn(f"data/usr/share/shadowfetch/openclaw/{OPENCLAW.OPENCLAW_VERSION}/{name}", install)

    def test_engines_range_and_sqlite_floor(self):
        spec = OPENCLAW.NODE_ENGINES
        for version, ok in (("v24.21.0", True), ("v24.16.0", True), ("v24.15.9", False), ("v25.2.0", False),
                            ("v26.0.9", False), ("v26.1.0", True), ("v22.22.2", False)):
            with self.subTest(version=version):
                self.assertEqual(ok, OPENCLAW.satisfies(version, spec))
        self.assertTrue(OPENCLAW.satisfies("v22.22.3", "^22.22.2 || ^24.15.0 || >=26.0.0"))
        info = {"node_version": "v24.21.0", "npm_version": "10.9.2", "sqlite_version": "3.51.2"}
        with self.assertRaisesRegex(OPENCLAW.SetupError, "SQLite"):
            OPENCLAW.check_runtime(info, spec)
        OPENCLAW.check_runtime(dict(info, sqlite_version="3.51.3"), spec)

    # ---- refusals ---------------------------------------------------------

    def test_offline_refuses_setup_update_and_open_before_npm(self):
        os.environ["SHADOWFETCH_AGENT_NETWORK"] = "offline"
        for argv in (("setup", "--yes", "--no-open"), ("update", "--yes"), ("update", "--check"), ("open",),
                     ("gateway", "enable", "--yes")):
            with self.subTest(argv=argv):
                code, output = self.box.run(*argv)
                self.assertEqual(1, code)
        self.assertEqual([], self.npm_calls())
        data = json.loads(self.box.run("status", "--json")[1])
        self.assertEqual(("offline", True), (data["agent_network"], data["blocked_by_offline"]))

    def test_root_is_refused(self):
        with patch.object(OPENCLAW.os, "geteuid", return_value=0):
            for argv in (("setup", "--yes", "--no-open"), ("update", "--yes"), ("open",), ("uninstall", "--yes")):
                with self.subTest(argv=argv):
                    self.assertEqual(1, self.box.run(*argv)[0])
        self.assertEqual([], self.box.calls())

    def test_missing_node_prints_the_debian_command_and_never_escalates(self):
        with patch.object(OPENCLAW, "TOOL_DIRS", (str(self.box.root / "empty"),)):
            code, output = self.box.run("setup", "--yes", "--no-open")
        self.assertEqual(1, code)
        self.assertIn("sudo apt install nodejs npm", output)
        self.assertIn("never adds NodeSource", output)
        source = (BIN / "shadowfetch-openclaw").read_text()
        self.assertNotRegex(source, r"\[\s*\"?/usr/bin/(sudo|pkexec)")
        self.assertNotIn("nodesource.com", source)

    def test_old_node_is_refused(self):
        self.cfg["node_version"] = "v22.11.0"
        self.save()
        code, output = self.box.run("setup", "--yes", "--no-open")
        self.assertEqual(1, code)
        self.assertIn("Node.js", output)
        self.assertFalse(any(" ci " in f" {c} " for c in self.npm_calls()))

    def test_no_download_without_consent(self):
        code, output = self.box.run("setup", "--no-open")
        self.assertEqual(1, code)
        self.assertIn("SECURITY NOTE", output)
        self.assertFalse(any(call.startswith("npm ci") for call in self.npm_calls()))

    # ---- verification -----------------------------------------------------

    def test_integrity_mismatch_activates_nothing(self):
        self.cfg["tamper"] = True
        self.save()
        code, output = self.box.run("setup", "--yes", "--no-open")
        self.assertEqual(1, code)
        self.assertIn("integrity", output)
        self.assertFalse(OPENCLAW.version_dir(OPENCLAW.OPENCLAW_VERSION).exists())
        self.assertFalse(OPENCLAW.link_path().exists())
        self.assertFalse(any(call.startswith("npm rebuild") for call in self.npm_calls()))

    def test_signature_failure_activates_nothing_and_runs_no_scripts(self):
        self.cfg["audit_rc"] = 1
        self.save()
        code, output = self.box.run("setup", "--yes", "--no-open")
        self.assertEqual(1, code)
        self.assertIn("signatures", output)
        self.assertFalse(any(call.startswith("npm rebuild") for call in self.npm_calls()))
        self.assertFalse(OPENCLAW.receipt_path().exists())

    def test_tampered_shipped_lockfile_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            copy = Path(folder) / OPENCLAW.OPENCLAW_VERSION
            shutil.copytree(DATA / OPENCLAW.OPENCLAW_VERSION, copy)
            with (copy / "package-lock.json").open("a") as lock:
                lock.write(" ")
            with patch.object(OPENCLAW, "DATA_DIR", Path(folder)):
                code, output = self.box.run("setup", "--yes", "--no-open")
        self.assertEqual(1, code)
        self.assertIn("SHA-256", output)

    def test_setup_orders_verification_before_any_package_script(self):
        self.installed()
        calls = [c.split()[1] for c in self.npm_calls() if c.split()[1] in ("ci", "audit", "rebuild")]
        self.assertEqual(["ci", "audit", "rebuild"], calls)
        ci = next(c for c in self.npm_calls() if c.startswith("npm ci"))
        self.assertIn("--ignore-scripts", ci)
        self.assertIn("--registry https://registry.npmjs.org/", ci)
        rebuild = next(c for c in self.npm_calls() if c.startswith("npm rebuild"))
        self.assertTrue(rebuild.startswith("npm rebuild openclaw "))
        receipt = json.loads(OPENCLAW.receipt_path().read_text())
        self.assertEqual((OPENCLAW.OPENCLAW_VERSION, OPENCLAW.OPENCLAW_INTEGRITY, False, False),
                         (receipt["version"], receipt["integrity"], receipt["gateway_enabled"],
                          receipt["credentials_stored_by_shadowfetch"]))
        self.assertTrue(OPENCLAW.link_path().is_symlink())
        status = OPENCLAW.status()
        self.assertEqual(("ready", True, False), (status["state"], status["launchable"], status["gateway_enabled"]))
        # No gateway, daemon or service is touched by setup.
        self.assertFalse([c for c in self.box.calls() if " gateway " in f" {c} " or "onboard" in c])

    def test_new_npm_gets_the_documented_allow_scripts_flag(self):
        self.cfg["npm_version"] = "11.16.0"
        self.save()
        self.installed()
        rebuild = next(c for c in self.npm_calls() if c.startswith("npm rebuild"))
        self.assertIn("--allow-scripts=openclaw", rebuild)

    # ---- update -----------------------------------------------------------

    def configure_latest(self, version=None, integrity=None):
        version, integrity = version or self.NEW, integrity or self.NEW_INTEGRITY
        self.cfg["view"] = {
            "openclaw dist-tags.latest": version,
            f"openclaw@{version} dist.integrity engines dist.tarball": {
                "dist.integrity": integrity, "engines": {"node": ">=24.16.0 <25 || >=26.1.0"},
                "dist.tarball": f"https://registry.npmjs.org/openclaw/-/openclaw-{version}.tgz"},
        }
        self.cfg["generated_lock"] = {"lockfileVersion": 3, "packages": {
            "": {"name": "shadowfetch-openclaw", "version": version, "dependencies": {"openclaw": version}},
            "node_modules/openclaw": {"version": version, "integrity": self.NEW_INTEGRITY,
                                      "resolved": f"https://registry.npmjs.org/openclaw/-/openclaw-{version}.tgz"},
        }}
        self.save()

    def test_update_resolves_npm_latest_and_installs_after_consent(self):
        self.installed()
        self.configure_latest()
        code, output = self.box.run("update", "--check", "--json")
        self.assertEqual(0, code, output)
        report = json.loads(output)
        self.assertEqual((OPENCLAW.OPENCLAW_VERSION, self.NEW, self.NEW_INTEGRITY, True),
                         (report["installed_version"], report["latest_version"], report["latest_integrity"],
                          report["update_available"]))
        code, output = self.box.run("update")
        self.assertEqual(1, code)
        self.assertIn(f"{OPENCLAW.OPENCLAW_VERSION} (", output)
        self.assertIn(self.NEW, output)
        code, output = self.box.run("update", "--yes", "--expect", self.NEW)
        self.assertEqual(0, code, output)
        self.assertTrue(any("--package-lock-only" in c and "--ignore-scripts" in c for c in self.npm_calls()))
        receipt = json.loads(OPENCLAW.receipt_path().read_text())
        self.assertEqual((self.NEW, "generated-at-update"), (receipt["version"], receipt["lockfile_source"]))
        self.assertEqual(Path(os.readlink(OPENCLAW.link_path())).parts[-4], self.NEW)
        # The previous version is kept for one update, then pruned.
        self.assertTrue(OPENCLAW.version_dir(OPENCLAW.OPENCLAW_VERSION).exists())

    def test_update_refuses_a_generated_lock_with_a_different_integrity(self):
        self.installed()
        self.configure_latest(integrity="sha512-" + "R" * 86 + "==")
        code, output = self.box.run("update", "--yes")
        self.assertEqual(1, code)
        self.assertIn("integrity", output)
        self.assertEqual(OPENCLAW.OPENCLAW_VERSION, json.loads(OPENCLAW.receipt_path().read_text())["version"])

    # ---- open / gateway / uninstall ---------------------------------------

    def test_first_open_onboards_inside_firebreak(self):
        self.installed()
        code, output = self.box.run("open")
        self.assertEqual(0, code, output)
        launch = self.box.wait_call("konsole")
        self.assertIn(str(self.firebreak) + " run --workspace openclaw", launch)
        self.assertTrue(launch.endswith("openclaw.mjs onboard"), launch)

    def test_open_runs_local_chat_inside_firebreak(self):
        self.installed()
        state = self.box.home / "Workspaces/openclaw/.openclaw"
        state.mkdir(parents=True)
        (state / "openclaw.json").write_text("{}\n")
        code, output = self.box.run("open")
        self.assertEqual(0, code, output)
        launch = self.box.wait_call("konsole")
        version_dir = OPENCLAW.version_dir(OPENCLAW.OPENCLAW_VERSION)
        for piece in (str(self.firebreak) + " run --workspace openclaw --net allow", f"--read {version_dir}",
                      f"OPENCLAW_STATE_DIR={self.box.home}/Workspaces/openclaw/.openclaw", "openclaw.mjs chat"):
            self.assertIn(piece, launch)
        self.assertTrue((self.box.home / "Workspaces/openclaw/.openclaw").is_dir())

    def test_open_refuses_without_the_sandbox(self):
        self.installed()
        with patch.object(OPENCLAW, "FIREBREAK", self.box.root / "missing"):
            code, output = self.box.run("open")
        self.assertEqual(1, code)
        self.assertIn("firebreak", output)
        self.assertFalse(any(c.startswith("konsole") for c in self.box.calls()))

    def test_gateway_is_opt_in_and_loopback_only(self):
        self.installed()
        code, output = self.box.run("gateway", "enable")
        self.assertEqual(1, code)  # no tty, no --yes
        # A config that answers anything but loopback must stop the service install.
        with patch.object(OPENCLAW, "gateway_bind", return_value="lan"):
            code, output = self.box.run("gateway", "enable", "--yes")
        self.assertEqual(1, code)
        self.assertIn("loopback", output)
        self.assertFalse(any("gateway install" in c for c in self.box.calls()))
        with patch.object(OPENCLAW, "gateway_bind", return_value="loopback"):
            code, output = self.box.run("gateway", "enable", "--yes")
        self.assertEqual(0, code, output)
        self.assertTrue(any("config set gateway.bind loopback" in c for c in self.box.calls()))
        self.assertTrue(any("gateway install" in c for c in self.box.calls()))
        self.assertTrue(json.loads(OPENCLAW.receipt_path().read_text())["gateway_enabled"])

    def test_uninstall_keeps_the_workspace_and_removes_the_install(self):
        self.installed()
        notes = self.box.home / "Workspaces/openclaw/notes.md"
        notes.parent.mkdir(parents=True)
        notes.write_text("mine\n")
        code, output = self.box.run("uninstall", "--yes")
        self.assertEqual(0, code, output)
        self.assertTrue(any("uninstall --service --yes --non-interactive" in c for c in self.box.calls()))
        self.assertFalse(OPENCLAW.prefix_root().exists())
        self.assertFalse(OPENCLAW.link_path().is_symlink())
        self.assertFalse(OPENCLAW.state_dir().exists())
        self.assertTrue(notes.exists())


class NothingRunsAsRoot(unittest.TestCase):
    def test_helpers_have_no_privileged_path(self):
        for name in ("shadowfetch-hermes", "shadowfetch-openclaw"):
            source = (BIN / name).read_text()
            with self.subTest(helper=name):
                self.assertTrue(source.startswith("#!/usr/bin/python3\n"))
                self.assertNotRegex(source, r"/usr/bin/(sudo|pkexec|doas|run0)\b")
                # No hidden root entry point like Grok Bot's pkexec'd `_install`.
                self.assertNotRegex(source, r"add_parser\(\s*\"_")
                self.assertIn("never as root", source)
                self.assertTrue(os.access(BIN / name, os.X_OK))


if __name__ == "__main__":
    unittest.main(verbosity=2)
