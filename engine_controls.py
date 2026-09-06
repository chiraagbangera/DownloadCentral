"""Uniform job controls for embedded engines that only expose read APIs."""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from functools import wraps
from pathlib import Path
from types import ModuleType

from flask import jsonify, request


TERMINAL_STATUSES = {"completed", "failed", "cancelled"}
STOPPABLE_STATUSES = {"queued", "preparing", "downloading", "processing"}
YOUTUBE_DEFAULT_PREFERENCES = {
    "resolution": "2160",
    "codec": "smart",
    "container": "mp4",
}
YOUTUBE_PREFERENCE_CHOICES = {
    "resolution": {"2160", "1440", "1080", "720", "480", "360"},
    "codec": {"smart", "h265", "av1", "vp9", "h264"},
    "container": {"mp4", "mkv"},
}
YOUTUBE_QUALITY_MARKER = "__DC_QUALITY__|"
YOUTUBE_QUALITY_TEMPLATE = (
    "before_dl:" + YOUTUBE_QUALITY_MARKER
    + '{"video":[%(requested_formats.0.format_id)j,'
    '%(requested_formats.0.resolution)j,%(requested_formats.0.height)j,'
    '%(requested_formats.0.width)j,%(requested_formats.0.fps)j,'
    '%(requested_formats.0.vcodec)j,%(requested_formats.0.dynamic_range)j,'
    '%(requested_formats.0.vbr)j,%(requested_formats.0.tbr)j,'
    '%(requested_formats.0.ext)j,%(requested_formats.0.format_note)j],'
    '"audio":[%(requested_formats.1.format_id)j,'
    '%(requested_formats.1.acodec)j,%(requested_formats.1.abr)j,'
    '%(requested_formats.1.tbr)j,%(requested_formats.1.asr)j,'
    '%(requested_formats.1.audio_channels)j,%(requested_formats.1.ext)j,'
    '%(requested_formats.1.format_note)j,%(requested_formats.1.language)j],'
    '"combined":[%(format_id)j,%(resolution)j,%(height)j,%(width)j,'
    '%(fps)j,%(vcodec)j,%(dynamic_range)j,%(vbr)j,%(tbr)j,%(ext)j,'
    '%(format_note)j,%(acodec)j,%(abr)j,%(asr)j,'
    '%(audio_channels)j,%(language)j]}'
)
_watchers: set[tuple[str, str]] = set()
_watchers_lock = threading.Lock()


def normalize_youtube_preferences(
    value: object,
    defaults: dict[str, str] | None = None,
) -> dict[str, str]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("YouTube preferences must be an object.")
    result = dict(YOUTUBE_DEFAULT_PREFERENCES)
    if defaults:
        result.update(defaults)
    for name, choices in YOUTUBE_PREFERENCE_CHOICES.items():
        if name not in value:
            continue
        selected = str(value[name]).strip().lower()
        if selected not in choices:
            raise ValueError(f"Unsupported YouTube {name}: {selected or 'empty'}.")
        result[name] = selected
    return result


def build_youtube_format_selector(preferences: dict[str, str]) -> str:
    preferences = normalize_youtube_preferences(preferences)
    height = preferences["resolution"]
    height_filter = f"[height<={height}]"
    codec_filters = {
        "av1_hdr": "[vcodec^=av01][dynamic_range~='^(HDR|HLG|DV)']",
        "h265_hdr": "[vcodec~='^(hvc1|hev1|hevc|h265)'][dynamic_range~='^(HDR|HLG|DV)']",
        "h265": "[vcodec~='^(hvc1|hev1|hevc|h265)']",
        "h264": "[vcodec^=avc1]",
        "vp9_hdr": "[vcodec~='^(vp09|vp9)'][dynamic_range~='^(HDR|HLG|DV)']",
        "vp9": "[vcodec~='^(vp09|vp9)']",
        "av1": "[vcodec^=av01]",
        "non_av1": "[vcodec!^=av01]",
        "any": "",
    }
    smart_order = ["av1_hdr", "h265_hdr", "h265", "h264", "vp9_hdr", "vp9", "non_av1", "any"]
    preferred = {
        "smart": [],
        "h265": ["h265"],
        "av1": ["av1"],
        "vp9": ["vp9"],
        "h264": ["h264"],
    }[preferences["codec"]]
    order: list[str] = []
    for name in preferred + smart_order:
        if name not in order:
            order.append(name)

    audio_filters = ["bestaudio[ext=m4a]", "bestaudio"] if preferences["container"] == "mp4" else ["bestaudio"]
    selectors: list[str] = []

    def add_video(video: str) -> None:
        selectors.extend(f"({video}+{audio})" for audio in audio_filters)

    # An explicit codec choice is codec-first. Smart is resolution-first so a
    # missing 4K HEVC stream does not silently turn an available 4K VP9 stream
    # into a 1080p download; codec priority is applied inside each quality tier.
    for name in preferred:
        add_video(f"bestvideo{height_filter}{codec_filters[name]}")
    resolution_steps = [2160, 1440, 1080, 720, 480, 360]
    for resolution in (value for value in resolution_steps if value <= int(height)):
        for name in smart_order:
            # YouTube's nominal 2160p/1440p/etc. label survives cropped cinema
            # and portrait sources whose literal pixel height is non-standard
            # (for example 3840x2026 is still the 2160p representation).
            add_video(f"bestvideo[format_note^={resolution}p]{codec_filters[name]}")
    for name in order:
        add_video(f"bestvideo{height_filter}{codec_filters[name]}")

    # Combined formats cover videos where separate streams are unavailable.
    if preferences["container"] == "mp4":
        selectors.append(f"best{height_filter}[ext=mp4]")
    selectors.append(f"best{height_filter}")
    return "/".join(selectors)


YOUTUBE_FORMAT_SELECTOR = build_youtube_format_selector(YOUTUBE_DEFAULT_PREFERENCES)


def _present(value):
    return None if value in {None, "", "NA", "none"} else value


def _video_quality(values: list) -> dict | None:
    if len(values) < 11 or not _present(values[5]):
        return None
    return {
        "format_id": _present(values[0]),
        "resolution": _present(values[1]),
        "height": _present(values[2]),
        "width": _present(values[3]),
        "fps": _present(values[4]),
        "codec": _present(values[5]),
        "dynamic_range": _present(values[6]),
        "bitrate_kbps": _present(values[7]) or _present(values[8]),
        "container": _present(values[9]),
        "note": _present(values[10]),
    }


def _audio_quality(values: list) -> dict | None:
    if len(values) < 9 or not _present(values[1]):
        return None
    return {
        "format_id": _present(values[0]),
        "codec": _present(values[1]),
        "bitrate_kbps": _present(values[2]) or _present(values[3]),
        "sample_rate_hz": _present(values[4]),
        "channels": _present(values[5]),
        "container": _present(values[6]),
        "note": _present(values[7]),
        "language": _present(values[8]),
    }


def _quality_from_payload(payload: dict) -> tuple[dict | None, dict | None]:
    video_values = payload.get("video") if isinstance(payload.get("video"), list) else []
    audio_values = payload.get("audio") if isinstance(payload.get("audio"), list) else []
    combined = payload.get("combined") if isinstance(payload.get("combined"), list) else []
    video = _video_quality(video_values)
    audio = _audio_quality(audio_values)

    # A single-file fallback has no requested_formats list. Use its top-level
    # fields for both tracks so quality is still visible on older/smaller media.
    if len(combined) >= 16:
        video = video or _video_quality(combined[:11])
        audio = audio or _audio_quality(
            [
                combined[0], combined[11], combined[12], combined[8],
                combined[13], combined[14], combined[9], combined[10], combined[15],
            ]
        )
    return video, audio


def _replace_command_option(command: list[str], option: str, value: str) -> None:
    try:
        index = command.index(option)
    except ValueError:
        command[-1:-1] = [option, value]
        return
    if index + 1 < len(command):
        command[index + 1] = value


def _install_youtube_quality_tracking(
    module: ModuleType,
    default_preferences: dict[str, str] | None = None,
) -> None:
    module._download_central_default_preferences = normalize_youtube_preferences(default_preferences)
    if getattr(module, "_download_central_quality_tracking", False):
        return
    if not all(hasattr(module, name) for name in ("build_command", "append_log")):
        return

    original_build_command = module.build_command
    original_append_log = module.append_log
    module.FORMAT_SELECTOR = build_youtube_format_selector(module._download_central_default_preferences)

    def build_command(job: dict) -> list[str]:
        preferences = normalize_youtube_preferences(
            job.get("preferences"),
            module._download_central_default_preferences,
        )
        job["preferences"] = preferences
        command = list(original_build_command(job))
        _replace_command_option(command, "--format", build_youtube_format_selector(preferences))
        requested_container = preferences["container"]
        fallback_container = "mkv" if requested_container == "mp4" else "mp4"
        container_order = f"{requested_container}/{fallback_container}"
        _replace_command_option(command, "--merge-output-format", container_order)
        # Remux mappings are source>target rules, not preference lists. For MP4,
        # preserve a merger-selected MKV fallback and turn other combined-file
        # formats into MKV rather than forcing an incompatible MP4 remux.
        remux_mapping = (
            "mp4>mp4/mkv>mkv/webm>mkv/mkv"
            if requested_container == "mp4"
            else "mkv"
        )
        # Reset only format sorting inherited from a user config. Other config
        # options (cookies, throttling, extractor args, etc.) remain available.
        return command[:-1] + [
            "--format-sort-reset",
            "--remux-video",
            remux_mapping,
            "--print",
            YOUTUBE_QUALITY_TEMPLATE,
            command[-1],
        ]

    def append_log(job_id: str, line: str) -> None:
        stripped = line.strip()
        if stripped.startswith(YOUTUBE_QUALITY_MARKER):
            try:
                payload = json.loads(stripped[len(YOUTUBE_QUALITY_MARKER):])
                video, audio = _quality_from_payload(payload)
            except (json.JSONDecodeError, TypeError, ValueError):
                original_append_log(job_id, line)
                return
            with module.jobs_lock:
                job = module.jobs.get(job_id)
                if job is not None:
                    job["video_quality"] = video
                    job["audio_quality"] = audio
            return
        original_append_log(job_id, line)

    module.build_command = build_command
    module.append_log = append_log
    module._download_central_quality_tracking = True


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


def _put_at_front(job_queue, job_id: str) -> None:
    """Requeue an interrupted job ahead of pending work."""
    with job_queue.not_full:
        job_queue.queue.appendleft(job_id)
        job_queue.unfinished_tasks += 1
        job_queue.not_empty.notify()


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
                    job.pop("restart_after_cancel", None)
                    job["cancel_requested"] = False
                    return
                if status in {"failed", "cancelled"} and not pid:
                    restart = bool(job.pop("restart_after_cancel", False))
                    if restart:
                        _reset_job(service, job)
                        job["message"] = "Restart queued with updated settings"
                        job["log"][-1] = "--- Restart queued with updated settings ---"
                    else:
                        job.update(
                            status="cancelled",
                            message="Stopped",
                            finished_at=module.utc_now(),
                            process_id=None,
                        )
            if status in {"failed", "cancelled"} and not pid:
                if restart:
                    _put_at_front(module.job_queue, job_id)
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
            video_quality=None,
            audio_quality=None,
        )
    job.update(common)
    job.setdefault("log", []).append("--- Retry queued ---")


def _install_youtube_preferences(
    module: ModuleType,
    default_preferences: dict[str, str],
) -> None:
    """Attach a preference snapshot before the engine exposes a queued job."""
    if getattr(module, "_download_central_preferences", False):
        return
    download_endpoint = next(
        (
            rule.endpoint
            for rule in module.app.url_map.iter_rules()
            if rule.rule == "/download" and "POST" in rule.methods
        ),
        None,
    )
    download_view = module.app.view_functions.get(download_endpoint) if download_endpoint else None
    if download_view is None:
        return

    preference_context = threading.local()
    original_put = module.job_queue.put

    def put_with_preferences(job_id, *args, **kwargs):
        preferences = getattr(preference_context, "value", default_preferences)
        with module.jobs_lock:
            job = module.jobs.get(job_id)
            if job is not None:
                job["preferences"] = normalize_youtube_preferences(preferences, default_preferences)
        return original_put(job_id, *args, **kwargs)

    @wraps(download_view)
    def download_with_preferences(*args, **kwargs):
        supplied = {
            name: request.form.get(name)
            for name in YOUTUBE_PREFERENCE_CHOICES
            if request.form.get(name) is not None
        }
        try:
            preference_context.value = normalize_youtube_preferences(supplied, default_preferences)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        try:
            return download_view(*args, **kwargs)
        finally:
            preference_context.__dict__.pop("value", None)

    module.job_queue.put = put_with_preferences
    module.app.view_functions[download_endpoint] = download_with_preferences
    module._download_central_preferences = True


def install_engine_controls(
    service: str,
    module: ModuleType,
    default_preferences: dict[str, str] | None = None,
) -> None:
    """Add the common control API to an imported HLS or YouTube engine."""
    if service not in {"hls", "youtube"}:
        return

    if service == "youtube":
        youtube_defaults = normalize_youtube_preferences(default_preferences)
        _install_youtube_quality_tracking(module, youtube_defaults)
        _install_youtube_preferences(module, youtube_defaults)

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

            job.pop("restart_after_cancel", None)
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

    def update_preferences(job_id: str):
        payload = request.get_json(silent=True) or {}
        with module.jobs_lock:
            job = module.jobs.get(job_id)
            if job is None:
                return jsonify({"error": "Download not found."}), 404
            status = str(job.get("status", ""))
            if status == "moving":
                return jsonify({"error": "The completed download is already being moved into place."}), 409
            if status in TERMINAL_STATUSES or status == "cancelling":
                return jsonify({"error": "Settings can only be changed for queued or active downloads."}), 409
            try:
                preferences = normalize_youtube_preferences(
                    payload,
                    job.get("preferences") or module._download_central_default_preferences,
                )
            except ValueError as error:
                return jsonify({"error": str(error)}), 400
            job["preferences"] = preferences
            if status == "queued":
                job["message"] = "Waiting for a worker — settings updated"
                return jsonify({"ok": True, "status": "queued", "preferences": preferences})
            if status not in {"preparing", "downloading", "processing"}:
                return jsonify({"error": f"Settings cannot be changed while the job is {status}."}), 409
            if payload.get("restart") is not True:
                return jsonify({"error": "Changing an active download requires restart confirmation."}), 409
            job["restart_after_cancel"] = True
            job["cancel_requested"] = True
            job["status"] = "cancelling"
            job["message"] = "Stopping to apply updated settings…"

        _start_cancel_watcher(service, module, job_id)
        return jsonify({"ok": True, "status": "cancelling", "preferences": preferences}), 202

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
    if service == "youtube" and not has_route("/api/jobs/<job_id>/preferences", "PATCH"):
        app.add_url_rule(
            "/api/jobs/<job_id>/preferences",
            endpoint="download_central_youtube_preferences",
            view_func=update_preferences,
            methods=["PATCH"],
        )
