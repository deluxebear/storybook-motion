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
        "prompt": "Preserve the illustration; gentle ripples move beneath the little boat.",
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

Required fields are those above except `source_pages`, `negative_prompt`, `candidates`, `base_seed`, `emotion_vector`, `duration_factor`, and `delivery_note`. Voice roles may specify `selected_voice` (an existing book-relative WAV), `temperature`, and `top_p`. Preserve useful cast descriptions and continuity notes as extra creative metadata; scripts do not interpret those notes.

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

`.pipeline/state.json` is local, atomically written, and protected by a per-book process lock. It records the input hash, PID, stages, timestamps, errors, Drive path, exact owned L4 session, and cleanup outcome. `status.json` mirrors the summary. `--timeout` bounds a stage; `--shot-timeout` bounds endpoint-outage tolerance per batch and contributes one tracking-timeout allowance per active prompt. A hard crash can leave state as `queued` or `running`; recovery reconciles recorded prompt and client IDs before advancing.

`l4_handoff` is `authorizing`, `ready`, or `consumed` (cleared after cleanup). `ready` means the foreground command verified Drive and reserved this exact L4 for the child; recovery must not stop it before adoption. Browser authorization runs in the persistent foreground PTY with a 30-minute bound. Its prompts remain in that terminal rather than being redirected to project logs. `--interactive` remains accepted for compatibility; a live PTY is required whenever new authorization is needed.

`logs/pipeline/` holds noninteractive local CLI logs and Drive-side model/assembly logs. `.pipeline/supervisor.log` captures detached errors. `needs_input` is a saved return state, not an agent waiting loop. New invocations supply missing runtime options explicitly. Endpoint URLs and session names are never inferred from another book.

For new LTX projects, the post-TTS prompt stage stores derived `prompt_refinement` metadata separately from the frozen creative inputs; see [comfyui.md](comfyui.md#ltx-prompt-preparation). New projects use the LTX template, while frozen H3 projects remain valid.
