"""5.0.0 VM qualification findings: report provenance, receipt model, interrupted exports.

1. Every cited report ended with "Generated through the Codex cloud CLI with
   explicit network permission" -- including reports the on-device local model
   produced offline. The provenance sentence is now built from the provider's
   manifest display name, the mission's network permission and the model.
2. Receipts recorded `model: null` although `create --model` was given. The
   requested model, and the model the provider's own stream reported, are now
   both recorded.
3. After the worker was SIGKILLed mid-export the mission said "Retry or Undo",
   Undo was refused (no after-index.json: receipt() never ran) and the
   `.partial.mp4` stayed in the workspace. Recovery now removes the unpublished
   temporaries and records the workspace as the interruption left it, so Undo
   restores the checkpoint under the same predicate as a finished mission; a
   mission with no completed checkpoint is offered Retry only.

Provider outputs here are controlled fixtures in each provider's own native
stream format, parsed by the real adapters. No mocked result is presented as a
live model integration.
"""
import importlib.util
import json
import os
from pathlib import Path
import signal
import tempfile
import types
import unittest
from unittest.mock import patch

import mission_approvals

SOURCE = Path(__file__).resolve().parents[1] / "data/usr/lib/shadowfetch/missions/sf_missions.py"
MANIFESTS = Path(__file__).resolve().parents[1] / "data/usr/share/shadowfetch/providers"
spec = importlib.util.spec_from_file_location("sf_missions", SOURCE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
import sf_providers                                              # noqa: E402
import sf_provider_offline_media as offline_media                # noqa: E402

ANSWER = "The launch is Friday. [S1:L1]\nThe release contains three workflows. [S1:L2]"

# One successful turn per provider, in that provider's NATIVE stream format.
STREAMS = {
    "codex": [
        {"type": "item.completed", "item": {"type": "agent_message", "text": ANSWER}},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 20}},
    ],
    "claude": [
        {"type": "system", "subtype": "init", "session_id": "s-1",
         "model": "claude-sonnet-4-5-20250929"},
        {"type": "result", "subtype": "success", "result": ANSWER, "session_id": "s-1",
         "usage": {"input_tokens": 10, "output_tokens": 20}},
    ],
    "localmodel": [
        {"event": "session.started", "session": "qa", "model": "qwen3:4b", "resumed": False},
        {"event": "token", "delta": ANSWER},
        {"event": "generation.done", "stop": "stop", "model": "qwen3:4b",
         "usage": {"input_tokens": 10, "output_tokens": 20}},
    ],
}


def manifest(provider_id):
    return json.loads((MANIFESTS / f"{provider_id}.json").read_text())


class _Provider:
    """The real adapter, with only the host-bound parts replaced.

    Parsing, final-message extraction, usage and model reporting are the
    adapter's own. accepts() is replaced because the local model's accepts()
    probes a live socket for the requested model, and build_invocation()
    because the cloud CLIs are not installed on a test host.
    """

    def __init__(self, provider_id):
        self._real = m.registry().get(provider_id)

    def __getattr__(self, name):
        return getattr(self._real, name)

    def accepts(self, capability, config):
        return sf_providers.Acceptance.yes()

    def build_invocation(self, capability, request):
        return types.SimpleNamespace(sandbox=None, request=request)


class Harness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.ws = self.base / "Workspaces" / "example"
        self.ws.mkdir(parents=True)
        (self.ws / "facts.md").write_text("The launch is Friday.\nThe release contains three workflows.\n")
        self.env = patch.dict(os.environ, {
            "SHADOWFETCH_AGENT_WORKSPACES": str(self.ws.parent),
            "SHADOWFETCH_MISSIONS_STATE": str(self.base / "state")})
        self.env.start()
        self.store = m.Store()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()


class ProvenanceLineTests(unittest.TestCase):
    """The sentence itself, for every shipped provider manifest."""

    def provider(self, provider_id):
        data = manifest(provider_id)
        return types.SimpleNamespace(id=data["id"], display_name=data["display_name"],
                                     manifest=data)

    def test_every_provider_is_named_by_its_own_manifest(self):
        for provider_id in ("codex", "claude", "localmodel", "offline-media"):
            with self.subTest(provider=provider_id):
                data = manifest(provider_id)
                network = "none" if data["network_policy"] == "none" else "allow"
                line = m.provenance_line(self.provider(provider_id), {"network": network}, [])
                self.assertIn(data["display_name"], line)
                if provider_id != "codex":
                    self.assertNotIn("Codex", line)
                if network == "none":
                    self.assertIn("no network access permitted", line)
                    self.assertNotIn("network permission", line)
                else:
                    self.assertIn("explicit network permission", line)
                self.assertIn("not independently identified", line)

    def test_the_model_is_stated_when_known(self):
        provider = self.provider("localmodel")
        inference = m.model_record(provider.display_name, "qwen3:4b", "qwen3:4b")
        line = m.provenance_line(provider, {"network": "none", "model": "qwen3:4b"}, [inference])
        self.assertIn("model qwen3:4b", line)
        self.assertNotIn("not independently identified", line)

        provider = self.provider("claude")
        inference = m.model_record(provider.display_name, "sonnet", "claude-sonnet-4-5-20250929")
        line = m.provenance_line(provider, {"network": "allow", "model": "sonnet"}, [inference])
        self.assertIn("claude-sonnet-4-5-20250929 (requested sonnet)", line)

    def test_a_requested_model_the_provider_did_not_confirm_says_so(self):
        provider = self.provider("localmodel")
        inference = m.model_record(provider.display_name, "qwen3:4b", None)
        line = m.provenance_line(provider, {"network": "none", "model": "qwen3:4b"}, [inference])
        self.assertIn("qwen3:4b", line)
        self.assertIn("did not report", inference["model_selection"])


class ModelRecordTests(unittest.TestCase):
    def test_requested_and_reported_are_kept_apart(self):
        self.assertEqual(
            m.model_record("X", "sonnet", "claude-sonnet-4-5"),
            {"model": "claude-sonnet-4-5", "model_requested": "sonnet",
             "model_reported": "claude-sonnet-4-5",
             "model_selection": "Requested sonnet; X reported claude-sonnet-4-5"})
        self.assertEqual(m.model_record("X", "a:1b", None)["model"], "a:1b")
        self.assertEqual(m.model_record("X", "", "a:1b")["model"], "a:1b")
        self.assertIsNone(m.model_record("X", "", None)["model"])

    def test_reported_model_reads_normalized_events_only(self):
        provider = m.registry().get("localmodel")
        events = provider.parse_stream("".join(json.dumps(r) + "\n" for r in STREAMS["localmodel"]))
        self.assertEqual(provider.reported_model(events), "qwen3:4b")
        codex = m.registry().get("codex")
        events = codex.parse_stream("".join(json.dumps(r) + "\n" for r in STREAMS["codex"]))
        self.assertIsNone(codex.reported_model(events))


class ReportReceiptTests(Harness):
    """End to end through run_mission: report.md and the receipt agree with reality."""

    def run_report(self, provider_id, *, network, model=""):
        fake = _Provider(provider_id)
        log = self.base / f"{provider_id}.log"
        log.write_text("".join(json.dumps(r) + "\n" for r in STREAMS[provider_id]))

        def run_invocation(executor, invocation, secrets=None):
            return 0, "", log

        with patch.object(m, "provider_for", lambda capability, pid=None: fake), \
                patch.object(m.Executor, "run_invocation", run_invocation), \
                patch.object(m.Executor, "credentials_for", lambda self, p: {"X": "y"}):
            mission = self.store.create(kind="report", provider_id=provider_id,
                                        workspace_value="example", title="Launch report",
                                        prompt="Summarize the launch", inputs=["facts.md"],
                                        network=network, model=model)
            mission_approvals.approve(self.store, mission)
            result = m.run_mission(self.store, mission["id"])
        self.assertEqual(result["state"], "waiting-review", result["error"])
        receipt = json.loads(Path(result["receipt"]).read_text())
        report = next(Path(p) for p in result["artifacts"] if p.endswith("report.md")).read_text()
        return receipt, report

    def test_localmodel_offline_report_is_not_attributed_to_codex(self):
        receipt, report = self.run_report("localmodel", network="none", model="qwen3:4b")
        self.assertNotIn("Codex", report)
        self.assertIn("Generated by Local model (on-device) with no network access permitted, "
                      "using model qwen3:4b.", report)
        inference = receipt["inferences"][0]
        self.assertEqual(inference["model"], "qwen3:4b")
        self.assertEqual(inference["model_requested"], "qwen3:4b")
        self.assertEqual(inference["model_reported"], "qwen3:4b")
        self.assertEqual(inference["network_requested"], "none")
        self.assertEqual(inference["provider_display_name"], "Local model (on-device)")

    def test_claude_report_records_requested_and_reported_model(self):
        receipt, report = self.run_report("claude", network="allow", model="sonnet")
        self.assertIn("Generated by Claude Code (cloud) with explicit network permission", report)
        self.assertNotIn("Codex", report)
        inference = receipt["inferences"][0]
        self.assertEqual(inference["model_requested"], "sonnet")
        self.assertEqual(inference["model_reported"], "claude-sonnet-4-5-20250929")
        self.assertEqual(inference["model"], "claude-sonnet-4-5-20250929")
        self.assertIn("claude-sonnet-4-5-20250929 (requested sonnet)", report)

    def test_codex_report_names_codex_and_its_unidentified_default(self):
        receipt, report = self.run_report("codex", network="allow")
        self.assertIn("Generated by Codex CLI (cloud) with explicit network permission", report)
        self.assertIn("not independently identified", report)
        self.assertIsNone(receipt["inferences"][0]["model"])
        self.assertIsNone(receipt["inferences"][0]["model_requested"])


class InterruptedExportTests(Harness):
    """The worker is SIGKILLed in the middle of an export, as in the VM run."""

    def create_media(self):
        (self.ws / "clip.mov").write_bytes(b"not really a movie")
        mission = self.store.create(kind="media", provider_id="offline-media",
                                    workspace_value="example", title="Export",
                                    prompt="Export the selected video", inputs=["clip.mov"],
                                    network="none")
        mission_approvals.approve(self.store, mission)
        return mission

    def kill_mid_export(self, mid):
        """Run the real run_mission in a child and SIGKILL it inside the encode."""
        pid = os.fork()
        if pid == 0:                                                  # child
            try:
                def build(self_, capability, request):
                    return types.SimpleNamespace(sandbox=None, request=request)

                def run_invocation(executor, invocation, secrets=None):
                    request = invocation.request
                    if request["stage"] == "probe":
                        Path(request["report_path"]).write_text("{}")
                        return 0, "", None
                    if request["stage"] == "encode":
                        Path(request["target"]).write_bytes(b"\0" * 48)
                        os.kill(os.getpid(), signal.SIGKILL)
                    return 0, "", None

                with patch.object(offline_media.OfflineMediaProvider, "build_invocation", build), \
                        patch.object(offline_media.OfflineMediaProvider, "read_probe",
                                     staticmethod(lambda path: {})), \
                        patch.object(offline_media.OfflineMediaProvider, "classify",
                                     staticmethod(lambda probe: (True, False))), \
                        patch.object(m.Executor, "run_invocation", run_invocation):
                    m.run_mission(m.Store(), mid)
            finally:
                os._exit(3)
        _, status = os.waitpid(pid, 0)
        self.assertTrue(os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL,
                        "the fixture did not die inside the export")

    def restart_worker(self):
        # What `worker` does on start: reconcile under the whole-system lock.
        with self.store.lock():
            return self.store.reconcile(reason="worker started")

    def test_interrupted_export_is_undoable_and_leaves_no_partial(self):
        mission = self.create_media()
        mid = mission["id"]
        self.kill_mid_export(mid)
        output = self.ws / "mission-output" / mid
        self.assertEqual(self.store.get(mid)["state"], "running")
        self.assertTrue(list(output.glob("*.partial.*")), "fixture left no partial encode")

        self.restart_worker()
        row = self.store.get(mid)
        self.assertEqual(row["state"], "failed")
        self.assertIn("Retry or Undo", row["error"])
        self.assertTrue(row["checkpoint"])
        self.assertEqual(list(output.glob("*.partial.*")), [],
                         "the interrupted attempt's temporary stayed in the workspace")
        self.assertIn("partial-output-discarded",
                      [e["event"] for e in self.store.events(mid)])

        # The Undo the message offers is the one review() performs.
        undone = m.review(self.store, mid, "undo")
        self.assertEqual(undone["state"], "undone")
        self.assertFalse((self.ws / "mission-output").exists())
        self.assertEqual((self.ws / "clip.mov").read_bytes(), b"not really a movie")

    def test_undo_still_refuses_edits_made_after_the_interruption(self):
        mission = self.create_media()
        mid = mission["id"]
        self.kill_mid_export(mid)
        self.restart_worker()
        (self.ws / "notes.md").write_text("written by a person after the crash\n")
        with self.assertRaisesRegex(m.MissionError, "Workspace changed after this mission"):
            m.review(self.store, mid, "undo")
        self.assertTrue((self.ws / "notes.md").exists())

    def test_retry_starts_without_the_old_partial(self):
        mission = self.create_media()
        mid = mission["id"]
        self.kill_mid_export(mid)
        output = self.ws / "mission-output" / mid
        stale = next(output.glob("*.partial.*"))
        # Stands in for a partial that survived to the retry (for example one
        # restored from backup): the retry itself must also discard it.
        self.restart_worker()
        stale.write_bytes(b"\0" * 7)
        self.store.retry(mid)
        seen = {}

        def build(self_, capability, request):
            return types.SimpleNamespace(sandbox=None, request=request)

        def run_invocation(executor, invocation, secrets=None):
            request = invocation.request
            if request["stage"] == "probe":
                Path(request["report_path"]).write_text("{}")
            elif request["stage"] == "encode":
                seen["stale_at_encode"] = Path(request["target"]).exists()
                Path(request["target"]).write_bytes(b"\1" * 64)
            return 0, "", None

        with patch.object(offline_media.OfflineMediaProvider, "build_invocation", build), \
                patch.object(offline_media.OfflineMediaProvider, "read_probe",
                             staticmethod(lambda path: {})), \
                patch.object(offline_media.OfflineMediaProvider, "classify",
                             staticmethod(lambda probe: (True, False))), \
                patch.object(m.Executor, "run_invocation", run_invocation):
            result = m.run_mission(self.store, mid)
        self.assertEqual(result["state"], "waiting-review", result["error"])
        self.assertFalse(seen["stale_at_encode"], "retry encoded on top of the old partial")
        self.assertEqual(list(output.glob("*.partial.*")), [])

    def test_no_checkpoint_means_retry_only(self):
        mission = self.create_media()
        mid = mission["id"]
        # Interrupted before the checkpoint task completed: running, no checkpoint.
        self.store.transition(mid, "running", expect="queued", attempt=1, detail="test fixture")
        self.restart_worker()
        row = self.store.get(mid)
        self.assertEqual(row["state"], "failed")
        self.assertNotIn("Undo", row["error"])
        self.assertIn("Retry", row["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
