"""Two things QA 5.0.1 saw Fireproof get wrong on a broken upgrade.

1. INTERRUPTED DPKG. After `fireproof update` had killed its own dpkg, the
   system had 61 packages unpacked but not configured and dpkg --audit was not
   empty -- and Fireproof's analyze offered "23 upgrade" as a normal update.
   The retry died on the same package. Analyze must refuse to offer an update
   there and print the one command that finishes the interrupted run; the
   commit must refuse it too, under the lock, whatever the client sent.

2. MIRROR host:port. The verify battery's "Mirror still resolves" check ran
   `getent hosts 10.0.2.2:8816`: the whole URI authority was taken for a host
   name, so any mirror with a port was reported as not resolving.
"""
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

from stubs import install_stubs, load_fireproofd

fp = load_fireproofd()

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(HERE, "..", "data", "usr", "bin", "fireproof")
REPAIR = "sudo dpkg --configure -a && sudo apt -f install"

CURSTATE_INSTALLED = 6
CURSTATE_UNPACKED = 1
CURSTATE_HALF_CONFIGURED = 2
INSTSTATE_OK = 0
INSTSTATE_REINSTREQ = 1


def fake_pkg(name, current=CURSTATE_INSTALLED, inst=INSTSTATE_OK):
    return SimpleNamespace(
        name=name, installed=SimpleNamespace(version="1"), candidate=None,
        is_upgradable=False,
        _pkg=SimpleNamespace(current_state=current, inst_state=inst,
                             selected_state=0))


class FakeCache(list):
    """Iterable like apt.Cache; any attempt to plan an upgrade is an error."""

    def upgrade(self, dist_upgrade=False):
        raise AssertionError("an upgrade was planned on an interrupted system")

    def get_changes(self):
        raise AssertionError("a change set was computed on an interrupted system")

    def clear(self):
        pass


def broken_cache():
    return FakeCache([
        fake_pkg("bash"),
        fake_pkg("shadowfetch-missions", current=CURSTATE_UNPACKED),
        fake_pkg("libfoo1", current=CURSTATE_HALF_CONFIGURED),
        fake_pkg("shadowfetch-fireproof", inst=INSTSTATE_REINSTREQ),
    ])


AUDIT_TEXT = ("The following packages have been unpacked but not yet "
              "configured.\n shadowfetch-missions   Mission Control engine")


class TestInterruptedDetection(unittest.TestCase):
    def test_the_repair_command_is_exactly_the_one_that_worked(self):
        self.assertEqual(fp.REPAIR_COMMAND, REPAIR)

    def test_cache_states_are_found(self):
        found = fp.interrupted_packages(broken_cache())
        self.assertEqual(found, [
            {"name": "libfoo1", "state": "half-configured"},
            {"name": "shadowfetch-fireproof", "state": "reinstall required"},
            {"name": "shadowfetch-missions", "state": "unpacked, not configured"},
        ])

    def test_trigger_states_count(self):
        cache = FakeCache([fake_pkg("man-db", current=8),
                           fake_pkg("dbus", current=7)])
        names = [p["name"] for p in fp.interrupted_packages(cache)]
        self.assertEqual(names, ["dbus", "man-db"])

    def test_clean_system_is_not_interrupted(self):
        cache = FakeCache([fake_pkg("bash"), fake_pkg("coreutils")])
        self.assertIsNone(fp.interrupted_report(cache, ("", True)))

    def test_dpkg_audit_alone_is_enough(self):
        cache = FakeCache([fake_pkg("bash")])
        report = fp.interrupted_report(cache, (AUDIT_TEXT, True))
        self.assertIsNotNone(report)
        self.assertEqual(report["packages"], [])
        self.assertEqual(report["audit"], AUDIT_TEXT)
        self.assertIn(REPAIR, report["message"])

    def test_cache_alone_is_enough_when_audit_cannot_run(self):
        report = fp.interrupted_report(broken_cache(), ("", False))
        self.assertIsNotNone(report)
        self.assertFalse(report["audit_established"])
        self.assertIn("3 packages are not finished installing", report["message"])

    def test_dpkg_audit_reads_output_and_untrusted(self):
        saved = fp.run
        try:
            fp.run = lambda cmd, timeout=30: (1, AUDIT_TEXT + "\n", "")
            self.assertEqual(fp.dpkg_audit(), (AUDIT_TEXT, True))
            fp.run = lambda cmd, timeout=30: (0, "", "")
            self.assertEqual(fp.dpkg_audit(), ("", True))
            fp.run = lambda cmd, timeout=30: (fp.UNTRUSTED_RC, "", "refused")
            self.assertEqual(fp.dpkg_audit(), ("", False))
        finally:
            fp.run = saved


class TestAnalyzeRefuses(unittest.TestCase):
    def setUp(self):
        self._saved = fp.phoenix_available
        fp.phoenix_available = lambda: True

    def tearDown(self):
        fp.phoenix_available = self._saved

    def test_no_change_set_is_offered(self):
        a = fp.build_analysis({}, cache=broken_cache(),
                              audit=(AUDIT_TEXT, True))
        self.assertEqual(a["counts"]["total"], 0)
        self.assertEqual(a["upgrades"] + a["installs"] + a["removals"], [])
        self.assertEqual(a["change_set_hash"], fp.change_set_hash([]))
        self.assertTrue(a["banners"]["default_dont_proceed"])
        self.assertEqual(a["interrupted"]["repair_command"], REPAIR)
        self.assertIn(REPAIR, a["banners"]["interrupted"])
        json.dumps(a)   # goes over D-Bus as JSON

    def test_every_key_a_normal_reader_uses_is_present(self):
        a = fp.build_analysis({}, cache=broken_cache(), audit=("", True))
        for key in ("schema", "generated_at", "change_set_hash", "counts",
                    "installs", "upgrades", "removals", "library_renames",
                    "holds", "kept_back", "banners", "update_count",
                    "notify_suppressed"):
            self.assertIn(key, a)
        for key in ("major_transition", "kernel", "nvidia",
                    "same_set_rollback", "phoenix_available"):
            self.assertIn(key, a["banners"])


class TestCommitRefuses(unittest.TestCase):
    """The same rule under the lock: a client that skips analyze gets nothing."""

    def test_commit_worker_refuses_and_names_the_repair(self):
        finished = []
        cache = broken_cache()
        saved = (fp.apt.Cache, fp.apt_pkg.get_lock, fp.dpkg_audit,
                 fp.build_analysis)
        with tempfile.TemporaryFile() as lockfile:
            fd = os.dup(lockfile.fileno())
            try:
                fp.apt.Cache = lambda *a, **k: cache
                fp.apt_pkg.get_lock = lambda *a, **k: fd
                fp.dpkg_audit = lambda: ("", True)
                fp.build_analysis = lambda state, **k: {"stub": True}
                obj = fp.Fireproof.__new__(fp.Fireproof)
                obj._lock = threading.Lock()
                obj._lock.acquire()
                obj._state = {}
                obj._phase = "idle"
                obj._quit_requested = False
                obj._emit_progress = lambda *a: None
                obj._notify_badge = lambda: None
                obj.Finished = finished.append
                obj._commit_worker("0" * 64)
            finally:
                (fp.apt.Cache, fp.apt_pkg.get_lock, fp.dpkg_audit,
                 fp.build_analysis) = saved
            with self.assertRaises(OSError):
                os.fstat(fd)        # the lock was released
        self.assertEqual(len(finished), 1)
        result = json.loads(finished[0])
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "interrupted")
        self.assertEqual(result["repair_command"], REPAIR)
        self.assertIn(REPAIR, result["message"])
        self.assertFalse(obj._lock.locked())


class TestMirrorHostname(unittest.TestCase):
    def test_uri_hostname(self):
        cases = {
            "http://10.0.2.2:8816/pool/main/s/x.deb": "10.0.2.2",
            "https://deb.debian.org/debian/pool/x.deb": "deb.debian.org",
            "http://mirror.lan:3142/debian/x.deb": "mirror.lan",
            "https://user:secret@mirror.example:443/x.deb": "mirror.example",
            "http://[2001:db8::1]:8080/x.deb": "2001:db8::1",
            "mirror+https://deb.debian.org/debian/x": "deb.debian.org",
            "file:/var/cache/apt/archives/x.deb": None,
            "cdrom:[Debian]/pool/x.deb": None,
            "": None,
        }
        for uri, host in cases.items():
            with self.subTest(uri=uri):
                self.assertEqual(fp.uri_hostname(uri), host)

    def test_recorded_hosts_from_older_state_are_normalised(self):
        cases = {
            "10.0.2.2:8816": "10.0.2.2",
            "deb.debian.org": "deb.debian.org",
            "[::1]:80": "::1",
            "2001:db8::1": "2001:db8::1",
            "http://mirror.lan:3142": "mirror.lan",
            "": None,
            None: None,
        }
        for value, host in cases.items():
            with self.subTest(value=value):
                self.assertEqual(fp.mirror_hostname(value), host)

    def test_download_progress_records_the_host_only(self):
        hosts = set()
        progress = fp.SignalAcquireProgress(lambda *a: None,
                                            threading.Event(), hosts)
        progress.fetch(SimpleNamespace(uri="http://10.0.2.2:8816/pool/a.deb"))
        progress.fetch(SimpleNamespace(uri="file:/srv/repo/b.deb"))
        self.assertEqual(hosts, {"10.0.2.2"})

    def test_verify_resolves_the_host_not_host_port(self):
        calls = []

        def fake_run(cmd, timeout=30):
            calls.append(list(cmd))
            if cmd[0] == "getent":
                return 0, "10.0.2.2  10.0.2.2\n", ""
            return 0, "", ""

        saved = (fp.run, fp.failed_units, fp.is_substitutable, fp.EXECUTOR)
        try:
            fp.run = fake_run
            fp.failed_units = lambda: []
            fp.is_substitutable = lambda name: False
            fp.EXECUTOR = SimpleNamespace(available=lambda name: False,
                                          explain=lambda name: name)
            report = fp.run_verify({"mirror_hosts": ["10.0.2.2:8816"]})
        finally:
            fp.run, fp.failed_units, fp.is_substitutable, fp.EXECUTOR = saved
        self.assertIn(["getent", "hosts", "10.0.2.2"], calls)
        dns = [c for c in report["checks"] if c["id"] == "dns"]
        self.assertEqual(dns[0]["status"], "pass")
        self.assertEqual(dns[0]["detail"], "10.0.2.2")


def load_cli():
    install_stubs()
    old = sys.argv
    sys.argv = ["fireproof", "check"]   # the script reads argv at import
    try:
        loader = importlib.machinery.SourceFileLoader("fireproof_cli_test", CLI)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
    finally:
        sys.argv = old
    return mod


class FakeDaemon:
    def __init__(self, analysis):
        self.analysis = analysis
        self.updates = []

    def Analyze(self, timeout=None):
        return json.dumps(self.analysis)

    def Update(self, *a, **k):
        self.updates.append(a)
        raise AssertionError("Update was called on an interrupted system")


class TestCliRefuses(unittest.TestCase):
    def setUp(self):
        self.cli = load_cli()
        saved = fp.phoenix_available
        fp.phoenix_available = lambda: True
        try:
            self.analysis = fp.build_analysis({}, cache=broken_cache(),
                                              audit=(AUDIT_TEXT, True))
        finally:
            fp.phoenix_available = saved
        self.daemon = FakeDaemon(self.analysis)
        self.cli.iface = lambda: self.daemon

    def run_cmd(self, fn, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = fn(*args)
        return rc, out.getvalue()

    def test_update_prints_the_repair_and_never_asks(self):
        import builtins
        saved = builtins.input
        builtins.input = lambda *_a: self.fail("the update was offered")
        try:
            rc, out = self.run_cmd(self.cli.cmd_update, True)
        finally:
            builtins.input = saved
        self.assertEqual(rc, self.cli.EXIT_INTERRUPTED)
        self.assertEqual(rc, 3)
        self.assertIn(REPAIR, out)
        self.assertIn("shadowfetch-missions", out)
        self.assertNotIn("Proceed with the update", out)
        self.assertEqual(self.daemon.updates, [])

    def test_check_prints_the_repair(self):
        rc, out = self.run_cmd(self.cli.cmd_check)
        self.assertEqual(rc, 3)
        self.assertIn(REPAIR, out)
        self.assertIn("unpacked but not yet", out)

    def test_a_normal_analysis_is_unaffected(self):
        normal = dict(self.analysis, interrupted=None)
        normal["banners"] = dict(self.analysis["banners"],
                                 default_dont_proceed=False)
        normal["kept_back"] = {"story": "full path removes 0", "packages": []}
        self.daemon.analysis = normal
        rc, out = self.run_cmd(self.cli.cmd_check)
        self.assertEqual(rc, 0)
        self.assertNotIn(REPAIR, out)
        rc, out = self.run_cmd(self.cli.cmd_update, True)
        self.assertEqual(rc, 0)
        self.assertIn("Nothing to update.", out)


if __name__ == "__main__":
    unittest.main()
