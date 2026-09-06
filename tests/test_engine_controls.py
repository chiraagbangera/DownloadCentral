from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, request
import pytest

import engine_controls


def fake_engine(tmp_path: Path, status: str = "queued"):
    app = Flask(f"fake-{status}-{id(tmp_path)}")
    job_queue: queue.Queue[str] = queue.Queue()
    job = {
        "id": "job-1",
        "url": "https://example.test/video",
        "status": status,
        "message": status,
        "progress": 25.0,
        "process_id": None,
        "log": [],
    }
    if status == "queued":
        job_queue.put(job["id"])
    return SimpleNamespace(
        app=app,
        jobs={job["id"]: job},
        jobs_lock=threading.Lock(),
        job_queue=job_queue,
        utc_now=lambda: "2026-09-04T00:00:00+00:00",
        TEMP_DOWNLOAD_DIR=tmp_path / "temp",
        DOWNLOAD_DIR=tmp_path / "downloads",
    )


def test_queued_job_can_be_stopped_retried_and_deleted(tmp_path):
    module = fake_engine(tmp_path)
    engine_controls.install_engine_controls("hls", module)
    client = module.app.test_client()

    stopped = client.post("/api/jobs/job-1/cancel")
    assert stopped.status_code == 200
    assert module.jobs["job-1"]["status"] == "cancelled"
    assert module.job_queue.empty()
    assert module.job_queue.unfinished_tasks == 0

    retried = client.post("/api/jobs/job-1/retry")
    assert retried.status_code == 200
    assert module.jobs["job-1"]["status"] == "queued"
    assert module.job_queue.get_nowait() == "job-1"

    module.jobs["job-1"]["status"] = "failed"
    deleted = client.delete("/api/jobs/job-1")
    assert deleted.status_code == 200
    assert "job-1" not in module.jobs


def test_active_job_starts_process_cancellation(tmp_path, monkeypatch):
    module = fake_engine(tmp_path, status="downloading")
    started = []
    monkeypatch.setattr(
        engine_controls,
        "_start_cancel_watcher",
        lambda service, engine, job_id: started.append((service, engine, job_id)),
    )
    engine_controls.install_engine_controls("youtube", module)

    response = module.app.test_client().post("/api/jobs/job-1/cancel")

    assert response.status_code == 202
    assert module.jobs["job-1"]["status"] == "cancelling"
    assert module.jobs["job-1"]["cancel_requested"] is True
    assert started == [("youtube", module, "job-1")]


def test_active_or_moving_job_cannot_be_deleted(tmp_path):
    module = fake_engine(tmp_path, status="moving")
    engine_controls.install_engine_controls("youtube", module)
    client = module.app.test_client()

    assert client.post("/api/jobs/job-1/cancel").status_code == 409
    assert client.delete("/api/jobs/job-1").status_code == 409


def test_youtube_tracks_actual_video_and_audio_quality():
    jobs = {"job-1": {"id": "job-1", "log": []}}
    lock = threading.Lock()

    def build_command(job):
        return ["yt-dlp", "--format", module.FORMAT_SELECTOR, job["url"]]

    def append_log(job_id, line):
        jobs[job_id]["log"].append(line)

    module = SimpleNamespace(
        FORMAT_SELECTOR="old-selector",
        build_command=build_command,
        append_log=append_log,
        jobs=jobs,
        jobs_lock=lock,
    )

    engine_controls._install_youtube_quality_tracking(module)
    command = module.build_command({"url": "https://example.test/video"})
    assert engine_controls.YOUTUBE_FORMAT_SELECTOR in command
    assert "--format-sort-reset" in command
    assert command[command.index("--remux-video") + 1] == "mp4>mp4/mkv>mkv/webm>mkv/mkv"
    assert engine_controls.YOUTUBE_QUALITY_TEMPLATE in command

    payload = {
        "video": ["401", "3840x2160", 2160, 3840, 60, "av01.0.13M.08", "SDR", 12500, 12600, "mp4", "2160p"],
        "audio": ["251", "opus", 128, 130, 48000, 2, "webm", "medium", "en"],
        "combined": [],
    }
    module.append_log(
        "job-1",
        engine_controls.YOUTUBE_QUALITY_MARKER + json.dumps(payload),
    )

    assert jobs["job-1"]["video_quality"] == {
        "format_id": "401",
        "resolution": "3840x2160",
        "height": 2160,
        "width": 3840,
        "fps": 60,
        "codec": "av01.0.13M.08",
        "dynamic_range": "SDR",
        "bitrate_kbps": 12500,
        "container": "mp4",
        "note": "2160p",
    }
    assert jobs["job-1"]["audio_quality"]["codec"] == "opus"
    assert jobs["job-1"]["audio_quality"]["bitrate_kbps"] == 128
    assert jobs["job-1"]["log"] == []


def test_combined_youtube_format_reports_both_tracks():
    payload = {
        "video": ["NA"] * 11,
        "audio": ["NA"] * 9,
        "combined": [
            "22", "1280x720", 720, 1280, 30, "avc1.64001F", "SDR",
            1800, 1950, "mp4", "720p", "mp4a.40.2", 128, 44100, 2, "en",
        ],
    }

    video, audio = engine_controls._quality_from_payload(payload)

    assert video["height"] == 720
    assert video["codec"] == "avc1.64001F"
    assert audio["codec"] == "mp4a.40.2"
    assert audio["sample_rate_hz"] == 44100


def test_smart_selector_caps_4k_and_prefers_hdr_av1_then_h265():
    selector = engine_controls.build_youtube_format_selector({
        "resolution": "2160",
        "codec": "smart",
        "container": "mp4",
    })

    av1_hdr = selector.index("[vcodec^=av01][dynamic_range~='^(HDR|HLG|DV)']")
    h265 = selector.index("[vcodec~='^(hvc1|hev1|hevc|h265)']")
    assert av1_hdr < h265
    assert selector.index("bestvideo[format_note^=2160p][vcodec~='^(vp09|vp9)']") < selector.index(
        "bestvideo[format_note^=1440p][vcodec~='^(hvc1|hev1|hevc|h265)']"
    )
    assert "bestvideo[height<=2160]" in selector
    assert "bestaudio[ext=m4a]" in selector
    assert selector.endswith("best[height<=2160]")


def test_smart_selector_handles_cropped_youtube_resolution_labels():
    selector = engine_controls.build_youtube_format_selector({
        "resolution": "2160",
        "codec": "smart",
        "container": "mp4",
    })

    # bXoZjadrHpU exposes 3840x2026 as format_note=2160p and 1920x1012 as
    # format_note=1080p. The nominal 4K tier must be tested before the generic
    # codec fallback that previously selected the 1012-high H.264 stream.
    assert selector.index("bestvideo[format_note^=2160p]") < selector.index(
        "bestvideo[height<=2160][vcodec^=avc1]"
    )


def test_youtube_command_uses_per_job_preferences():
    jobs = {"job-1": {"id": "job-1", "log": []}}

    def build_command(job):
        return [
            "yt-dlp", "--format", module.FORMAT_SELECTOR,
            "--merge-output-format", "mkv", job["url"],
        ]

    module = SimpleNamespace(
        FORMAT_SELECTOR="old-selector",
        build_command=build_command,
        append_log=lambda job_id, line: None,
        jobs=jobs,
        jobs_lock=threading.Lock(),
    )
    engine_controls._install_youtube_quality_tracking(module)

    command = module.build_command({
        "url": "https://example.test/video",
        "preferences": {"resolution": "1080", "codec": "h264", "container": "mkv"},
    })

    selector = command[command.index("--format") + 1]
    assert "bestvideo[height<=1080][vcodec^=avc1]" in selector
    assert command[command.index("--merge-output-format") + 1] == "mkv/mp4"
    assert command[command.index("--remux-video") + 1] == "mkv"


def test_queued_youtube_preferences_update_without_moving_job(tmp_path):
    module = fake_engine(tmp_path)
    engine_controls.install_engine_controls("youtube", module)
    client = module.app.test_client()

    response = client.patch(
        "/api/jobs/job-1/preferences",
        json={"resolution": "1080", "codec": "h264", "container": "mkv"},
    )

    assert response.status_code == 200
    assert list(module.job_queue.queue) == ["job-1"]
    assert module.jobs["job-1"]["preferences"] == {
        "resolution": "1080", "codec": "h264", "container": "mkv",
    }


def test_active_youtube_preference_change_requests_front_restart(tmp_path, monkeypatch):
    module = fake_engine(tmp_path, status="downloading")
    started = []
    monkeypatch.setattr(
        engine_controls,
        "_start_cancel_watcher",
        lambda service, engine, job_id: started.append(job_id),
    )
    engine_controls.install_engine_controls("youtube", module)

    response = module.app.test_client().patch(
        "/api/jobs/job-1/preferences",
        json={"resolution": "720", "codec": "smart", "container": "mp4", "restart": True},
    )

    assert response.status_code == 202
    assert module.jobs["job-1"]["restart_after_cancel"] is True
    assert module.jobs["job-1"]["status"] == "cancelling"
    assert started == ["job-1"]


def test_invalid_youtube_preference_is_rejected():
    with pytest.raises(ValueError, match="Unsupported YouTube codec"):
        engine_controls.normalize_youtube_preferences({"codec": "mpeg2"})


def test_new_youtube_job_captures_submitted_preferences(tmp_path):
    module = fake_engine(tmp_path)

    @module.app.post("/download")
    def download():
        module.jobs["job-2"] = {
            "id": "job-2", "url": request.form["url"], "status": "queued", "log": [],
        }
        module.job_queue.put("job-2")
        return "queued", 202

    engine_controls.install_engine_controls("youtube", module)
    response = module.app.test_client().post(
        "/download",
        data={
            "url": "https://example.test/two",
            "resolution": "720",
            "codec": "vp9",
            "container": "mkv",
        },
    )

    assert response.status_code == 202
    assert module.jobs["job-2"]["preferences"] == {
        "resolution": "720", "codec": "vp9", "container": "mkv",
    }


def test_restart_watcher_requeues_job_at_front(tmp_path):
    module = fake_engine(tmp_path, status="failed")
    module.job_queue.put("later-job")
    module.jobs["job-1"].update(cancel_requested=True, restart_after_cancel=True)

    engine_controls._cancel_watcher("youtube", module, "job-1")

    assert list(module.job_queue.queue) == ["job-1", "later-job"]
    assert module.jobs["job-1"]["status"] == "queued"
