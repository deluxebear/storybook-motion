#!/usr/bin/env python3
"""Adapt the user-supplied MiniMax H3 API workflow for Khaya's 12 I2V shots."""

import json
import os
from pathlib import Path


root = Path(__file__).resolve().parents[1] / "books/khaya-wants-to-row"
source = Path.home() / "Downloads/video_minimax_h3_multiframe_reference.json"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


workflow = json.loads(source.read_text(encoding="utf-8"))
# Use the single first-frame reference directly. The remaining multiframe guide
# nodes become disconnected and therefore are not part of the output graph.
workflow["126"]["inputs"]["conditioning"] = ["136", 0]
workflow["115"]["inputs"].update(aspect_ratio="16:9 (Widescreen)", megapixels=0.9, multiple=32)
workflow["146"]["inputs"]["value"] = False
write_json(root / "comfyui/workflow_api.json", workflow)
write_json(
    root / "comfyui/bindings.json",
    {
        "prompt": {"node": "138", "input": "value"},
        "reference_image": {"node": "164", "input": "image"},
        "seed": {"node": "129", "input": "noise_seed"},
        "duration_seconds": {"node": "132", "input": "value"},
        "output_prefix": {"node": "92", "input": "filename_prefix"},
    },
)

storyboard = json.loads((root / "planning/storyboard.json").read_text(encoding="utf-8"))["shots"]
durations = {shot["shot_id"]: shot["duration_seconds"] for shot in storyboard}
manifest_path = root / "planning/video_manifest.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
for job in manifest["jobs"]:
    job["inputs"]["duration_seconds"] = durations[job["shot_id"]]
write_json(manifest_path, manifest)

print({"workflow_nodes": len(workflow), "shots": len(manifest["jobs"]), "megapixels": 0.9, "aspect_ratio": "16:9"})
