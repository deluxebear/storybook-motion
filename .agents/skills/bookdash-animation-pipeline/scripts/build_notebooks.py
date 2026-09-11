#!/usr/bin/env python3
import argparse, json
from pathlib import Path

def code(src): return {"cell_type":"code","execution_count":None,"metadata":{},"outputs":[],"source":[x+"\n" for x in src.strip().splitlines()]}
def markdown(src): return {"cell_type":"markdown","metadata":{},"source":[src+"\n"]}

VOICE=r'''
from pathlib import Path
import json, random, numpy as np, soundfile as sf, torch
from qwen_tts import Qwen3TTSModel
book=Path(BOOK_DIR); specs=json.loads((book/'planning/voice_specs.json').read_text())
out=book/'voices/candidates'; out.mkdir(parents=True,exist_ok=True)
model=Qwen3TTSModel.from_pretrained('Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign',device_map='cuda:0',dtype=torch.bfloat16,attn_implementation='sdpa')
records=[]
for ri,(role,spec) in enumerate(specs.items()):
    d=out/role; d.mkdir(parents=True,exist_ok=True)
    for n in range(1,int(spec.get('candidates',3))+1):
        seed=int(spec.get('base_seed',20260909))+ri*100+n
        random.seed(seed); np.random.seed(seed); torch.manual_seed(seed); torch.cuda.manual_seed_all(seed)
        wavs,sr=model.generate_voice_design(text=spec['reference_text'],language=spec['language'],instruct=spec['instruct'],do_sample=True,temperature=float(spec.get('temperature',.9)),top_p=float(spec.get('top_p',.95)))
        target=d/f'{role}_{n:02d}.wav'; sf.write(target,wavs[0],sr,subtype='PCM_16')
        records.append({'role_id':role,'candidate':n,'seed':seed,'output':str(target.relative_to(book)),'seconds':len(wavs[0])/sr})
(out/'manifest.json').write_text(json.dumps({'jobs':records},indent=2))
print('SUCCESS',len(records))
'''

TTS=r'''
from pathlib import Path
import json, os, wave
from indextts.infer_v2_5 import IndexTTS2
book=Path(BOOK_DIR); model_dir=Path(MODEL_DIR); mp=book/'planning/tts_manifest.json'
data=json.loads(mp.read_text()); jobs=data.get('jobs',data)
tts=IndexTTS2(cfg_path=str(model_dir/'config.yaml'),model_dir=str(model_dir),use_bf16=True)
for job in jobs:
    output=book/job['output']
    if job.get('status')=='succeeded' and output.is_file() and output.stat().st_size>44: continue
    output.parent.mkdir(parents=True,exist_ok=True); tmp=output.with_suffix('.tmp.wav')
    try:
        kwargs={}
        if job.get('emotion_vector') is not None: kwargs['emo_vector']=job['emotion_vector']
        tts.infer(spk_audio_prompt=str(book/job['reference_voice']),text=job['text'],lang=job['lang'],output_path=str(tmp),duration_factor=float(job.get('duration_factor',1)),max_text_tokens_per_segment=int(job.get('max_text_tokens_per_segment',60)),verbose=False,**kwargs)
        with wave.open(str(tmp),'rb') as w: seconds=w.getnframes()/w.getframerate()
        if seconds<.15: raise ValueError('implausibly short audio')
        os.replace(tmp,output); job.update(status='succeeded',seconds=round(seconds,3),bytes=output.stat().st_size,error=None)
    except Exception as e:
        if tmp.exists(): tmp.unlink()
        job.update(status='failed',error=repr(e))
    tmp_manifest=mp.with_suffix('.json.tmp'); tmp_manifest.write_text(json.dumps({'jobs':jobs},ensure_ascii=False,indent=2)); os.replace(tmp_manifest,mp)
failed=[j for j in jobs if j.get('required',True) and j.get('status')!='succeeded']
print('SUCCESS',len(jobs)-len(failed),'FAILED',len(failed))
if failed: raise RuntimeError([j['audio_id'] for j in failed])
'''

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("kind",choices=["voice","tts"]); ap.add_argument("--book-dir",required=True); ap.add_argument("--output",required=True); ap.add_argument("--model-dir",default="/content/drive/MyDrive/vidio/models/IndexTTS-2.5")
    a=ap.parse_args(); params=f"BOOK_DIR={a.book_dir.rstrip('/')!r}\nMODEL_DIR={a.model_dir!r}"
    if a.kind=='voice':
        setup="import subprocess,sys\nsubprocess.check_call([sys.executable,'-m','pip','install','-q','-U','qwen-tts','soundfile'])"
        body=VOICE
    else:
        setup="""import subprocess
subprocess.check_call(['bash','-lc','set -e; command -v uv >/dev/null || python -m pip install -q -U uv; test -d /content/index-tts/.git || git clone --depth 1 https://github.com/index-tts/index-tts.git /content/index-tts; cd /content/index-tts; uv sync'])"""
        body=f"""from pathlib import Path
import subprocess
runner=Path('/content/index-tts/run_book_tts.py')
runner.write_text({(params+chr(10)+TTS)!r},encoding='utf-8')
subprocess.check_call(['bash','-lc','cd /content/index-tts && PYTHONPATH=. uv run python run_book_tts.py'])"""
    doc={"cells":[markdown(f"# Generated {a.kind} notebook — {Path(a.book_dir).name}"),code(params),code(setup),code(body)],"metadata":{"kernelspec":{"display_name":"Python 3","language":"python","name":"python3"}},"nbformat":4,"nbformat_minor":5}
    out=Path(a.output); out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(doc,ensure_ascii=False,indent=1)+"\n"); print(out)
if __name__=='__main__': main()
