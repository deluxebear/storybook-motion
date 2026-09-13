#!/usr/bin/env python3
"""Submit a complete ComfyUI batch first, then reconcile all prompts together."""
import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import mimetypes
import os
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path


TRANSPORT_ERRORS = (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError)


@dataclass(frozen=True)
class BatchContext:
    base: str
    book: Path
    drive_prefix: str
    drive_root: Path
    manifest_path: Path
    restart_confirmed: bool = False
    reference_mode: str = "upload"
    drive_input_prefix: str = "vidio"

    def save(self, jobs):
        save_manifest(self.manifest_path, jobs)


def raw_request(url, method="GET", data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    tries = 1 if method == "POST" else 3
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if not 500 <= exc.code < 600 or attempt == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == tries - 1:
                raise
        time.sleep(2 * (attempt + 1))


def request_json(url, method="GET", obj=None):
    data = None if obj is None else json.dumps(obj).encode()
    return json.loads(raw_request(url, method, data, {"Content-Type": "application/json"}))


def set_binding(workflow, binding, value):
    node = str(binding["node"])
    key = binding["input"]
    if node not in workflow or key not in workflow[node].get("inputs", {}):
        raise KeyError(f"unresolved binding {node}.{key}")
    workflow[node]["inputs"][key] = value


def upload(base, path):
    boundary = "----codex" + uuid.uuid4().hex
    content = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    head = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{path.name}"\r\n'
            f"Content-Type: {mime}\r\n\r\n").encode()
    body = head + content + f"\r\n--{boundary}--\r\n".encode()
    response = raw_request(base + "/upload/image", "POST", body,
                           {"Content-Type": f"multipart/form-data; boundary={boundary}"}, 180)
    return json.loads(response)["name"]


def save_manifest(path, jobs):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps({"jobs": jobs}, ensure_ascii=False, indent=2) + "\n")
    os.replace(temporary, path)


def selected_jobs(jobs, shot_id=None):
    return [job for job in jobs if not shot_id or job["shot_id"] == shot_id]


def prompt_record(record):
    """Return (prompt_id, client_id) from a ComfyUI queue/history prompt tuple."""
    if not isinstance(record, list) or len(record) < 2:
        return None, None
    extra = record[3] if len(record) > 3 and isinstance(record[3], dict) else {}
    return record[1], extra.get("client_id")


def queue_snapshot(payload):
    prompts = {}
    for key, status in (("queue_running", "running"), ("queue_pending", "queued")):
        for record in payload.get(key, []):
            prompt_id, client_id = prompt_record(record)
            if prompt_id:
                prompts[prompt_id] = {"status": status, "client_id": client_id}
    return prompts


def history_clients(payload):
    clients = {}
    for prompt_id, history in payload.items():
        recorded_id, client_id = prompt_record(history.get("prompt", []))
        if client_id:
            clients.setdefault(client_id, []).append(recorded_id or prompt_id)
    return clients


def mark_lost_after_restart(job, base):
    prompt_id = job.pop("prompt_id", None)
    client_id = job.get("client_id")
    if prompt_id:
        job.setdefault("previous_prompt_ids", []).append(prompt_id)
    if client_id:
        job.setdefault("previous_client_ids", []).append(client_id)
    job.update(status="failed", error=f"prompt absent after endpoint changed to {base}; eligible for bounded retry")


def reconcile_jobs(context, jobs, selected):
    candidates = [job for job in selected
                  if job.get("status") in {"submitting", "queued", "running"}
                  or (job.get("status") == "failed" and job.get("prompt_id"))]
    if not candidates:
        return
    try:
        queue = queue_snapshot(request_json(context.base + "/queue"))
        history_payload = request_json(context.base + "/history")
        history = history_clients(history_payload)
    except TRANSPORT_ERRORS as exc:
        for job in candidates:
            job["error"] = f"submission reconciliation unavailable; retain client_id: {exc}"
        context.save(jobs)
        return
    clients = {}
    for prompt_id, record in queue.items():
        if record["client_id"]:
            clients.setdefault(record["client_id"], []).append(prompt_id)
    for client_id, prompt_ids in history.items():
        clients.setdefault(client_id, []).extend(prompt_ids)
    for job in candidates:
        if job.get("status") == "submitting":
            matches = sorted(set(clients.get(job.get("client_id"), [])))
            if len(matches) == 1:
                job.update(status=queue.get(matches[0], {}).get("status", "queued"),
                           prompt_id=matches[0], error=None)
            elif (context.restart_confirmed or
                  (job.get("endpoint") and job["endpoint"] != context.base)) and not matches:
                mark_lost_after_restart(job, context.base)
            else:
                job["error"] = ("ambiguous submission requires reconciliation: "
                                f"client_id matched {len(matches)} prompts")
            continue
        prompt_id = job.get("prompt_id")
        if prompt_id in queue:
            job.update(status=queue[prompt_id]["status"], error=None)
        elif prompt_id in history_payload:
            finish_job(context, job, history_payload[prompt_id])
        elif context.restart_confirmed or (job.get("endpoint") and job["endpoint"] != context.base):
            mark_lost_after_restart(job, context.base)
    context.save(jobs)


def build_workflow(context, job):
    workflow = json.loads((context.book / job.get("workflow", "comfyui/workflow_api.json")).read_text())
    bindings = json.loads((context.book / job.get("bindings", "comfyui/bindings.json")).read_text())
    workflow = copy.deepcopy(workflow)
    inputs = job["inputs"]
    values = {
        "prompt": job.get("prompt_refinement", {}).get("prompt", inputs["prompt"]),
        "seed": inputs.get("seed", 0),
        "output_prefix": f"{context.drive_prefix}/{job['shot_id']}",
    }
    negative_prompt = job.get("prompt_refinement", {}).get("negative_prompt", inputs.get("negative_prompt"))
    if negative_prompt:
        values["negative_prompt"] = negative_prompt
    if "duration_seconds" in inputs:
        values["duration_seconds"] = inputs["duration_seconds"]
        if any(n["class_type"] == "LTXVConditioning" for n in workflow.values()):
            duration = float(values["duration_seconds"])
            if not duration.is_integer() or duration <= 0:
                raise ValueError("LTX duration must be positive whole seconds at 24 fps")
            values["duration_seconds"] = int(duration)
    if inputs.get("reference_image"):
        source = (context.book / inputs["reference_image"]).resolve()
        if not source.is_relative_to(context.book.resolve()):
            raise ValueError(f"reference image escapes book: {inputs['reference_image']}")
        if context.reference_mode == "drive":
            # ComfyUI's input/vidio symlink points at the shared Drive root.
            # LoadImage deliberately receives a path relative to input/, never
            # an arbitrary absolute filesystem path.
            relative = source.relative_to(context.book.resolve()).as_posix()
            values["reference_image"] = (
                f"{context.drive_input_prefix}/books/{context.book.name}/{relative}"
            )
        else:
            values["reference_image"] = upload(context.base, source)
    for key, value in values.items():
        if key in bindings:
            set_binding(workflow, bindings[key], value)
    return workflow


def submission_limit_reached(job):
    return max(job.get("attempts", 0), job.get("post_attempts", 0)) >= 3


def submit_batch(context, jobs, selected):
    """Submit every eligible job without waiting for any generation to finish."""
    for job in selected:
        if job.get("status") in {"succeeded", "queued", "running", "submitting"}:
            continue
        if job.get("status") == "failed" and submission_limit_reached(job):
            continue
        try:
            workflow = build_workflow(context, job)
        except Exception as exc:
            job.update(status="failed", error=f"workflow preparation failed: {exc}")
            context.save(jobs)
            print("PREPARE_ERROR", job["shot_id"], repr(exc), flush=True)
            continue
        client_id = uuid.uuid4().hex
        for key in ("prompt_id", "remote_output", "remote_filename", "remote_subfolder"):
            job.pop(key, None)
        post_attempts = max(job.get("attempts", 0), job.get("post_attempts", 0)) + 1
        job.update(status="submitting", client_id=client_id, endpoint=context.base,
                   post_attempts=post_attempts, error=None)
        context.save(jobs)
        try:
            response = request_json(context.base + "/prompt", "POST", {"prompt": workflow, "client_id": client_id})
            job.update(prompt_id=response["prompt_id"], status="queued",
                       attempts=job.get("attempts", 0) + 1, submitted_at=time.time())
            context.save(jobs)
            print("SUBMITTED", job["shot_id"], job["prompt_id"], flush=True)
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500:
                job.update(status="failed", error=f"submission rejected by ComfyUI: HTTP {exc.code}")
            else:
                job["error"] = f"submission response uncertain: HTTP {exc.code}; retain client_id"
            context.save(jobs)
            print("SUBMIT_ERROR", job["shot_id"], repr(exc), flush=True)
        except Exception as exc:
            job["error"] = f"submission response uncertain; retain client_id: {exc}"
            context.save(jobs)
            print("SUBMIT_ERROR", job["shot_id"], repr(exc), flush=True)


def finish_job(context, job, history):
    if history.get("status", {}).get("status_str") == "error":
        job.update(status="failed", error=history.get("status"))
        return
    files = []
    for node in history.get("outputs", {}).values():
        files.extend(node.get("videos", []) + node.get("gifs", []) + node.get("images", []))
    files = [item for item in files
             if Path(item.get("filename", "")).suffix.lower() in {".mp4", ".webm", ".mov"}
             and item.get("type", "output") == "output"]
    if not files:
        job.update(status="failed", error="no output files")
        return
    output = files[0]
    subfolder = output.get("subfolder", "").strip("/")
    if subfolder != context.drive_prefix and not subfolder.startswith(context.drive_prefix + "/"):
        job.update(status="failed", error=f"output is outside configured Drive prefix: {subfolder!r}")
        return
    remote = context.drive_root / subfolder / output["filename"]
    expected_root = (context.drive_root / context.drive_prefix).resolve()
    if (Path(output["filename"]).name != output["filename"] or ".." in Path(subfolder).parts
            or not remote.resolve().is_relative_to(expected_root)):
        job.update(status="failed", error="unsafe output path")
        return
    job.update(status="succeeded", remote_output=str(remote), remote_filename=output["filename"],
               remote_subfolder=subfolder, error=None, completed_at=time.time())


def monitor_batch(context, jobs, selected, timeout, poll, missing_grace=30):
    """Track all acknowledged prompts; one transport failure never aborts the batch."""
    active = {job["prompt_id"]: job for job in selected
              if job.get("status") in {"queued", "running"} and job.get("prompt_id")}
    if not active:
        return
    started = time.monotonic()
    deadline = started + timeout * max(1, len(active))
    last_contact = started
    backoff = poll
    missing_since = {}
    while active:
        now = time.monotonic()
        if now >= deadline:
            for job in active.values():
                job["error"] = "batch tracking timeout; retain prompt_id for resume"
            context.save(jobs)
            raise TimeoutError("batch tracking timeout; prompt IDs remain resumable")
        try:
            queue = queue_snapshot(request_json(context.base + "/queue"))
            last_contact = now
            backoff = poll
        except TRANSPORT_ERRORS as exc:
            if now - last_contact >= timeout:
                for job in active.values():
                    job["error"] = f"endpoint unavailable; retain prompt_id for resume: {exc}"
                context.save(jobs)
                raise TimeoutError("ComfyUI endpoint remained unavailable; prompt IDs remain resumable") from exc
            print("TRACK_RETRY", repr(exc), flush=True)
            time.sleep(backoff)
            backoff = min(max(poll, backoff * 2), 30)
            continue
        changed = False
        for prompt_id, job in list(active.items()):
            if prompt_id in queue:
                status = queue[prompt_id]["status"]
                if job.get("status") != status or job.get("error"):
                    job.update(status=status, error=None)
                    changed = True
                missing_since.pop(prompt_id, None)
                continue
            try:
                payload = request_json(context.base + "/history/" + prompt_id)
                last_contact = time.monotonic()
            except TRANSPORT_ERRORS as exc:
                print("HISTORY_RETRY", job["shot_id"], repr(exc), flush=True)
                continue
            if prompt_id in payload:
                finish_job(context, job, payload[prompt_id])
                active.pop(prompt_id)
                missing_since.pop(prompt_id, None)
                changed = True
                print("FINISHED", job["shot_id"], job["status"], flush=True)
                continue
            first_missing = missing_since.setdefault(prompt_id, time.monotonic())
            if time.monotonic() - first_missing >= missing_grace:
                job["error"] = "prompt absent from queue and history; reconciliation required"
                active.pop(prompt_id)
                changed = True
                print("MISSING", job["shot_id"], prompt_id, flush=True)
        if changed:
            context.save(jobs)
        if active:
            time.sleep(poll)


@contextlib.contextmanager
def endpoint_submission_lock(args, base):
    configured = getattr(args, "lock_file", None)
    lock_path = (Path(configured) if configured else
                 Path("/tmp") / ("bookdash-comfy-" + hashlib.sha256(base.encode()).hexdigest()[:16] + ".lock"))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        print("WAITING_FOR_COMFY_LOCK", lock_path, flush=True)
        deadline = time.monotonic() + args.timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("ComfyUI endpoint lock timeout")
                time.sleep(args.poll)
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        print("ACQUIRED_COMFY_LOCK", lock_path, flush=True)
        yield


def run_batch(args):
    base = args.base_url.split("#")[0].rstrip("/")
    book = Path(args.book_dir).resolve()
    request_json(base + "/system_stats")
    drive_prefix = (getattr(args, "drive_output_prefix", None)
                    or f"books/{book.name}/video/shots").strip("/")
    manifest_path = book / "planning/video_manifest.json"
    context = BatchContext(base, book, drive_prefix, Path(args.drive_root), manifest_path,
                           getattr(args, "comfy_restarted", False),
                           getattr(args, "reference_mode", "upload"),
                           getattr(args, "drive_input_prefix", "vidio").strip("/"))
    data = json.loads(manifest_path.read_text())
    jobs = data["jobs"] if isinstance(data, dict) else data
    selected = selected_jobs(jobs, getattr(args, "shot_id", None))
    with endpoint_submission_lock(args, base):
        reconcile_jobs(context, jobs, selected)
        submit_batch(context, jobs, selected)
    monitor_batch(context, jobs, selected, args.timeout, args.poll, getattr(args, "missing_grace", 30))
    failed = [job for job in selected if job.get("required", True) and job.get("status") != "succeeded"]
    print("SUCCESS", len(selected) - len(failed), "FAILED", len(failed))
    raise SystemExit(bool(failed))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--book-dir", required=True)
    parser.add_argument("--poll", type=float, default=3)
    parser.add_argument("--timeout", type=int, default=3600)
    parser.add_argument("--missing-grace", type=float, default=30,
                        help="seconds before an acknowledged prompt missing from queue/history needs reconciliation")
    parser.add_argument("--comfy-restarted", action="store_true",
                        help="confirm the endpoint restarted so absent old prompts may be retried")
    parser.add_argument("--lock-file", help="shared lock for a single ComfyUI endpoint")
    parser.add_argument("--drive-root", default="/content/drive/MyDrive/vidio",
                        help="Drive directory configured as ComfyUI output root")
    parser.add_argument("--drive-output-prefix",
                        help="output subdirectory below --drive-root; defaults to books/<book>/video/shots")
    parser.add_argument("--reference-mode", choices=("upload", "drive"), default="upload",
                        help="upload reference images, or read them through ComfyUI input/vidio mapped to Drive")
    parser.add_argument("--drive-input-prefix", default="vidio",
                        help="relative ComfyUI input/ path that maps to --drive-root in --reference-mode drive")
    parser.add_argument("--shot-id", help="run only one shot; used for Drive-output preflight")
    run_batch(parser.parse_args())


if __name__ == "__main__":
    main()
