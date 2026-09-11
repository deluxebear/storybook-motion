"""LTX integration tests without GPU or network."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from test_pipeline import fixture
from production_plan import compile_plan, apply_tts_video_durations, read
from projectctl import atomic_json
from video_prompt import refine_video_prompts
from comfy_batch import build_workflow


class LTXTests(unittest.TestCase):
    def test_refinement_submission_and_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, plan = fixture(tmp)
            template = Path(__file__).resolve().parents[1] / 'templates/ltx2_5_i2v'
            for name in ('workflow_api.json', 'bindings.json'):
                shutil.copyfile(template / name, book / 'comfyui' / name)
            workflow = read(book / 'comfyui/workflow_api.json')
            self.assertEqual(workflow['398:362']['inputs']['value'], 5)
            self.assertNotIn('audio', workflow['398:370']['inputs'])
            compile_plan(book)
            path = book / 'planning/tts_manifest.json'
            tts = read(path)
            for row in tts['jobs']:
                row.update(status='succeeded', seconds=3.2)
            atomic_json(path, tts)
            apply_tts_video_durations(book)
            refine_video_prompts(book)
            video = book / 'planning/video_manifest.json'
            job = read(video)['jobs'][0]
            self.assertEqual(job['inputs']['duration_seconds'], 8)
            self.assertEqual(job['inputs']['prompt'], 'Gentle motion.')
            self.assertIn('[3.20-6.40s]', job['prompt_refinement']['prompt'])
            before = video.read_bytes()
            self.assertEqual(refine_video_prompts(book)['changed'], [])
            compile_plan(book)
            self.assertEqual(before, video.read_bytes())
            context = SimpleNamespace(book=book, drive_prefix='books/example/video/shots', reference_mode='drive', drive_input_prefix='vidio')
            bound = build_workflow(context, job)
            self.assertEqual(bound['398:376']['inputs']['value'], job['prompt_refinement']['prompt'])
            self.assertEqual(bound['398:362']['inputs']['value'], 8)
            self.assertEqual(bound['seed']['inputs']['value'], 42)
            job.update(status='queued', prompt_id='existing')
            atomic_json(video, {'jobs':[job]})
            tts['jobs'][0]['seconds'] = 4
            atomic_json(path, tts)
            self.assertEqual(refine_video_prompts(book)['changed'], [])
            self.assertEqual(read(video)['jobs'][0], job)

    def test_h3_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            book, _ = fixture(tmp)
            compile_plan(book)
            path = book / 'planning/video_manifest.json'
            before = path.read_bytes()
            refine_video_prompts(book)
            self.assertEqual(before, path.read_bytes())

if __name__ == '__main__':
    unittest.main()
