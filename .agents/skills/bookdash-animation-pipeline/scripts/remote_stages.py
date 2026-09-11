"""Deterministic Drive-side stages. Heavy dependencies load only on the GPU."""
import argparse
import json
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

from production_plan import apply_tts_video_durations, read, relative
from projectctl import atomic_json


def audio_qa(path, minimum=0.15, maximum=120):
    import numpy as np
    import soundfile as sf
    wav, sr = sf.read(path, always_2d=True)
    seconds = len(wav) / sr
    if not len(wav) or not np.isfinite(wav).all():
        raise ValueError(f"invalid samples: {path}")
    peak = float(np.max(np.abs(wav)))
    rms = float(np.sqrt(np.mean(wav ** 2)))
    clipping = float(np.mean(np.abs(wav) >= .999))
    if not (sr >= 16000 and minimum <= seconds <= maximum and 0.001 < rms and peak <= 1 and clipping < .001):
        raise ValueError(f"audio QA failed: {path} ({seconds=}, {sr=}, {rms=}, {clipping=})")
    return {"seconds": seconds, "sample_rate": sr, "peak": peak, "rms": rms, "clipping_fraction": clipping}


def valid_audio(path, minimum=.15, maximum=120):
    try:
        return audio_qa(path, minimum, maximum)
    except (OSError, RuntimeError, ValueError):
        return None


def voices(book):
    import numpy as np
    import soundfile as sf
    import torch
    specs = read(book / "planning/voice_specs.json")
    model = None
    report_path = book / "qa/voice_selection.json"
    reports = read(report_path) if report_path.exists() else {}
    for ri, (role, spec) in enumerate(sorted(specs.items())):
        target = book / f"voices/selected/{role}.wav"
        target.parent.mkdir(parents=True, exist_ok=True)
        if valid_audio(target, .8, 30):
            reports.setdefault(role, {"method": "reuse validated selected voice", "path": str(target.relative_to(book)), **audio_qa(target, .8, 30)})
            atomic_json(report_path, reports)
            continue
        if spec.get("selected_voice"):
            source = relative(book, spec["selected_voice"])
            qa = audio_qa(source, .8, 30)
            shutil.copyfile(source, target)
            reports[role] = {"method": "explicit", "source": spec["selected_voice"], **qa}
            atomic_json(report_path, reports)
            continue
        candidates = []
        for n in range(1, spec.get("candidates", 3) + 1):
            seed = spec.get("base_seed", 20260909) + ri * 100 + n
            path = book / f"voices/candidates/{role}/{role}_{n:02d}.wav"
            path.parent.mkdir(parents=True, exist_ok=True)
            qa = valid_audio(path, .8, 30)
            if qa is None:
                if model is None:
                    from qwen_tts import Qwen3TTSModel
                    model = Qwen3TTSModel.from_pretrained("Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign", device_map="cuda:0", dtype=torch.bfloat16, attn_implementation="sdpa")
                random.seed(seed); np.random.seed(seed); torch.manual_seed(seed); torch.cuda.manual_seed_all(seed)
                wavs, sr = model.generate_voice_design(text=spec["reference_text"], language=spec["language"], instruct=spec["instruct"], do_sample=True, temperature=spec.get("temperature", .9), top_p=spec.get("top_p", .95))
                temp = path.with_suffix(".tmp.wav")
                sf.write(temp, wavs[0], sr, subtype="PCM_16")
                os.replace(temp, path)
                qa = valid_audio(path, .8, 30)
            if qa:
                candidates.append({"path": str(path.relative_to(book)), "seed": seed, **qa})
        if not candidates:
            raise ValueError(f"no valid voice candidate: {role}")
        expected = max(2, len(spec["reference_text"].split()) / 2.5)
        chosen = min(candidates, key=lambda x: (abs(x["seconds"] - expected), x["path"]))
        shutil.copyfile(book / chosen["path"], target)
        reports[role] = {"method": "technical QA then closest reference duration", "selected": chosen, "candidates": candidates}
        atomic_json(report_path, reports)


def tts(book, model_dir):
    from indextts.infer_v2_5 import IndexTTS2
    manifest = book / "planning/tts_manifest.json"
    jobs = read(manifest)["jobs"]
    model = None
    for job in jobs:
        output = relative(book, job["output"])
        qa = valid_audio(output)
        if job.get("status") == "succeeded" and qa:
            continue
        temporary = output.with_suffix(".tmp.wav")
        output.parent.mkdir(parents=True, exist_ok=True)
        try:
            if model is None:
                model = IndexTTS2(cfg_path=str(model_dir / "config.yaml"), model_dir=str(model_dir), use_bf16=True)
            kwargs = {"emo_vector": job["emotion_vector"]} if "emotion_vector" in job else {}
            model.infer(spk_audio_prompt=str(relative(book, job["reference_voice"])), text=job["text"], lang=job["lang"], output_path=str(temporary), duration_factor=job.get("duration_factor", 1), max_text_tokens_per_segment=job.get("max_text_tokens_per_segment", 60), verbose=False, **kwargs)
            qa = audio_qa(temporary)
            os.replace(temporary, output)
            job.update(status="succeeded", error=None, **qa)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            job.update(status="failed", error=str(exc))
        atomic_json(manifest, {"jobs": jobs})
    validate_tts(book)
    apply_tts_video_durations(book)


def validate_tts(book):
    jobs = read(book / "planning/tts_manifest.json")["jobs"]
    rows = []
    for job in jobs:
        if job.get("required", True):
            if job["status"] != "succeeded":
                raise ValueError(f"TTS incomplete: {job['audio_id']}")
            rows.append({"audio_id": job["audio_id"], **audio_qa(relative(book, job["output"]))})
    atomic_json(book / "qa/tts_validation.json", {"status": "passed", "files": rows})


def setup_tts(model_dir):
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "uv"], check=True)
    repo = Path("/content/index-tts")
    if not (repo / ".git").exists():
        subprocess.run(["git", "clone", "--depth", "1", "https://github.com/index-tts/index-tts.git", str(repo)], check=True)
    subprocess.run(["uv", "sync"], cwd=repo, check=True)
    required = ["config.yaml", "gpt.pth", "codec.pth", "s2mel.pth", "wav2vec2bert_stats.pt"]
    if any(not (model_dir / f).is_file() or (model_dir / f).stat().st_size == 0 for f in required):
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "huggingface_hub"], check=True)
        from huggingface_hub import snapshot_download
        snapshot_download("IndexTeam/IndexTTS-2.5", local_dir=str(model_dir))
    if any(not (model_dir / f).is_file() or (model_dir / f).stat().st_size == 0 for f in required):
        raise ValueError("IndexTTS model is incomplete after download")
    return repo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stage", choices=["voice", "tts", "tts-worker", "validate-tts"])
    ap.add_argument("--book-dir", required=True)
    a = ap.parse_args()
    book = Path(a.book_dir)
    model_dir = Path("/content/drive/MyDrive/vidio/models/IndexTTS-2.5")
    if a.stage == "voice":
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "qwen-tts", "soundfile"], check=True)
        voices(book)
    elif a.stage == "tts":
        repo = setup_tts(model_dir)
        env = {**os.environ, "PYTHONPATH": str(repo)}
        subprocess.run(["uv", "run", "python", str(Path(__file__).resolve()), "tts-worker", "--book-dir", str(book)], cwd=repo, env=env, check=True)
    elif a.stage == "tts-worker":
        tts(book, model_dir)
    else:
        validate_tts(book)


if __name__ == "__main__":
    main()
