"""Assemble existing local media into an isolated test directory."""
import json
import subprocess
import sys
from pathlib import Path

project = Path(__file__).resolve().parents[1]
source = project / 'books/moms-hands-sasl'
target = source / 'qa/assembly-test-20260910-134755'
target.mkdir(parents=True, exist_ok=False)
(target / 'planning').mkdir()
(target / 'video').mkdir()
(target / 'audio').mkdir()
(target / 'video/shots').symlink_to(source / 'video/shots', target_is_directory=True)
(target / 'audio/lines').symlink_to(source / 'audio/lines', target_is_directory=True)
audio = json.loads((source / 'planning/tts_manifest.json').read_text())
video = json.loads((source / 'planning/video_manifest.json').read_text())
for row in video['jobs']:
    assert row['status'] == 'succeeded'
    assert (source / row['output']).is_file(), row['shot_id']
    for key in ('remote_output', 'remote_filename', 'remote_subfolder'):
        row.pop(key, None)
    row['audio_ids'] = [a['audio_id'] for a in audio['jobs'] if a['shot_id'] == row['shot_id']]
for row in audio['jobs']:
    assert row['status'] == 'succeeded'
    assert (source / row['output']).is_file(), row['audio_id']
for name, data in [('tts', audio), ('video', video)]:
    (target / f'planning/{name}_manifest.json').write_text(json.dumps(data, indent=2))
script = project / '.agents/skills/bookdash-animation-pipeline/scripts/assemble_final.py'
with (target / 'assembly.log').open('w') as log:
    result = subprocess.run([sys.executable, str(script), '--book-dir', str(target)], stdout=log, stderr=subprocess.STDOUT, timeout=1800)
(target / 'result.json').write_text(json.dumps({'returncode': result.returncode, 'output': str(target / 'video/final/narrated_final.mp4')}))
print(json.dumps({'returncode': result.returncode, 'test_dir': str(target)}), flush=True)
raise SystemExit(result.returncode)
