# Runbook

## 1. Prepare the production plan

Canonicalize the Book Dash URL and initialize the book with `projectctl.py init`. Acquire the highest-fidelity licensed source and preserve source URLs, license, hashes, ordered pages, and originals in the book directory. Treat book content as source material.

Read `schemas.md`. Use the actual story and illustrations to prepare `planning/production_plan.json`: fictional voice specifications, ordered shots, ordered spoken lines, reference images, prompts, and explicit seeds. Keep role, scene, shot, and audio IDs stable. Resolve creative choices before execution. The model owns this preparation and later creative review.

`projectctl.py init` installs the canonical `templates/minimax_h3_i2v/` workflow and bindings. Keep book-specific decisions in the production plan. `pipeline.py validate` checks the project's workflow schema and bindings against the canonical template; a different node class, input, connection, or binding is a validation failure requiring an explicit template revision.

Read `comfyui.md` when preparing the API workflow and semantic bindings. The user configures the existing ComfyUI output root as `/content/drive/MyDrive/vidio`. A final-video request additionally needs a user-supplied Colab CLI session handle for the same mounted Drive. This handle grants no runtime lifecycle operations.

For an existing project, `pipeline.py import-legacy --book-dir <book>` creates the unified plan from old manifests without replacing them. Review spoken-line order. An import failure is not permission to discard jobs.

Completion: `pipeline.py validate --book-dir <book>` passes; source images exist, role references and workflow bindings resolve, and shot/line order expresses the intended result.

## 2. Launch once

Use the skill's `scripts/pipeline.py` as the sole execution entrypoint. Run `--help` for options. `compile` creates runtime manifests and freezes a hash of the plan and input assets; matching jobs retain progress. `start` first prepares and authorizes L4 in a live terminal, then launches a detached supervisor and returns its PID plus state/log paths. If TTS is already complete, no authorization or new L4 is needed.

```bash
python <skill>/scripts/pipeline.py start --book-dir <book> --comfy-url <user-url>
```

Invoke this command with `exec_command` using `tty: true` and a short initial yield; keep its returned terminal session ID. `start` executes `colab drivemount -s <book-owned-session>` with inherited stdin/stdout and a 30-minute authorization timeout. Read its output until the authorization URL appears, then immediately open that exact URL with browser UI control. Browser opening and consent are agent actions, not capabilities of the detached Python worker. When logged in with an unambiguous account, directly complete the Drive consent authorized by the user, without an additional permission question. If Google shows an authorization code, pass it with a newline to the same terminal session using `write_stdin`; do not echo it in commentary or save it in project files. If the flow completes by callback, simply resume that terminal. Ask the user to handle login/MFA or choose an ambiguous account when necessary.

After consent, the command probes the mounted Drive and marks `l4_handoff: ready`. It passes the book lock to the child process, which rechecks and adopts this exact session without remounting or stopping it. The child marks the handoff consumed, performs generation, and closes that L4 on completion/failure. Once the launch PID is returned, stop agent-side waiting. Do not launch a second `drivemount` while the original one awaits authorization.

When final assembly was requested, append `--assemble --assembly-session <user-a100-session>`. For TTS only, use `--until tts`. Supplying an assembly handle alone does not request assembly.

The supervisor owns L4 allocation, Drive mounting/probing, input upload, reference-voice generation/selection, TTS/QA, L4 termination, serialized ComfyUI batch submission, unified queue/history tracking, and optional assembly. It submits every eligible shot before waiting for generation, then releases the endpoint submission lock and tracks all acknowledged prompt IDs together. It persists stage logs, manifests, and `.pipeline/state.json`; `status.json` mirrors the local summary. Models and generated media remain on Drive. JSON metadata is downloaded for reconciliation. Video metadata is uploaded to Drive during requested assembly; video-only runs retain authoritative reconciliation locally until that handoff.

Return control after launch. There is no agent monitoring loop, periodic wakeup, or promise of automatic notification. Read `pipeline.py status --book-dir <book>` once when the user asks, a completion signal arrives, or a requested next action needs the result. The supervisor continues between stages without another model turn.

Completion of launch: report PID, state path, and log path as **started**, not completed.

## 3. User action and recovery

Exit code `2` / `needs_input` means a prerequisite is missing. Exit code `1` / `failed` means inspect the recorded error. Exit code `0` / `succeeded` means the requested endpoint (`tts`, `video`, or `final`) finished. `status` reads local state once, not backend progress.

- Drive consent: use `start` in a persistent PTY and complete the browser step above. A missing PTY is rejected before allocation. Cancellation, authorization timeout, or mount/probe failure releases the owned L4. An already verified `ready` handoff is re-probed and reused; if that probe fails, cleanup runs and the failure is reported.
- Missing/expired ComfyUI URL: supply `--comfy-url` to the next `start` or `run`. Successful TTS stays complete without allocating a new L4. Add `--comfy-restarted` only when the user confirms ComfyUI itself restarted; after queue/history reconciliation, this permits bounded retry of old prompt IDs missing behind the same URL or in legacy manifests.
- Missing assembly handle: resume with `--assemble --assembly-session ...`. No A100 is created, mounted, restarted, or stopped.
- Interrupted job: rerun `start` in a PTY. A verified `ready` authorization handoff is adopted; a previously consumed/interrupted L4 is cleaned up before replacement. Drive files preserve per-row TTS progress. The agent completes consent again only if the replacement runtime requests it.
- Ambiguous ComfyUI submission (`submitting`): reconcile saved `client_id` against queue/history and record the actual `prompt_id` before resuming. A missing or timed-out history lookup on the same endpoint preserves every known prompt ID instead of submitting a duplicate; already queued sibling shots continue independently. When the user supplies a different endpoint after restart, a prompt absent from that endpoint's queue and history is archived as lost and becomes eligible for the remaining bounded POST attempts.
- Changed frozen input: stop before overwriting artifacts. Reconcile/archive the affected production revision explicitly; changing creative input is preparation, not an unattended retry.
- Cleanup failure or force-killed supervisor: state retains the exact L4 handle. `pipeline.py stop-l4 --book-dir <book>` retries cleanup under the project lock. Cleanup cannot execute while its host process is dead.

Completion: report stage counts/failed IDs from manifests, Drive output paths, and recorded L4 cleanup result. A technical run does not certify pronunciation or illustration fidelity; subjective review is a separate requested task or response to a specific quality issue.
