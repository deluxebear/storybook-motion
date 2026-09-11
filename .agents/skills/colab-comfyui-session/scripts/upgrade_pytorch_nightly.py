#!/usr/bin/env python3
"""Upgrade PyTorch in the named Colab session and verify the remote version."""

from __future__ import annotations

import argparse
from collections import deque
import re
import subprocess
import sys


INDEX_URL = "https://download.pytorch.org/whl/nightly/cu132"
TARGET_VERSION = re.compile(r"\b2\.15\.0\.dev\d+\+cu132\b")


def run_exec(session: str, code: str, timeout: float) -> str:
    """Run code through colab exec, silently listen, and return combined output."""
    proc = subprocess.Popen(
        ["colab", "exec", "-s", session, "--timeout", str(timeout)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    try:
        raw_output, _ = proc.communicate(code, timeout=timeout + 30)
    except subprocess.TimeoutExpired:
        proc.kill()
        raw_output, _ = proc.communicate()
        print(f"远程命令超过本地超时（{timeout + 30:.0f}s）", file=sys.stderr)
        print("".join(raw_output.splitlines(keepends=True)[-20:]), file=sys.stderr, end="")
        raise

    # Keep only a bounded tail: enough for diagnostics without retaining a
    # potentially enormous pip transcript after this function returns.
    output: deque[str] = deque(raw_output.splitlines(keepends=True), maxlen=40)
    return_code = proc.returncode
    if return_code:
        # Keep diagnostics bounded; normal pip progress is intentionally hidden.
        print("远程命令失败，末尾输出：", file=sys.stderr)
        print("".join(list(output)[-20:]), file=sys.stderr, end="")
        raise subprocess.CalledProcessError(return_code, proc.args)
    return "".join(output)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-s", "--session", default="ComfyUI")
    parser.add_argument("--timeout", type=float, default=3600.0)
    args = parser.parse_args()

    failed_steps: list[str] = []

    for step, code in (
        ("uninstall", "!pip uninstall -y torch torchvision torchaudio\n"),
        (
            "install",
            f"!pip install --pre torch torchvision torchaudio --index-url {INDEX_URL}\n",
        ),
    ):
        try:
            run_exec(args.session, code, args.timeout)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            failed_steps.append(step)
            print(f"PyTorch {step} step failed; continuing.", file=sys.stderr)

    try:
        verification = run_exec(
            args.session,
            'import torch\nprint("PyTorch version:", torch.__version__)\n',
            args.timeout,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        failed_steps.append("verification")
        verification = ""

    match = TARGET_VERSION.search(verification)
    if match and not failed_steps:
        print(f"PyTorch upgrade verified: {match.group(0)}")
    else:
        reason = ", ".join(failed_steps) if failed_steps else "version mismatch"
        print(f"PyTorch upgrade not verified ({reason}); continuing.", file=sys.stderr)

    # This is an informational bootstrap step. Its result must not prevent the
    # caller from continuing with Drive mounting and notebook execution.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
