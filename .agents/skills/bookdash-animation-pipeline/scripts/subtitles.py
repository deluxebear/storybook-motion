"""Export line-level sidecars against the measured assembly timeline."""
import html
import math
import os
from pathlib import Path


def timestamp(milliseconds: int, separator: str) -> str:
    seconds, ms = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}{separator}{ms:03}"


def build_cues(segments, rows, measure, alignment=None):
    lookup = {row['audio_id']: row for row in rows if row.get('required', True)}
    cues = []
    offset = 0.0
    seen = set()
    for segment in segments:
        length = float(segment['final_seconds'])
        if not math.isfinite(length) or length <= 0:
            raise ValueError('invalid segment duration')
        local = 0.0
        for aid in segment['audio_ids']:
            if aid in seen:
                raise ValueError(f'duplicate subtitle audio: {aid}')
            seen.add(aid)
            row = lookup[aid]
            if row.get('status') != 'succeeded' or row['shot_id'] != segment['shot_id']:
                raise ValueError(f'invalid subtitle audio: {aid}')
            text = ' '.join(row['text'].split())
            seconds = float(measure(row))
            if not text or not math.isfinite(seconds) or seconds <= 0:
                raise ValueError(f'invalid subtitle text/duration: {aid}')
            # Permit only frame/sample rounding at the end of a rendered shot.
            if local + seconds > length + 0.05:
                raise ValueError(f'narration exceeds assembled shot: {aid}')
            entry = alignment(row, seconds) if alignment else {}
            words = entry.get('words', [])
            line_start = float(words[0]['start']) if words else 0.0
            line_end = float(words[-1]['end']) if words else seconds
            start = round((offset + local + line_start) * 1000)
            end = round((offset + min(local + line_end, length)) * 1000)
            if end <= start:
                raise ValueError(f'empty subtitle interval: {aid}')
            cues.append({'audio_id': aid, 'shot_id': row['shot_id'],
                         'start_ms': start, 'end_ms': end, 'text': text,
                         'timing': 'forced_alignment' if words else 'tts_line_duration',
                         'fallback_reason': entry.get('reason') if not words else None,
                         'words': [{'text': word['text'],
                                    'start_ms': round((offset + local + float(word['start'])) * 1000),
                                    'end_ms': round((offset + min(local + float(word['end']), length)) * 1000)}
                                   for word in words]})
            local += seconds
        offset += length
    if seen != set(lookup):
        raise ValueError('required narration missing from subtitle timeline')
    return cues


def export_subtitles(book: Path, output: Path, segments, rows, measure):
    from production_plan import file_hash
    from projectctl import atomic_json
    from alignment import identity, load_report, validate_words
    report = load_report(book)

    def aligned(row, seconds):
        entry = report['lines'].get(row['audio_id'], {})
        if entry.get('status') != 'aligned':
            return {'reason': entry.get('reason', 'alignment unavailable')}
        try:
            if entry.get('source_hash') != identity(book, row):
                raise ValueError('alignment source changed')
            validate_words(entry['words'], row['text'], seconds)
            return entry
        except (OSError, KeyError, TypeError, ValueError) as exc:
            return {'reason': str(exc)}

    cues = build_cues(segments, rows, measure, aligned)
    files = {}
    for extension, separator in [('srt', ','), ('vtt', '.')]:
        blocks = []
        for index, cue in enumerate(cues, 1):
            text = html.escape(cue['text'], quote=False)
            blocks.append(f"{index}\n{timestamp(cue['start_ms'], separator)} --> "
                          f"{timestamp(cue['end_ms'], separator)}\n{text}\n\n")
        content = ('WEBVTT\n\n' if extension == 'vtt' else '') + ''.join(blocks)
        path = output.with_suffix('.' + extension)
        if not path.exists() or path.read_text(encoding='utf-8') != content:
            temporary = path.with_suffix(path.suffix + '.tmp')
            temporary.write_text(content, encoding='utf-8')
            os.replace(temporary, path)
        files[extension] = {'path': str(path.relative_to(book)), 'sha256': file_hash(path)}
    aligned_count = sum(cue['timing'] == 'forced_alignment' for cue in cues)
    result = {'version': 2, 'timing': 'forced_alignment_with_line_fallback',
              'aligned_count': aligned_count, 'fallback_count': len(cues) - aligned_count,
              'cue_count': len(cues),
              'files': files, 'cues': cues}
    atomic_json(output.with_suffix('.subtitles.json'), result)
    return result
