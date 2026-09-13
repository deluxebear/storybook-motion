"""Repository-level, self-recovering book pipeline supervisor."""
import argparse
import concurrent.futures
import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from projectctl import atomic_json
from production_plan import compile_plan, read, validate


def _paths(args):
    books = Path(args.books_dir).expanduser().resolve()
    root = books.parent
    runtime = root / ".pipeline"
    runtime.mkdir(parents=True, exist_ok=True)
    return books, runtime, runtime / "watcher-state.json"


def _alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


def _endpoint(args):
    if args.comfy_url:
        return args.comfy_url.strip()
    path = Path(args.comfy_url_file).expanduser().resolve()
    if not path.is_file():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def _notify(runtime, state, key, message):
    """Persist every notification and best-effort deliver it to the desktop."""
    keys = state.setdefault("notification_keys", [])
    if key in keys:
        return
    event = {"at": time.time(), "key": key, "message": message}
    with (runtime / "notifications.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")
    if shutil_which("notify-send"):
        subprocess.run(["notify-send", "Vidio pipeline needs attention", message],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10, check=False)
    keys.append(key)


def shutil_which(command):
    # Kept local so the watcher has no optional dependency.
    from shutil import which
    return which(command)


def _book_args(args, book, endpoint):
    values = vars(args).copy()
    values.update(command="run", book_dir=str(book), comfy_url=endpoint, lock_fd=None)
    return argparse.Namespace(**values)


def _is_complete(book, args):
    path = book / ".pipeline/state.json"
    if not path.is_file():
        return False
    state = read(path)
    expected = "tts" if args.until == "tts" else ("final" if args.assemble else "video")
    return state.get("status") == "succeeded" and state.get("current_stage") == expected


def _next_stage(book, args):
    """Return the first incomplete dependency stage for one frozen book."""
    path = book / ".pipeline/state.json"
    state = read(path) if path.is_file() else {"stages": {}}
    stages = state.get("stages", {})
    if _is_complete(book, args):
        return None
    if stages.get("tts", {}).get("status") == "succeeded":
        if args.until == "tts":
            return None
        if stages.get("video", {}).get("status") != "succeeded":
            return "video"
        if args.assemble and stages.get("final", {}).get("status") != "succeeded":
            return "final"
        return None
    if stages.get("voice_design", {}).get("status") == "succeeded":
        return "tts"
    return "voice"


def _discover(books):
    if not books.is_dir():
        return []
    return [path for path in sorted(books.iterdir(), key=lambda p: p.name)
            if path.is_dir() and (path / "planning/production_plan.json").is_file()]


def _run_stage(book, args, endpoint, stage):
    from pipeline import Pipeline, project_lock
    with project_lock(book):
        Pipeline(book, _book_args(args, book, endpoint)).run_stage(stage)


def watch(args):
    from pipeline import NeedsInput

    books, runtime, state_path = _paths(args)
    lock_path = runtime / "watcher.lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("a repository watcher is already running") from exc
        state = read(state_path) if state_path.is_file() else {"schema_version": 1, "books": {}}
        state.update(status="running", pid=os.getpid(), books_dir=str(books),
                     workers={"voice": args.voice_session, "tts": args.tts_session},
                     assembly={"enabled": args.assemble,
                               "session": args.assembly_session if args.assemble else None},
                     started_at=state.get("started_at", time.time()), updated_at=time.time())
        state["scheduler"] = "voice_tts_video_final_pipeline"
        state["slots"] = {"voice": None, "tts": None, "video": None, "final": None}
        atomic_json(state_path, state)
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="vidio-stage")
        active = {}
        try:
            while True:
                now = time.time()
                endpoint = _endpoint(args)

                # Finalize stage results in the main thread so watcher state has
                # one writer even while three books execute concurrently.
                for stage, task in list(active.items()):
                    if not task["future"].done():
                        continue
                    book = task["book"]
                    record = state["books"].setdefault(book.name, {})
                    try:
                        task["future"].result()
                        record.update(status="stage_succeeded", stage=stage, error=None,
                                      updated_at=now)
                        record.setdefault("retry_at", {})[stage] = 0
                        if stage == "final" or (stage == "video" and not args.assemble):
                            record.update(status="succeeded", completed_at=now)
                        if stage == "video":
                            state["notification_keys"] = [
                                key for key in state.get("notification_keys", [])
                                if not key.startswith("comfy:")]
                    except NeedsInput as exc:
                        message = str(exc)
                        record.update(status="needs_input", stage=stage, error=message, updated_at=now)
                        record.setdefault("retry_at", {})[stage] = now + args.retry_delay
                        key = f"comfy:{endpoint or 'missing'}" if "ComfyUI" in message else f"input:{book.name}:{message}"
                        _notify(runtime, state, key, message)
                    except Exception as exc:
                        message = str(exc)
                        record.update(status="failed", stage=stage, error=message, updated_at=now)
                        record.setdefault("retry_at", {})[stage] = now + args.retry_delay
                        kind = "workers" if stage in ("voice", "tts") else "book"
                        _notify(runtime, state, f"{kind}:{book.name}:{message}", message)
                    del active[stage]

                active_books = {task["book"].name for task in active.values()}
                candidates = {"voice": [], "tts": [], "video": [], "final": []}
                for book in _discover(books):
                    if book.name in active_books:
                        continue
                    record = state["books"].setdefault(book.name, {})
                    retry_at = record.setdefault("retry_at", {})
                    if now < retry_at.get("prepare", 0):
                        continue
                    try:
                        plan = read(book / "planning/production_plan.json")
                        validate(book, plan)
                        _, plan_hash = compile_plan(book)
                        stage = _next_stage(book, args)
                    except Exception as exc:
                        message = str(exc)
                        record.update(status="failed", error=message, updated_at=now)
                        retry_at["prepare"] = now + args.retry_delay
                        _notify(runtime, state, f"book:{book.name}:{message}", message)
                        continue
                    record["plan_hash"] = plan_hash
                    retry_at["prepare"] = 0
                    if stage is None:
                        record.update(status="succeeded", error=None, updated_at=now)
                        continue
                    stage_retry_at = retry_at.get(stage, record.get("next_retry_at", 0))
                    if now >= stage_retry_at:
                        candidates[stage].append(book)

                # Each slot owns one resource. Different slots may work on
                # different books simultaneously; dependencies come from
                # _next_stage, never from queue timing.
                for stage in ("tts", "voice", "video", "final"):
                    if stage in active or not candidates[stage]:
                        continue
                    book = candidates[stage][0]
                    record = state["books"][book.name]
                    attempts = record.setdefault("attempts_by_stage", {})
                    attempts[stage] = attempts.get(stage, 0) + 1
                    record.update(status="running", stage=stage, started_at=now, error=None)
                    future = executor.submit(_run_stage, book, args, endpoint, stage)
                    active[stage] = {"book": book, "future": future}

                state["slots"] = {stage: active[stage]["book"].name if stage in active else None
                                  for stage in ("voice", "tts", "video", "final")}
                state.update(status="running" if active else "idle", current_book=None,
                             updated_at=time.time())
                atomic_json(state_path, state)
                time.sleep(args.scan_interval)
        finally:
            executor.shutdown(wait=False, cancel_futures=True)


def start(args):
    _, runtime, state_path = _paths(args)
    prior = {"schema_version": 1, "books": {}}
    if state_path.is_file():
        prior = read(state_path)
        if _alive(prior.get("pid")):
            raise ValueError(f"repository watcher is already running: {prior['pid']}")
    log_path = runtime / "watcher.log"
    argv = [sys.executable, str(Path(__file__).resolve().parent / "pipeline.py"), "watch"]
    skip = {"command", "book_dir", "lock_fd"}
    defaults = {"assemble": True, "assembly_session": "ComfyUI", "comfy_restarted": False}
    for key, value in vars(args).items():
        if key in skip or value is None or value == defaults.get(key):
            continue
        option = "--" + key.replace("_", "-")
        if isinstance(value, bool):
            if value:
                argv.append(option)
        else:
            argv.extend([option, str(value)])
    prior.update(status="launching", pid=None, updated_at=time.time())
    atomic_json(state_path, prior)
    with log_path.open("a", encoding="utf-8") as log:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
    current = read(state_path)
    current.update(pid=process.pid, updated_at=time.time())
    atomic_json(state_path, current)
    print(json.dumps({"launched_pid": process.pid, "state": str(state_path), "log": str(log_path)}))


def dispatch(args):
    _, _, state_path = _paths(args)
    if args.command == "watch-start":
        start(args)
    elif args.command == "watch-status":
        if not state_path.is_file():
            raise ValueError("repository watcher has not been started")
        print(json.dumps(read(state_path), ensure_ascii=False, indent=2))
    else:
        watch(args)
