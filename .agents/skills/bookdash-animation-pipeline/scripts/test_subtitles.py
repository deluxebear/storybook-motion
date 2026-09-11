"""Offline subtitle timeline and sidecar recovery checks."""
import tempfile
import unittest
from pathlib import Path

from subtitles import build_cues, export_subtitles, timestamp


class SubtitleTests(unittest.TestCase):
    def setUp(self):
        self.rows = [dict(audio_id=aid, shot_id=shot, text=text, status='succeeded', seconds=seconds)
                     for aid, shot, text, seconds in [('b', 'one', '第二句。', 0.5),
                                                      ('a', 'one', 'Hello & <friend>!', 1),
                                                      ('c', 'three', 'Goodbye.', 1.2)]]
        self.segments = [dict(shot_id='one', audio_ids=['a', 'b'], final_seconds=3.04),
                         dict(shot_id='silent', audio_ids=[], final_seconds=2),
                         dict(shot_id='three', audio_ids=['c'], final_seconds=2)]

    def test_order_tail_padding_and_silent_shots(self):
        cues = build_cues(self.segments, self.rows, lambda row: row['seconds'])
        self.assertEqual([(c['audio_id'], c['start_ms'], c['end_ms']) for c in cues],
                         [('a', 0, 1000), ('b', 1000, 1500), ('c', 5040, 6240)])

    def test_reject_truncation_and_omitted_audio(self):
        self.segments[0]['final_seconds'] = 1
        with self.assertRaises(ValueError):
            build_cues(self.segments, self.rows, lambda row: row['seconds'])
        with self.assertRaises(ValueError):
            build_cues([], self.rows, lambda row: row['seconds'])

    def test_export_and_repair_without_changing_valid_file(self):
        with tempfile.TemporaryDirectory() as folder:
            book = Path(folder)
            output = book / 'final.mp4'
            result = export_subtitles(book, output, self.segments, self.rows, lambda row: row['seconds'])
            srt = output.with_suffix('.srt')
            vtt = output.with_suffix('.vtt')
            before = srt.stat().st_mtime_ns
            self.assertEqual(result['cue_count'], 3)
            self.assertIn('00:00:05,040 --> 00:00:06,240', srt.read_text())
            self.assertIn('第二句。', srt.read_text())
            self.assertIn('Hello &amp; &lt;friend&gt;!', srt.read_text())
            vtt.write_text('broken')
            export_subtitles(book, output, self.segments, self.rows, lambda row: row['seconds'])
            self.assertTrue(vtt.read_text().startswith('WEBVTT\n\n'))
            self.assertEqual(before, srt.stat().st_mtime_ns)

    def test_timestamp_rounding_and_empty_book(self):
        self.assertEqual(timestamp(3600001, ','), '01:00:00,001')
        self.assertEqual(build_cues([dict(final_seconds=1, audio_ids=[])], [], None), [])


if __name__ == '__main__':
    unittest.main()
