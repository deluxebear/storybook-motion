"""Deterministic, duration-aware prompt preparation after TTS (no model service)."""
from pathlib import Path

from production_plan import digest, read, relative
from projectctl import atomic_json

VERSION = 1
LOCKED = {"submitting", "queued", "running", "succeeded"}


def refine_video_prompts(root):
    root = Path(root)
    path = root / "planning/video_manifest.json"
    data = read(path)
    audio = {row["audio_id"]: row for row in read(root / "planning/tts_manifest.json")["jobs"]}
    changed = []
    for job in data["jobs"]:
        workflow = read(relative(root, job["workflow"]))
        if not any(n["class_type"] == "LTXVConditioning" for n in workflow.values()):
            continue
        if job.get("status") in LOCKED or job.get("prompt_id") or job.get("post_attempts", 0):
            continue
        duration = float(job["inputs"]["duration_seconds"])
        timeline, cursor = [], 0.0
        for aid in job.get("audio_ids", []):
            row = audio[aid]
            seconds = row.get("seconds")
            if row.get("status") != "succeeded" or not isinstance(seconds, (int, float)) or not 0 < seconds < float("inf"):
                raise ValueError(f"{job['shot_id']}: TTS timing unavailable for {aid}")
            timeline.append({"audio_id": aid, "start": cursor, "end": cursor + seconds,
                             "text": row["text"], "role_id": row["role_id"]})
            cursor += seconds
        if duration < cursor:
            raise ValueError(f"{job['shot_id']}: video shorter than narration")
        source = {"version": VERSION, "inputs": job["inputs"], "timeline": timeline}
        fingerprint = digest(source)
        if job.get("prompt_refinement", {}).get("input_hash") == fingerprint:
            continue
        action_end = cursor if timeline else max(0, duration - 1)
        budget = ("Use one simple continuous action." if action_end <= 5 else
                  "Develop only the existing action in a few gentle consecutive phases; use natural pauses rather than adding new events.")
        prompt = (
            f"Create a single continuous {duration:g}-second storybook shot from the supplied first image. "
            "Preserve its illustration medium, colors, characters and spatial composition. "
            f"Visual intent: {job['inputs']['prompt'].strip()} "
            f"Pacing: {budget} Let the described action unfold naturally during the first {action_end:g} seconds. "
            f"From {action_end:g} to {duration:g} seconds, settle into a calm pose with subtle environmental motion. "
            "Keep camera movement consistent with the visual intent. "
            "The following narration is timing context, not visible text or spoken dialogue to generate. "
        )
        if timeline:
            prompt += "Narration timeline: " + " ".join(
                f"[{r['start']:.2f}-{r['end']:.2f}s] {r['text']}" for r in timeline)
        else:
            prompt += "This shot has no narration."
        prompt += " Keep the shot free of captions, added characters, scene cuts and invented story events."
        job["prompt_refinement"] = {"method": "duration_rules", "version": VERSION,
                                    "input_hash": fingerprint, "prompt": prompt,
                                    "duration_seconds": duration, "timeline": timeline}
        changed.append(job["shot_id"])
    if changed:
        atomic_json(path, data)
    atomic_json(root / "planning/video_prompt_refinements.json", {
        "method": "duration_rules", "version": VERSION,
        "shots": {j["shot_id"]: j["prompt_refinement"] for j in data["jobs"] if "prompt_refinement" in j}})
    return {"changed": changed}
