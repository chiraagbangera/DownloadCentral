from __future__ import annotations

import queue
import threading
from pathlib import Path
from types import SimpleNamespace

from flask import Flask

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

