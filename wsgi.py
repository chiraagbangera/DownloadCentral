"""Single-process WSGI composition for all Download Central engines."""

from __future__ import annotations

import importlib.util
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from werkzeug.middleware.dispatcher import DispatcherMiddleware

from app import app, read_settings, read_youtube_preferences, set_embedded_services
from engine_controls import install_engine_controls


ENGINE_PATHS = {
    "files": Path(os.environ.get("FILES_ENGINE_PATH", "/opt/raspi-download-manager/app.py")),
    "hls": Path(os.environ.get("HLS_ENGINE_PATH", "/opt/hls-video-downloader/app.py")),
    "youtube": Path(os.environ.get("YOUTUBE_ENGINE_PATH", "/opt/pi-ytdlp-web/app.py")),
}


@contextmanager
def temporary_environment(values: dict[str, str]):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def load_engine(name: str, path: Path, environment: dict[str, str]):
    if not path.is_file():
        raise RuntimeError(f"The {name} engine was not found at {path}")
    module_name = f"download_central_engine_{name}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load the {name} engine from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    with temporary_environment(environment):
        spec.loader.exec_module(module)
    if not hasattr(module, "app"):
        raise RuntimeError(f"The {name} engine does not expose a Flask app")
    return module


paths = read_settings()

files_module = load_engine(
    "files",
    ENGINE_PATHS["files"],
    {
        "DOWNLOAD_ROOT": paths["files"],
        "TEMP_ROOT": os.environ.get("FILES_TEMP_ROOT", "/var/tmp/raspi-download-manager"),
        "STATE_DIR": os.environ.get("FILES_STATE_DIR", "/var/lib/raspi-download-manager"),
        "DATABASE_PATH": os.environ.get("FILES_DATABASE_PATH", "/var/lib/raspi-download-manager/downloads.db"),
        "MAX_CONCURRENT_DOWNLOADS": os.environ.get("FILES_MAX_CONCURRENT_DOWNLOADS", "2"),
    },
)

hls_module = load_engine(
    "hls",
    ENGINE_PATHS["hls"],
    {
        "DOWNLOAD_DIR": paths["hls"],
        "TEMP_DOWNLOAD_DIR": os.environ.get("HLS_TEMP_DOWNLOAD_DIR", "/var/tmp/hls-video-downloader/jobs"),
        "FFMPEG_BIN": os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg"),
        "FFPROBE_BIN": os.environ.get("FFPROBE_BIN", "/usr/bin/ffprobe"),
        "MAX_CONCURRENT_DOWNLOADS": os.environ.get("HLS_MAX_CONCURRENT_DOWNLOADS", "2"),
    },
)

youtube_module = load_engine(
    "youtube",
    ENGINE_PATHS["youtube"],
    {
        "DOWNLOAD_DIR": paths["youtube"],
        "TEMP_DOWNLOAD_DIR": os.environ.get("YOUTUBE_TEMP_DOWNLOAD_DIR", "/var/tmp/ytdlp-web"),
        "NAS_TEMP_DOWNLOAD_DIR": str(Path(paths["youtube"]) / ".ytdlp-temp"),
        "YTDLP_BIN": os.environ.get("YTDLP_BIN", "/opt/pi-ytdlp-web/.venv/bin/yt-dlp"),
        "MAX_CONCURRENT_DOWNLOADS": os.environ.get("YOUTUBE_MAX_CONCURRENT_DOWNLOADS", "2"),
    },
)

install_engine_controls("hls", hls_module)
install_engine_controls("youtube", youtube_module, read_youtube_preferences())

engine_apps = {
    "files": files_module.app,
    "hls": hls_module.app,
    "youtube": youtube_module.app,
}
set_embedded_services(engine_apps)

application = DispatcherMiddleware(
    app,
    {
        "/backend/files": engine_apps["files"],
        "/backend/hls": engine_apps["hls"],
        "/backend/youtube": engine_apps["youtube"],
    },
)
