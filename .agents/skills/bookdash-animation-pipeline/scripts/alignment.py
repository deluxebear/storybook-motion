"""Drive-side optional forced alignment, with durable line-duration fallback."""
import argparse
import json
import math
import os
import signal
import subprocess
import unicodedata
from pathlib import Path

from production_plan import digest, file_hash, read, relative
from projectctl import atomic_json

MODEL = 'Qwen/Qwen3-ForcedAligner-0.6B'
PACKAGE = 'qwen-asr==0.0.6'
LANGUAGES = dict(zip(
    ['zh', 'en', 'yue', 'fr', 'de', 'it', 'ja', 'ko', 'pt', 'ru', 'es'],
    ['Chinese', 'English', 'Cantonese', 'French', 'German', 'Italian', 'Japanese',
     'Korean', 'Portuguese', 'Russian', 'Spanish']))


def language(value):
    value = value.strip().lower()
    return LANGUAGES.get(value) or next((v for v in LANGUAGES.values() if v.lower() == value), None)


def identity(book, row):
    return digest({'version': 1, 'text': row['text'], 'lang': row['lang'],
                   'wav': file_hash(relative(book, row['output']))})


def letters(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold() if c.isalnum())


def validate_words(words, text, seconds):
    if not words or not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('empty alignment or invalid duration')
    previous = 0.0
    for word in words:
        start, end = float(word['start']), float(word['end'])
        if not word['text'].strip() or not (math.isfinite(start) and math.isfinite(end)):
            raise ValueError('invalid alignment token')
        if start < previous or end <= start or end > seconds:
            raise ValueError('alignment timestamps overlap or exceed audio')
        previous = end
    if not letters(text) or letters(''.join(w['text'] for w in words)) != letters(text):
        raise ValueError('alignment does not cover the original text')
    return words


def load_report(book):
    try:
        report = read(book / 'qa/subtitle_alignment.json')
        if report.get('version') == 1 and isinstance(report.get('lines'), dict):
            return report
    except (OSError, ValueError, AttributeError):
        pass
    return {'version': 1, 'model': MODEL, 'package': PACKAGE, 'lines': {}}


def save_report(book, report):
    lines = list(report['lines'].values())
    report['aligned_count'] = sum(row['status'] == 'aligned' for row in lines)
    report['fallback_count'] = len(lines) - report['aligned_count']
    report['status'] = 'passed' if not report['fallback_count'] else 'completed_with_fallback'
    atomic_json(book / 'qa/subtitle_alignment.json', report)


def prepare(book):
    prior = load_report(book)
    report = {'version': 1, 'model': MODEL, 'package': PACKAGE, 'lines': {}}
    pending = []
    for row in read(book / 'planning/tts_manifest.json')['jobs']:
        if not row.get('required', True):
            continue
        fingerprint = identity(book, row)
        old = prior['lines'].get(row['audio_id'], {})
        if old.get('source_hash') == fingerprint and old.get('status') == 'aligned':
            try:
                validate_words(old['words'], row['text'], float(row['seconds']))
                report['lines'][row['audio_id']] = old
                continue
            except (KeyError, TypeError, ValueError):
                pass
        report['lines'][row['audio_id']] = {
            'status': 'fallback', 'source_hash': fingerprint,
            'reason': 'alignment not completed' if language(row['lang']) else 'unsupported language'}
        if language(row['lang']):
            pending.append(row)
    save_report(book, report)
    return report, pending


def worker(book):
    report, pending = prepare(book)
    if not pending:
        return
    import torch
    from qwen_asr import Qwen3ForcedAligner
    model = Qwen3ForcedAligner.from_pretrained(MODEL, dtype=torch.bfloat16,
                                             device_map='cuda:0', attn_implementation='sdpa')
    for row in pending:
        entry = report['lines'][row['audio_id']]
        try:
            if row['status'] != 'succeeded':
                raise ValueError('TTS is not complete')
            result = model.align(audio=str(relative(book, row['output'])), text=row['text'],
                                 language=language(row['lang']))[0]
            words = [{'text': item.text, 'start': float(item.start_time),
                      'end': float(item.end_time)} for item in result]
            validate_words(words, row['text'], float(row['seconds']))
            entry.update(status='aligned', words=words, reason=None)
        except Exception as exc:
            entry.update(status='fallback', reason=f'{type(exc).__name__}: {str(exc)[:300]}')
        save_report(book, report)


def bounded(command, env, log, timeout):
    with subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                          start_new_session=True) as process:
        try:
            code = process.wait(timeout=timeout)
        except BaseException:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            raise
        if code:
            raise RuntimeError(f'alignment subprocess exited {code}')


def run_optional(book):
    # Fallback is persisted before installation or inference can fail/time out.
    report, pending = prepare(book)
    if not pending:
        return
    cache = Path('/content/drive/MyDrive/vidio/models/huggingface')
    env = {**os.environ, 'HF_HOME': str(cache), 'HF_HUB_CACHE': str(cache / 'hub'),
           'HF_HUB_DISABLE_PROGRESS_BARS': '1', 'TOKENIZERS_PARALLELISM': 'false'}
    env.pop('PYTHONPATH', None)
    venv = Path('/content/vidio-aligner-venv')
    log_path = book / 'qa/subtitle_alignment.log'
    try:
        with log_path.open('a') as log:
            if not (venv / 'bin/python').exists():
                bounded(['uv', 'venv', '--python', '3.12', str(venv)], env, log, 120)
            bounded(['uv', 'pip', 'install', '--python', str(venv / 'bin/python'), PACKAGE], env, log, 600)
            bounded([str(venv / 'bin/python'), str(Path(__file__).resolve()), '--book-dir', str(book)],
                    env, log, 1800)
    except Exception as exc:
        report = load_report(book)
        for entry in report['lines'].values():
            if entry['status'] != 'aligned' and entry.get('reason') == 'alignment not completed':
                entry['reason'] = f'{type(exc).__name__}: {str(exc)[:300]}'
        report['warning'] = f'Optional alignment failed; see {log_path.name}'
        save_report(book, report)
    report = load_report(book)
    print(json.dumps({'alignment': report['status'], 'aligned': report['aligned_count'],
                      'fallback': report['fallback_count']}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--book-dir', required=True)
    worker(Path(parser.parse_args().book_dir))
