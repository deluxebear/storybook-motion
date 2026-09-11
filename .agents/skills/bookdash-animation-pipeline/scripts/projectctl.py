#!/usr/bin/env python3
import argparse, datetime as dt, json, re, shutil, sys
from pathlib import Path
from urllib.parse import urlparse

DIRS = ["source/original", "source/pages", "planning", "notebooks", "voices/candidates",
        "voices/selected", "audio/lines", "audio/mixes", "images/references", "images/shots",
        "comfyui/submissions", "video/shots", "video/final", "logs"]
TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates/minimax_h3_i2v"

def slug_from_url(url):
    p=urlparse(url)
    if p.scheme not in {"http","https"} or p.netloc.lower() not in {"bookdash.org","www.bookdash.org"}:
        raise ValueError("expected an http(s) Book Dash URL")
    m=re.search(r"/books/([^/]+)/?",p.path)
    if not m: raise ValueError("URL must contain /books/<slug>/")
    slug=re.sub(r"[^a-z0-9-]+","-",m.group(1).lower()).strip("-")
    if not slug: raise ValueError("empty book slug")
    return slug

def atomic_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix(path.suffix+".tmp")
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n",encoding="utf-8"); tmp.replace(path)

def do_init(a):
    slug=slug_from_url(a.book_url); root=Path(a.root).expanduser().resolve()/slug
    for rel in DIRS: (root/rel).mkdir(parents=True,exist_ok=True)
    now=dt.datetime.now(dt.timezone.utc).isoformat()
    if not (root/"planning/book.json").exists():
        atomic_json(root/"planning/book.json",{"schema_version":1,"book_slug":slug,"book_url":a.book_url,"sources":[]})
    if not (root/"status.json").exists():
        atomic_json(root/"status.json",{"schema_version":1,"book_slug":slug,"book_url":a.book_url,"updated_at":now,
          "stages":{k:{"status":"pending"} for k in ["acquire","plan","voice_design","tts","comfy_endpoint","video","final"]}})
    for name in ("workflow_api.json", "bindings.json"):
        target = root / "comfyui" / name
        if not target.exists():
            shutil.copyfile(TEMPLATE_DIR / name, target)
    print(root); return 0

def do_validate(a):
    root=Path(a.book_dir).expanduser().resolve(); errors=[]
    for rel in ["planning/book.json","status.json"]:
        if not (root/rel).is_file(): errors.append(f"missing {rel}")
    for name in ["tts_manifest.json","video_manifest.json"]:
        p=root/"planning"/name
        if not p.exists(): continue
        try: data=json.loads(p.read_text(encoding="utf-8"))
        except Exception as e: errors.append(f"invalid {name}: {e}"); continue
        rows=data.get("jobs",[]) if isinstance(data,dict) else data
        for row in rows:
            if row.get("required",True) and row.get("status")=="succeeded":
                if row.get("remote_output"):
                    remote=Path(row["remote_output"])
                    if not str(remote).startswith("/content/drive/MyDrive/"):
                        errors.append(f"invalid Drive output for {row.get('audio_id') or row.get('shot_id')}")
                    continue
                out=root/row.get("output","")
                if not out.is_file() or out.stat().st_size==0: errors.append(f"missing output for {row.get('audio_id') or row.get('shot_id')}")
    if errors: print("\n".join(errors),file=sys.stderr); return 1
    print(f"VALID {root}"); return 0

def main():
    ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest="cmd",required=True)
    p=sub.add_parser("init"); p.add_argument("--root",required=True); p.add_argument("--book-url",required=True); p.set_defaults(fn=do_init)
    p=sub.add_parser("validate"); p.add_argument("--book-dir",required=True); p.set_defaults(fn=do_validate)
    a=ap.parse_args(); raise SystemExit(a.fn(a))
if __name__=="__main__": main()
