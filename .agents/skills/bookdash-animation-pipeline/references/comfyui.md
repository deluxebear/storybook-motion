# ComfyUI batch contract

Use the HTTP API with an API-format workflow against the user-supplied running endpoint. The Cloudflare URL may be ephemeral; record it in `comfyui/endpoint.json` as checked runtime metadata, but never start, restart, mount, configure, or stop the user’s ComfyUI environment.

The default workflow for new books is `templates/ltx2_5_i2v/workflow_api.json` (5 seconds, 24 fps) with its adjacent bindings. Project initialization copies both files. Validation ignores literal input values but requires identical node IDs/classes, input names, edge topology, and bindings, so prompts, images, seeds, durations, and output prefixes can vary without permitting structural drift. The legacy `minimax_h3_i2v` template remains accepted for frozen existing projects.

Normalize the base URL by removing its fragment and trailing slash. Require `/system_stats` before uploads. If the workflow came from another instance, compare its class types with `/object_info` and stop with the exact missing-node list.

For Book Dash animation, use `16:9 (Widescreen)` at approximately `0.9` megapixels by default. This is the validated balance between preserving illustration composition and readable signed-hand gestures. Do not use a square output unless the user explicitly requests it. When an endpoint's resolution node quantizes dimensions, record the actual returned dimensions in the video manifest.

`comfyui/bindings.json` maps semantic values to workflow node inputs:

```json
{
  "prompt": {"node": "12", "input": "text"},
  "negative_prompt": {"node": "13", "input": "text"},
  "reference_image": {"node": "41", "input": "image"},
  "seed": {"node": "27", "input": "seed"},
  "duration_seconds": {"node": "28", "input": "value"},
  "output_prefix": {"node": "66", "input": "filename_prefix"}
}
```

Resolve every binding before the first submission; derive node IDs from the actual API workflow rather than labels or guesses. `duration_seconds` is populated after TTS QA from measured narration length and must reach the workflow's duration primitive.

Configure ComfyUI's output root to `/content/drive/MyDrive/vidio` before starting the batch. For Drive-reference mode, configure `input/vidio` as a symlink to that same directory. The batch then binds `LoadImage` to `vidio/books/<book_slug>/<book-relative-reference-image>`, so ComfyUI reads the shared Drive file directly without a local HTTP upload. Start a new pipeline with `--reference-mode drive` only after this mapping exists; use the default `--reference-mode upload` as the compatible fallback. The pipeline deep-copies the workflow per shot and sets `output_prefix` to `books/<book_slug>/video/shots/<shot_id>`. Under the endpoint submission lock, it saves `client_id` and `submitting` before each POST, saves every returned `prompt_id` immediately, and submits every eligible shot without waiting for generation. A failure to submit one shot is recorded without preventing later shots from entering the queue. The lock is released after the complete submission pass.

The supervisor then tracks all acknowledged prompt IDs together. It obtains the queue once per pass, reconciles completed prompts from history, and atomically persists every state transition and Drive output reference. A transient queue or history transport error causes bounded exponential backoff rather than terminating the batch; an outage timeout retains all known prompt IDs for resume. A prompt absent from both queue and history retains its ID and requires reconciliation instead of automatic resubmission. On resume, `submitting` jobs are matched by `client_id` against queue and history before any new POST. If the normalized endpoint changed and the old prompt is absent from both the new queue and history, the supervisor archives its IDs and makes the shot eligible for a bounded retry. For a confirmed restart behind the same URL, pass `--comfy-restarted`; this supplies the missing endpoint-generation evidence and applies the same rule, including to legacy jobs without an `endpoint` field. Never use this flag for an ordinary supervisor restart while ComfyUI itself stayed running.

Count every `/prompt` POST in `post_attempts`, including uncertain responses and definitive rejections, and cap each shot at three POST attempts. Count acknowledged prompt IDs in `attempts`. A definitive HTTP 4xx rejection becomes `failed`; transport failures, HTTP 5xx responses, and malformed acknowledgements remain `submitting` with their `client_id`, because ComfyUI may have accepted them. Execution errors remain in the manifest and become retryable on a later supervisor run only while both counters remain below the cap.

Do not download outputs via `/view`. Require returned output subfolders to be under `books/<book_slug>/video/shots/`, record the returned filename and its absolute Drive path in `video_manifest.json`, and treat a history result outside that prefix as a failed job. Queueing the complete book does not increase generation concurrency: ComfyUI remains responsible for executing its queue at the endpoint's configured concurrency, which defaults to one until capacity is measured.

## LTX prompt preparation

After TTS duration adjustment and before submission, `video_prompt.py` derives a final prompt from the original visual intent and cumulative measured line WAV durations. It adds a duration-dependent action budget, narration context and a settling interval. This is deterministic rule-based preparation, not a language-model rewrite or exact animation/word synchronization. Word timestamps are not currently used. Silent shots use their planned duration and a one-second settling interval.

Keep the original `inputs.prompt` unchanged. Final prompts, timing context and input hashes are stored as `prompt_refinement` in the video manifest and in `planning/video_prompt_refinements.json`. The submitter uses the persisted final prompt. Compilation preserves the metadata. Submitted or previously attempted jobs retain their prompt; retries use the same input. Existing H3 jobs bypass refinement. Refined metadata is included when bundling for final assembly.

The supplied LTX graph retains joint AV sampling but exports silent video, with existing TTS providing final narration. Both sampling passes share the bound shot seed. Internal prompt enhancement stays disabled. The negative prompt excludes illustration-hostile terms. The graph uses integer seconds at 24 fps and `seconds * fps + 1` frames; live model/node compatibility, feasible duration and speed must be checked on the user-supplied endpoint before production. Do not infer readiness from the local template alone.
