#!/usr/bin/env python3
"""Validate and atomically persist a trycloudflare ComfyUI endpoint."""

from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path


URL_RE = re.compile(r"https://[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.trycloudflare\.com", re.I)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--url", required=True, help="Captured Cloudflare tunnel URL or containing text")
    args = parser.parse_args()

    root = args.project_root.expanduser().resolve()
    if not root.is_dir():
        parser.error(f"project root is not a directory: {root}")
    match = URL_RE.search(args.url)
    if not match:
        parser.error("no valid https://*.trycloudflare.com URL found")
    url = match.group(0).lower()

    destination = root / "url"
    fd, temporary_name = tempfile.mkstemp(prefix=".url.", dir=root, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temporary:
            temporary.write(url + "\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        Path(temporary_name).unlink(missing_ok=True)
        raise

    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
