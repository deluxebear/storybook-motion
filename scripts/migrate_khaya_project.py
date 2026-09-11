#!/usr/bin/env python3
"""Migrate the already-prepared Khaya assets into the resumable pipeline schema."""

import datetime as dt
import hashlib
import json
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "books" / "khaya-wants-to-row"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


book_manifest = json.loads((ROOT / "book_manifest.json").read_text(encoding="utf-8"))
archive = ROOT / "source/original/khaya-wants-to-row_e-book_download.zip"
archive_sha = hashlib.sha256(archive.read_bytes()).hexdigest()

write_json(
    ROOT / "planning/book.json",
    {
        "schema_version": 1,
        "book_slug": "khaya-wants-to-row",
        "book_url": "https://bookdash.org/books/khaya-wants-to-row/",
        "title": "Khaya Wants to Row",
        "language": "en",
        "license": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "creators": {
            "writer": "Zandile Mnisi",
            "illustrator": "Maria Lebedeva",
            "designer": "Wilna Combrinck",
            "editor": "Tania Kliphuis",
        },
        "sources": [
            {
                "kind": "user_provided_bookdash_ebook_zip",
                "path": "source/original/khaya-wants-to-row_e-book_download.zip",
                "sha256": archive_sha,
                "download_skipped": True,
            },
            {
                "kind": "english_pdf_from_archive",
                "path": "source/english/khaya-wants-to-row_en.pdf",
            },
        ],
        "ordered_story_pages": [f"source/pages/scene_{i:03d}.jpg" for i in range(1, 13)],
        "source_page_numbers": list(range(4, 16)),
    },
)

characters = [
    {
        "role_id": "narrator",
        "display_name": "Narrator",
        "story_function": "Warm external storyteller",
        "visual_traits": ["not shown"],
        "voice_traits": ["adult woman", "warm", "clear", "gentle South African English character"],
        "languages": ["en"],
        "source_pages": list(range(4, 16)),
    },
    {
        "role_id": "khaya",
        "display_name": "Khaya",
        "story_function": "Young protagonist who dreams of rowing",
        "visual_traits": ["young Black boy", "short dark curls", "orange-and-white striped shirt", "dark shorts"],
        "voice_traits": ["young boy", "bright", "curious", "energetic"],
        "languages": ["en"],
        "source_pages": list(range(4, 16)),
    },
    {
        "role_id": "gogo",
        "display_name": "Gogo Lucy",
        "story_function": "Affectionate grandmother and encourager",
        "visual_traits": ["elderly Black woman", "pale blue headscarf", "pale blue dress", "striped apron"],
        "voice_traits": ["elderly woman", "wise", "affectionate", "reassuring"],
        "languages": ["en"],
        "source_pages": [4, 5, 6, 9],
    },
    {
        "role_id": "mkhulu",
        "display_name": "Mkhulu Majozi",
        "story_function": "Supportive grandfather who introduces rowing",
        "visual_traits": ["elderly Black man", "bald", "small grey moustache", "pale blue tunic", "ochre trousers"],
        "voice_traits": ["elderly man", "calm", "dependable", "affectionate"],
        "languages": ["en"],
        "source_pages": [4, 7, 9, 11, 14],
    },
]
write_json(ROOT / "planning/characters.json", {"characters": characters})

legacy_specs = json.loads((ROOT / "voices/candidates/voice_specs.json").read_text(encoding="utf-8"))
selections = {"narrator": 2, "khaya": 3, "gogo": 2, "mkhulu": 3}
voice_specs = {}
for role, spec in legacy_specs.items():
    candidate = selections[role]
    voice_specs[role] = {
        "reference_text": spec["text"],
        "language": "English",
        "instruct": spec["instruct"],
        "candidates": 3,
        "base_seed": 20260909,
        "selection_rule": "median duration candidate; tie resolved by lower candidate number",
        "selected_candidate": f"voices/candidates/{role}/{role}_{candidate:02d}.wav",
        "selected_output": f"voices/selected/{role}.wav",
    }
write_json(ROOT / "planning/voice_specs.json", voice_specs)

legacy_rows = [json.loads(line) for line in (ROOT / "audio/index-tts-2.5/utterances_en.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
scene_to_shot = {f"scene_{i:03d}": f"S{i:03d}_SH001" for i in range(1, 13)}
tts_jobs = []
audio_by_shot: dict[str, list[str]] = {}
for index, row in enumerate(legacy_rows, 1):
    shot_id = scene_to_shot[row["scene_id"]]
    audio_id = f"{shot_id}_A{len(audio_by_shot.setdefault(shot_id, [])) + 1:03d}"
    audio_by_shot[shot_id].append(audio_id)
    tts_jobs.append(
        {
            "audio_id": audio_id,
            "scene_id": shot_id.split("_SH")[0],
            "shot_id": shot_id,
            "role_id": row["speaker"],
            "text": row["text"],
            "lang": "EN",
            "delivery_note": row.get("delivery", "natural storybook delivery"),
            "emotion_vector": row["emo_vector"],
            "duration_factor": row["duration_factor"],
            "reference_voice": f"voices/selected/{row['speaker']}.wav",
            "max_text_tokens_per_segment": 60,
            "output": f"audio/lines/{audio_id}.wav",
            "required": True,
            "status": "pending",
            "legacy_order": index,
        }
    )
write_json(ROOT / "planning/tts_manifest.json", {"jobs": tts_jobs})

motion_prompts = [
    "Slow sunrise light spreads across Joza township; faint chimney smoke drifts and the camera gently pushes toward Khaya with his grandparents. Preserve the hand-painted paper texture and exact character designs.",
    "Gogo stirs porridge while steam curls upward; the kettle gives a tiny wobble and Khaya leans forward eagerly. Gentle morning light, restrained natural motion, no camera distortion.",
    "Gogo gestures softly as an imagined translucent rowing boat glides along the blue hillside; Khaya watches with growing wonder. Preserve the sparse watercolor composition.",
    "The old television flickers with rowers pulling their oars in rhythm; Mkhulu turns his head toward Khaya off frame. Subtle seated body movement and static room composition.",
    "On the television, rowers sweep their oars in synchronized strokes; Khaya lifts one hand excitedly toward the screen. Gentle parallax only, preserve face and braided hair silhouette.",
    "Khaya bounces and claps once while Mkhulu and Gogo sway and smile on either side. Joyful but controlled movement, fixed full-body proportions, white background remains clean.",
    "Khaya splashes through the painted blue puddle, makes a small rowing gesture, and a tiny paper boat drifts at lower left. Playful tracking motion without adding objects.",
    "In moonlit blue, sleeping Khaya curls under the blanket; the blanket rises subtly with breathing and the moon glow drifts across the room. Calm, dreamlike, no movement inside the blank speech bubble.",
    "The minibus travels smoothly along the rolling blue road toward the distant orange oars; landscape layers move with gentle parallax. Keep the minimalist watercolor shapes.",
    "The rowing crew advances across the water with synchronized oar strokes while Khaya waves from shore; soft ripples trail the boat. Preserve every figure and the orange-blue palette.",
    "Khaya hugs Mkhulu tightly; Mkhulu lowers an arm around him as the river lines drift gently behind them. Warm affectionate pause, stable anatomy and faces.",
    "Khaya imagines himself rowing across flowing blue water; his arms make a small steady oar stroke and the camera slowly pulls back. Serene triumphant ending, preserve paper-grain illustration style.",
]
negative = "photorealistic, 3D render, style change, extra people, extra limbs, malformed hands, face distortion, text, subtitles, logo, watermark, flicker, jitter, aggressive camera motion, crop"
storyboard = []
video_jobs = []
for i, scene in enumerate(book_manifest["scenes"], 1):
    scene_id = f"S{i:03d}"
    shot_id = f"{scene_id}_SH001"
    image = f"images/references/scene_{i:03d}.jpg"
    storyboard.append(
        {
            "scene_id": scene_id,
            "shot_id": shot_id,
            "source_pages": [scene["source_page"]],
            "duration_seconds": scene["duration_seconds"],
            "visual_prompt": motion_prompts[i - 1],
            "negative_prompt": negative,
            "reference_images": [image],
            "continuity_in": "Preserve established watercolor style and recurring character design.",
            "continuity_out": "End on a stable composition suitable for continuity or a clean cut.",
            "audio_ids": audio_by_shot[shot_id],
        }
    )
    video_jobs.append(
        {
            "shot_id": shot_id,
            "workflow": "comfyui/workflow_api.json",
            "bindings": "comfyui/bindings.json",
            "inputs": {
                "prompt": motion_prompts[i - 1],
                "negative_prompt": negative,
                "reference_image": image,
                "seed": 2026090900 + i,
                "output_prefix": f"books/khaya-wants-to-row/video/shots/{shot_id}",
            },
            "output": f"video/shots/{shot_id}.mp4",
            "required": True,
            "status": "pending",
            "attempts": 0,
        }
    )
write_json(ROOT / "planning/storyboard.json", {"shots": storyboard})
write_json(ROOT / "planning/video_manifest.json", {"jobs": video_jobs})

now = dt.datetime.now(dt.timezone.utc).isoformat()
write_json(
    ROOT / "status.json",
    {
        "schema_version": 1,
        "book_slug": "khaya-wants-to-row",
        "book_url": "https://bookdash.org/books/khaya-wants-to-row/",
        "updated_at": now,
        "stages": {
            "acquire": {"status": "succeeded", "archive_sha256": archive_sha, "download_skipped": True, "ordered_pages": 12},
            "plan": {"status": "succeeded", "scenes": 12, "shots": 12, "tts_rows": len(tts_jobs)},
            "voice_design": {"status": "succeeded", "roles": 4, "candidates": 12, "selection": selections},
            "tts": {"status": "pending", "session_name": "bookdash-khaya-wants-to-row-tts", "required": len(tts_jobs)},
            "comfy_endpoint": {"status": "pending"},
            "video": {"status": "pending", "required": 12},
            "final": {"status": "pending"},
        },
    },
)

print(f"Prepared {ROOT}: {len(tts_jobs)} TTS rows, {len(storyboard)} shots")
