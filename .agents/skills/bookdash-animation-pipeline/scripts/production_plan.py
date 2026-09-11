"""Versioned creative input; compile runtime manifests without losing progress."""
import hashlib
import json
import math
import re
from pathlib import Path

from projectctl import atomic_json, slug_from_url


TTS_VIDEO_PADDING_SECONDS = 1.0
MIN_VIDEO_SECONDS = 5
TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "templates/minimax_h3_i2v"


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def workflow_schema(workflow):
    """Return the value-independent node/input/edge contract of an API workflow."""
    node_ids = set(workflow)
    nodes = {}
    for node_id, node in workflow.items():
        inputs = {}
        for name, value in node.get("inputs", {}).items():
            if (isinstance(value, list) and len(value) == 2 and
                    str(value[0]) in node_ids and isinstance(value[1], int)):
                inputs[name] = {"edge": [str(value[0]), value[1]]}
            else:
                inputs[name] = {"value_type": type(value).__name__}
        nodes[str(node_id)] = {"class_type": node.get("class_type"), "inputs": inputs}
    return nodes


def validate_workflow_template(workflow_path, bindings_path):
    actual_workflow = read(workflow_path)
    actual_bindings = read(bindings_path)
    for name in ("minimax_h3_i2v", "ltx2_5_i2v"):
        template = TEMPLATE_DIR.parent / name
        if workflow_schema(actual_workflow) == workflow_schema(read(template / "workflow_api.json")):
            if actual_bindings != read(template / "bindings.json"):
                raise ValueError(f"workflow bindings differ from {name} template")
            return name
    raise ValueError("workflow schema differs from supported templates: minimax_h3_i2v, ltx2_5_i2v")


def apply_tts_video_durations(root, padding=TTS_VIDEO_PADDING_SECONDS, minimum=MIN_VIDEO_SECONDS):
    """Set unsubmitted video durations from measured narration WAV durations."""
    root = Path(root)
    video_path = root / "planning/video_manifest.json"
    tts = read(root / "planning/tts_manifest.json")["jobs"]
    videos = read(video_path)["jobs"]
    audio = {row["audio_id"]: row for row in tts}
    changed, skipped = [], []
    for job in videos:
        audio_ids = job.get("audio_ids", [])
        if not audio_ids:  # Silent shots retain their creative planned duration.
            continue
        rows = []
        for audio_id in audio_ids:
            row = audio.get(audio_id)
            if row is None or row.get("status") != "succeeded":
                raise ValueError(f"TTS incomplete for video duration: {job['shot_id']} / {audio_id}")
            seconds = row.get("seconds")
            if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds <= 0:
                raise ValueError(f"missing measured TTS duration: {job['shot_id']} / {audio_id}")
            rows.append(row)
        target = max(float(minimum), float(math.ceil(sum(row["seconds"] for row in rows) + padding)))
        if job["inputs"].get("duration_seconds") == target:
            continue
        # Prompt IDs and outputs describe immutable submissions. Only future or
        # explicitly failed attempts may receive the corrected runtime input.
        if job.get("status", "pending") in {"submitting", "queued", "running", "succeeded"}:
            skipped.append(job["shot_id"])
            continue
        job["inputs"]["duration_seconds"] = target
        changed.append(job["shot_id"])
    if changed:
        atomic_json(video_path, {"jobs": videos})
    return {"changed": changed, "skipped_submitted": skipped}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_hash(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def relative(root, value):
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        raise ValueError(f"expected book-relative path: {value!r}")
    target = (root / value).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f"path escapes book: {value}")
    return target


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", value):
        raise ValueError(f"invalid ID: {value!r}")
    return value


def positive(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"expected positive finite number: {value!r}")


def validate(root, plan):
    if plan.get("schema_version") != 1:
        raise ValueError("production plan schema_version must be 1")
    slug = slug_from_url(plan["book_url"])
    if plan["book_slug"] != slug or root.name != slug:
        raise ValueError("book_slug, book URL and directory must agree")
    roles, shots = plan["roles"], plan["shots"]
    if not roles or not shots:
        raise ValueError("roles and shots must be nonempty")
    for role, spec in roles.items():
        identifier(role)
        for key in ("language", "reference_text", "instruct"):
            if not isinstance(spec.get(key), str) or not spec[key].strip():
                raise ValueError(f"{role}: missing {key}")
        if not isinstance(spec.get("candidates", 3), int) or spec.get("candidates", 3) < 1:
            raise ValueError(f"{role}: candidates must be a positive integer")
        if spec.get("selected_voice"):
            if not relative(root, spec["selected_voice"]).is_file():
                raise ValueError(f"missing selected voice: {role}")
    seen_shots, seen_audio = set(), set()
    for shot in shots:
        sid = identifier(shot["shot_id"])
        identifier(shot["scene_id"])
        if sid in seen_shots:
            raise ValueError(f"duplicate shot: {sid}")
        seen_shots.add(sid)
        video = shot["video"]
        if not video.get("prompt", "").strip():
            raise ValueError(f"{sid}: missing video prompt")
        positive(video["duration_seconds"])
        if not isinstance(video["seed"], int):
            raise ValueError(f"{sid}: seed must be an integer")
        if not relative(root, video["reference_image"]).is_file():
            raise ValueError(f"{sid}: missing reference image")
        for line in shot["lines"]:
            aid = identifier(line["audio_id"])
            if aid in seen_audio or line["role_id"] not in roles:
                raise ValueError(f"duplicate audio or unknown role: {aid}")
            seen_audio.add(aid)
            if not line.get("text", "").strip() or not line.get("lang", "").strip():
                raise ValueError(f"{aid}: text and lang are required")
            positive(line.get("duration_factor", 1))
            emotion = line.get("emotion_vector", [0] * 8)
            if len(emotion) != 8 or any(not isinstance(x, (int, float)) or not math.isfinite(x) or not 0 <= x <= 1 for x in emotion):
                raise ValueError(f"{aid}: invalid emotion_vector")
    wf = read(relative(root, plan["workflow"]))
    bindings = read(relative(root, plan["bindings"]))
    if not isinstance(wf, dict) or not wf or any(not isinstance(n, dict) or "class_type" not in n for n in wf.values()):
        raise ValueError("workflow must be ComfyUI API format")
    for key in ("prompt", "reference_image", "seed", "duration_seconds", "output_prefix"):
        if key not in bindings:
            raise ValueError(f"missing workflow binding: {key}")
    for key, binding in bindings.items():
        if binding["input"] not in wf.get(str(binding["node"]), {}).get("inputs", {}):
            raise ValueError(f"unresolved workflow binding: {key}")
    validate_workflow_template(relative(root, plan["workflow"]), relative(root, plan["bindings"]))
    return plan


def compile_plan(root):
    plan = validate(root, read(root / "planning/production_plan.json"))
    assets = {plan["workflow"], plan["bindings"]}
    assets.update(s["video"]["reference_image"] for s in plan["shots"])
    assets.update(s["selected_voice"] for s in plan["roles"].values() if s.get("selected_voice"))
    fingerprint = digest({"plan": plan, "assets": {p: file_hash(relative(root, p)) for p in sorted(assets)}})
    marker = root / "planning/compiled_plan.json"
    if marker.exists() and read(marker)["plan_hash"] != fingerprint:
        raise ValueError("plan changed after compilation; use a new project revision to preserve existing jobs")
    tts, video = [], []
    for shot in plan["shots"]:
        sid = shot["shot_id"]
        for line in shot["lines"]:
            tts.append({**line, "scene_id": shot["scene_id"], "shot_id": sid,
                        "reference_voice": f"voices/selected/{line['role_id']}.wav",
                        "output": f"audio/lines/{line['audio_id']}.wav", "required": True,
                        "status": "pending"})
        video.append({"shot_id": sid, "workflow": plan["workflow"], "bindings": plan["bindings"],
                      "inputs": shot["video"], "audio_ids": [x["audio_id"] for x in shot["lines"]],
                      "output": f"video/shots/{sid}.mp4", "required": True, "status": "pending", "attempts": 0})
    writes = []
    for name, rows, key in (("tts", tts, "audio_id"), ("video", video, "shot_id")):
        path = root / f"planning/{name}_manifest.json"
        if path.exists():
            old = read(path)
            previous = {x[key]: x for x in (old["jobs"] if isinstance(old, dict) else old)}
            if set(previous) != {x[key] for x in rows}:
                raise ValueError(f"{name}: existing IDs differ from plan; use a new revision")
            for row in rows:
                # Adoption is allowed only when creative inputs match exactly.
                prior = previous[row[key]]
                fields = ("text", "role_id", "lang", "reference_voice", "output", "emotion_vector", "duration_factor") if name == "tts" else ("inputs", "workflow", "bindings", "output")
                # Older manifests may omit an optional duration override even
                # when the current workflow exposes that binding.  Preserve
                # the Drive-side runtime configuration in this one compatible
                # case instead of treating it as a new creative revision.
                if name == "video":
                    prior_inputs = {k: v for k, v in prior["inputs"].items() if k != "duration_seconds"}
                    incoming_inputs = {k: v for k, v in row["inputs"].items() if k != "duration_seconds"}
                    if prior_inputs == incoming_inputs:
                        row["inputs"] = prior["inputs"]
                if any(prior.get(f) != row.get(f) for f in fields):
                    raise ValueError(f"{row[key]}: existing input differs from plan")
                row.update({k: v for k, v in prior.items() if k not in row or k in ("status", "attempts")})
        writes.append((path, {"jobs": rows}))
    for path, payload in writes:
        atomic_json(path, payload)
    atomic_json(root / "planning/voice_specs.json", plan["roles"])
    atomic_json(marker, {"plan_hash": fingerprint})
    return plan, fingerprint


def import_legacy(root, output):
    book = read(root / "planning/book.json")
    def jobs(name):
        data = read(root / f"planning/{name}_manifest.json")
        return data["jobs"] if isinstance(data, dict) else data
    audio, videos = jobs("tts"), jobs("video")
    used = set()
    shots = []
    for video in videos:
        lines = []
        for row in audio:
            if row["shot_id"] == video["shot_id"]:
                used.add(row["audio_id"])
                lines.append({k: row[k] for k in ("audio_id", "role_id", "text", "lang", "emotion_vector", "duration_factor", "delivery_note", "max_text_tokens_per_segment") if k in row})
        shots.append({"shot_id": video["shot_id"], "scene_id": next((r["scene_id"] for r in audio if r["shot_id"] == video["shot_id"]), video["shot_id"].split("_SH")[0]), "lines": lines, "video": video["inputs"]})
    if used != {r["audio_id"] for r in audio}:
        raise ValueError("orphan audio rows in legacy project")
    plan = {"schema_version": 1, "book_slug": book["book_slug"], "book_url": book["book_url"],
            "roles": read(root / "planning/voice_specs.json"), "shots": shots,
            "workflow": "comfyui/workflow_api.json", "bindings": "comfyui/bindings.json"}
    validate(root, plan)
    if output.exists():
        raise ValueError(f"refusing to overwrite {output}")
    atomic_json(output, plan)
    return plan
