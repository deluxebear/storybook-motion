#!/usr/bin/env python3
"""Build a narrated final MP4 from reconciled Book Dash shot videos and WAVs."""
import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path


def duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        text=True, capture_output=True, check=True,
    )
    value = float(result.stdout.strip())
    if value <= 0:
        raise ValueError(f"non-positive duration: {path}")
    return value


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--book-dir", required=True)
    parser.add_argument("--output-name", default="narrated_final.mp4")
    args = parser.parse_args()
    book = Path(args.book_dir).resolve()
    videos = json.loads((book / "planning/video_manifest.json").read_text())["jobs"]
    tts = json.loads((book / "planning/tts_manifest.json").read_text())
    if isinstance(tts, dict):
        tts = tts.get("jobs", tts.get("rows", []))
    audio_by_shot = {}
    for row in tts:
        if row.get("required", True):
            audio_by_shot.setdefault(row["shot_id"], []).append(row)
    final_dir = book / "video/final"
    if Path(args.output_name).name != args.output_name:
        raise ValueError("output-name must be a filename")
    # Reuse a verified final render only for the same media and ordered manifests.
    from production_plan import digest, file_hash
    sources = []
    for job in videos:
        if job.get("required", True):
            path = Path(job["remote_output"]) if job.get("remote_output") else book / job["output"]
            sources.append((str(path), file_hash(path)))
    for row in tts:
        if row.get("required", True):
            path = book / row["output"]
            sources.append((str(path), file_hash(path)))
    render_hash = digest({"version": 1, "videos": videos, "tts": tts, "sources": sources})
    output = final_dir / args.output_name
    existing_report = final_dir / "assembly_manifest.json"
    if existing_report.exists() and output.is_file():
        prior = json.loads(existing_report.read_text())
        if prior.get("render_hash") == render_hash and prior.get("output") == str(output.relative_to(book)):
            run(["ffmpeg", "-v", "error", "-xerror", "-i", str(output), "-f", "null", "-"])
            print(output)
            return
    segments_dir = final_dir / "segments"
    segments_dir.mkdir(parents=True, exist_ok=True)
    segments: list[Path] = []
    report: list[dict] = []
    for job in videos:
        if not job.get("required", True):
            continue
        shot_id = job["shot_id"]
        if job.get("status") != "succeeded":
            raise RuntimeError(f"video is not complete: {shot_id}")
        audios = audio_by_shot.get(shot_id, [])
        if "audio_ids" in job:
            lookup = {row["audio_id"]: row for row in audios}
            if set(lookup) != set(job["audio_ids"]):
                raise RuntimeError(f"audio mapping differs: {shot_id}")
            audios = [lookup[aid] for aid in job["audio_ids"]]
        if any(row.get("status") != "succeeded" for row in audios):
            raise RuntimeError(f"narration is not complete: {shot_id}")
        video_path = Path(job["remote_output"]) if job.get("remote_output") else book / job["output"]
        if not video_path.resolve().is_relative_to((book / "video/shots").resolve()):
            raise ValueError(f"video path escapes book: {shot_id}")
        audio_paths = [book / row["output"] for row in audios]
        if not video_path.is_file() or any(not p.is_file() for p in audio_paths):
            raise FileNotFoundError(f"missing media for {shot_id}")
        # The plan's line order is the narration order, including several speakers.
        audio_path = book / "audio/mixes" / f"{shot_id}.wav"
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_audio = audio_path.with_suffix(".tmp.wav")
        if audio_paths:
            inputs = [arg for path in audio_paths for arg in ("-i", str(path))]
            filters = [f"[{i}:a]aresample=24000,aformat=sample_fmts=s16:channel_layouts=mono,asetpts=N/SR/TB[a{i}]" for i in range(len(audio_paths))]
            filters.append("".join(f"[a{i}]" for i in range(len(audio_paths))) + f"concat=n={len(audio_paths)}:v=0:a=1[out]")
            run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(filters), "-map", "[out]", "-c:a", "pcm_s16le", str(temporary_audio)])
        else:
            run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", str(duration(video_path)), "-c:a", "pcm_s16le", str(temporary_audio)])
        os.replace(temporary_audio, audio_path)
        video_seconds, audio_seconds = duration(video_path), duration(audio_path)
        target_seconds = max(video_seconds, audio_seconds)
        extension = max(0.0, target_seconds - video_seconds)
        segment = segments_dir / f"{shot_id}.mp4"
        temporary = segment.with_suffix(".tmp.mp4")
        graph = (
            f"[0:v]scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,tpad=stop_mode=clone:stop_duration={extension:.3f},fps=24,format=yuv420p[v];"
            f"[1:a]apad=pad_dur={target_seconds:.3f},atrim=duration={target_seconds:.3f},asetpts=N/SR/TB[a]"
        )
        run([
            "ffmpeg", "-y", "-v", "error", "-i", str(video_path), "-i", str(audio_path),
            "-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-t", f"{target_seconds:.3f}",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", str(temporary),
        ])
        os.replace(temporary, segment)
        report.append({"shot_id": shot_id, "audio_ids": [row["audio_id"] for row in audios], "video_seconds": video_seconds,
                       "audio_seconds": audio_seconds, "final_seconds": duration(segment)})
        segments.append(segment)
    if not segments:
        raise ValueError("no required shots to assemble")
    output = final_dir / args.output_name
    temporary_output = output.with_suffix(".tmp.mp4")
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as listing:
        for segment in segments:
            listing.write("file '" + str(segment).replace("'", "'\\''") + "'\n")
        listing_path = Path(listing.name)
    try:
        run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing_path),
             "-c", "copy", "-movflags", "+faststart", str(temporary_output)])
        os.replace(temporary_output, output)
    finally:
        listing_path.unlink(missing_ok=True)
    run(["ffmpeg", "-v", "error", "-xerror", "-i", str(output), "-f", "null", "-"])
    from projectctl import atomic_json
    atomic_json(final_dir / "assembly_manifest.json", {
        "output": str(output.relative_to(book)), "seconds": duration(output), "segments": report, "render_hash": render_hash,
    })
    print(output)


if __name__ == "__main__":
    main()
