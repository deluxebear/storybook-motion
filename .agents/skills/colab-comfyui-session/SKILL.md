---
name: colab-comfyui-session
description: Launch or resume this project's ComfyUI notebook in a named Google Colab A100 high-memory session, authorize and mount Google Drive through the browser, and persist the Cloudflare tunnel endpoint for other project skills. Use when starting, recovering, or checking the project's Colab-hosted ComfyUI service.
---

# Colab ComfyUI Session

Start the repository notebook `notebooks/ComfyUIonColab_cli.ipynb` in the Colab session named `ComfyUI`, mount Drive, and write the live tunnel URL to the repository-root file `url`.

## Preconditions

- Work from the repository root and resolve it before running commands. Do not assume the caller's current directory.
- Require the local notebook at `<project-root>/notebooks/ComfyUIonColab_cli.ipynb` and the `colab` CLI.
- Read `colab help new`, `colab help drivemount`, and `colab help exec` because CLI options vary by installed version.
- Treat the Colab VM as billable. Reuse a healthy session named `ComfyUI`; do not create a duplicate or silently substitute a different accelerator.

## Provision the runtime

1. Inspect `colab sessions` and `colab status -s ComfyUI` before allocation.
2. If no usable `ComfyUI` session exists, create exactly the requested A100 high-memory runtime:

   ```bash
   colab new -s ComfyUI --gpu A100 --high-mem
   ```

3. If the installed CLI does not advertise `--high-mem`, stop and report that incompatibility. Do not omit the flag, silently downgrade the machine, or repeatedly retry allocation. A CLI upgrade or explicit user approval to run without high memory is required.
4. After creation, confirm the session exists and reports A100. If allocation or entitlement fails, preserve the error and stop; never fall back to another GPU automatically.

## Prepare the runtime (optional PyTorch nightly upgrade)

To upgrade the existing session's PyTorch nightly build and wait for verified
completion, use the bundled listener script before running the notebook:

```bash
python <skill-dir>/scripts/upgrade_pytorch_nightly.py -s ComfyUI
```

It listens to both remote commands through `colab exec` in order without
forwarding normal `pip` progress, waits for each process to exit, then imports
PyTorch remotely and requires a version matching `2.15.0.dev<date>+cu132`. A
non-matching version or failed command is reported as an upgrade failure, but
this informational bootstrap step does not block the remaining session flow;
only bounded failure output is shown, so routine installation does not consume
unnecessary context.

## Mount Google Drive

Run this in a PTY because Drive authorization is interactive:

```bash
colab drivemount -s ComfyUI
```

When the command presents an authorization URL, open it using browser UI control. If the browser already has exactly one signed-in Google account, continue the Google consent flow and grant the requested Drive access. Do not type credentials, bypass MFA, select among ambiguous accounts, or alter account security settings; ask the user to take over in those cases. Return to the PTY and complete any prompt after browser consent.

Verify the mount with remote Python rather than trusting the success message:

```python
from pathlib import Path
p = Path('/content/drive/MyDrive')
assert p.is_dir(), 'Google Drive is not mounted'
probe = p / '.vidio-colab-drive-probe'
probe.write_text('ok')
assert probe.read_text() == 'ok'
probe.unlink()
```

Execute that probe with `colab exec -s ComfyUI`. A failed probe means the mount is not ready; do not launch the notebook.

## Run the notebook and capture the endpoint

Run the local notebook on the existing session, with a long execution timeout and streamed output:

```bash
colab exec -s ComfyUI -f <project-root>/notebooks/ComfyUIonColab_cli.ipynb --timeout 86400
```

The final cell intentionally keeps ComfyUI running, so keep the local execution attached or in a managed terminal/session. Do not stop the Colab runtime after the URL appears. Watch output for the first complete HTTPS URL matching `https://<host>.trycloudflare.com`.

Persist it atomically with:

```bash
python <skill-dir>/scripts/record_tunnel_url.py --project-root <project-root> --url <captured-url>
```

The script validates the host and writes only the normalized URL plus a newline to `<project-root>/url`. Overwrite an old URL only after a new valid URL is observed. Then make a bounded health request to the URL; Cloudflare may need a short warm-up. If health never succeeds, keep the captured URL for diagnosis but report it as unhealthy rather than claiming readiness.

If the notebook command exits before emitting a URL, inspect its output and `colab status -s ComfyUI`; do not invent or retain an old endpoint as though it were current. Retry only after addressing the concrete failure.

## Completion and cleanup

Report the session name, verified accelerator, Drive mount result, absolute notebook path, absolute `url` file path, and endpoint health. The successful outcome leaves the `ComfyUI` session running for other skills.

Stop it only when the user explicitly asks, using `colab stop -s ComfyUI`. When stopped or confirmed expired, remove the stale `url` file so consumers cannot mistake it for a live endpoint.
