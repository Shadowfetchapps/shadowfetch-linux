"""Fireproof upgrading itself: nothing in the transaction may stop fireproofd.

QA 5.0.1 (5.0.0 -> 5.0.1, `fireproof update`): dpkg runs as fireproofd's
child, inside fireproofd.service's cgroup. The new package's preinst -- the
dh_installsystemd output for `--no-start`, unchanged since 2.1.4 -- ran
`deb-systemd-invoke stop fireproofd.service`; systemd's default
KillMode=control-group SIGTERMed dpkg with the daemon, and needrestart (apt's
DPkg::Post-Invoke, $nrconf{restart} = 'a') then restarted fireproofd from
inside its own transaction and hung until TimeoutStopSec.

The fence, one class per layer:
  * debian/rules: fireproofd gets --no-stop-on-upgrade, and the generated
    maintainer-script snippets never name it (run against the real recipe
    with the host's dh_installsystemd when it is installed);
  * the unit: KillMode=mixed, so a stop SIGTERMs the daemon only;
  * needrestart: the shipped conffile excludes fireproofd, and is installed;
  * the postinst and its helper restart the daemon AFTER the transaction,
    never from inside it;
  * the daemon exits after a successful commit, so the next D-Bus
    activation runs the code on disk.
"""
import importlib.machinery
import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import unittest

from stubs import load_fireproofd

HERE = os.path.dirname(os.path.abspath(__file__))
PKG = os.path.dirname(HERE)
RULES = os.path.join(PKG, "debian", "rules")
POSTINST = os.path.join(PKG, "debian", "postinst")
INSTALL = os.path.join(PKG, "debian", "shadowfetch-fireproof.install")
UNIT_DIR = os.path.join(PKG, "data", "usr", "lib", "systemd", "system")
UNIT = os.path.join(UNIT_DIR, "fireproofd.service")
NEEDRESTART = os.path.join(PKG, "data", "etc", "needrestart", "conf.d",
                           "50-shadowfetch-fireproof.conf")
HELPER = os.path.join(PKG, "data", "usr", "libexec",
                      "fireproof-restart-after-upgrade")
DAEMON = os.path.join(PKG, "data", "usr", "libexec", "fireproofd")
DEFAULTS_NEEDRESTART = os.path.join(
    PKG, "..", "shadowfetch-defaults", "data", "etc", "needrestart",
    "conf.d", "00-shadowfetch.conf")

# A maintainer-script line that would stop or restart fireproofd synchronously.
STOPS_FIREPROOFD = re.compile(
    r"(deb-systemd-invoke|systemctl)\b[^\n]*fireproofd\.service")


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def recipe(target="override_dh_installsystemd"):
    """The shell lines of one debian/rules target, continuations joined."""
    lines, inside = [], False
    for raw in read(RULES).splitlines():
        if raw.startswith(target + ":"):
            inside = True
            continue
        if inside:
            if raw.startswith("\t"):
                lines.append(raw[1:].strip())
            elif raw.strip() == "" or not raw.startswith("#"):
                break
    return [ln for ln in lines if ln and not ln.startswith("#")]


def load_helper():
    loader = importlib.machinery.SourceFileLoader(
        "fireproof_restart_after_upgrade", HELPER)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class TestRulesNeverStopFireproofd(unittest.TestCase):
    def invocations(self):
        calls = [shlex.split(ln) for ln in recipe()]
        self.assertTrue(calls, "override_dh_installsystemd has no recipe")
        for argv in calls:
            self.assertEqual(argv[0], "dh_installsystemd", argv)
        return calls

    def test_fireproofd_is_named_once_with_no_stop_on_upgrade(self):
        named = [a for a in self.invocations() if "fireproofd.service" in a]
        self.assertEqual(len(named), 1, named)
        self.assertIn("--no-stop-on-upgrade", named[0])
        self.assertIn("--no-start", named[0])
        self.assertNotIn("--restart-after-upgrade", named[0])

    def test_no_invocation_covers_every_unit_implicitly(self):
        # A bare `dh_installsystemd [--flags]` acts on EVERY unit in the
        # package, fireproofd included -- the 2.1.4..5.0.0 shape.
        for argv in self.invocations():
            units = [a for a in argv[1:] if not a.startswith("-")]
            self.assertTrue(units, "unit-less invocation: %s" % argv)

    def test_every_shipped_unit_is_handled_exactly_once(self):
        shipped = sorted(os.listdir(UNIT_DIR))
        named = sorted(a for argv in self.invocations() for a in argv[1:]
                       if not a.startswith("-"))
        self.assertEqual(named, shipped)

    def test_no_unit_is_started_at_install(self):
        for argv in self.invocations():
            self.assertIn("--no-start", argv)

    @unittest.skipUnless(shutil.which("dh_installsystemd"),
                         "debhelper is not installed on this host")
    def test_generated_maintainer_scripts_never_touch_fireproofd(self):
        """Run the real recipe with the host's debhelper, read its output."""
        with tempfile.TemporaryDirectory() as tmp:
            debian = os.path.join(tmp, "debian")
            units = os.path.join(debian, "shadowfetch-fireproof", "usr", "lib",
                                 "systemd", "system")
            os.makedirs(units)
            for name in ("control", "changelog"):
                shutil.copy(os.path.join(PKG, "debian", name), debian)
            for name in os.listdir(UNIT_DIR):
                shutil.copy(os.path.join(UNIT_DIR, name), units)
            for argv in self.invocations():
                proc = subprocess.run(argv, cwd=tmp, capture_output=True,
                                      text=True, check=False)
                self.assertEqual(proc.returncode, 0, proc.stderr)
            snippets = {}
            for root, _dirs, files in os.walk(debian):
                if "usr" in root.split(os.sep):
                    continue
                for name in files:
                    if name.endswith(".debhelper") or \
                            os.sep + "generated" + os.sep in root + os.sep:
                        snippets[os.path.join(root, name)] = read(
                            os.path.join(root, name))
            self.assertTrue(snippets, "debhelper generated nothing")
            text = "\n".join(snippets.values())
            for path, body in snippets.items():
                self.assertNotRegex(body, STOPS_FIREPROOFD, path)
            # The postboot units keep their stop-before-upgrade preinst.
            self.assertRegex(text, r"deb-systemd-invoke stop "
                                   r"'fireproof-postboot\.service'")


class TestUnitKillMode(unittest.TestCase):
    def setUp(self):
        self.unit = read(UNIT)

    def directive(self, key):
        values = [ln.split("=", 1)[1].strip() for ln in self.unit.splitlines()
                  if ln.strip().startswith(key + "=")]
        self.assertEqual(len(values), 1, "%s set %d times" % (key, len(values)))
        return values[0]

    def test_a_stop_signals_the_daemon_only(self):
        self.assertEqual(self.directive("KillMode"), "mixed")

    def test_the_deferral_window_is_kept(self):
        self.assertEqual(self.directive("TimeoutStopSec"), "3600")
        self.assertEqual(self.directive("Type"), "dbus")

    def test_the_daemon_still_defers_a_stop_during_commit(self):
        # KillMode=mixed is only correct because the daemon does not exit
        # while dpkg runs; if the deferral went, mixed would SIGKILL dpkg.
        daemon = read(DAEMON)
        self.assertIn('if service._phase == "committing":', daemon)
        self.assertIn("service._quit_requested = True", daemon)


class TestNeedrestartExcludesFireproofd(unittest.TestCase):
    def test_shipped_as_a_conffile_under_needrestart_conf_d(self):
        rows = [ln.split() for ln in read(INSTALL).splitlines() if ln.strip()]
        self.assertIn(["data/etc/needrestart/conf.d/50-shadowfetch-fireproof.conf",
                       "etc/needrestart/conf.d/"], rows)
        self.assertTrue(os.path.isfile(NEEDRESTART))

    def test_loads_after_the_restart_mode_it_qualifies(self):
        # conf.d is read in name order; shadowfetch-defaults sets restart='a'.
        self.assertTrue(os.path.isfile(DEFAULTS_NEEDRESTART))
        self.assertLess(os.path.basename(DEFAULTS_NEEDRESTART),
                        os.path.basename(NEEDRESTART))

    def test_override_line(self):
        body = [ln for ln in read(NEEDRESTART).splitlines()
                if ln.strip() and not ln.lstrip().startswith("#")]
        self.assertEqual(body, [
            r"$nrconf{override_rc}{qr(^fireproofd\.service$)} = 0;"])

    @unittest.skipUnless(shutil.which("perl"), "perl is not installed")
    def test_perl_evaluates_it_and_it_matches_only_fireproofd(self):
        # needrestart evaluates conf.d files as Perl into %nrconf, after its
        # own defaults have populated override_rc.
        script = r"""
            our %nrconf = (override_rc => {qr(^sddm) => 0});
            my $rv = do $ARGV[0];
            die "load failed: $@\n" if $@;
            for my $svc ("fireproofd.service", "xfireproofd.service",
                         "fireproofd.service.d", "sddm.service") {
                my @hit = grep { $svc =~ $_ } keys %{$nrconf{override_rc}};
                my $val = @hit ? $nrconf{override_rc}{$hit[0]} : "none";
                print "$svc=$val\n";
            }
        """
        proc = subprocess.run(["perl", "-we", script, NEEDRESTART],
                              capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.split(), [
            "fireproofd.service=0", "xfireproofd.service=none",
            "fireproofd.service.d=none", "sddm.service=0"])


class TestPostinstRestartsAfterNotDuring(unittest.TestCase):
    def setUp(self):
        self.text = read(POSTINST)

    def test_syntax(self):
        subprocess.run(["sh", "-n", POSTINST], check=True)

    def test_never_stops_or_restarts_fireproofd_itself(self):
        self.assertNotRegex(self.text, STOPS_FIREPROOFD)

    def test_hands_the_restart_to_the_helper_on_upgrade_only(self):
        tail = self.text.split("#DEBHELPER#", 1)[1]
        block = tail.split("exit 0")[0]
        self.assertIn('[ "$1" = configure ]', block)
        self.assertIn('[ -n "$2" ]', block)
        self.assertIn("/run/systemd/system", block)
        self.assertIn(
            "/usr/libexec/fireproof-restart-after-upgrade schedule || true",
            block)

    def test_helper_is_installed_and_executable(self):
        rows = [ln.split() for ln in read(INSTALL).splitlines() if ln.strip()]
        self.assertIn(["data/usr/libexec/fireproof-restart-after-upgrade",
                       "usr/libexec/"], rows)
        self.assertTrue(os.access(HELPER, os.X_OK))


class FakeRun:
    """Answers systemctl/systemd-run/busctl the way the test says."""

    def __init__(self, active=True, cgroup="/system.slice/fireproofd.service",
                 main_pid="4242", busy="b false", systemd_run_rc=0,
                 policy_rc=0):
        self.calls = []
        self.active = active
        self.cgroup = cgroup
        self.main_pid = main_pid
        self.busy = busy
        self.systemd_run_rc = systemd_run_rc
        self.policy_rc = policy_rc

    def __call__(self, argv, timeout=30):
        self.calls.append(list(argv))
        prog = os.path.basename(argv[0])
        if prog == "systemctl" and "is-active" in argv:
            return (0 if self.active else 3), "", ""
        if prog == "systemctl" and "show" in argv:
            prop = argv[argv.index("--property") + 1]
            return 0, {"ControlGroup": self.cgroup,
                       "MainPID": self.main_pid}[prop] + "\n", ""
        if prog == "systemd-run":
            return self.systemd_run_rc, "", "" if not self.systemd_run_rc \
                else "Unit fireproofd-restart-after-upgrade.service exists"
        if prog == "busctl":
            return (0, self.busy + "\n", "") if self.busy else (1, "", "denied")
        if prog == "policy-rc.d":
            return self.policy_rc, "", ""
        return 0, "", ""

    def named(self, prog):
        return [c for c in self.calls if os.path.basename(c[0]) == prog]


class TestRestartHelperSchedule(unittest.TestCase):
    INSIDE = "0::/system.slice/fireproofd.service\n"
    OUTSIDE = "0::/user.slice/user-1000.slice/session-2.scope\n"

    def setUp(self):
        self.h = load_helper()
        self.h.POLICY_RC_D = os.path.join(HERE, "no-such-policy-rc.d")

    def schedule(self, fake, cgroup_text, systemd=True):
        rc = self.h.schedule(run=fake, self_cgroup_text=cgroup_text,
                             systemd_running=systemd)
        self.assertEqual(rc, 0)     # never fails the postinst
        return fake

    def assert_never_stopped_synchronously(self, fake):
        for argv in fake.named("systemctl"):
            if "fireproofd.service" in argv and (
                    "stop" in argv or "restart" in argv or "try-restart" in argv):
                self.assertIn("--no-block", argv, argv)

    def test_cgroup_parsing_is_path_exact(self):
        h = self.h
        self.assertEqual(h.unified_cgroup("1:name=systemd:/x\n0::/a/b\n"), "/a/b")
        self.assertIsNone(h.unified_cgroup("12:cpu:/a\n"))
        unit = "/system.slice/fireproofd.service"
        self.assertTrue(h.cgroup_contains(unit, unit))
        self.assertTrue(h.cgroup_contains(unit, unit + "/sub"))
        self.assertFalse(h.cgroup_contains(unit, unit + "-x"))
        self.assertFalse(h.cgroup_contains(unit, "/system.slice"))
        self.assertFalse(h.cgroup_contains("", unit))
        self.assertFalse(h.cgroup_contains("/", unit))

    def test_no_systemd_does_nothing(self):
        fake = self.schedule(FakeRun(), self.INSIDE, systemd=False)
        self.assertEqual(fake.calls, [])

    def test_not_running_needs_no_restart(self):
        fake = self.schedule(FakeRun(active=False), self.OUTSIDE)
        self.assertEqual(fake.named("systemd-run"), [])
        self.assertFalse(any("try-restart" in c for c in fake.calls))

    def test_the_new_unit_is_loaded_before_anything_else(self):
        for active in (True, False):
            fake = self.schedule(FakeRun(active=active), self.OUTSIDE)
            self.assertEqual(fake.calls[0],
                             [self.h.SYSTEMCTL, "--system", "daemon-reload"])

    def test_inside_its_own_transaction_the_restart_is_deferred(self):
        fake = self.schedule(FakeRun(), self.INSIDE)
        runs = fake.named("systemd-run")
        self.assertEqual(len(runs), 1)
        argv = runs[0]
        self.assertIn("--no-block", argv)
        self.assertEqual(argv[-3:], [self.h.SELF, "wait", "4242"])
        self.assertFalse(any("try-restart" in c or "restart" in c or "stop" in c
                             for c in fake.named("systemctl")))

    def test_a_failed_deferral_is_reported_not_fatal(self):
        fake = self.schedule(FakeRun(systemd_run_rc=1), self.INSIDE)
        self.assertEqual(len(fake.named("systemd-run")), 1)
        self.assertFalse(any("try-restart" in c for c in fake.calls))

    def test_outside_it_an_idle_daemon_is_restarted_without_waiting(self):
        fake = self.schedule(FakeRun(), self.OUTSIDE)
        self.assertEqual(fake.named("systemd-run"), [])
        restarts = [c for c in fake.named("systemctl") if "try-restart" in c]
        self.assertEqual(restarts, [[self.h.SYSTEMCTL, "--no-block",
                                     "try-restart", "fireproofd.service"]])
        self.assert_never_stopped_synchronously(fake)

    def test_unknown_own_cgroup_is_treated_as_outside(self):
        fake = self.schedule(FakeRun(), "")
        self.assertTrue(any("try-restart" in c for c in fake.calls))
        self.assert_never_stopped_synchronously(fake)

    def test_policy_rc_d_101_is_honoured(self):
        with tempfile.TemporaryDirectory() as tmp:
            policy = os.path.join(tmp, "policy-rc.d")
            with open(policy, "w") as fh:
                fh.write("#!/bin/sh\nexit 101\n")
            os.chmod(policy, 0o755)
            self.h.POLICY_RC_D = policy
            fake = FakeRun(policy_rc=101)
            self.assertFalse(self.h.policy_allows("try-restart", fake, policy))
            self.h.schedule(run=fake, self_cgroup_text=self.OUTSIDE,
                            systemd_running=True)
            self.assertEqual(len(fake.named("policy-rc.d")), 2)
            self.assertFalse(any("try-restart" in c
                                 for c in fake.named("systemctl")))


class TestRestartHelperWait(unittest.TestCase):
    def setUp(self):
        self.h = load_helper()

    def probe(self, fake, procs):
        return self.h.probe("4242", run=fake, read=lambda _path: procs)

    def test_children_in_the_cgroup_mean_busy(self):
        self.assertEqual(self.probe(FakeRun(), "4242\n5150\n"), self.h.BUSY)

    def test_busy_property_means_busy(self):
        self.assertEqual(self.probe(FakeRun(busy="b true"), "4242\n"),
                         self.h.BUSY)

    def test_only_the_daemon_and_not_busy_is_idle(self):
        self.assertEqual(self.probe(FakeRun(), "4242\n"), self.h.IDLE)

    def test_nothing_readable_is_never_idle(self):
        self.assertEqual(self.probe(FakeRun(busy=None), None), self.h.BUSY)

    def test_replaced_or_stopped_daemon_is_gone(self):
        self.assertEqual(self.probe(FakeRun(main_pid="777"), "777\n"),
                         self.h.GONE)
        self.assertEqual(self.probe(FakeRun(active=False), None), self.h.GONE)

    def run_wait(self, states):
        clock = [0.0]
        restarted = []
        seq = iter(states)

        def look():
            return next(seq)

        def sleep(sec):
            clock[0] += sec

        rc = self.h.wait("4242", look=look, do_restart=lambda: restarted.append(clock[0]),
                         clock=lambda: clock[0], sleep=sleep)
        self.assertEqual(rc, 0)
        return restarted

    def test_restarts_only_after_idle_holds(self):
        poll = self.h.POLL_SECONDS
        need = self.h.QUIET_SECONDS // poll + 1
        busy = [self.h.BUSY] * 5
        restarted = self.run_wait(busy + [self.h.IDLE] * (need + 5))
        self.assertEqual(len(restarted), 1)
        self.assertGreaterEqual(restarted[0] - 5 * poll, self.h.QUIET_SECONDS)

    def test_busy_resets_the_quiet_period(self):
        h = self.h
        need = h.QUIET_SECONDS // h.POLL_SECONDS + 1
        states = [h.IDLE] * (need - 2) + [h.BUSY] + [h.IDLE] * (need + 2)
        restarted = self.run_wait(states)
        self.assertEqual(len(restarted), 1)
        self.assertGreaterEqual(restarted[0], (need - 1) * h.POLL_SECONDS
                                + h.QUIET_SECONDS)

    def test_gone_daemon_is_not_restarted(self):
        restarted = self.run_wait([self.h.BUSY, self.h.GONE])
        self.assertEqual(restarted, [])

    def test_gives_up_at_the_limit(self):
        h = self.h
        restarted = self.run_wait(
            [h.BUSY] * (h.WAIT_LIMIT_SECONDS // h.POLL_SECONDS + 2))
        self.assertEqual(restarted, [])


class TestDaemonLeavesAfterCommit(unittest.TestCase):
    def test_successful_commit_exits_after_finished(self):
        fp = load_fireproofd()
        src = read(DAEMON)
        finished_at = src.index("GLib.idle_add(self.Finished, json.dumps(result))")
        exit_at = src.index('GLib.idle_add(self._exit_after, "post-commit")')
        self.assertLess(finished_at, exit_at)
        self.assertIn("elif committed and commit_ok:", src)
        self.assertTrue(hasattr(fp.Fireproof, "_exit_after"))

    def test_exit_flushes_the_bus_before_quitting(self):
        fp = load_fireproofd()
        order = []
        obj = fp.Fireproof.__new__(fp.Fireproof)
        obj._bus = type("Bus", (), {"flush": lambda self: order.append("flush")})()
        obj._loop = type("Loop", (), {"quit": lambda self: order.append("quit")})()
        self.assertFalse(obj._exit_after("post-commit"))
        self.assertEqual(order, ["flush", "quit"])


if __name__ == "__main__":
    unittest.main()
