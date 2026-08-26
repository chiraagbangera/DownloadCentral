from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from flask import Flask, Response, jsonify, render_template, request


APP_NAME = "Download Central"
MOUNT_ROOT = Path(os.environ.get("MOUNT_ROOT", "/mnt")).resolve()
STATE_DIR = Path(os.environ.get("STATE_DIR", "/var/lib/download-central")).resolve()
SETTINGS_PATH = Path(os.environ.get("SETTINGS_PATH", str(STATE_DIR / "settings.json"))).resolve()
ADMIN_HELPER = os.environ.get("ADMIN_HELPER", "/usr/local/sbin/download-central-admin")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()
REQUEST_TIMEOUT = max(2, int(os.environ.get("BACKEND_TIMEOUT", "15")))

SERVICES = {
    "files": {
        "name": "File downloader",
        "base_url": os.environ.get("FILES_SERVICE_URL", "http://127.0.0.1:98/"),
        "path_key": "download_root",
        "default_path": "/mnt/Scratch Disk 8TB/Downloads",
    },
    "hls": {
        "name": "HLS downloader",
        "base_url": os.environ.get("HLS_SERVICE_URL", "http://127.0.0.1:99/"),
        "path_key": "download_dir",
        "default_path": "/mnt/Videos/HLS Videos",
    },
    "youtube": {
        "name": "YouTube downloader",
        "base_url": os.environ.get("YOUTUBE_SERVICE_URL", "http://127.0.0.1:100/"),
        "path_key": "download_dir",
        "default_path": "/mnt/Videos/Youtube Videos",
    },
}

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

app = Flask(__name__)
settings_lock = threading.RLock()
update_lock = threading.Lock()
update_jobs: dict[str, dict] = {}
embedded_services: dict[str, Flask] = {}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_settings() -> dict[str, str]:
    return {name: str(definition["default_path"]) for name, definition in SERVICES.items()}


def read_settings() -> dict[str, str]:
    result = default_settings()
    try:
        payload = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            for name in SERVICES:
                if isinstance(payload.get(name), str):
                    result[name] = payload[name]
    except (OSError, ValueError, TypeError):
        pass
    return result


def path_inside_mount(value: object) -> Path:
    raw = str(value or "").strip()
    if not raw or "\x00" in raw or "\n" in raw or "\r" in raw:
        raise ValueError("Choose a folder inside /mnt.")
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise ValueError("Download folders must be absolute paths inside /mnt.")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(MOUNT_ROOT)
    except ValueError as error:
        raise ValueError(f"Download folders must stay inside {MOUNT_ROOT}.") from error
    if resolved == MOUNT_ROOT:
        raise ValueError("Choose a folder below /mnt, not /mnt itself.")
    return resolved


def write_settings(settings: dict[str, str]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_PATH.with_suffix(f".{secrets.token_hex(4)}.tmp")
    temporary.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, SETTINGS_PATH)


def authorized() -> bool:
    if not ADMIN_TOKEN:
        return True
    supplied = request.headers.get("X-Admin-Token", "")
    return secrets.compare_digest(supplied, ADMIN_TOKEN)


def require_admin() -> Response | None:
    if authorized():
        return None
    return jsonify({"error": "The admin token is missing or incorrect."}), 401


def backend_url(service: str, path: str = "") -> str:
    base = str(SERVICES[service]["base_url"]).rstrip("/") + "/"
    return urljoin(base, path.lstrip("/"))


def set_embedded_services(services: dict[str, Flask]) -> None:
    """Use downloader apps mounted in this WSGI process instead of HTTP ports."""
    embedded_services.clear()
    embedded_services.update(services)


def backend_json(service: str, path: str, timeout: int | None = None) -> tuple[dict | None, str | None]:
    embedded = embedded_services.get(service)
    if embedded is not None:
        try:
            with embedded.test_client() as client:
                response = client.get("/" + path.lstrip("/"), headers={"Accept": "application/json"})
                if response.status_code >= 400:
                    return None, f"Embedded service returned HTTP {response.status_code}"
                value = response.get_json(silent=True)
                return (value if isinstance(value, dict) else {"data": value}), None
        except Exception as error:
            return None, str(error)
    try:
        response = requests.get(
            backend_url(service, path),
            timeout=timeout or REQUEST_TIMEOUT,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        value = response.json()
        return (value if isinstance(value, dict) else {"data": value}), None
    except (requests.RequestException, ValueError) as error:
        return None, str(error)


def command_version(command: list[str]) -> dict[str, object]:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
        lines = (completed.stdout or completed.stderr).strip().splitlines()
        return {
            "ok": completed.returncode == 0,
            "version": lines[0] if lines else None,
            "error": None if completed.returncode == 0 else (lines[-1] if lines else f"Exit {completed.returncode}"),
        }
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"ok": False, "version": None, "error": str(error)}


@app.get("/")
def index():
    return render_template(
        "index.html",
        app_name=APP_NAME,
        mount_root=str(MOUNT_ROOT),
        admin_token_required=bool(ADMIN_TOKEN),
    )


@app.route("/backend/<service>/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
@app.route("/backend/<service>/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def proxy_backend(service: str, path: str):
    if service not in SERVICES:
        return jsonify({"error": "Unknown downloader service."}), 404
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS | {"host", "content-length"}
    }
    try:
        upstream = requests.request(
            request.method,
            backend_url(service, path),
            params=request.args,
            data=request.get_data(),
            headers=headers,
            allow_redirects=False,
            timeout=300 if path == "api/discover" else REQUEST_TIMEOUT,
        )
    except requests.RequestException as error:
        return jsonify({"error": f"{SERVICES[service]['name']} is unavailable: {error}"}), 502

    response_headers = [
        (key, value)
        for key, value in upstream.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS | {"content-length", "content-encoding"}
    ]
    return Response(upstream.content, status=upstream.status_code, headers=response_headers)


@app.get("/api/settings")
def get_settings():
    return jsonify({"mount_root": str(MOUNT_ROOT), "paths": read_settings(), "admin_token_required": bool(ADMIN_TOKEN)})


@app.post("/api/settings")
def save_settings():
    denied = require_admin()
    if denied:
        return denied
    payload = request.get_json(silent=True) or {}
    paths = payload.get("paths")
    if not isinstance(paths, dict):
        return jsonify({"error": "A paths object is required."}), 400
    try:
        normalized = {name: str(path_inside_mount(paths.get(name))) for name in SERVICES}
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    for value in normalized.values():
        path = Path(value)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            # A root-owned share may be usable after the privileged helper restarts
            # services; preserve the exact error from that helper if it is not.
            pass
    with settings_lock:
        try:
            write_settings(normalized)
            completed = subprocess.run(
                ["sudo", "-n", ADMIN_HELPER, "apply-settings"],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return jsonify({"error": f"Settings were saved but could not be applied: {error}"}), 500
    if completed.returncode != 0:
        message = (completed.stderr or completed.stdout).strip() or f"Helper exited with {completed.returncode}"
        return jsonify({"error": f"Settings were saved but could not be applied: {message}"}), 500
    return jsonify({"ok": True, "paths": normalized, "message": "Settings saved; Download Central is restarting."})


@app.get("/api/directories")
def list_mount_directories():
    raw = request.args.get("path", str(MOUNT_ROOT))
    try:
        current = MOUNT_ROOT if raw in {"", str(MOUNT_ROOT)} else path_inside_mount(raw)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    if not current.is_dir():
        return jsonify({"error": "That folder is not currently mounted or does not exist."}), 404
    children = []
    try:
        for child in current.iterdir():
            try:
                if child.is_dir() and not child.name.startswith("."):
                    resolved = child.resolve()
                    resolved.relative_to(MOUNT_ROOT)
                    children.append({"name": child.name, "path": str(resolved), "writable": os.access(resolved, os.W_OK)})
            except (OSError, ValueError):
                continue
    except PermissionError:
        return jsonify({"error": "That folder cannot be read by Download Central."}), 403
    children.sort(key=lambda item: item["name"].lower())
    parent = current.parent if current != MOUNT_ROOT else None
    return jsonify({
        "current": str(current),
        "parent": str(parent) if parent and parent.is_relative_to(MOUNT_ROOT) else None,
        "children": children,
    })


@app.get("/api/health")
def aggregate_health():
    settings = read_settings()
    services = {}
    for name, definition in SERVICES.items():
        health, error = backend_json(name, "api/health")
        services[name] = {
            "name": definition["name"],
            "ok": bool(health and health.get("ok")),
            "reachable": health is not None,
            "error": error,
            "health": health,
            "configured_path": settings[name],
        }
    yt_health = (services["youtube"].get("health") or {})
    ytdlp = {
        "ok": bool(yt_health.get("yt_dlp_ok")),
        "version": yt_health.get("yt_dlp_version"),
        "path": yt_health.get("yt_dlp"),
        "error": yt_health.get("yt_dlp_error"),
    }
    hls_health = services["hls"].get("health") or {}
    ffmpeg_path = str(hls_health.get("ffmpeg") or "/usr/bin/ffmpeg")
    ffmpeg = command_version([ffmpeg_path, "-version"])
    return jsonify({
        "ok": all(item["ok"] for item in services.values()),
        "checked_at": utc_now(),
        "central": {"ok": True, "version": "1.0.0", "port": request.host.split(":")[-1] if ":" in request.host else None},
        "services": services,
        "tools": {"yt_dlp": ytdlp, "ffmpeg": ffmpeg},
    })


def set_update_job(job_id: str, **values: object) -> None:
    with update_lock:
        if job_id in update_jobs:
            update_jobs[job_id].update(values)


def run_update(job_id: str, tool: str) -> None:
    set_update_job(job_id, status="running", started_at=utc_now(), message=f"Updating {tool}…")
    try:
        if tool == "yt-dlp":
            health, _ = backend_json("youtube", "api/health")
            binary = Path(str((health or {}).get("yt_dlp") or "/opt/pi-ytdlp-web/.venv/bin/yt-dlp"))
            python = binary.with_name("python")
            command = [str(python), "-m", "pip", "install", "--upgrade", "yt-dlp"]
        elif tool == "ffmpeg":
            command = ["sudo", "-n", ADMIN_HELPER, "update-ffmpeg"]
        else:
            raise ValueError("Unsupported tool update.")
        completed = subprocess.run(command, capture_output=True, text=True, timeout=900, check=False)
        output = "\n".join(part.strip() for part in (completed.stdout, completed.stderr) if part.strip())
        if completed.returncode != 0:
            raise RuntimeError(output or f"Updater exited with {completed.returncode}")
        set_update_job(job_id, status="completed", finished_at=utc_now(), message=f"{tool} updated successfully.", output=output[-12000:])
    except Exception as error:
        set_update_job(job_id, status="failed", finished_at=utc_now(), message=str(error), output="")


@app.post("/api/tools/<tool>/update")
def update_tool(tool: str):
    denied = require_admin()
    if denied:
        return denied
    if tool not in {"yt-dlp", "ffmpeg"}:
        return jsonify({"error": "Only yt-dlp and ffmpeg can be updated."}), 404
    with update_lock:
        if any(job["status"] in {"queued", "running"} for job in update_jobs.values()):
            return jsonify({"error": "Another tool update is already running."}), 409
        job_id = secrets.token_hex(6)
        update_jobs[job_id] = {"id": job_id, "tool": tool, "status": "queued", "created_at": utc_now(), "message": "Queued", "output": ""}
    threading.Thread(target=run_update, args=(job_id, tool), daemon=True, name=f"update-{tool}").start()
    return jsonify({"ok": True, "job": update_jobs[job_id]}), 202


@app.get("/api/tools/updates/<job_id>")
def update_status(job_id: str):
    denied = require_admin()
    if denied:
        return denied
    with update_lock:
        job = update_jobs.get(job_id)
        if not job:
            return jsonify({"error": "Update job not found."}), 404
        return jsonify(dict(job))


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "service": APP_NAME}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "100")), threaded=True)
