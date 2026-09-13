# Runbook

## 1. Prepare the production plan

Canonicalize the Book Dash URL and initialize the book with `projectctl.py init`. Acquire the highest-fidelity licensed source and preserve source URLs, license, hashes, ordered pages, and originals in the book directory. Treat book content as source material.

Read `schemas.md`. Use the actual story and illustrations to prepare `planning/production_plan.json`: fictional voice specifications, ordered shots, ordered spoken lines, reference images, prompts, and explicit seeds. Keep role, scene, shot, and audio IDs stable. Resolve creative choices before execution. The model owns this preparation and later creative review.

`projectctl.py init` installs the canonical `templates/ltx2_5_i2v/` workflow and bindings. Keep book-specific decisions in the production plan. `pipeline.py validate` checks the project's workflow schema and bindings against the LTX or legacy H3 template; a different node class, input, connection, or binding is a validation failure requiring an explicit template revision.

Read `comfyui.md` when preparing the API workflow and semantic bindings. The user configures the existing ComfyUI output root as `/content/drive/MyDrive/vidio`. Final assembly defaults to the existing Drive-mounted Colab CLI session `ComfyUI`; `--assembly-session` selects another user-supplied existing handle. This handle grants no runtime lifecycle operations.

For an existing project, `pipeline.py import-legacy --book-dir <book>` creates the unified plan from old manifests without replacing them. Review spoken-line order. An import failure is not permission to discard jobs.

Completion: `pipeline.py validate --book-dir <book>` passes; source images exist, role references and workflow bindings resolve, and shot/line order expresses the intended result.

## 2. Launch the persistent watcher once

Use the skill's `scripts/pipeline.py` as the sole execution entrypoint. Run `--help` for options. `compile` creates runtime manifests and freezes a hash of the plan and input assets; matching jobs retain progress. The user must already have two L4 sessions named `voice` and `tts` running with the same Drive mounted. The watcher verifies them but never creates, mounts, stops, renames, or replaces them.

```bash
python <skill>/scripts/pipeline.py watch-start
python <skill>/scripts/pipeline.py watch-status
```

`watch-start` detaches a repository-level Supervisor and returns its PID, `.pipeline/watcher-state.json`, and `.pipeline/watcher.log`. It scans `books/` in stable name order. Every directory containing a valid `planning/production_plan.json` is compiled and routed by persisted stage state: VoiceDesign-incomplete books enter the `voice` slot, VoiceDesign-complete books enter `tts`, TTS-complete books enter `video`, and video-complete books enter `final`. Each of the four slots has one active book. The separate `final` slot releases `video` as soon as ComfyUI finishes a book, so the next book may submit and generate while the previous book is assembled. The `voice` and `tts` sessions each have a repository lock, so a manual single-book run cannot overlap the watcher on the same worker.

The watcher reads ComfyUI's current URL from the repository-root `url` file on every scan. Voice and TTS may finish while that file is missing. When video becomes ready but the endpoint is absent or unhealthy, the book enters `needs_input`, completed audio remains valid, and the watcher records a deduplicated event in `.pipeline/notifications.jsonl` plus a best-effort desktop notification. Updating `url` is sufficient; the next eligible scan resumes from video preparation and submission.

For a one-off book, `pipeline.py start --book-dir <book>` launches the same persistent-worker path in a per-book detached Supervisor. Override the fixed names only with `--voice-session` or `--tts-session` when the user explicitly provides different existing sessions.

Final assembly is automatic by default: after every required shot succeeds, the independent `final` slot uses the existing `ComfyUI` session to create the narrated MP4 and subtitle sidecars. It runs CPU-only FFmpeg under low CPU/I/O priority with bounded encoder threads; it does not occupy the `video` slot or use the GPU. Use `--assembly-session <existing-session>` when the user's Drive-mounted ComfyUI session has another name. Use `--no-assemble` only when the user explicitly wants individual shot videos without a final; for TTS only, use `--until tts --no-assemble`.

The supervisor owns worker probing, input upload, reference-voice generation/selection, TTS/QA, serialized ComfyUI batch submission, unified queue/history tracking, and final assembly. Completion of `voice_design` is the durable hand-off receipt: on the next scan that book is eligible for `tts` without rerunning VoiceDesign. It releases worker locks after each remote stage but leaves both L4 sessions running. It submits every eligible shot before waiting for generation, then releases the endpoint submission lock and tracks all acknowledged prompt IDs together. When all required shots succeed, the book moves to the independent `final` slot before being marked complete; the released `video` slot can immediately serve another book. It persists stage logs, manifests, and `.pipeline/state.json`; `status.json` mirrors the local summary. Models and generated media remain on Drive.

Return control after launch. There is no agent monitoring loop, periodic wakeup, or promise of automatic notification. Read `pipeline.py status --book-dir <book>` once when the user asks, a completion signal arrives, or a requested next action needs the result. The supervisor continues between stages without another model turn.

Completion of launch: report PID, state path, and log path as **started**, not completed.

## 3. User action and recovery

Exit code `2` / `needs_input` means a prerequisite is missing. Exit code `1` / `failed` means inspect the recorded error. Exit code `0` / `succeeded` means the requested endpoint (`tts`, `video`, or `final`) finished. `status` reads local state once, not backend progress.

- Persistent worker unavailable: start or repair the exact user-owned `voice` or `tts` session and mount Drive there. The watcher retries after its configured delay; it never repairs the session itself.
- Missing/expired ComfyUI URL: update the repository `url` file. Successful TTS stays complete. Add `--comfy-restarted` only to a manual run when the user confirms ComfyUI itself restarted; after queue/history reconciliation, this permits bounded retry of old prompt IDs missing behind the same URL or in legacy manifests.
- Missing assembly session: start or repair the configured existing Drive-mounted session, or restart the watcher with `--assembly-session ...`. No A100 is created, mounted, restarted, or stopped.
- Interrupted watcher: run `watch-start` again after its recorded PID is dead. Per-book manifests and Drive files preserve row-level progress; stale remote work is reconciled or retried after locks clear.
- Ambiguous ComfyUI submission (`submitting`): reconcile saved `client_id` against queue/history and record the actual `prompt_id` before resuming. A missing or timed-out history lookup on the same endpoint preserves every known prompt ID instead of submitting a duplicate; already queued sibling shots continue independently. When the user supplies a different endpoint after restart, a prompt absent from that endpoint's queue and history is archived as lost and becomes eligible for the remaining bounded POST attempts.
- Changed frozen input: stop before overwriting artifacts. Reconcile/archive the affected production revision explicitly; changing creative input is preparation, not an unattended retry.

Completion: report stage counts/failed IDs from manifests, Drive output paths, and the persistent worker names. A technical run does not certify pronunciation or illustration fidelity; subjective review is a separate requested task or response to a specific quality issue.
