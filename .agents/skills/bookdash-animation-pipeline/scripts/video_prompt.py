"""Deterministic, duration-aware prompt preparation after TTS (no model service)."""
from pathlib import Path

from production_plan import digest, read, relative
from projectctl import atomic_json

VERSION = 4
LOCKED = {"submitting", "queued", "running", "succeeded"}
IDENTITY_NEGATIVE = (
    "new or replacement character, character redesign, changed face, changed facial features, changed age, "
    "changed skin tone, changed hair, changed clothing, changed species, extra limbs, added anatomy or facial "
    "features absent from the reference image, duplicate subject, changed subject count, style drift, "
    "photorealistic restyling, slideshow, presentation animation, intro, outro, transition, page turn, "
    "wipe, dissolve, fade in, fade out, pop in, pop out, subject flying into frame, subject flying out of frame"
)


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
        budget = ("Use one clear continuous action with readable progress." if action_end <= 5 else
                  "Develop only the existing action in a few chronological phases with natural pauses; do not add a new event.")
        visual_intent = job["inputs"]["prompt"].strip().rstrip()
        if visual_intent and visual_intent[-1] not in ".!?":
            visual_intent += "."
        prompt = (
            f"{visual_intent} "
            "The first image alone defines every subject's identity and count. Keep its silhouettes, collage construction, "
            "existing or absent facial features, colors, proportions, clothing, and accessories. Never turn abstract or collage "
            "figures into conventional or realistic characters or invent unseen anatomy. "
            "Animate principal subjects and action objects; camera motion cannot be the sole movement. Make the action clear; "
            "secondary motion may affect only existing scene elements. "
            "Keep the source medium, palette, linework, and background identity. "
            "Start immediately in the supplied composition and remain in the same continuous scene. No intro, outro, transition, "
            "page turn, wipe, pop-in, pop-out, or subject flying into or out of frame unless the described action explicitly requires it. "
            f"Create one continuous {duration:g}-second shot. {budget} "
            f"Let the main action develop during the first {action_end:g} seconds. "
            f"From {action_end:g} to {duration:g} seconds, continue natural follow-through and living secondary motion through the final frame; do not freeze. "
        )
        prompt += "Keep the shot free of captions, added characters, scene cuts and invented story events."
        negative = ", ".join(filter(None, (
            job["inputs"].get("negative_prompt", "").strip().rstrip(" ,"), IDENTITY_NEGATIVE)))
        job["prompt_refinement"] = {"method": "duration_rules", "version": VERSION,
                                    "input_hash": fingerprint, "prompt": prompt,
                                    "negative_prompt": negative,
                                    "duration_seconds": duration, "timeline": timeline}
        changed.append(job["shot_id"])
    if changed:
        atomic_json(path, data)
    atomic_json(root / "planning/video_prompt_refinements.json", {
        "method": "duration_rules", "version": VERSION,
        "shots": {j["shot_id"]: j["prompt_refinement"] for j in data["jobs"] if "prompt_refinement" in j}})
    return {"changed": changed}
