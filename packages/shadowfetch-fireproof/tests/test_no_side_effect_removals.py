"""An update never removes a package the person did not ask to remove.

QA 5.0.1, on a system upgraded from 4.1 while Debian testing was in the
middle of its libavcodec/mlt transition: Fireproof's plan (python3-apt
dist-upgrade) removed 14-16 packages, among them shadowfetch-desktop,
shadowfetch-creative-base, krita and kdenlive. `apt full-upgrade` on the same
system held 12 packages back and removed nothing.

plan_update() keeps every removal it refuses, lets libapt's ResolveByKeep
hold back the upgrades that needed it, and says so. The only removals an
update may make are retirements: required by an incoming shadowfetch-*
package's own Conflicts/Breaks, of an automatically installed package, never
the two desktop metapackages.

The cache here is a small model of apt.Cache: each planned change lists the
other changes it needs (`needs`), and the fake ResolveByKeep keeps back any
change whose needs are no longer met, until nothing is broken -- the contract
libapt's pkgProblemResolver::ResolveByKeep has.
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace

from stubs import install_stubs, load_fireproofd

fp = load_fireproofd()

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(HERE, "..", "data", "usr", "bin", "fireproof")
CURSTATE_INSTALLED = 6


class FakeVersion:
    def __init__(self, pkg, version, conflicts=(), breaks=()):
        self.package = pkg
        self.version = version
        self.priority = "optional"
        self._rel = {"Conflicts": list(conflicts), "Breaks": list(breaks)}

    def get_dependencies(self, *types):
        """Each relation names a package; it targets that package's
        installed version (a real BaseDependency.target_versions also
        applies the version constraint and Provides)."""
        deps = []
        for t in types:
            for name in self._rel.get(t, []):
                target = self.package.world.get(name)
                versions = ([target.installed] if target is not None
                            and target.installed is not None else [])
                deps.append(SimpleNamespace(or_dependencies=[
                    SimpleNamespace(target_versions=versions)]))
        return deps


class FakePkg:
    def __init__(self, world, name, installed=None, candidate=None,
                 auto=True, conflicts=(), breaks=()):
        self.world = world
        self.name = name
        self.essential = False
        self.is_auto_installed = auto
        self.installed = FakeVersion(self, installed) if installed else None
        self.candidate = (FakeVersion(self, candidate, conflicts, breaks)
                          if candidate else self.installed)
        self.is_upgradable = bool(installed and candidate
                                  and candidate != installed)
        self._pkg = SimpleNamespace(name=name, current_state=CURSTATE_INSTALLED,
                                    inst_state=0, selected_state=0)
        self.mark = None          # None | "install" | "upgrade" | "delete"
        world[name] = self

    @property
    def marked_delete(self):
        return self.mark == "delete"

    @property
    def marked_install(self):
        return self.mark == "install"

    @property
    def marked_upgrade(self):
        return self.mark == "upgrade"

    def mark_keep(self):
        self.mark = None

    @property
    def is_auto_removable(self):
        """libapt's garbage, for a new package: nothing planned needs it."""
        if self.installed is not None or not self.marked_install:
            return False
        return not any(self.name in self.world.needs.get(p.name, ())
                       for p in self.world.get_changes())


class FakeCache(dict):
    """{name: FakePkg}. full/safe: {name: mark}. needs: {name: [names]}."""

    def __init__(self):
        super().__init__()
        self.full, self.safe, self.needs = {}, {}, {}
        self.resolver_works = True
        self.protected = set()
        self.required_download = 1000
        self.required_space = 2000
        self._depcache = SimpleNamespace()
        self.calls = []

    def __iter__(self):
        return iter(list(self.values()))

    def pkg(self, *a, **k):
        return FakePkg(self, *a, **k)

    def upgrade(self, dist_upgrade=False):
        self.calls.append("dist-upgrade" if dist_upgrade else "upgrade")
        for name, mark in (self.full if dist_upgrade else self.safe).items():
            self[name].mark = mark

    def clear(self):
        for p in self.values():
            p.mark = None
        self.protected = set()

    def get_changes(self):
        return [p for p in self.values() if p.mark]

    @contextlib.contextmanager
    def actiongroup(self):
        yield

    def broken(self):
        return [p for p in self.get_changes()
                if any(self[n].mark is None for n in self.needs.get(p.name, ()))]


class FakeResolver:
    def __init__(self, cache):
        self.cache = cache

    def protect(self, pkg):
        self.cache.protected.add(pkg.name)

    def resolve_by_keep(self):
        if not self.cache.resolver_works:
            return
        while True:
            broken = [p for p in self.cache.broken()
                      if p.name not in self.cache.protected]
            if not broken:
                return
            for p in broken:
                p.mark_keep()


@contextlib.contextmanager
def fake_resolver():
    saved = (fp._problem_resolver, fp._broken_count, fp.phoenix_available)
    fp._problem_resolver = FakeResolver
    fp._broken_count = lambda cache: len(cache.broken())
    fp.phoenix_available = lambda: True
    try:
        yield
    finally:
        (fp._problem_resolver, fp._broken_count,
         fp.phoenix_available) = saved


def transition_cache():
    """The QA 5.0.1 situation, in miniature: the libav/mlt transition."""
    c = FakeCache()
    c.pkg("bash", "5.2-1", "5.2-2")
    c.pkg("shadowfetch-missions", "5.0.0-1", "5.0.1-1")
    c.pkg("libavutil60", "8.1.2-2", "9.0.2-1")
    c.pkg("libavcodec-extra62", "8.1.2-2", "9.0.2-1")
    c.pkg("libavcodec63", None, "9.0.2-1")
    c.pkg("krita", "1:5.3.4-1", None, auto=False)
    c.pkg("kdenlive", "26.08.1-1", None, auto=False)
    c.pkg("libmlt7", "7.40.0-1")
    c.pkg("shadowfetch-desktop", "5.0.1-1", None, auto=False)
    c.pkg("shadowfetch-creative-base", "5.0.1-1")
    c.full = {"bash": "upgrade", "shadowfetch-missions": "upgrade",
              "libavutil60": "upgrade", "libavcodec-extra62": "upgrade",
              "libavcodec63": "install", "krita": "delete",
              "kdenlive": "delete", "libmlt7": "delete",
              "shadowfetch-desktop": "delete",
              "shadowfetch-creative-base": "delete"}
    c.safe = {"bash": "upgrade", "shadowfetch-missions": "upgrade"}
    # The new libav can only go in if the packages built against the old
    # one leave.
    c.needs = {"libavutil60": ["krita", "kdenlive", "libmlt7",
                               "libavcodec63"],
               "libavcodec-extra62": ["libavutil60"],
               "libavcodec63": ["libavutil60"]}
    return c


def retirement_cache(old_auto=True, conflicts=("shadowfetch-oldtool",)):
    """shadowfetch-defaults retires shadowfetch-oldtool with Conflicts."""
    c = FakeCache()
    c.pkg("bash", "5.2-1", "5.2-2")
    c.pkg("shadowfetch-defaults", "5.0.1-1", "5.0.2-1", conflicts=conflicts)
    c.pkg("shadowfetch-oldtool", "4.1.0-1", None, auto=old_auto)
    c.pkg("shadowfetch-desktop", "5.0.1-1", None, auto=True)
    c.full = {"bash": "upgrade", "shadowfetch-defaults": "upgrade"}
    for name in conflicts:
        c.full[name] = "delete"
    c.safe = {"bash": "upgrade"}
    c.needs = {"shadowfetch-defaults": list(conflicts)}
    return c


def analyze(cache):
    with fake_resolver():
        return fp.build_analysis({}, cache=cache, audit=("", True))


class TestRemovalsHeldBack(unittest.TestCase):
    def test_transition_removes_nothing(self):
        a = analyze(transition_cache())
        self.assertEqual(a["removals"], [])
        self.assertEqual(a["counts"]["remove"], 0)
        self.assertEqual(a["library_renames"], [])
        self.assertEqual(sorted(p["name"] for p in a["upgrades"]),
                         ["bash", "shadowfetch-missions"])
        self.assertEqual(a["installs"], [])

    def test_only_the_affected_upgrades_are_held_back(self):
        a = analyze(transition_cache())
        held = a["held_back"]
        self.assertEqual([h["name"] for h in held["packages"]],
                         ["libavcodec-extra62", "libavutil60"])
        self.assertEqual(held["packages"][1],
                         {"name": "libavutil60", "installed": "8.1.2-2",
                          "candidate": "9.0.2-1",
                          "reason": "debian-transition"})
        self.assertEqual(held["fallback"], "hold-back")
        self.assertEqual(
            [r["name"] for r in held["avoided_removals"]],
            ["kdenlive", "krita", "libmlt7", "shadowfetch-creative-base",
             "shadowfetch-desktop"])
        self.assertIn("held back", a["kept_back"]["story"])

    def test_an_upgrade_the_full_plan_never_touched_is_listed_too(self):
        # apt full-upgrade lists every installed package it does not
        # upgrade as kept back; the classic resolver's full plan may not
        # have tried this one at all once it chose the removals.
        cache = transition_cache()
        cache.pkg("python3-eventlet", "0.40.4-1", "0.40.4-4")
        held_pin = cache.pkg("firefox-esr", "128.1-1", "128.2-1")
        held_pin._pkg.selected_state = fp.apt_pkg.SELSTATE_HOLD
        a = analyze(cache)
        self.assertEqual([h["name"] for h in a["held_back"]["packages"]],
                         ["libavcodec-extra62", "libavutil60",
                          "python3-eventlet"])
        self.assertEqual([h["name"] for h in a["holds"]], ["firefox-esr"])
        self.assertIn("Installing 3 updates now", a["held_back"]["message"])
        # 6 upgradable: 2 upgraded, 3 held back, 1 dpkg hold (still counted
        # as before: the hold is the person's own choice).
        self.assertEqual(a["update_count"], 3)

    def test_badge_counts_only_what_update_installs(self):
        a = analyze(transition_cache())
        # 4 upgradable installed packages, 2 held back.
        self.assertEqual(a["update_count"], 2)

    def test_the_commit_replans_to_the_same_hash(self):
        cache = transition_cache()
        a = analyze(cache)
        with fake_resolver():
            fp.plan_update(cache)
        self.assertEqual(fp.change_set_hash(cache.get_changes()),
                         a["change_set_hash"])
        self.assertFalse(any(p.marked_delete for p in cache.get_changes()))

    def test_new_packages_only_held_back_upgrades_wanted_are_left_out(self):
        cache = transition_cache()
        cache.pkg("libavcodec-extra63", None, "9.0.2-1")
        cache.pkg("libnew-tool1", None, "1.0-1")
        cache.full["libavcodec-extra63"] = "install"
        cache.full["libnew-tool1"] = "install"
        # libavutil60's upgrade pulls libavcodec-extra63 in; that package is
        # not broken when libavutil60 is held back, only no longer needed.
        cache.needs["libavutil60"].append("libavcodec-extra63")
        # shadowfetch-missions' upgrade, which goes ahead, needs libnew-tool1.
        cache.needs["shadowfetch-missions"] = ["libnew-tool1"]
        a = analyze(cache)
        self.assertEqual([p["name"] for p in a["installs"]], ["libnew-tool1"])
        self.assertFalse(cache["libavcodec-extra63"].marked_install)
        self.assertEqual(a["removals"], [])

    def test_safe_upgrade_when_holding_back_cannot_settle_it(self):
        cache = transition_cache()
        cache.resolver_works = False
        with fake_resolver():
            plan = fp.plan_update(cache)
        self.assertEqual(plan["fallback"], "safe-upgrade")
        self.assertEqual(cache.calls[-1], "upgrade")
        self.assertEqual(sorted(p.name for p in cache.get_changes()),
                         ["bash", "shadowfetch-missions"])
        self.assertFalse(any(p.marked_delete for p in cache.get_changes()))

    def test_no_removal_means_the_full_plan_unchanged(self):
        cache = transition_cache()
        cache.full = {"bash": "upgrade", "libavutil60": "upgrade",
                      "libavcodec63": "install"}
        cache.needs = {}
        a = analyze(cache)
        self.assertEqual(a["held_back"]["packages"], [])
        self.assertIsNone(a["held_back"]["message"])
        self.assertIsNone(a["banners"]["held_back"])
        self.assertEqual(a["counts"]["install"], 1)
        self.assertEqual(a["counts"]["upgrade"], 2)


class TestCommitPlansTheSame(unittest.TestCase):
    """Under the lock the commit re-plans with plan_update, so the hash the
    person approved (no removals) matches, and the download starts with no
    package marked for removal."""

    def run_commit(self, cache, expected_hash):
        import tempfile
        import threading
        finished, marked = [], {}

        def fetch_archives(progress=None):
            marked["delete"] = [p.name for p in cache.get_changes()
                                if p.marked_delete]
            raise fp.apt.cache.FetchCancelledException()

        cache.fetch_archives = fetch_archives
        saved = (fp.apt.Cache, fp.apt_pkg.get_lock, fp.dpkg_audit,
                 fp.build_analysis, fp.failed_units, fp.snapper_max_number)
        with tempfile.TemporaryFile() as lockfile, fake_resolver():
            fd = os.dup(lockfile.fileno())
            try:
                fp.apt.Cache = lambda *a, **k: cache
                fp.apt_pkg.get_lock = lambda *a, **k: fd
                fp.dpkg_audit = lambda: ("", True)
                fp.build_analysis = lambda state, **k: {"stub": True}
                fp.failed_units = lambda: []
                fp.snapper_max_number = lambda: 0
                obj = fp.Fireproof.__new__(fp.Fireproof)
                obj._lock = threading.Lock()
                obj._lock.acquire()
                obj._state = {}
                obj._phase = "idle"
                obj._quit_requested = False
                obj._cancel_download = threading.Event()
                obj._emit_progress = lambda *a: None
                obj._notify_badge = lambda: None
                obj.DriftDetected = lambda *a: None
                obj.Finished = finished.append
                obj._commit_worker(expected_hash)
            finally:
                (fp.apt.Cache, fp.apt_pkg.get_lock, fp.dpkg_audit,
                 fp.build_analysis, fp.failed_units,
                 fp.snapper_max_number) = saved
        return json.loads(finished[0]), marked

    def test_approved_plan_is_the_one_downloaded(self):
        approved = analyze(transition_cache())["change_set_hash"]
        result, marked = self.run_commit(transition_cache(), approved)
        self.assertEqual(result["stage"], "download")   # no drift
        self.assertEqual(marked["delete"], [])

    def test_the_old_full_plan_hash_is_drift(self):
        cache = transition_cache()
        cache.upgrade(dist_upgrade=True)
        full_hash = fp.change_set_hash(cache.get_changes())
        cache.clear()
        result, marked = self.run_commit(cache, full_hash)
        self.assertEqual(result["stage"], "drift")
        self.assertEqual(marked, {})


class TestIntendedRetirement(unittest.TestCase):
    def test_retirement_by_a_shadowfetch_conflicts_is_allowed(self):
        a = analyze(retirement_cache())
        self.assertEqual([r["name"] for r in a["removals"]],
                         ["shadowfetch-oldtool"])
        self.assertIn("shadowfetch-defaults",
                      [p["name"] for p in a["upgrades"]])
        self.assertEqual(a["held_back"]["packages"], [])

    def test_retirement_by_breaks_is_allowed(self):
        c = retirement_cache(conflicts=())
        c["shadowfetch-defaults"].candidate._rel["Breaks"] = [
            "shadowfetch-oldtool"]
        c.full["shadowfetch-oldtool"] = "delete"
        a = analyze(c)
        self.assertEqual([r["name"] for r in a["removals"]],
                         ["shadowfetch-oldtool"])

    def test_a_manually_installed_package_is_never_retired(self):
        a = analyze(retirement_cache(old_auto=False))
        self.assertEqual(a["removals"], [])
        self.assertEqual([h["name"] for h in a["held_back"]["packages"]],
                         ["shadowfetch-defaults"])
        self.assertEqual(a["held_back"]["avoided_removals"][0]["reason"],
                         "installed by you, never removed by an update")

    def test_only_a_shadowfetch_package_can_retire(self):
        c = retirement_cache()
        targets = fp.retirement_targets(
            [SimpleNamespace(name="vlc", marked_delete=False,
                             candidate=c["shadowfetch-defaults"].candidate)])
        self.assertEqual(targets, set())
        self.assertIsNotNone(fp.removal_refusal(c["shadowfetch-oldtool"],
                                                targets))

    def test_a_removal_nothing_asked_for_is_refused(self):
        c = retirement_cache()
        c.pkg("libfoo1", "1.0-1")
        c.full["libfoo1"] = "delete"
        c.needs["bash"] = ["libfoo1"]
        a = analyze(c)
        self.assertEqual([r["name"] for r in a["removals"]],
                         ["shadowfetch-oldtool"])
        self.assertEqual([h["name"] for h in a["held_back"]["packages"]],
                         ["bash"])


class TestProtectedMetapackages(unittest.TestCase):
    def test_metapackages_are_never_retired_even_if_conflicted(self):
        for name in ("shadowfetch-desktop", "shadowfetch-creative-base"):
            with self.subTest(name=name):
                c = retirement_cache(conflicts=(name,))
                if name not in c:
                    c.pkg(name, "5.0.1-1", None, auto=True)
                a = analyze(c)
                self.assertEqual(a["removals"], [])
                self.assertEqual(
                    a["held_back"]["avoided_removals"][0]["reason"],
                    "Shadowfetch metapackage, never removed by an update")
                self.assertIn("shadowfetch-defaults",
                              [h["name"] for h in a["held_back"]["packages"]])

    def test_protected_set(self):
        self.assertEqual(fp.PROTECTED_PACKAGES,
                         {"shadowfetch-desktop", "shadowfetch-creative-base"})

    def test_transition_keeps_desktop_krita_and_kdenlive(self):
        cache = transition_cache()
        analyze(cache)
        for name in ("shadowfetch-desktop", "shadowfetch-creative-base",
                     "krita", "kdenlive"):
            self.assertFalse(cache[name].marked_delete, name)


class TestMessage(unittest.TestCase):
    def test_message_says_what_and_why(self):
        a = analyze(transition_cache())
        msg = a["held_back"]["message"]
        self.assertEqual(a["banners"]["held_back"], msg)
        self.assertIn("Debian testing is in the middle of a library "
                      "transition", msg)
        self.assertIn("Installing 2 updates now would remove 5 installed "
                      "packages", msg)
        self.assertIn("krita", msg)
        self.assertIn("shadowfetch-desktop", msg)
        self.assertIn("holds them back and removes nothing", msg)
        self.assertIn("They will update once the transition is complete",
                      msg)

    def test_singular_and_long_lists(self):
        avoided = [{"name": "pkg%02d" % i} for i in range(10)]
        msg = fp.held_back_message([{"name": "x"}], avoided)
        self.assertIn("Installing 1 update now would remove 10 installed "
                      "packages", msg)
        self.assertIn("pkg07 and 2 more", msg)
        self.assertIn("holds it back", msg)
        self.assertIn("It will update", msg)

    def test_message_when_only_new_packages_were_left_out(self):
        msg = fp.held_back_message([], [{"name": "krita"}])
        self.assertIn("would remove 1 installed package (krita)", msg)
        self.assertIn("removes nothing", msg)
        self.assertNotIn("Installing 0", msg)

    def test_cli_prints_the_held_back_packages_and_message(self):
        cli = load_cli()
        a = analyze(transition_cache())
        out = io.StringIO()
        with redirect_stdout(out):
            cli.print_analysis(a)
        text = " ".join(out.getvalue().split())
        self.assertIn("0 removal", text)
        self.assertIn("Held back - nothing will be removed:", text)
        self.assertIn("libavutil60 (8.1.2-2 installed; 9.0.2-1 held back)",
                      text)
        self.assertIn("Debian testing is in the middle of a library "
                      "transition", text)
        self.assertIn("Would have been removed: kdenlive, krita", text)
        self.assertNotIn("Removals:", text)

    def test_update_with_everything_held_back_exits_0(self):
        cli = load_cli()
        cache = transition_cache()
        cache.full = {k: v for k, v in cache.full.items()
                      if k not in ("bash", "shadowfetch-missions")}
        cache.safe = {}
        a = analyze(cache)
        self.assertEqual(a["counts"]["total"], 0)
        daemon = SimpleNamespace(
            Analyze=lambda timeout=None: json.dumps(a),
            Update=lambda *x, **k: self.fail("nothing to update"))
        cli.iface = lambda: daemon
        cli.dbus.mainloop.glib.DBusGMainLoop = lambda **k: None
        out = io.StringIO()
        with redirect_stdout(out):
            rc = cli.cmd_update(True)
        self.assertEqual(rc, 0)
        self.assertIn("Nothing to update.", out.getvalue())
        self.assertIn("Held back - nothing will be removed", out.getvalue())

    def test_cli_reads_an_analysis_from_an_older_daemon(self):
        cli = load_cli()
        a = analyze(retirement_cache())
        del a["held_back"]
        del a["banners"]["held_back"]
        out = io.StringIO()
        with redirect_stdout(out):
            cli.print_analysis(a)
        self.assertNotIn("Held back", out.getvalue())

    def test_a_red_removal_reads_as_a_removal(self):
        cli = load_cli()
        a = analyze(retirement_cache())
        out = io.StringIO()
        with redirect_stdout(out):
            cli.print_analysis(a)
        self.assertIn("shadowfetch-oldtool  (remove 4.1.0-1)", out.getvalue())


def load_cli():
    install_stubs()
    old = sys.argv
    sys.argv = ["fireproof", "check"]   # the script reads argv at import
    try:
        loader = importlib.machinery.SourceFileLoader(
            "fireproof_cli_removals_test", CLI)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
    finally:
        sys.argv = old
    return mod


if __name__ == "__main__":
    unittest.main()
