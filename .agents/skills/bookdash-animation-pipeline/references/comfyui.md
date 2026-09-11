# ComfyUI batch contract

Use the HTTP API with an API-format workflow against the user-supplied running endpoint. The Cloudflare URL may be ephemeral; record it in `comfyui/endpoint.json` as checked runtime metadata, but never start, restart, mount, configure, or stop the user’s ComfyUI environment.

The canonical per-book workflow is `templates/minimax_h3_i2v/workflow_api.json` with its adjacent bindings. Project initialization copies both files. Validation ignores literal input values but requires identical node IDs/classes, input names, edge topology, and bindings, so prompts, images, seeds, durations, and output prefixes can vary without permitting structural drift.

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

Configure ComfyUI's output root to `/content/drive/MyDrive/vidio` before starting the batch. The pipeline invokes `comfy_batch.py` to upload images, deep-copy the workflow per shot, apply bindings, and set `output_prefix` to `books/<book_slug>/video/shots/<shot_id>`. It saves `client_id` and `submitting` before POST, then saves the returned `prompt_id` immediately. The script polls history; a timeout retains the prompt for resume. Read requests have at most three transport attempts. Ambiguous POST results require reconciliation, not automatic resubmission. Execution errors remain in the manifest; retries are capped at three acknowledged submissions per shot.

Do not download outputs via `/view`. Require returned output subfolders to be under `books/<book_slug>/video/shots/`, record the returned filename and its absolute Drive path in `video_manifest.json`, and treat a history result outside that prefix as a failed job. Default concurrency is one until endpoint capacity is measured.
