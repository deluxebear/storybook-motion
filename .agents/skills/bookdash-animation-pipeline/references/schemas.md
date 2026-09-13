# Production plan and runtime state

Canonical Drive root: `MyDrive/vidio/books/<book_slug>/`. The local project directory has the same basename. Keep originals under `source/original/`, derived pages under `source/pages/`, and acquisition provenance in `planning/book.json`.

## Creative input: planning/production_plan.json

The model prepares this one versioned contract. Array order matters: `shots` is storyboard order; each shot's `lines` is spoken order. Multiple speakers per shot and silent shots (`lines: []`) are supported. A shot owns its lines, so execution does not infer audio/video correspondence.

```json
{
  "schema_version": 1,
  "book_slug": "example",
  "book_url": "https://bookdash.org/books/example/",
  "workflow": "comfyui/workflow_api.json",
  "bindings": "comfyui/bindings.json",
  "roles": {
    "narrator": {
      "language": "English",
      "reference_text": "A little boat waited beside the river.",
      "instruct": "A fictional warm adult storyteller, clear and gentle, dry recording.",
      "candidates": 3,
      "base_seed": 20260909
    }
  },
  "shots": [
    {
      "scene_id": "S001",
      "shot_id": "S001_SH001",
      "source_pages": [1],
      "video": {
        "reference_image": "source/pages/page-01.jpg",
        "prompt": "The child leans forward and pulls the oar through the water; their arms and torso shift with the effort while the boat glides ahead and ripples spread behind it.",
        "negative_prompt": "flicker, changed character design",
        "duration_seconds": 5,
        "seed": 20260909
      },
      "lines": [
        {
          "audio_id": "S001_SH001_A001",
          "role_id": "narrator",
          "text": "A little boat waited beside the river.",
          "lang": "English",
          "emotion_vector": [0.3, 0, 0, 0, 0, 0, 0.1, 0.7],
          "duration_factor": 1,
          "delivery_note": "Warm and unhurried."
        }
      ]
    }
  ]
}
```

Required fields are those above except `source_pages`, `negative_prompt`, `candidates`, `base_seed`, `emotion_vector`, `duration_factor`, `delivery_note`, and per-line `seed`. Voice roles may specify `selected_voice` (an existing book-relative WAV), `temperature`, and `top_p`. When a line seed is omitted, compilation derives a stable 32-bit seed from its ID and text and freezes it into `tts_manifest.json`. Preserve useful cast descriptions and continuity notes as extra creative metadata; scripts do not interpret those notes.

Before writing LTX shots, inspect all illustrations and establish a book-wide cast/style reference from visible evidence. Map each character name to the same silhouette, construction method, colors, face or intentionally absent facial features, hair/fur, clothing, proportions, and accessories across pages. The exact shot reference image is the visual source of truth: mention only subjects actually visible in it and preserve their count. Narrative labels such as “boy”, “grandmother”, or “bee” must not cause visual completion. For example, if Banzi is represented as a geometric paper-collage figure without a conventional face or anatomy, describe and animate that existing collage figure—never request a newly drawn human child. Do not infer age, ethnicity, skin tone, facial anatomy, clothing, limbs, or realism that the illustration does not show.

Write `video.prompt` as an action-first, literal description of what changes over time in the supplied image. Begin with the principal visible subject and a concrete verb. Describe movement using only visible parts and the source artwork's own visual vocabulary; add secondary motion from existing hair, clothing, fur, foliage, water, smoke, or loose objects when present. Camera motion is optional and supporting—it must never be the only animation. Keep style preservation compact and secondary: retain the source medium, palette, linework, character identity, proportions, and background identity, without demanding an unchanged pose or rigid spatial composition. Every shot starts directly in the supplied composition and remains one uninterrupted scene through its last frame. Do not request intros, outros, transitions, page turns, wipes, dissolves, fades, pop-ins, pop-outs, or subjects entering/leaving the frame unless that boundary crossing is an essential story action visible from the source. Use chronological wording for multi-phase motion, avoid vague prompts such as `gentle motion`, and do not invent or duplicate characters, props, anatomy, cuts, or story events. Aim to keep the creative prompt below 80 words so runtime identity, continuity, and timing context keep the final LTX prompt concise.

IDs use ASCII letters, digits, underscores, or hyphens and are unique within their kind. Prefer zero-padded scene/shot/audio IDs. Input paths are book-relative, stay inside the project, and exist before validation. Emotion order is `[happy, angry, sad, afraid, disgusted, melancholic, surprised, calm]`, eight finite values in `[0,1]`. `duration_factor` is positive; optional `max_text_tokens_per_segment` defaults to 60.

Workflow bindings must resolve `prompt`, `reference_image`, `seed`, `duration_seconds`, and `output_prefix`. Bind `negative_prompt` when supported. After TTS QA, the runtime replaces each unsubmitted narrated shot's planned duration with `max(5, ceil(sum(measured line WAV seconds) + 1))`; the extra second provides a natural tail. Silent shots retain their planned duration. An unbound optional value leaves the workflow's configured value unchanged; inspect those fixed values during preparation.

`production_plan.py` is the executable validator/compiler. Compilation hashes the plan, workflow, bindings, reference images, and selected voices. Changed frozen input is rejected to prevent reuse of stale outputs.

## Generated files

The compiler derives these; the model does not maintain them separately:

- `planning/voice_specs.json`: role specifications.
- `planning/tts_manifest.json`: ordered `jobs` with IDs, TTS inputs, reference/output paths, `required`, and `status`.
- `planning/video_manifest.json`: ordered `jobs` with `shot_id`, `audio_ids`, workflow/bindings paths, video `inputs`, `output`, `required`, `status`, and `attempts`.
- `planning/compiled_plan.json`: frozen input hash.

TTS outputs are `voices/selected/<role_id>.wav` and `audio/lines/<audio_id>.wav`; candidates/QA stay under `voices/candidates/` and `qa/`. The assembler concatenates lines into `audio/mixes/<shot_id>.wav` in plan order.

Video jobs persist `client_id` before submission and `prompt_id` after acknowledgement. Status is `pending`, `submitting`, `queued`, `running`, `succeeded`, or `failed`. The submitter records the normalized `endpoint`, `post_attempts`, and `submitted_at`; `attempts` counts acknowledged prompt IDs, while `post_attempts` counts every POST and caps submissions at three. When an endpoint change confirms that a recorded prompt was lost, its identifiers move to `previous_prompt_ids` and `previous_client_ids` before a bounded retry. Actual output fields are `remote_output`, `remote_filename`, and `remote_subfolder`; returned filenames take precedence over planned names and stay beneath the book's Drive `video/shots/`. No local video copy is required.

`video/final/assembly_manifest.json` records the final output and ordered segments, audio IDs, and measured durations. Assembly normalizes to 1280×720/24fps with padded framing and extends the final frame when narration is longer.

## Supervisor state

`.pipeline/state.json` is local, atomically written, and protected by a per-book process lock. It records the input hash, PID, stages, timestamps, errors, Drive path, and persistent worker names (`voice` and `tts` by default). `status.json` mirrors the summary. Repository watcher state lives at `.pipeline/watcher-state.json`; it records `scheduler: voice_tts_video_final_pipeline`, one book name or `null` in each `slots.voice`, `slots.tts`, `slots.video`, and `slots.final`, plus per-book `stage`, `attempts_by_stage`, and `retry_at`. `video` releases as soon as ComfyUI finishes; CPU-only low-priority assembly proceeds independently in `final` on the existing ComfyUI session. Notification events are append-only in `.pipeline/notifications.jsonl`. `--timeout` bounds a stage; `--shot-timeout` bounds endpoint-outage tolerance per batch and contributes one tracking-timeout allowance per active prompt. A hard crash can leave state as `queued` or `running`; recovery routes from durable stage receipts and reconciles recorded prompt and client IDs before advancing.

`logs/pipeline/` holds noninteractive local CLI logs and Drive-side model/assembly logs. `.pipeline/supervisor.log` captures per-book detached errors; `.pipeline/watcher.log` captures repository watcher errors. `needs_input` is a saved return state, not an agent waiting loop. The watcher reloads the repository `url` file and resumes eligible books. Endpoint URLs are not copied from another book.

For new LTX projects, the post-TTS prompt stage stores derived `prompt_refinement` metadata separately from the frozen creative inputs; see [comfyui.md](comfyui.md#ltx-prompt-preparation). New projects use the LTX template, while frozen H3 projects remain valid.
