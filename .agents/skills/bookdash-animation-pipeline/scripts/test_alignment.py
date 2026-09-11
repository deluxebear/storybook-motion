"""Offline forced-alignment contracts, cache invalidation and failure recovery."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from alignment import identity, load_report, prepare, run_optional, save_report, validate_words, worker
from projectctl import atomic_json
from subtitles import export_subtitles


class AlignmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.book = Path(self.temp.name)
        (self.book / 'line.wav').write_bytes(b'fake wav for hashing')
        self.row = dict(audio_id='a', shot_id='s', text='Hello, world!', lang='en',
                        output='line.wav', status='succeeded', seconds=2)
        self.words = [dict(text='Hello', start=.2, end=.7), dict(text='world', start=1, end=1.5)]
        atomic_json(self.book / 'planning/tts_manifest.json', {'jobs': [self.row]})

    def store_aligned(self):
        report, _ = prepare(self.book)
        report['lines']['a'].update(status='aligned', words=self.words, reason=None)
        save_report(self.book, report)

    def test_validation_rejects_missing_words_overlap_nan_and_overrun(self):
        validate_words(self.words, self.row['text'], 2)
        for words in [self.words[:1], [dict(text='Hello world', start=0, end=3)],
                      [dict(text='Hello world', start=float('nan'), end=1)],
                      [self.words[0], dict(text='world', start=.6, end=1)]]:
            with self.assertRaises(ValueError):
                validate_words(words, self.row['text'], 2)

    def test_resume_and_audio_change(self):
        self.store_aligned()
        self.assertEqual(prepare(self.book)[1], [])
        (self.book / 'line.wav').write_bytes(b'new wav')
        report, pending = prepare(self.book)
        self.assertEqual(len(pending), 1)
        self.assertEqual(report['lines']['a']['status'], 'fallback')

    def test_unsupported_language_never_installs(self):
        self.row['lang'] = 'Zulu'
        atomic_json(self.book / 'planning/tts_manifest.json', {'jobs': [self.row]})
        with patch('alignment.bounded') as process:
            run_optional(self.book)
        process.assert_not_called()
        self.assertEqual(load_report(self.book)['fallback_count'], 1)

    def test_install_failure_and_timeout_preserve_fallback(self):
        for error in [RuntimeError('install failed'), subprocess.TimeoutExpired('aligner', 1800)]:
            with patch('alignment.bounded', side_effect=error):
                run_optional(self.book)
            report = load_report(self.book)
            self.assertEqual(report['fallback_count'], 1)
            self.assertNotEqual(report['lines']['a']['reason'], 'alignment not completed')

    def test_worker_official_api_contract(self):
        model = Mock()
        model.align.return_value = [[SimpleNamespace(text=w['text'], start_time=w['start'], end_time=w['end']) for w in self.words]]
        factory = Mock()
        factory.from_pretrained.return_value = model
        with patch.dict('sys.modules', {'torch': SimpleNamespace(bfloat16='bf16'),
                                       'qwen_asr': SimpleNamespace(Qwen3ForcedAligner=factory)}):
            worker(self.book)
        self.assertEqual(load_report(self.book)['aligned_count'], 1)
        model.align.assert_called_once_with(audio=str(self.book / 'line.wav'), text='Hello, world!', language='English')

    def test_worker_inference_failure_is_nonfatal(self):
        factory = Mock()
        factory.from_pretrained.return_value.align.side_effect = RuntimeError('CUDA OOM')
        with patch.dict('sys.modules', {'torch': SimpleNamespace(bfloat16='bf16'),
                                       'qwen_asr': SimpleNamespace(Qwen3ForcedAligner=factory)}):
            worker(self.book)
        self.assertIn('CUDA OOM', load_report(self.book)['lines']['a']['reason'])

    def test_final_timeline_preserves_text_and_trims_silence(self):
        self.store_aligned()
        segments = [dict(shot_id='silent', audio_ids=[], final_seconds=3),
                    dict(shot_id='s', audio_ids=['a'], final_seconds=4)]
        result = export_subtitles(self.book, self.book / 'final.mp4', segments, [self.row], lambda row: 2)
        cue = result['cues'][0]
        self.assertEqual((cue['start_ms'], cue['end_ms'], cue['text']), (3200, 4500, 'Hello, world!'))
        self.assertEqual(cue['words'][1]['start_ms'], 4000)
        self.assertEqual(result['aligned_count'], 1)
        (self.book / 'line.wav').write_bytes(b'changed')
        result = export_subtitles(self.book, self.book / 'final.mp4', segments, [self.row], lambda row: 2)
        self.assertEqual(result['fallback_count'], 1)
        self.assertEqual(result['cues'][0]['start_ms'], 3000)


if __name__ == '__main__':
    unittest.main()
