"""Offline contract and failure-path tests; never allocate a GPU or contact ComfyUI."""
import argparse
import contextlib
import copy
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import comfy_batch
import pipeline_watcher
from pipeline import NeedsInput, Pipeline, project_lock
from production_plan import apply_tts_video_durations, compile_plan, read, validate
from projectctl import atomic_json
from video_prompt import refine_video_prompts

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates/minimax_h3_i2v"
LTX_TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates/ltx2_5_i2v"
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
                                 "comfy_url": "http://fake", "until": "video",
                                 "comfy_restarted": False, "reference_mode": "upload",
                                 "drive_input_prefix": "vidio", "voice_session": "voice",
                                 "tts_session": "tts", **updates})


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
        worker_patch = patch("pipeline.worker_lock", side_effect=lambda session: contextlib.nullcontext())
        worker_patch.start()
        self.addCleanup(worker_patch.stop)
        self.book, self.plan = fixture(self.temp.name)

    def add_second_shot(self):
        second = copy.deepcopy(self.plan["shots"][0])
        second.update(scene_id="S002", shot_id="S002_SH001")
        for index, line in enumerate(second["lines"], 1):
            line["audio_id"] = f"S002_SH001_A00{index}"
        self.plan["shots"].append(second)
        atomic_json(self.book / "planning/production_plan.json", self.plan)
        compile_plan(self.book)

    def batch_context(self, base="http://fake", restart_confirmed=False):
        return comfy_batch.BatchContext(
            base=base,
            book=self.book,
            drive_prefix="books/example/video/shots",
            drive_root=Path("/content/drive/MyDrive/vidio"),
            manifest_path=self.book / "planning/video_manifest.json",
            restart_confirmed=restart_confirmed,
        )

    def test_compile_keeps_line_order_and_runtime_ids(self):
        compile_plan(self.book)
        path = self.book / "planning/video_manifest.json"
        payload = read(path)
        payload["jobs"][0].update(status="running", prompt_id="already-submitted", attempts=1)
        atomic_json(path, payload)
        compile_plan(self.book)
        self.assertEqual(read(path)["jobs"][0]["prompt_id"], "already-submitted")
        self.assertEqual(read(path)["jobs"][0]["audio_ids"], ["S001_SH001_A001", "S001_SH001_A002"])

    def test_compile_freezes_stable_tts_seeds(self):
        compile_plan(self.book)
        first = [row["seed"] for row in read(self.book / "planning/tts_manifest.json")["jobs"]]
        compile_plan(self.book)
        second = [row["seed"] for row in read(self.book / "planning/tts_manifest.json")["jobs"]]
        self.assertEqual(first, second)
        self.assertTrue(all(isinstance(seed, int) and 0 <= seed < 2 ** 32 for seed in first))

    def test_watcher_completion_matches_requested_endpoint(self):
        compile_plan(self.book)
        state_path = self.book / ".pipeline/state.json"
        state_path.parent.mkdir(exist_ok=True)
        atomic_json(state_path, {"status": "succeeded", "current_stage": "tts"})
        self.assertFalse(pipeline_watcher._is_complete(self.book, arguments(until="video")))
        self.assertTrue(pipeline_watcher._is_complete(self.book, arguments(until="tts")))
        atomic_json(state_path, {"status": "succeeded", "current_stage": "video"})
        self.assertFalse(pipeline_watcher._is_complete(
            self.book, arguments(assemble=True, assembly_session="ComfyUI")))
        atomic_json(state_path, {"status": "succeeded", "current_stage": "final"})
        self.assertTrue(pipeline_watcher._is_complete(
            self.book, arguments(assemble=True, assembly_session="ComfyUI")))

    def test_watcher_routes_books_through_dependency_stages(self):
        compile_plan(self.book)
        state_path = self.book / ".pipeline/state.json"
        state_path.parent.mkdir(exist_ok=True)
        self.assertEqual(pipeline_watcher._next_stage(self.book, arguments()), "voice")
        atomic_json(state_path, {"status": "ready_for_tts", "current_stage": "voice_design",
                                 "stages": {"voice_design": {"status": "succeeded"}}})
        self.assertEqual(pipeline_watcher._next_stage(self.book, arguments()), "tts")
        atomic_json(state_path, {"status": "ready_for_video", "current_stage": "tts",
                                 "stages": {"voice_design": {"status": "succeeded"},
                                            "tts": {"status": "succeeded"}}})
        self.assertEqual(pipeline_watcher._next_stage(self.book, arguments()), "video")
        self.assertIsNone(pipeline_watcher._next_stage(self.book, arguments(until="tts")))
        atomic_json(state_path, {"status": "ready_for_final", "current_stage": "video",
                                 "stages": {"voice_design": {"status": "succeeded"},
                                            "tts": {"status": "succeeded"},
                                            "video": {"status": "succeeded"}}})
        self.assertEqual(pipeline_watcher._next_stage(
            self.book, arguments(assemble=True, assembly_session="tts")), "final")

    def test_watcher_notification_is_deduplicated(self):
        runtime = Path(self.temp.name) / ".pipeline"
        runtime.mkdir()
        state = {}
        with patch.object(pipeline_watcher, "shutil_which", return_value=None):
            pipeline_watcher._notify(runtime, state, "comfy:missing", "provide ComfyUI")
            pipeline_watcher._notify(runtime, state, "comfy:missing", "provide ComfyUI")
        self.assertEqual(len((runtime / "notifications.jsonl").read_text().splitlines()), 1)

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

    def test_video_duration_does_not_rewrite_queued_job(self):
        compile_plan(self.book)
        tts_path = self.book / "planning/tts_manifest.json"
        data = read(tts_path)
        for row in data["jobs"]:
            row.update(status="succeeded", seconds=10)
        atomic_json(tts_path, data)
        video_path = self.book / "planning/video_manifest.json"
        data = read(video_path)
        data["jobs"][0].update(status="queued", prompt_id="already-submitted")
        atomic_json(video_path, data)
        result = apply_tts_video_durations(self.book)
        self.assertEqual(read(video_path)["jobs"][0]["inputs"]["duration_seconds"], 2)
        self.assertEqual(result["skipped_submitted"], ["S001_SH001"])

    def test_ltx_prompt_is_action_first_and_prevents_camera_only_motion(self):
        shutil.copyfile(LTX_TEMPLATE_DIR / "workflow_api.json", self.book / "comfyui/workflow_api.json")
        shutil.copyfile(LTX_TEMPLATE_DIR / "bindings.json", self.book / "comfyui/bindings.json")
        action = ("The child reaches down, grips the oar with both hands, and pulls it through the water; "
                  "their shoulders turn while the boat advances and ripples spread behind it.")
        self.plan["shots"][0]["video"].update(prompt=action, duration_seconds=5)
        atomic_json(self.book / "planning/production_plan.json", self.plan)
        compile_plan(self.book)
        tts_path = self.book / "planning/tts_manifest.json"
        tts = read(tts_path)
        for row, seconds in zip(tts["jobs"], (1.5, 1.0)):
            row.update(status="succeeded", seconds=seconds)
        atomic_json(tts_path, tts)

        self.assertEqual(refine_video_prompts(self.book)["changed"], ["S001_SH001"])
        refinement = read(self.book / "planning/video_manifest.json")["jobs"][0]["prompt_refinement"]
        prompt = refinement["prompt"]
        self.assertEqual(refinement["version"], 4)
        self.assertTrue(prompt.startswith(action))
        self.assertLess(prompt.index("camera motion cannot be the sole"), prompt.index("Keep the source"))
        self.assertIn("alone defines every subject's identity and count", prompt)
        self.assertIn("existing or absent facial features", prompt)
        self.assertIn("Never turn abstract or collage", prompt)
        self.assertIn("Start immediately in the supplied composition", prompt)
        self.assertIn("No intro, outro, transition", prompt)
        self.assertIn("do not freeze", prompt)
        self.assertNotIn("settle into a calm pose", prompt)
        self.assertNotIn("First line.", prompt)
        self.assertEqual(refinement["timeline"][0]["text"], "First line.")
        self.assertLess(len(prompt.split()), 200)
        self.assertIn("added anatomy or facial features absent from the reference image",
                      refinement["negative_prompt"])
        self.assertIn("slideshow, presentation animation", refinement["negative_prompt"])
        self.assertIn("subject flying into frame", refinement["negative_prompt"])
        job = read(self.book / "planning/video_manifest.json")["jobs"][0]
        with patch.object(comfy_batch, "upload", return_value="uploaded.png"):
            workflow = comfy_batch.build_workflow(self.batch_context(), job)
        bindings = read(self.book / "comfyui/bindings.json")
        negative_binding = bindings["negative_prompt"]
        self.assertEqual(workflow[str(negative_binding["node"])]["inputs"][negative_binding["input"]],
                         refinement["negative_prompt"])

    def test_ltx_prompt_enhancement_is_bypassed(self):
        workflow = read(LTX_TEMPLATE_DIR / "workflow_api.json")
        switch = workflow["398:382"]["inputs"]
        self.assertFalse(workflow["398:383"]["inputs"]["value"])
        self.assertEqual(workflow[switch["on_false"][0]]["class_type"], "PrimitiveStringMultiline")
        self.assertEqual(workflow[switch["on_true"][0]]["class_type"], "TextGenerateLTX2Prompt")
        self.assertEqual(workflow["398:364"]["inputs"]["text"], ["398:382", 0])

    def test_ltx_prompt_refinement_does_not_rewrite_submitted_job(self):
        shutil.copyfile(LTX_TEMPLATE_DIR / "workflow_api.json", self.book / "comfyui/workflow_api.json")
        shutil.copyfile(LTX_TEMPLATE_DIR / "bindings.json", self.book / "comfyui/bindings.json")
        compile_plan(self.book)
        tts_path = self.book / "planning/tts_manifest.json"
        tts = read(tts_path)
        for row in tts["jobs"]:
            row.update(status="succeeded", seconds=0.5)
        atomic_json(tts_path, tts)
        video_path = self.book / "planning/video_manifest.json"
        video = read(video_path)
        video["jobs"][0].update(status="queued", prompt_id="already-submitted",
                                prompt_refinement={"version": 1, "prompt": "frozen prompt"})
        atomic_json(video_path, video)

        self.assertEqual(refine_video_prompts(self.book)["changed"], [])
        current = read(video_path)["jobs"][0]["prompt_refinement"]
        self.assertEqual(current, {"version": 1, "prompt": "frozen prompt"})

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
    def test_pipeline_routes_voice_and_tts_without_stopping_workers(self, request):
        runner = FakePipeline(self.book, arguments())
        runner.run()
        self.assertLess(runner.calls.index(("remote", "voice")), runner.calls.index(("remote", "tts")))
        self.assertNotIn(("colab", "new"), runner.calls)
        self.assertNotIn(("colab", "drivemount"), runner.calls)
        self.assertNotIn(("colab", "stop"), runner.calls)
        self.assertEqual(runner.state["status"], "succeeded")
        self.assertEqual(runner.state["workers"], {"voice": "voice", "tts": "tts"})
        self.assertNotIn(("remote", "final"), runner.calls)

    def test_completed_voice_design_goes_directly_to_tts(self):
        compile_plan(self.book)
        state_path = self.book / ".pipeline/state.json"
        state_path.parent.mkdir(exist_ok=True)
        atomic_json(state_path, {"schema_version": 1, "status": "ready_for_tts",
                                 "current_stage": "voice_design",
                                 "stages": {"voice_design": {"status": "succeeded"}}})
        runner = FakePipeline(self.book, arguments(until="tts"))
        runner.run()
        self.assertNotIn(("remote", "voice"), runner.calls)
        self.assertIn(("remote", "tts"), runner.calls)
        self.assertEqual(runner.state["status"], "succeeded")

    def test_tts_failure_leaves_user_owned_workers_running(self):
        runner = FakePipeline(self.book, arguments(), fail="tts")
        with self.assertRaises(RuntimeError):
            runner.run()
        self.assertNotIn(("colab", "stop"), runner.calls)
        self.assertNotIn(("command", "video"), runner.calls)

    def test_missing_url_does_not_undo_completed_tts(self):
        runner = FakePipeline(self.book, arguments(comfy_url=None))
        with self.assertRaises(NeedsInput):
            runner.run()
        self.assertEqual(runner.state["stages"]["tts"]["status"], "succeeded")
        resumed = FakePipeline(self.book, arguments(until="tts"))
        resumed.run()
        self.assertNotIn(("colab", "new"), resumed.calls)

    @patch("comfy_batch.request_json", return_value=TEMPLATE_OBJECT_INFO)
    def test_final_is_explicit_and_uses_existing_session(self, request):
        runner = FakePipeline(self.book, arguments(assemble=True, assembly_session="user-a100"))
        fake_command = runner.command
        def command(argv, name, *args, **kwargs):
            fake_command(argv, name, *args, **kwargs)
            if name == "video":
                path = self.book / "planning/video_manifest.json"
                data = read(path)
                for job in data["jobs"]:
                    job["status"] = "succeeded"
                atomic_json(path, data)
        runner.command = command
        runner.run()
        self.assertEqual(runner.calls.count(("colab", "new")), 0)
        self.assertEqual(runner.calls.count(("colab", "stop")), 0)
        self.assertIn(("remote", "final"), runner.calls)

    @patch("comfy_batch.request_json", return_value=TEMPLATE_OBJECT_INFO)
    def test_video_stage_releases_comfy_before_final_stage(self, request):
        runner = FakePipeline(self.book, arguments(assemble=True, assembly_session="ComfyUI"))
        runner.state["stages"].update(voice_design={"status": "succeeded"},
                                      tts={"status": "succeeded"})
        compile_plan(self.book)
        tts_path = self.book / "planning/tts_manifest.json"
        tts = read(tts_path)
        for row in tts["jobs"]:
            row.update(status="succeeded", seconds=0.5)
        atomic_json(tts_path, tts)
        runner.run_stage("video")
        self.assertIn(("command", "video"), runner.calls)
        self.assertNotIn(("remote", "final"), runner.calls)
        self.assertEqual(runner.state["status"], "ready_for_final")

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

    def test_history_images_mp4_recovers_failed_job_without_resubmit(self):
        compile_plan(self.book)
        path = self.book / "planning/video_manifest.json"
        jobs = read(path)["jobs"]
        jobs[0].update(status="failed", prompt_id="pid", attempts=1, post_attempts=1,
                       error="no output files")
        atomic_json(path, {"jobs": jobs})
        history = {"pid": {"status": {"status_str": "success"}, "outputs": {"75": {
            "images": [{"filename": "S001_SH001_00001_.mp4",
                        "subfolder": "books/example/video/shots", "type": "output"}],
            "animated": [True],
        }}}}
        with patch.object(comfy_batch, "request_json", side_effect=({"queue_pending": [], "queue_running": []}, history)), \
                patch.object(comfy_batch, "upload") as upload:
            comfy_batch.reconcile_jobs(self.batch_context(), jobs, jobs)
            comfy_batch.submit_batch(self.batch_context(), jobs, jobs)
        current = read(path)["jobs"][0]
        self.assertEqual(current["status"], "succeeded")
        self.assertEqual(current["remote_filename"], "S001_SH001_00001_.mp4")
        self.assertEqual(current["post_attempts"], 1)
        upload.assert_not_called()

    def test_unknown_submission_blocks_retry(self):
        compile_plan(self.book)
        path = self.book / "planning/video_manifest.json"
        data = read(path); data["jobs"][0].update(status="submitting", client_id="unknown"); atomic_json(path, data)
        args = argparse.Namespace(base_url="http://fake", book_dir=str(self.book), drive_output_prefix=None,
                                  drive_root="/content/drive/MyDrive/vidio", lock_file=None, shot_id=None,
                                  timeout=0, poll=.01, missing_grace=0)
        with patch.object(comfy_batch, "request_json", return_value={}):
            with self.assertRaises(SystemExit):
                comfy_batch.run_batch(args)
        current = read(path)["jobs"][0]
        self.assertEqual(current["status"], "submitting")
        self.assertIn("ambiguous submission requires reconciliation", current["error"])

    def test_submits_whole_batch_before_monitoring(self):
        self.add_second_shot()
        calls = []
        prompt_ids = iter(("pid-1", "pid-2"))

        def request(url, method="GET", obj=None):
            calls.append((url, method))
            if url.endswith("/prompt"):
                return {"prompt_id": next(prompt_ids)}
            if url.endswith("/queue") or url.endswith("/system_stats"):
                return {}
            prompt_id = url.rsplit("/", 1)[-1]
            shot_id = "S001_SH001" if prompt_id == "pid-1" else "S002_SH001"
            return {prompt_id: {"status": {"status_str": "success"}, "outputs": {"1": {"videos": [{
                "filename": f"{shot_id}_00001.mp4",
                "subfolder": f"books/example/video/shots/{shot_id}",
                "type": "output",
            }]}}}}

        args = argparse.Namespace(base_url="http://fake", book_dir=str(self.book), drive_output_prefix=None,
                                  drive_root="/content/drive/MyDrive/vidio", lock_file=str(self.book / "endpoint.lock"),
                                  shot_id=None, timeout=2, poll=.01, missing_grace=.01)
        with patch.object(comfy_batch, "request_json", side_effect=request), \
                patch.object(comfy_batch, "upload", return_value="uploaded.png"):
            with self.assertRaises(SystemExit) as stopped:
                comfy_batch.run_batch(args)
        self.assertFalse(stopped.exception.code)
        posts = [index for index, call in enumerate(calls) if call[0].endswith("/prompt")]
        first_queue = next(index for index, call in enumerate(calls) if call[0].endswith("/queue"))
        self.assertEqual(len(posts), 2)
        self.assertLess(max(posts), first_queue)
        self.assertTrue(all(job["status"] == "succeeded" for job in read(
            self.book / "planning/video_manifest.json")["jobs"]))

    def test_drive_reference_mode_binds_shared_drive_without_upload(self):
        compile_plan(self.book)
        job = read(self.book / "planning/video_manifest.json")["jobs"][0]
        context = comfy_batch.BatchContext(
            base="http://fake", book=self.book, drive_prefix="books/example/video/shots",
            drive_root=Path("/content/drive/MyDrive/vidio"),
            manifest_path=self.book / "planning/video_manifest.json", reference_mode="drive",
        )
        with patch.object(comfy_batch, "upload") as upload:
            workflow = comfy_batch.build_workflow(context, job)
        upload.assert_not_called()
        self.assertEqual(workflow["114"]["inputs"]["image"],
                         "vidio/books/example/source/pages/1.png")

    def test_upload_reference_mode_remains_available(self):
        compile_plan(self.book)
        job = read(self.book / "planning/video_manifest.json")["jobs"][0]
        with patch.object(comfy_batch, "upload", return_value="uploaded.png") as upload:
            workflow = comfy_batch.build_workflow(self.batch_context(), job)
        upload.assert_called_once_with("http://fake", self.book / "source/pages/1.png")
        self.assertEqual(workflow["114"]["inputs"]["image"], "uploaded.png")

    def test_submission_error_does_not_block_later_jobs(self):
        self.add_second_shot()
        path = self.book / "planning/video_manifest.json"
        jobs = read(path)["jobs"]
        posts = 0

        def request(url, method="GET", obj=None):
            nonlocal posts
            if not url.endswith("/prompt"):
                return {}
            posts += 1
            if posts == 1:
                raise urllib.error.URLError("connection reset")
            return {"prompt_id": "pid-2"}

        with patch.object(comfy_batch, "request_json", side_effect=request), \
                patch.object(comfy_batch, "upload", return_value="uploaded.png"):
            comfy_batch.submit_batch(self.batch_context(), jobs, jobs)
        current = read(path)["jobs"]
        self.assertEqual(posts, 2)
        self.assertEqual(current[0]["status"], "submitting")
        self.assertEqual(current[1]["status"], "queued")

    def test_submitting_job_reconciles_by_client_id(self):
        compile_plan(self.book)
        path = self.book / "planning/video_manifest.json"
        jobs = read(path)["jobs"]
        jobs[0].update(status="submitting", client_id="client-1")
        atomic_json(path, {"jobs": jobs})
        queue = {"queue_pending": [[1, "pid-1", {}, {"client_id": "client-1"}]], "queue_running": []}
        with patch.object(comfy_batch, "request_json", side_effect=(queue, {})):
            comfy_batch.reconcile_jobs(self.batch_context(), jobs, jobs)
        current = read(path)["jobs"][0]
        self.assertEqual(current["status"], "queued")
        self.assertEqual(current["prompt_id"], "pid-1")

    def test_reconciliation_error_does_not_block_later_submission(self):
        self.add_second_shot()
        path = self.book / "planning/video_manifest.json"
        jobs = read(path)["jobs"]
        jobs[0].update(status="submitting", client_id="client-1")
        atomic_json(path, {"jobs": jobs})

        def request(url, method="GET", obj=None):
            if url.endswith("/queue"):
                raise urllib.error.URLError("temporary outage")
            if url.endswith("/prompt"):
                return {"prompt_id": "pid-2"}
            return {}

        with patch.object(comfy_batch, "request_json", side_effect=request), \
                patch.object(comfy_batch, "upload", return_value="uploaded.png"):
            comfy_batch.reconcile_jobs(self.batch_context(), jobs, jobs)
            comfy_batch.submit_batch(self.batch_context(), jobs, jobs)
        current = read(path)["jobs"]
        self.assertEqual(current[0]["status"], "submitting")
        self.assertIn("reconciliation unavailable", current[0]["error"])
        self.assertEqual(current[1]["status"], "queued")

    def test_tracker_recovers_from_transient_queue_error(self):
        compile_plan(self.book)
        path = self.book / "planning/video_manifest.json"
        jobs = read(path)["jobs"]
        jobs[0].update(status="queued", prompt_id="pid-1")
        atomic_json(path, {"jobs": jobs})
        calls = 0

        def request(url, method="GET", obj=None):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise urllib.error.URLError("connection reset")
            if url.endswith("/queue"):
                return {}
            return {"pid-1": {"status": {"status_str": "success"}, "outputs": {"1": {"videos": [{
                "filename": "S001_SH001_00001.mp4",
                "subfolder": "books/example/video/shots/S001_SH001",
                "type": "output",
            }]}}}}

        with patch.object(comfy_batch, "request_json", side_effect=request), \
                patch.object(comfy_batch.time, "sleep"):
            comfy_batch.monitor_batch(self.batch_context(), jobs, jobs, 2, .01, .01)
        self.assertEqual(read(path)["jobs"][0]["status"], "succeeded")

    def test_restart_lost_prompt_is_requeued_on_new_endpoint(self):
        compile_plan(self.book)
        path = self.book / "planning/video_manifest.json"
        jobs = read(path)["jobs"]
        jobs[0].update(status="queued", prompt_id="old-pid", client_id="old-client",
                       endpoint="http://old", attempts=1, post_attempts=1)
        atomic_json(path, {"jobs": jobs})
        with patch.object(comfy_batch, "request_json", side_effect=({}, {})):
            comfy_batch.reconcile_jobs(self.batch_context("http://new"), jobs, jobs)
        current = read(path)["jobs"][0]
        self.assertEqual(current["status"], "failed")
        self.assertNotIn("prompt_id", current)
        self.assertEqual(current["previous_prompt_ids"], ["old-pid"])
        self.assertFalse(comfy_batch.submission_limit_reached(current))

    def test_confirmed_same_endpoint_restart_recovers_legacy_prompt(self):
        compile_plan(self.book)
        path = self.book / "planning/video_manifest.json"
        jobs = read(path)["jobs"]
        jobs[0].update(status="running", prompt_id="legacy-pid", client_id="legacy-client", attempts=1)
        atomic_json(path, {"jobs": jobs})
        with patch.object(comfy_batch, "request_json", side_effect=({}, {})):
            comfy_batch.reconcile_jobs(self.batch_context(restart_confirmed=True), jobs, jobs)
        current = read(path)["jobs"][0]
        self.assertEqual(current["status"], "failed")
        self.assertEqual(current["previous_prompt_ids"], ["legacy-pid"])
        self.assertNotIn("prompt_id", current)

    def test_definitive_prompt_rejection_is_retryable_and_bounded(self):
        compile_plan(self.book)
        path = self.book / "planning/video_manifest.json"
        jobs = read(path)["jobs"]
        rejected = urllib.error.HTTPError("http://fake/prompt", 400, "bad prompt", {}, io.BytesIO())
        self.addCleanup(rejected.close)
        with patch.object(comfy_batch, "request_json", side_effect=rejected), \
                patch.object(comfy_batch, "upload", return_value="uploaded.png"):
            comfy_batch.submit_batch(self.batch_context(), jobs, jobs)
        current = read(path)["jobs"][0]
        self.assertEqual(current["status"], "failed")
        self.assertEqual(current["post_attempts"], 1)
        self.assertFalse(comfy_batch.submission_limit_reached(current))
        current["post_attempts"] = 3
        self.assertTrue(comfy_batch.submission_limit_reached(current))

    def test_targeted_reconciliation_preserves_unselected_jobs(self):
        self.add_second_shot()
        path = self.book / "planning/video_manifest.json"
        jobs = read(path)["jobs"]
        jobs[0].update(status="submitting", client_id="client-1")
        atomic_json(path, {"jobs": jobs})
        queue = {"queue_pending": [[1, "pid-1", {}, {"client_id": "client-1"}]], "queue_running": []}
        with patch.object(comfy_batch, "request_json", side_effect=(queue, {})):
            comfy_batch.reconcile_jobs(self.batch_context(), jobs, [jobs[0]])
        current = read(path)["jobs"]
        self.assertEqual(len(current), 2)
        self.assertEqual(current[0]["prompt_id"], "pid-1")
        self.assertEqual(current[1]["status"], "pending")

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
