#!/usr/bin/env python3
import argparse, copy, fcntl, hashlib, json, mimetypes, os, time, urllib.error, urllib.request, uuid
from pathlib import Path

def raw_request(url,method="GET",data=None,headers=None,timeout=60):
    req=urllib.request.Request(url,data=data,headers=headers or {},method=method)
    tries = 1 if method == "POST" else 3
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req,timeout=timeout) as r: return r.read()
        except urllib.error.HTTPError as exc:
            if not 500 <= exc.code < 600 or attempt == tries - 1: raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == tries - 1: raise
        time.sleep(2 * (attempt + 1))
def request_json(url,method="GET",obj=None):
    data=None if obj is None else json.dumps(obj).encode()
    return json.loads(raw_request(url,method,data,{"Content-Type":"application/json"}))
def set_binding(workflow,binding,value):
    node=str(binding["node"]); key=binding["input"]
    if node not in workflow or key not in workflow[node].get("inputs",{}): raise KeyError(f"unresolved binding {node}.{key}")
    workflow[node]["inputs"][key]=value
def upload(base,path):
    boundary="----codex"+uuid.uuid4().hex; content=path.read_bytes(); mime=mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    head=f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{path.name}"\r\nContent-Type: {mime}\r\n\r\n'.encode()
    body=head+content+f"\r\n--{boundary}--\r\n".encode()
    return json.loads(raw_request(base+"/upload/image","POST",body,{"Content-Type":f"multipart/form-data; boundary={boundary}"},180))["name"]
def save_manifest(path,jobs):
    tmp=path.with_suffix(path.suffix+".tmp"); tmp.write_text(json.dumps({"jobs":jobs},ensure_ascii=False,indent=2)+"\n"); os.replace(tmp,path)
def run_batch(a):
    base=a.base_url.split("#")[0].rstrip("/"); book=Path(a.book_dir).resolve(); request_json(base+"/system_stats")
    drive_prefix=(a.drive_output_prefix or f"books/{book.name}/video/shots").strip("/")
    mp=book/"planning/video_manifest.json"; data=json.loads(mp.read_text()); jobs=data["jobs"] if isinstance(data,dict) else data
    for job in jobs:
        if a.shot_id and job["shot_id"] != a.shot_id: continue
        if job.get("status")=="succeeded": continue
        if job.get("status")=="failed" and job.get("attempts",0)>=3: continue
        if job.get("status") == "submitting":
            raise RuntimeError(f"{job['shot_id']}: ambiguous submission; reconcile client_id {job.get('client_id')} against ComfyUI queue/history before retrying")
        pid=job.get("prompt_id") if job.get("status")=="running" else None
        if not pid:
            workflow=json.loads((book/job.get("workflow", "comfyui/workflow_api.json")).read_text())
            bindings=json.loads((book/job.get("bindings", "comfyui/bindings.json")).read_text())
            wf=copy.deepcopy(workflow); inputs=job["inputs"]
            values={"prompt":inputs["prompt"],"negative_prompt":inputs.get("negative_prompt",""),"seed":inputs.get("seed",0),"output_prefix":f"{drive_prefix}/{job['shot_id']}"}
            if "duration_seconds" in inputs: values["duration_seconds"]=inputs["duration_seconds"]
            if inputs.get("reference_image"): values["reference_image"]=upload(base,book/inputs["reference_image"])
            for key,value in values.items():
                if key in bindings: set_binding(wf,bindings[key],value)
            client_id=uuid.uuid4().hex
            job.update(status="submitting",client_id=client_id); save_manifest(mp,jobs)
            res=request_json(base+"/prompt","POST",{"prompt":wf,"client_id":client_id}); pid=res["prompt_id"]
            job.update(prompt_id=pid,status="running",attempts=job.get("attempts",0)+1); save_manifest(mp,jobs)
        deadline=time.time()+a.timeout; history=None
        while time.time()<deadline:
            state=request_json(base+"/history/"+pid)
            if pid in state: history=state[pid]; break
            time.sleep(a.poll)
        if not history:
            job.update(error="history timeout; retain prompt_id for resume"); save_manifest(mp,jobs)
            raise TimeoutError(f"{job['shot_id']}: history timeout; prompt remains resumable")
        if history.get("status",{}).get("status_str") == "error":
            job.update(status="failed",error=history.get("status")); save_manifest(mp,jobs); continue
        files=[]
        for node in history.get("outputs",{}).values(): files.extend(node.get("videos",[])+node.get("gifs",[]))
        files=[f for f in files if Path(f.get("filename", "")).suffix.lower() in {".mp4", ".webm", ".mov"} and f.get("type", "output") == "output"]
        if not files: job.update(status="failed",error="no output files"); save_manifest(mp,jobs); continue
        f=files[0]; subfolder=f.get("subfolder","").strip("/")
        if subfolder != drive_prefix and not subfolder.startswith(drive_prefix+"/"):
            job.update(status="failed",error=f"output is outside configured Drive prefix: {subfolder!r}")
            save_manifest(mp,jobs); continue
        remote=Path(a.drive_root)/subfolder/f["filename"]
        if Path(f["filename"]).name != f["filename"] or ".." in Path(subfolder).parts or not remote.resolve().is_relative_to((Path(a.drive_root)/drive_prefix).resolve()):
            job.update(status="failed",error="unsafe output path"); save_manifest(mp,jobs); continue
        remote_output=str(remote)
        job.update(status="succeeded",remote_output=remote_output,remote_filename=f["filename"],remote_subfolder=subfolder,error=None)
        save_manifest(mp,jobs)
    selected=[j for j in jobs if not a.shot_id or j["shot_id"] == a.shot_id]
    failed=[j for j in selected if j.get("required",True) and j.get("status")!="succeeded"]
    print("SUCCESS",len(selected)-len(failed),"FAILED",len(failed)); raise SystemExit(bool(failed))

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--base-url",required=True); ap.add_argument("--book-dir",required=True)
    ap.add_argument("--poll",type=float,default=3); ap.add_argument("--timeout",type=int,default=3600)
    ap.add_argument("--lock-file",help="shared lock for a single ComfyUI endpoint")
    ap.add_argument("--drive-root",default="/content/drive/MyDrive/vidio",help="Drive directory configured as ComfyUI output root")
    ap.add_argument("--drive-output-prefix",help="output subdirectory below --drive-root; defaults to books/<book>/video/shots")
    ap.add_argument("--shot-id",help="run only one shot; used for Drive-output preflight")
    a=ap.parse_args(); base=a.base_url.split("#")[0].rstrip("/")
    lock_path=Path(a.lock_file) if a.lock_file else Path("/tmp")/("bookdash-comfy-"+hashlib.sha256(base.encode()).hexdigest()[:16]+".lock")
    lock_path.parent.mkdir(parents=True,exist_ok=True)
    with lock_path.open("a+") as lock:
        print("WAITING_FOR_COMFY_LOCK",lock_path,flush=True)
        deadline=time.monotonic()+a.timeout
        while True:
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB); break
            except BlockingIOError:
                if time.monotonic() >= deadline: raise TimeoutError("ComfyUI endpoint lock timeout")
                time.sleep(a.poll)
        lock.seek(0); lock.truncate(); lock.write(str(os.getpid())); lock.flush()
        print("ACQUIRED_COMFY_LOCK",lock_path,flush=True)
        run_batch(a)
if __name__=="__main__": main()
