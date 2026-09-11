#!/usr/bin/env python3
"""Build the isolated Colab notebook used to evaluate native FastH3/VSA."""

import json
from pathlib import Path


def markdown(source):
    return {"cell_type": "markdown", "metadata": {}, "source": source.splitlines(keepends=True)}


def code(source):
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": source.splitlines(keepends=True),
    }


CELLS = [
    markdown("""# FastH3 / VSA isolated ComfyUI test

This notebook creates a separate `/content/ComfyUI-FastH3` checkout and a separate
Drive output directory. It uses the native ComfyUI `Model Sparse Attention`
(`BlockSparseAttention`) node merged in ComfyUI PR #16072 and the `sol_attn` VSA
backend merged in comfy-kitchen PR #117. PR #90 is W4A8 quantization support; it
is not, by itself, the FastH3 integration.

Before running, select an **A100 High-RAM** runtime, mount Google Drive, and accept
the MiniMax H3 model licence on Hugging Face. Put `HF_TOKEN` in Colab Secrets if
the repositories require authentication. No token is printed or written to disk.
"""),
    code("""from google.colab import drive
drive.mount('/content/drive')
"""),
    code("""from pathlib import Path
import os

WORKSPACE = Path('/content/ComfyUI-FastH3')
DRIVE_ROOT = Path('/content/drive/MyDrive')
MODEL_ROOT = DRIVE_ROOT / 'ComfyUI-FastH3' / 'models'
OUTPUT_ROOT = DRIVE_ROOT / 'vidio-fasth3-test'
SHARED_MODEL_ROOT = DRIVE_ROOT / 'ComfyUI' / 'models'
REUSE_EXISTING_H3_MODELS = True  # @param {type:"boolean"}
DOWNLOAD_MODELS = True  # @param {type:"boolean"}

if not DRIVE_ROOT.is_dir():
    raise RuntimeError('Google Drive is not mounted')
for directory in ('diffusion_models', 'text_encoders', 'vae', 'loras'):
    (MODEL_ROOT / directory).mkdir(parents=True, exist_ok=True)
OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
print('Workspace:', WORKSPACE)
print('Models:', MODEL_ROOT)
print('Output:', OUTPUT_ROOT)
"""),
    code("""import subprocess

def run(*args, cwd=None):
    subprocess.check_call([str(a) for a in args], cwd=cwd)

if not (WORKSPACE / '.git').is_dir():
    run('git', 'clone', '--depth', '1', 'https://github.com/Comfy-Org/ComfyUI.git', WORKSPACE)
else:
    run('git', 'pull', '--ff-only', cwd=WORKSPACE)

run('python', '-m', 'pip', 'install', '-q', '-U', '-r', WORKSPACE / 'requirements.txt')
run('python', '-m', 'pip', 'install', '-q', '-U', 'huggingface_hub', 'hf_transfer')

local_models = WORKSPACE / 'models'
if local_models.is_symlink():
    local_models.unlink()
elif local_models.exists():
    local_models.rename(WORKSPACE / 'models.original')
local_models.symlink_to(MODEL_ROOT, target_is_directory=True)

local_output = WORKSPACE / 'output'
if local_output.is_symlink():
    local_output.unlink()
elif local_output.exists():
    local_output.rename(WORKSPACE / 'output.original')
local_output.symlink_to(OUTPUT_ROOT, target_is_directory=True)
"""),
    markdown("""## Models

The VSA checkpoint must contain all 50 `to_gate_compress` tensors. A normal H3
checkpoint plus the existing 8-step Turbo LoRA is not equivalent. Standard text
encoder and VAE files can be linked from the existing Drive installation to avoid
duplicating them; the FastH3 diffusion checkpoint remains isolated.
"""),
    code("""import shutil
from huggingface_hub import hf_hub_download

try:
    from google.colab import userdata
    HF_TOKEN = userdata.get('HF_TOKEN')
except Exception:
    HF_TOKEN = os.environ.get('HF_TOKEN')

assets = [
    # repo, filename, destination subdirectory, may reuse shared installation
    ('Kijai/MiniMax-H3-experimental',
     'minimax_h3_fastvideo_vsa_datafree_1300step_4step_int8_convrot.safetensors',
     'diffusion_models', False),
    ('Comfy-Org/MiniMax-H3', 'qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors',
     'text_encoders', True),
    ('Comfy-Org/MiniMax-H3', 'minimax_h3_video_vae_fp16.safetensors',
     'vae', True),
    ('Comfy-Org/MiniMax-H3', 'minimax_h3_audio_vae_fp32.safetensors',
     'vae', True),
]

for repo, filename, subdir, reusable in assets:
    target = MODEL_ROOT / subdir / filename
    shared = SHARED_MODEL_ROOT / subdir / filename
    if target.exists():
        print('ready:', target)
        continue
    if reusable and REUSE_EXISTING_H3_MODELS and shared.is_file():
        target.symlink_to(shared)
        print('linked:', target, '->', shared)
        continue
    if not DOWNLOAD_MODELS:
        raise FileNotFoundError(target)
    downloaded = Path(hf_hub_download(
        repo_id=repo,
        filename=filename,
        token=HF_TOKEN,
        local_dir=MODEL_ROOT / subdir,
    ))
    if downloaded != target:
        shutil.move(downloaded, target)
    print('downloaded:', target)
"""),
    code("""import importlib
import subprocess
import sys
import torch

if torch.cuda.get_device_capability() != (8, 0):
    print('WARNING: expected A100/SM80, got', torch.cuda.get_device_name())
else:
    print('GPU:', torch.cuda.get_device_name(), 'VRAM GiB:', round(torch.cuda.get_device_properties(0).total_memory / 2**30, 1))

sys.path.insert(0, str(WORKSPACE))
ck = importlib.import_module('comfy_kitchen')
backends = ck.get_backends() if hasattr(ck, 'get_backends') else None
print('comfy-kitchen:', getattr(ck, '__version__', 'unknown'))
print('backend inventory:', backends)
kernel_checks = {
    'sol_attn': callable(getattr(ck, 'sol_attn', None)),
    'sol_attn_chunked': callable(getattr(ck, 'sol_attn_chunked', None)),
}
print('VSA kernels:', kernel_checks)
if not all(kernel_checks.values()):
    raise RuntimeError('comfy-kitchen was installed without the required VSA kernels')

sparse_source = WORKSPACE / 'comfy_extras' / 'nodes_sparse_attention.py'
model_source = WORKSPACE / 'comfy' / 'model_detection.py'
checks = {
    'BlockSparseAttention': 'class BlockSparseAttention' in sparse_source.read_text(),
    'VSA option': 'Option("vsa"' in sparse_source.read_text(),
    'FastH3 gate detection': 'gate_compress' in model_source.read_text(),
}
print(checks)
if not all(checks.values()):
    raise RuntimeError('This ComfyUI checkout does not contain the merged native FastH3/VSA integration')
"""),
    markdown("""## Start the isolated server

After the URL appears, open ComfyUI and build this model path:

`UNETLoader (FastH3 VSA checkpoint)` → `Model Sparse Attention` → sampler/guider.

Use `method=vsa`, `keep_percent=10`, `start_percent=0`, `end_percent=1`,
`min_tokens=0`, `sink_conditioning=exact_kv_and_rows`, `verbose=true`, then use
`euler`, `simple`, **4 steps**. Do not add the existing H3 Turbo 8-step LoRA.

Start/end 0→1 forces VSA on for every step; this is important for a four-step
VSA-trained model. The first compilation run is not a valid speed benchmark.
"""),
    code("""import re
import socket
import subprocess
import threading
import time

run('bash', '-lc', 'command -v cloudflared >/dev/null || (wget -q https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb -O /tmp/cloudflared.deb && dpkg -i /tmp/cloudflared.deb)')

def expose(port=8188):
    while True:
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1', port)) == 0:
                break
        time.sleep(0.5)
    proc = subprocess.Popen(
        ['cloudflared', 'tunnel', '--protocol', 'http2', '--url', f'http://127.0.0.1:{port}'],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    for line in proc.stdout:
        match = re.search(r'https://[a-z0-9-]+\\.trycloudflare\\.com', line)
        if match:
            print('FASTH3_COMFY_URL=' + match.group(0), flush=True)
        else:
            print(line, end='')

threading.Thread(target=expose, daemon=True).start()
os.chdir(WORKSPACE)
subprocess.check_call([
    sys.executable, 'main.py', '--listen', '127.0.0.1', '--port', '8188',
    '--dont-print-server', '--highvram',
])
"""),
]


def main():
    output = Path(__file__).resolve().parents[1] / "notebooks" / "ComfyUI_FastH3_VSA_test.ipynb"
    notebook = {
        "cells": CELLS,
        "metadata": {
            "accelerator": "GPU",
            "colab": {"gpuType": "A100", "provenance": []},
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    output.write_text(json.dumps(notebook, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
