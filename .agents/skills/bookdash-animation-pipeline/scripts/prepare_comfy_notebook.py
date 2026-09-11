#!/usr/bin/env python3
"""Create a deterministic, noninteractive execution copy of ComfyUIonColab.ipynb."""
import argparse, json
from pathlib import Path

def code(source):
    return {"cell_type":"code","execution_count":None,"metadata":{},"outputs":[],"source":[line+"\n" for line in source.strip().splitlines()]}
def markdown(source):
    return {"cell_type":"markdown","metadata":{},"source":[source+"\n"]}

VERIFY = r'''
from pathlib import Path
import subprocess
gpu=subprocess.check_output(['nvidia-smi','--query-gpu=name','--format=csv,noheader'],text=True).strip()
if 'A100' not in gpu: raise RuntimeError(f'Expected A100, got {gpu}')
drive=Path('/content/drive/MyDrive')
if not drive.is_dir(): raise RuntimeError('Drive is not mounted')
probe=drive/'vidio/.mount-probes'/f'{BOOK_SLUG}-a100.txt'; probe.parent.mkdir(parents=True,exist_ok=True)
token=f'{BOOK_SLUG}-a100-mount-ok'; probe.write_text(token)
if probe.read_text()!=token: raise RuntimeError('Drive read/write probe mismatch')
probe.unlink()
print('A100_AND_DRIVE_OK',gpu)
'''

INSTALL_TUNNEL = r'''
from pathlib import Path
import os, subprocess, urllib.request
target=Path('/usr/local/bin/cloudflared')
if not target.exists():
    urllib.request.urlretrieve('https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64',target)
    target.chmod(0o755)
print(subprocess.check_output([str(target),'--version'],text=True).strip())
'''

LAUNCH = r'''
from pathlib import Path
import datetime as dt, json, os, re, socket, subprocess, time, urllib.request
book=Path(BOOK_DIR); log_dir=book/'logs'; log_dir.mkdir(parents=True,exist_ok=True)
comfy_log=log_dir/'comfyui-a100.log'; tunnel_log=log_dir/'cloudflared-a100.log'
for p in (comfy_log,tunnel_log): p.write_text('')
comfy_f=comfy_log.open('ab',buffering=0)
comfy=subprocess.Popen(['python','main.py','--dont-print-server','--highvram','--listen','127.0.0.1','--port','8188'],cwd='/content/ComfyUI',stdout=comfy_f,stderr=subprocess.STDOUT,start_new_session=True)
deadline=time.time()+900
while time.time()<deadline:
    if comfy.poll() is not None: raise RuntimeError(f'ComfyUI exited: {comfy.returncode}')
    try:
        with urllib.request.urlopen('http://127.0.0.1:8188/system_stats',timeout=2) as r:
            if r.status==200: break
    except Exception: time.sleep(2)
else: raise TimeoutError('ComfyUI health timeout')
tunnel_f=tunnel_log.open('ab',buffering=0)
tunnel=subprocess.Popen(['/usr/local/bin/cloudflared','tunnel','--no-autoupdate','--url','http://127.0.0.1:8188'],stdout=tunnel_f,stderr=subprocess.STDOUT,start_new_session=True)
url=None; deadline=time.time()+180
pattern=re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com')
while time.time()<deadline:
    if tunnel.poll() is not None: raise RuntimeError(f'cloudflared exited: {tunnel.returncode}')
    match=pattern.search(tunnel_log.read_text(errors='replace'))
    if match:
        candidate=match.group(0)
        try:
            with urllib.request.urlopen(candidate+'/system_stats',timeout=10) as r:
                if r.status==200: url=candidate; break
        except Exception: pass
    time.sleep(2)
if not url: raise TimeoutError('Cloudflare URL discovery/health timeout')
runtime={'book_slug':BOOK_SLUG,'session':SESSION_NAME,'accelerator':'A100','url':url,'comfy_pid':comfy.pid,'tunnel_pid':tunnel.pid,'started_at':dt.datetime.now(dt.timezone.utc).isoformat(),'comfy_log':str(comfy_log.relative_to(book)),'tunnel_log':str(tunnel_log.relative_to(book))}
path=book/'comfyui/runtime.json'; path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix('.json.tmp'); tmp.write_text(json.dumps(runtime,indent=2)); os.replace(tmp,path)
print('COMFYUI_URL='+url)
'''

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--source',required=True); ap.add_argument('--output',required=True); ap.add_argument('--book-dir',required=True); ap.add_argument('--book-slug',required=True); ap.add_argument('--session',required=True); a=ap.parse_args()
    source=Path(a.source); original=json.loads(source.read_text(encoding='utf-8'))
    if len(original.get('cells',[]))<20: raise ValueError('unexpected ComfyUI notebook layout')
    setup=''.join(original['cells'][3].get('source',[]))
    required=["WORKSPACE = '/content/ComfyUI'","OUTPUT_PATH = Path(DRIVE_PATH) / 'ComfyUI' / 'output'"]
    if any(x not in setup for x in required): raise ValueError('source setup cell no longer matches the supported layout')
    setup=setup.replace("OUTPUT_PATH = Path(DRIVE_PATH) / 'ComfyUI' / 'output'","OUTPUT_PATH = Path(BOOK_DIR) / 'video' / 'comfyui-output'")
    params=f"BOOK_DIR={a.book_dir.rstrip('/')!r}\nBOOK_SLUG={a.book_slug!r}\nSESSION_NAME={a.session!r}"
    doc={"cells":[markdown(f"# A100 execution copy of {source.name}"),code(params),code(VERIFY),code(setup),code(INSTALL_TUNNEL),code(LAUNCH)],"metadata":original.get('metadata',{}),"nbformat":4,"nbformat_minor":5}
    out=Path(a.output); out.parent.mkdir(parents=True,exist_ok=True); out.write_text(json.dumps(doc,ensure_ascii=False,indent=1)+"\n",encoding='utf-8'); print(out)
if __name__=='__main__': main()
