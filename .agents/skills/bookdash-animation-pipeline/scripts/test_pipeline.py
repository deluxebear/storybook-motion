"""Offline contract and failure-path tests; never allocate a GPU or contact ComfyUI."""
import argparse
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import comfy_batch
from pipeline import NeedsInput, Pipeline, project_lock
from production_plan import apply_tts_video_durations, compile_plan, read, validate
from projectctl import atomic_json

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates/minimax_h3_i2v"
TEMPLATE_OBJECT_INFO = {
    node["class_type"]: {}
    for node in json.loads((TEMPLATE_DIR / "workflow_api.json").read_text()).values()
}


def fixture(parent):
    book = Path(parent) / "example"
    (book / "source/pages").mkdir(parents=True)
    (book / "source/pages/1.png").write_bytes(b"image fixture")
    (book / "comfyui").mkdir()
    shutil.copyfile(TEMPLATE_DIR / "workflow_api.json", book / "comfyui/workflow_api.json")
    shutil.copyfile(TEMPLATE_DIR / "bindings.json", book / "comfyui/bindings.json")
    plan = {"schema_version": 1, "book_slug": "example", "book_url": "https://bookdash.org/books/example/",
            "workflow": "comfyui/workflow_api.json", "bindings": "comfyui/bindings.json",
            "roles": {"narrator": {"language": "English", "reference_text": "Hello there.", "instruct": "Warm fictional storyteller."}},
            "shots": [{"scene_id": "S001", "shot_id": "S001_SH001", "video": {"prompt": "Gentle motion.", "reference_image": "source/pages/1.png", "seed": 42, "duration_seconds": 2},
                       "lines": [{"audio_id": f"S001_SH001_A00{i}", "role_id": "narrator", "text": text, "lang": "English"} for i, text in enumerate(["First line.", "Second line."], 1)]}]}
    atomic_json(book / "planning/production_plan.json", plan)
    return book, plan


def arguments(**updates):
    return argparse.Namespace(**{"timeout": 30, "shot_timeout": 2, "assemble": False, "assembly_session": None,
                                 "comfy_url": "http://fake", "until": "video", "interactive": False, **updates})


class FakePipeline(Pipeline):
    def require_terminal(self):
        pass

    def __init__(self, book, args, fail=None):
        super().__init__(book, args)
        self.calls = []
        self.fail = fail

    def colab(self, *args, **kwargs):
        self.calls.append(("colab", args[0]))
        if args[0] == "sessions":
            (self.log_dir / "verify-stop.log").write_text("COMMAND colab sessions\nNo sessions")

    def command(self, argv, name, *args, **kwargs):
        self.calls.append(("command", name))
        if name == self.fail:
            raise RuntimeError("injected failure")

    def probe(self, *args, **kwargs):
        self.calls.append(("probe", "drive"))
        if self.fail == "drive":
            raise RuntimeError("consent needed")

    def bundle(self, *args):
        self.calls.append(("bundle", "inputs"))

    def remote_stage(self, session, stage, argv):
        self.calls.append(("remote", stage))
        if stage == self.fail:
            raise RuntimeError("injected failure")
        if stage == "tts":
            path = self.book / "planning/tts_manifest.json"
            data = read(path)
            for row in data["jobs"]:
                row.update(status="succeeded", seconds=1.0)
            atomic_json(path, data)

    def sync(self, *args):
        self.calls.append(("sync", "metadata"))


class LocalRemote(Pipeline):
    """Execute the real bundle installer locally, with file copies replacing CLI I/O."""
    def colab(self, *args, **kwargs):
        if args[0] not in ("upload", "download"):
            raise AssertionError(args)
        shutil.copyfile(args[-2], args[-1])

    def execute(self, session, code, name, timeout=None):
        exec(compile(code, name, "exec"), {})


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.book, self.plan = fixture(self.temp.name)

    def test_compile_keeps_line_order_and_runtime_ids(self):
        compile_plan(self.book)
        path = self.book / "planning/video_manifest.json"
        payload = read(path)
        payload["jobs"][0].update(status="running", prompt_id="already-submitted", attempts=1)
        atomic_json(path, payload)
        compile_plan(self.book)
        self.assertEqual(read(path)["jobs"][0]["prompt_id"], "already-submitted")
        self.assertEqual(read(path)["jobs"][0]["audio_ids"], ["S001_SH001_A001", "S001_SH001_A002"])

    def test_video_duration_uses_measured_tts_with_padding(self):
        compile_plan(self.book)
        path = self.book / "planning/tts_manifest.json"
        data = read(path)
        for row, seconds in zip(data["jobs"], (2.2, 3.1)):
            row.update(status="succeeded", seconds=seconds)
        atomic_json(path, data)
        result = apply_tts_video_durations(self.book)
        video = read(self.book / "planning/video_manifest.json")["jobs"][0]
        self.assertEqual(video["inputs"]["duration_seconds"], 7.0)
        self.assertEqual(result["changed"], ["S001_SH001"])

    def test_video_duration_does_not_rewrite_submitted_job(self):
        compile_plan(self.book)
        tts_path = self.book / "planning/tts_manifest.json"
        data = read(tts_path)
        for row in data["jobs"]:
            row.update(status="succeeded", seconds=10)
        atomic_json(tts_path, data)
        video_path = self.book / "planning/video_manifest.json"
        data = read(video_path)
        data["jobs"][0].update(status="running", prompt_id="already-submitted")
        atomic_json(video_path, data)
        result = apply_tts_video_durations(self.book)
        self.assertEqual(read(video_path)["jobs"][0]["inputs"]["duration_seconds"], 2)
        self.assertEqual(result["skipped_submitted"], ["S001_SH001"])

    def test_changed_asset_rejected_before_overwrite(self):
        compile_plan(self.book)
        before = (self.book / "planning/video_manifest.json").read_bytes()
        (self.book / "source/pages/1.png").write_bytes(b"changed image")
        with self.assertRaises(ValueError):
            compile_plan(self.book)
        self.assertEqual((self.book / "planning/video_manifest.json").read_bytes(), before)

    def test_bad_references_and_duplicates_fail(self):
        for change in (lambda p: p["shots"][0]["lines"][0].update(role_id="missing"),
                       lambda p: p["shots"].append(copy.deepcopy(p["shots"][0])),
                       lambda p: p["shots"][0]["video"].update(reference_image="../../outside")):
            plan = copy.deepcopy(self.plan); change(plan)
            with self.assertRaises(ValueError):
                validate(self.book, plan)

    def test_duration_binding_is_required(self):
        bindings = read(self.book / "comfyui/bindings.json")
        del bindings["duration_seconds"]
        atomic_json(self.book / "comfyui/bindings.json", bindings)
        with self.assertRaisesRegex(ValueError, "missing workflow binding: duration_seconds"):
            validate(self.book, self.plan)

    def test_workflow_schema_drift_is_rejected(self):
        workflow = read(self.book / "comfyui/workflow_api.json")
        workflow["114"]["inputs"]["unexpected"] = 1
        atomic_json(self.book / "comfyui/workflow_api.json", workflow)
        with self.assertRaisesRegex(ValueError, "workflow schema differs"):
            validate(self.book, self.plan)

    def test_workflow_allows_bound_value_changes(self):
        workflow = read(self.book / "comfyui/workflow_api.json")
        workflow["105:111"]["inputs"]["value"] = 13
        atomic_json(self.book / "comfyui/workflow_api.json", workflow)
        self.assertIs(validate(self.book, self.plan), self.plan)

    def test_single_book_lock(self):
        with project_lock(self.book):
            with self.assertRaises(ValueError):
                with project_lock(self.book):
                    pass

    def test_real_bundle_installer_preserves_remote_progress(self):
        plan, fingerprint = compile_plan(self.book)
        runner = LocalRemote(self.book, arguments())
        runner.remote_book = Path(self.temp.name) / "drive/books/example"
        runner.bundle("fake", plan, fingerprint)
        path = runner.remote_book / "planning/tts_manifest.json"
        data = read(path); data["jobs"][0].update(status="succeeded"); atomic_json(path, data)
        runner.bundle("fake", plan, fingerprint)
        self.assertEqual(read(path)["jobs"][0]["status"], "succeeded")
        data["jobs"][0]["text"] = "conflicting remote text"; atomic_json(path, data)
        with self.assertRaisesRegex(AssertionError, "Drive manifest inputs differ"):
            runner.bundle("fake", plan, fingerprint)

    @patch("comfy_batch.request_json", side_effect=lambda url: TEMPLATE_OBJECT_INFO)
    def test_pipeline_orders_cleanup_before_video(self, request):
        runner = FakePipeline(self.book, arguments())
        runner.run()
        self.assertLess(runner.calls.index(("remote", "voice")), runner.calls.index(("remote", "tts")))
        self.assertLess(runner.calls.index(("colab", "stop")), runner.calls.index(("command", "video")))
        self.assertEqual(runner.state["status"], "succeeded")
        self.assertNotIn(("remote", "final"), runner.calls)

    def test_tts_failure_stops_only_owned_gpu(self):
        runner = FakePipeline(self.book, arguments(), fail="tts")
        with self.assertRaises(RuntimeError):
            runner.run()
        self.assertIn(("colab", "stop"), runner.calls)
        self.assertNotIn(("command", "video"), runner.calls)
        self.assertIsNone(runner.state["l4_session"])

    def test_missing_url_does_not_undo_completed_tts(self):
        runner = FakePipeline(self.book, arguments(comfy_url=None))
        with self.assertRaises(NeedsInput):
            runner.run()
        self.assertEqual(runner.state["stages"]["tts"]["status"], "succeeded")
        resumed = FakePipeline(self.book, arguments(until="tts"))
        resumed.run()
        self.assertNotIn(("colab", "new"), resumed.calls)

    def test_oauth_needs_input_cleans_up(self):
        runner = FakePipeline(self.book, arguments(), fail="drive")
        with self.assertRaises(RuntimeError):
            runner.run()
        self.assertEqual(runner.state["status"], "failed")
        self.assertIn(("colab", "stop"), runner.calls)

    def test_authorized_session_is_adopted_without_remount_or_reallocation(self):
        runner = FakePipeline(self.book, arguments(until="tts"))
        runner.prepare_l4()
        authorized = runner.state["l4_session"]
        self.assertEqual(runner.state["l4_handoff"], "ready")
        resumed = FakePipeline(self.book, arguments(until="tts"))
        resumed.run()
        self.assertNotIn(("colab", "new"), resumed.calls)
        self.assertNotIn(("colab", "drivemount"), resumed.calls)
        self.assertLess(resumed.calls.index(("remote", "tts")), resumed.calls.index(("colab", "stop")))
        self.assertEqual(resumed.state["l4_cleanup"]["session"], authorized)

    def test_no_terminal_rejected_before_gpu_allocation(self):
        runner = Pipeline(self.book, arguments())
        with patch("sys.stdin.isatty", return_value=False), patch.object(runner, "colab") as command:
            with self.assertRaises(NeedsInput):
                runner.prepare_l4()
        command.assert_not_called()
        self.assertEqual(runner.state["status"], "needs_input")

    def test_mount_inherits_live_terminal_input_and_output(self):
        runner = FakePipeline(self.book, arguments())
        with patch.object(runner, "colab") as command:
            runner.prepare_l4()
        mount = next(c for c in command.call_args_list if c.args[0] == "drivemount")
        self.assertTrue(mount.kwargs["interactive"])
        self.assertEqual(mount.args, ("drivemount", "-s", runner.state["l4_session"]))
        # Exercise the actual command boundary, not just the high-level mock.
        real = Pipeline(self.book, arguments())
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            real.command(["colab", "drivemount", "-s", "fake"], "mount", interactive=True)
        self.assertIsNone(run.call_args.kwargs["stdin"])
        self.assertIsNone(run.call_args.kwargs["stdout"])

    @patch("comfy_batch.request_json", return_value=TEMPLATE_OBJECT_INFO)
    def test_final_is_explicit_and_uses_existing_session(self, request):
        runner = FakePipeline(self.book, arguments(assemble=True, assembly_session="user-a100"))
        runner.run()
        self.assertEqual(runner.calls.count(("colab", "new")), 1)
        self.assertEqual(runner.calls.count(("colab", "stop")), 1)
        self.assertIn(("remote", "final"), runner.calls)

    def test_history_timeout_retains_prompt_without_resubmit(self):
        compile_plan(self.book)
        path = self.book / "planning/video_manifest.json"
        data = read(path); data["jobs"][0].update(status="running", prompt_id="pid", attempts=1); atomic_json(path, data)
        args = argparse.Namespace(base_url="http://fake", book_dir=str(self.book), drive_output_prefix=None,
                                  drive_root="/content/drive/MyDrive/vidio", shot_id=None, timeout=0, poll=.01)
        with patch.object(comfy_batch, "request_json", return_value={}) as request:
            with self.assertRaises(TimeoutError):
                comfy_batch.run_batch(args)
        self.assertEqual(read(path)["jobs"][0]["status"], "running")
        self.assertEqual(read(path)["jobs"][0]["prompt_id"], "pid")
        self.assertFalse(any(call.args[0].endswith("/prompt") for call in request.call_args_list))

    def test_unknown_submission_blocks_retry(self):
        compile_plan(self.book)
        path = self.book / "planning/video_manifest.json"
        data = read(path); data["jobs"][0].update(status="submitting", client_id="unknown"); atomic_json(path, data)
        args = argparse.Namespace(base_url="http://fake", book_dir=str(self.book), drive_output_prefix=None, shot_id=None)
        with patch.object(comfy_batch, "request_json", return_value={}):
            with self.assertRaisesRegex(RuntimeError, "ambiguous submission"):
                comfy_batch.run_batch(args)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
    def test_assembly_contains_both_lines_and_uses_actual_video_path(self):
        compile_plan(self.book)
        def ffmpeg(*args):
            subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True)
        video = self.book / "video/shots/actual_00001.mp4"; video.parent.mkdir(parents=True)
        ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=160x90:r=24", "-t", "0.5", "-c:v", "libx264", str(video))
        data = read(self.book / "planning/tts_manifest.json")
        for row in data["jobs"]:
            path = self.book / row["output"]; path.parent.mkdir(parents=True, exist_ok=True)
            ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=24000", "-t", "0.6", str(path))
            row["status"] = "succeeded"
        atomic_json(self.book / "planning/tts_manifest.json", data)
        data = read(self.book / "planning/video_manifest.json")
        data["jobs"][0].update(status="succeeded", remote_output=str(video))
        atomic_json(self.book / "planning/video_manifest.json", data)
        subprocess.run([sys.executable, str(Path(__file__).with_name("assemble_final.py")), "--book-dir", str(self.book)], check=True, capture_output=True)
        report = read(self.book / "video/final/assembly_manifest.json")
        self.assertEqual(len(report["segments"][0]["audio_ids"]), 2)
        self.assertAlmostEqual(report["segments"][0]["audio_seconds"], 1.2, places=1)
        self.assertGreaterEqual(report["seconds"], 1.19)
        output = self.book / "video/final/narrated_final.mp4"
        original_mtime = output.stat().st_mtime_ns
        subprocess.run([sys.executable, str(Path(__file__).with_name("assemble_final.py")), "--book-dir", str(self.book)], check=True, capture_output=True)
        self.assertEqual(output.stat().st_mtime_ns, original_mtime)


if __name__ == "__main__":
    unittest.main()
