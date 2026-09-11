#!/usr/bin/env python3
"""Compile, start, resume and inspect a Book Dash production pipeline."""
import argparse
import contextlib
import fcntl
import json
import os
import shlex
import signal
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path

from production_plan import compile_plan, digest, import_legacy, read, relative, validate
from projectctl import atomic_json

HERE = Path(__file__).resolve().parent
DRIVE = Path("/content/drive/MyDrive/vidio")


class NeedsInput(Exception):
    pass


@contextlib.contextmanager
def project_lock(book, inherited_fd=None):
    folder = book / ".pipeline"
    folder.mkdir(exist_ok=True)
    if inherited_fd is not None:
        expected, actual = (folder / "lock").stat(), os.fstat(inherited_fd)
        if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
            raise ValueError("invalid inherited project lock")
    with (os.fdopen(inherited_fd, "a+") if inherited_fd is not None else (folder / "lock").open("a+")) as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("a pipeline already owns this book")
        yield handle


class Pipeline:
    def __init__(self, book, args):
        self.book, self.args = book, args
        self.folder = book / ".pipeline"
        self.folder.mkdir(exist_ok=True)
        self.path = self.folder / "state.json"
        self.state = read(self.path) if self.path.exists() else {"stages": {}}
        self.remote_book = DRIVE / "books" / book.name
        self.log_dir = book / "logs/pipeline"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.current = "prepare"

    def save(self, **values):
        self.state.update(values, updated_at=time.time())
        atomic_json(self.path, self.state)
        summary_path = self.book / "status.json"
        summary = read(summary_path) if summary_path.exists() else {"schema_version": 1, "book_slug": self.book.name, "stages": {}}
        summary.setdefault("stages", {}).update(self.state["stages"])
        summary.update(pipeline_status=self.state.get("status"), updated_at=self.state["updated_at"],
                       l4_cleanup=self.state.get("l4_cleanup"), l4_session=self.state.get("l4_session"))
        atomic_json(summary_path, summary)

    def command(self, argv, name, timeout=None, interactive=False):
        log = self.log_dir / f"{name}.log"
        with log.open("a") as handle:
            handle.write("\nCOMMAND " + shlex.join(argv) + "\n"); handle.flush()
            result = subprocess.run(argv, stdin=None if interactive else subprocess.DEVNULL,
                                    stdout=None if interactive else handle,
                                    stderr=None if interactive else subprocess.STDOUT,
                                    timeout=timeout or self.args.timeout, check=False)
        if result.returncode:
            raise RuntimeError(f"{name} exited {result.returncode}; see {log}")

    def colab(self, *args, name="colab", timeout=None, interactive=False):
        self.command(["colab", *map(str, args)], name, timeout, interactive)

    def execute(self, session, code, name, timeout=None):
        script = self.folder / f"{name}.py"
        script.write_text(code, encoding="utf-8")
        limit = timeout or self.args.timeout
        self.colab("exec", "-s", session, "--file", script, "--timeout", str(limit), name=name, timeout=limit + 30)

    def remote_stage(self, session, stage, argv):
        token = uuid.uuid4().hex
        receipt = self.remote_book / f"logs/pipeline/{token}.json"
        local = self.folder / f"{token}.json"
        code = f'''from pathlib import Path
import fcntl, json, subprocess, os, signal, time
book=Path({str(self.remote_book)!r})
logdir=book/'logs/pipeline'; logdir.mkdir(parents=True,exist_ok=True)
with (book/'.remote-stage.lock').open('a+') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    with (logdir/{(stage + '.log')!r}).open('a') as log:
        try:
            process=subprocess.Popen({argv!r},stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
            try:
                code=process.wait(timeout={self.args.timeout})
            except subprocess.TimeoutExpired:
                os.killpg(process.pid,signal.SIGKILL); process.wait(); raise
            data={{'stage':{stage!r},'returncode':code}}
        except Exception as exc:
            data={{'stage':{stage!r},'returncode':1,'error':str(exc)}}
    target=Path({str(receipt)!r}); tmp=target.with_suffix('.tmp'); tmp.write_text(json.dumps(data)); os.replace(tmp,target)
'''
        self.execute(session, code, stage, self.args.timeout + 30)
        self.colab("download", "-s", session, receipt, local, name=f"{stage}-receipt", timeout=120)
        result = read(local)
        if result["returncode"]:
            raise RuntimeError(f"{stage} failed; see Drive logs/pipeline/{stage}.log: {result.get('error', result['returncode'])}")

    def probe(self, session, gpu=False):
        code = f'''from pathlib import Path
import subprocess, uuid, shutil
root=Path('/content/drive/MyDrive')
assert root.is_dir(), 'Google Drive is not mounted'
p=root/('.bookdash-probe-'+uuid.uuid4().hex)
try:
    p.write_text('bookdash'); assert p.read_text()=='bookdash'
finally:
    p.unlink(missing_ok=True)
assert shutil.disk_usage(root).free > 100000000, 'Drive has insufficient free space'
'''
        if gpu:
            code += "assert 'L4' in subprocess.check_output(['nvidia-smi','--query-gpu=name','--format=csv,noheader'],text=True), 'L4 required'\n"
        # A receipt avoids relying on the CLI's handling of remote Python exceptions.
        marker = f"/tmp/bookdash-probe-{uuid.uuid4().hex}.json"
        code += f"Path({marker!r}).write_text('{{\"ok\":true}}')\n"
        self.execute(session, code, "probe", 90)
        target = self.folder / "probe.json"
        target.unlink(missing_ok=True)
        self.colab("download", "-s", session, marker, target, name="probe-receipt", timeout=90)
        if read(target) != {"ok": True}:
            raise RuntimeError("Drive probe failed")

    def bundle(self, session, plan, plan_hash):
        files = {"planning/production_plan.json", "planning/compiled_plan.json", "planning/voice_specs.json",
                 "planning/tts_manifest.json", "planning/video_manifest.json", plan["workflow"], plan["bindings"]}
        files.update(s["video"]["reference_image"] for s in plan["shots"])
        files.update(s["selected_voice"] for s in plan["roles"].values() if s.get("selected_voice"))
        archive = self.folder / "inputs.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            for name in sorted(files):
                tar.add(relative(self.book, name), arcname=name, recursive=False)
            for path in HERE.glob("*.py"):
                if path.name.startswith("test_"):
                    continue
                tar.add(path, arcname=f"pipeline_scripts/{path.name}")
        remote_archive = f"/tmp/bookdash-{uuid.uuid4().hex}.tar.gz"
        self.colab("upload", "-s", session, archive, remote_archive, name="upload-inputs", timeout=600)
        code = f'''from pathlib import Path
import tarfile, json, shutil
book=Path({str(self.remote_book)!r}); book.mkdir(parents=True,exist_ok=True)
marker=book/'planning/compiled_plan.json'
if marker.exists():
    assert json.loads(marker.read_text())['plan_hash']=={plan_hash!r}, 'Drive plan differs; use a new revision'
with tarfile.open({remote_archive!r}) as tar:
    for member in tar.getmembers():
        target=(book/member.name).resolve()
        assert target.is_relative_to(book.resolve()) and member.isfile(), 'invalid bundle member'
        if member.name in ('planning/tts_manifest.json','planning/video_manifest.json') and target.exists():
            prior=json.loads(target.read_text()); prior=prior['jobs'] if isinstance(prior,dict) else prior
            incoming=json.load(tar.extractfile(member))['jobs']
            key='audio_id' if 'tts_manifest' in member.name else 'shot_id'
            old={{row[key]:row for row in prior}}
            assert set(old)=={{row[key] for row in incoming}}, 'Drive manifest IDs differ'
            fields=('text','role_id','lang','emotion_vector','duration_factor','output','reference_voice') if key=='audio_id' else ('inputs','workflow','bindings','output')
            assert all(all(old[row[key]].get(k)==row.get(k) for k in fields) for row in incoming), 'Drive manifest inputs differ'
            continue
        target.parent.mkdir(parents=True,exist_ok=True)
        with tar.extractfile(member) as src, target.open('wb') as dst: shutil.copyfileobj(src,dst)
Path({remote_archive!r}).unlink()
'''
        install_receipt = f"/tmp/bookdash-install-{uuid.uuid4().hex}.json"
        code += f"Path({install_receipt!r}).write_text('{{\"ok\":true}}')\n"
        self.execute(session, code, "install-inputs", 120)
        checked = self.folder / "install-receipt.json"
        checked.unlink(missing_ok=True)
        self.colab("download", "-s", session, install_receipt, checked, name="install-receipt", timeout=120)
        if read(checked) != {"ok": True}:
            raise RuntimeError("input installation did not complete")
        # Verify that install succeeded before any model work.
        local = self.folder / "remote-plan.json"
        local.unlink(missing_ok=True)
        self.colab("download", "-s", session, self.remote_book / "planning/compiled_plan.json", local, name="check-plan", timeout=120)
        if read(local)["plan_hash"] != plan_hash:
            raise ValueError("Drive plan differs")

    def sync(self, session, names):
        for name in names:
            destination = self.book / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            temp = destination.with_suffix(destination.suffix + ".download")
            temp.unlink(missing_ok=True)
            self.colab("download", "-s", session, self.remote_book / name, temp, name="sync-metadata", timeout=120)
            data = read(temp)
            atomic_json(destination, data)
            temp.unlink()

    def stage(self, name, action):
        self.current = name
        self.state["stages"][name] = {"status": "running", "started_at": time.time()}
        self.save(current_stage=name)
        action()
        self.state["stages"][name].update(status="succeeded", completed_at=time.time())
        self.save()

    def stop_l4(self):
        session = self.state.get("l4_session")
        if not session:
            return
        try:
            self.colab("stop", "-s", session, name="stop-l4", timeout=120)
        except Exception:
            pass
        self.colab("sessions", name="verify-stop", timeout=120)
        # Match the exact name in the CLI's server-synchronized session listing.
        import re
        listing = (self.log_dir / "verify-stop.log").read_text().split("COMMAND colab sessions")[-1]
        if re.search(r"(?<![\w-])" + re.escape(session) + r"(?![\w-])", listing):
            raise RuntimeError(f"L4 is still listed: {session}")
        self.save(l4_session=None, l4_handoff=None, l4_cleanup={"status": "succeeded", "session": session})

    def require_terminal(self):
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise NeedsInput("Drive authorization needs a persistent PTY. Run pipeline.py start in a PTY; open its authorization URL and return any code to that same terminal.")

    def prepare_l4(self):
        """Complete browser-assisted authorization before detaching the supervisor."""
        _, fingerprint = compile_plan(self.book)
        if self.state.get("plan_hash", fingerprint) != fingerprint:
            raise ValueError("pipeline state belongs to another production plan")
        if self.state["stages"].get("tts", {}).get("status") == "succeeded":
            return
        if self.state.get("l4_session") and self.state.get("l4_handoff") == "ready":
            try:
                self.probe(self.state["l4_session"], gpu=True)
            except BaseException as exc:
                self.save(status="failed", error=str(exc))
                self.stop_l4()
                raise
            return
        # Check before allocating or touching an existing runtime.
        try:
            self.require_terminal()
        except NeedsInput as exc:
            self.save(status="needs_input", error=str(exc))
            raise
        if self.state.get("l4_session"):
            self.stop_l4()
        session = f"bookdash-{self.book.name}-tts-{uuid.uuid4().hex[:8]}"
        self.save(status="authorizing", plan_hash=fingerprint, l4_session=session,
                  l4_handoff="authorizing", error=None)
        try:
            self.stage("l4", lambda: self.colab("new", "-s", session, "--gpu", "L4", name="start-l4", timeout=300))
            def mount():
                # Inherit the live PTY, including stdin. Keep this exact process
                # alive while the agent completes Google consent in the browser.
                self.colab("drivemount", "-s", session, name="mount-drive", timeout=1800, interactive=True)
                self.probe(session, gpu=True)
            self.stage("drive", mount)
            self.save(status="ready", l4_handoff="ready")
        except BaseException as exc:
            self.state["stages"].setdefault(self.current, {}).update(status="failed", error=str(exc))
            self.save(status="failed", error=str(exc))
            self.stop_l4()
            raise

    def run(self):
        plan, plan_hash = compile_plan(self.book)
        if self.state.get("plan_hash", plan_hash) != plan_hash:
            raise ValueError("pipeline state belongs to another production plan")
        self.save(status="running", pid=os.getpid(), plan_hash=plan_hash, error=None,
                  drive_project=str(self.remote_book), assemble=self.args.assemble)
        try:
            # A verified authorization handoff is deliberately adopted, not stopped.
            if self.state.get("l4_session") and (self.state.get("l4_handoff") != "ready" or self.state["stages"].get("tts", {}).get("status") == "succeeded"):
                self.stop_l4()
            if self.state["stages"].get("tts", {}).get("status") != "succeeded":
                self.prepare_l4()
                session = self.state["l4_session"]
                try:
                    self.save(status="running", l4_handoff="consumed")
                    def inputs():
                        self.bundle(session, plan, plan_hash)
                        self.sync(session, ["planning/tts_manifest.json", "planning/video_manifest.json"])
                    self.stage("inputs", inputs)
                    script = str(self.remote_book / "pipeline_scripts/remote_stages.py")
                    self.stage("voice_design", lambda: self.remote_stage(session, "voice", ["python", script, "voice", "--book-dir", str(self.remote_book)]))
                    def synthesize():
                        self.remote_stage(session, "tts", ["python", script, "tts", "--book-dir", str(self.remote_book)])
                        self.sync(session, ["planning/tts_manifest.json", "planning/video_manifest.json", "qa/tts_validation.json", "qa/voice_selection.json"])
                    self.stage("tts", synthesize)
                finally:
                    self.stop_l4()
            if self.args.until == "tts":
                self.save(status="succeeded", current_stage="tts")
                return
            from production_plan import apply_tts_video_durations
            apply_tts_video_durations(self.book)
            if not self.args.comfy_url:
                raise NeedsInput("TTS complete. Supply --comfy-url for the user-run ComfyUI endpoint and resume.")
            from comfy_batch import request_json
            try:
                base = self.args.comfy_url.split("#")[0].rstrip("/")
                request_json(base + "/system_stats")
                info = request_json(base + "/object_info")
                missing = {node["class_type"] for node in read(relative(self.book, plan["workflow"])).values()} - set(info)
                if missing:
                    raise ValueError(f"missing ComfyUI nodes: {sorted(missing)}")
            except Exception as exc:
                raise NeedsInput(f"ComfyUI endpoint needs attention: {exc}") from exc
            atomic_json(self.book / "comfyui/endpoint.json", {"base_url": base, "checked_at": time.time()})
            self.save(comfy_url=base)
            video_command = [sys.executable, str(HERE / "comfy_batch.py"), "--book-dir", str(self.book),
                             "--base-url", base, "--timeout", str(self.args.shot_timeout),
                             "--reference-mode", self.args.reference_mode,
                             "--drive-input-prefix", self.args.drive_input_prefix]
            if self.args.comfy_restarted:
                video_command.append("--comfy-restarted")
            self.stage("video", lambda: self.command(video_command, "video", timeout=self.args.timeout))
            if self.args.assemble:
                if not self.args.assembly_session:
                    raise NeedsInput("Videos complete. Supply --assembly-session for the existing A100 with this Drive mounted.")
                session = self.args.assembly_session
                self.probe(session)
                # Transfer JSON/scripts only; generated media remain on Drive.
                self.bundle(session, plan, plan_hash)
                remote_manifest = self.remote_book / "planning/video_manifest.json"
                self.colab("upload", "-s", session, self.book / "planning/video_manifest.json", remote_manifest, name="upload-video-metadata", timeout=120)
                self.stage("final", lambda: self.remote_stage(session, "final", ["python", str(self.remote_book / "pipeline_scripts/assemble_final.py"), "--book-dir", str(self.remote_book)]))
                self.sync(session, ["video/final/assembly_manifest.json"])
            self.save(status="succeeded", current_stage="final" if self.args.assemble else "video")
        except BaseException as exc:
            status = "needs_input" if isinstance(exc, NeedsInput) else "failed"
            if self.state["stages"].get(self.current, {}).get("status") == "running":
                self.state["stages"][self.current].update(status=status, error=str(exc))
            self.save(status=status, error=str(exc))
            raise


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("command", choices=["import-legacy", "validate", "compile", "start", "run", "status", "stop-l4"])
    ap.add_argument("--book-dir", required=True)
    ap.add_argument("--comfy-url")
    ap.add_argument("--assembly-session")
    ap.add_argument("--assemble", action="store_true", help="explicitly request final narrated MP4")
    ap.add_argument("--until", choices=["tts", "video"], default="video")
    ap.add_argument("--timeout", type=int, default=14400, help="maximum seconds per stage")
    ap.add_argument("--shot-timeout", type=int, default=3600)
    ap.add_argument("--comfy-restarted", action="store_true",
                    help="confirm ComfyUI restarted so missing old prompt IDs may be retried")
    ap.add_argument("--reference-mode", choices=("upload", "drive"), default="upload",
                    help="use uploaded images, or references directly from the ComfyUI Drive input mapping")
    ap.add_argument("--drive-input-prefix", default="vidio",
                    help="relative ComfyUI input/ path mapped to the shared Drive root")
    ap.add_argument("--interactive", action="store_true", help="allow foreground Drive OAuth consent")
    ap.add_argument("--lock-fd", type=int, help=argparse.SUPPRESS)
    args = ap.parse_args()
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"interrupted by signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    book = Path(args.book_dir).expanduser().resolve()
    try:
        if args.timeout <= 0 or args.shot_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if args.assemble and args.until == "tts":
            raise ValueError("--assemble conflicts with --until tts")
        if args.command == "status":
            print(json.dumps(read(book / ".pipeline/state.json"), ensure_ascii=False, indent=2)); return
        if args.command == "validate":
            validate(book, read(book / "planning/production_plan.json")); print("VALID"); return
        if args.command == "start":
            validate(book, read(book / "planning/production_plan.json"))
            with project_lock(book) as lock:
                compile_plan(book)
                pipeline = Pipeline(book, args)
                pipeline.prepare_l4()
                folder = book / ".pipeline"
                try:
                    with (folder / "supervisor.log").open("a") as log:
                        argv = [sys.executable, str(Path(__file__).resolve()), "run", *sys.argv[2:], "--lock-fd", str(lock.fileno())]
                        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, pass_fds=(lock.fileno(),))
                except BaseException:
                    pipeline.stop_l4()
                    raise
            print(json.dumps({"launched_pid": process.pid, "state": str(folder / "state.json"), "log": str(folder / "supervisor.log")})); return
        with project_lock(book, args.lock_fd):
            if args.command == "import-legacy":
                import_legacy(book, book / "planning/production_plan.json"); print("IMPORTED"); return
            if args.command == "compile":
                _, fingerprint = compile_plan(book); print(fingerprint); return
            pipeline = Pipeline(book, args)
            if args.command == "stop-l4":
                pipeline.stop_l4(); return
            pipeline.run()
    except NeedsInput as exc:
        print(str(exc), file=sys.stderr); raise SystemExit(2)
    except (Exception, KeyboardInterrupt) as exc:
        print(str(exc), file=sys.stderr); raise SystemExit(1)


if __name__ == "__main__":
    main()
