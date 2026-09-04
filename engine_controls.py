"""Uniform job controls for embedded engines that only expose read APIs."""

from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path
from types import ModuleType

from flask import jsonify


TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
STOPPABLE_STATUSES = {"queued", "preparing", "downloading", "processing"}
_watchers: set[tuple[str, str]] = set()
_watchers_lock = threading.Lock()


def _remove_from_queue(job_queue, job_id: str) -> bool:
    """Remove one pending item while keeping queue.Queue accounting correct."""
    with job_queue.mutex:
        try:
            job_queue.queue.remove(job_id)
        except ValueError:
            return False
        job_queue.unfinished_tasks = max(0, job_queue.unfinished_tasks - 1)
        if job_queue.unfinished_tasks == 0:
            job_queue.all_tasks_done.notify_all()
        return True


def _child_processes(pid: int) -> list[int]:
    """Return Linux child processes without adding a runtime dependency."""
    path = Path(f"/proc/{pid}/task/{pid}/children")
    try:
        return [int(value) for value in path.read_text(encoding="utf-8").split()]
    except (OSError, ValueError):
        return []


def _signal_process_tree(pid: int, sig: signal.Signals) -> None:
    # yt-dlp can have ffmpeg children. Signal those first, but never use a
    # process group because the engines share Gunicorn's process group.
    for child_pid in _child_processes(pid):
        _signal_process_tree(child_pid, sig)
    try:
        os.kill(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _cancel_watcher(service: str, module: ModuleType, job_id: str) -> None:
    key = (service, job_id)
    signalled_at: dict[int, float] = {}
    deadline = time.monotonic() + 180
    try:
        while time.monotonic() < deadline:
            with module.jobs_lock:
                job = module.jobs.get(job_id)
                if job is None or not job.get("cancel_requested"):
                    return
                status = str(job.get("status", ""))
                pid = job.get("process_id")

                if status == "completed":
                    # A final move won a race with the stop request. Keep the
                    # successfully completed result instead of mislabelling it.
                    job["cancel_requested"] = False
                    return
                if status in {"failed", "cancelled"} and not pid:
                    job.update(
                        status="cancelled",
                        message="Stopped",
                        finished_at=module.utc_now(),
                        process_id=None,
                    )
                    return

            if isinstance(pid, int) and pid > 1:
                first_signal = signalled_at.get(pid)
                if first_signal is None:
                    _signal_process_tree(pid, signal.SIGTERM)
                    signalled_at[pid] = time.monotonic()
                elif time.monotonic() - first_signal > 5:
                    _signal_process_tree(pid, signal.SIGKILL)
            time.sleep(0.1)
    finally:
        with _watchers_lock:
            _watchers.discard(key)


def _start_cancel_watcher(service: str, module: ModuleType, job_id: str) -> None:
    key = (service, job_id)
    with _watchers_lock:
        if key in _watchers:
            return
        _watchers.add(key)
    threading.Thread(
        target=_cancel_watcher,
        args=(service, module, job_id),
        daemon=True,
        name=f"{service}-cancel-{job_id}",
    ).start()


def _reset_job(service: str, job: dict) -> None:
    common = {
        "status": "queued",
        "message": (
            "Waiting for a worker — stream will restart from the beginning"
            if service == "hls"
            else "Waiting for a worker — partial media will resume when possible"
        ),
        "progress": 0.0,
        "started_at": None,
        "finished_at": None,
        "return_code": None,
        "worker_number": None,
        "process_id": None,
        "output_path": None,
        "output_bytes": None,
        "estimated_bytes": None,
        "cancel_requested": False,
    }
    if service == "hls":
        common.update(
            downloaded_seconds=None,
            downloaded_bytes=None,
            speed=None,
            storage_mode="pending",
        )
    else:
        common.update(
            downloaded_size="",
            total_size="",
            speed="",
            eta="",
            storage_mode=None,
            working_directory=None,
        )
    job.update(common)
    job.setdefault("log", []).append("--- Retry queued ---")


def install_engine_controls(service: str, module: ModuleType) -> None:
    """Add the common control API to an imported HLS or YouTube engine."""
    if service not in {"hls", "youtube"}:
        return

    app = module.app

    def has_route(rule: str, method: str) -> bool:
        return any(
            existing.rule == rule and method in existing.methods
            for existing in app.url_map.iter_rules()
        )

    def cancel_job(job_id: str):
        with module.jobs_lock:
            job = module.jobs.get(job_id)
            if job is None:
                return jsonify({"error": "Download not found."}), 404
            status = str(job.get("status", ""))
            if status in TERMINAL_STATUSES:
                return jsonify({"error": "That download is already finished."}), 409
            if status == "moving":
                return jsonify({"error": "The completed download is already being moved into place."}), 409
            if status not in STOPPABLE_STATUSES and status != "cancelling":
                return jsonify({"error": f"A {status or 'pending'} download cannot be stopped."}), 409

            job["cancel_requested"] = True
            if status == "queued" and _remove_from_queue(module.job_queue, job_id):
                job.update(
                    status="cancelled",
                    message="Stopped before download",
                    finished_at=module.utc_now(),
                )
                return jsonify({"ok": True, "status": "cancelled"})

            job["status"] = "cancelling"
            job["message"] = "Stopping downloader…"

        _start_cancel_watcher(service, module, job_id)
        return jsonify({"ok": True, "status": "cancelling"}), 202

    def retry_job(job_id: str):
        with module.jobs_lock:
            job = module.jobs.get(job_id)
            if job is None:
                return jsonify({"error": "Download not found."}), 404
            if job.get("status") not in {"failed", "cancelled"}:
                return jsonify({"error": "Only failed or stopped downloads can be retried."}), 409
            _reset_job(service, job)
        module.job_queue.put(job_id)
        return jsonify({"ok": True, "status": "queued"})

    def delete_job(job_id: str):
        with module.jobs_lock:
            job = module.jobs.get(job_id)
            if job is None:
                return jsonify({"error": "Download not found."}), 404
            if job.get("status") not in TERMINAL_STATUSES:
                return jsonify({"error": "Stop the active download before deleting it."}), 409
            module.jobs.pop(job_id, None)

        # The HLS engine gives each local job its own directory, so cleaning it
        # cannot touch another download. YouTube's temporary directory is
        # shared and is intentionally left for yt-dlp's resume support.
        if service == "hls":
            temp_root = Path(module.TEMP_DOWNLOAD_DIR)
            job_dir = temp_root / job_id
            try:
                if job_dir.parent == temp_root and job_dir.is_dir():
                    import shutil

                    shutil.rmtree(job_dir)
            except OSError:
                pass
            try:
                (Path(module.DOWNLOAD_DIR) / f".hls-{job_id}.part.mp4").unlink(missing_ok=True)
            except OSError:
                pass
        return jsonify({"ok": True})

    if not has_route("/api/jobs/<job_id>/cancel", "POST"):
        app.add_url_rule(
            "/api/jobs/<job_id>/cancel",
            endpoint=f"download_central_{service}_cancel",
            view_func=cancel_job,
            methods=["POST"],
        )
    if not has_route("/api/jobs/<job_id>/retry", "POST"):
        app.add_url_rule(
            "/api/jobs/<job_id>/retry",
            endpoint=f"download_central_{service}_retry",
            view_func=retry_job,
            methods=["POST"],
        )
    if not has_route("/api/jobs/<job_id>", "DELETE"):
        app.add_url_rule(
            "/api/jobs/<job_id>",
            endpoint=f"download_central_{service}_delete",
            view_func=delete_job,
            methods=["DELETE"],
        )
