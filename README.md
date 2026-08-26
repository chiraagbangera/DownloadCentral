# Download Central

Download Central provides one uniform, tabbed UI for the Raspberry Pi file, HLS, and YouTube downloaders. One Gunicorn process loads all three proven engines and listens at `http://192.168.1.5:100`.

## What stays intact

- Existing per-service queues and parallel workers
- Pi-local staging when the estimated download fits
- Direct-to-NAS downloads when size is unknown or local free space is insufficient
- File discovery, Google Drive folders, resume, cancel, retry, and cleanup
- Direct `.m3u8` capture, Referer support, ffmpeg progress, and MP4 output
- YouTube playlist support, HDR/codec selection, merge headroom, and activity logs

## What is new

- One responsive UI with Files, HLS, YouTube, and Health & settings tabs
- Aggregated reachability, storage, ffmpeg, and yt-dlp health
- Live Raspberry Pi CPU usage/load, CPU temperature, memory/swap, uptime, process, network throughput/counters, and filesystem utilization
- Persistent per-service destinations anywhere below `/mnt`
- Folder browser that cannot escape `/mnt`
- In-app yt-dlp and Raspberry Pi OS ffmpeg updates
- Admin token protection for settings, service restarts, and updates
- A single web service and TCP port: `100`

The former downloader systemd services are stopped, disabled, and their unit files are removed during installation. Their application directories remain installed because Download Central imports those engines directly into its single process. Nothing listens on the former ports 98 or 99, and port 100 belongs only to Download Central.

## Install on the Pi

The three downloader applications must already be installed in their current locations:

- `/opt/raspi-download-manager/app.py`
- `/opt/hls-video-downloader/app.py`
- `/opt/pi-ytdlp-web/app.py`

Then run:

```bash
sudo ./install.sh
```

The installer stops and removes the three old service units before starting Download Central. Their `/opt` application directories and downloader state are preserved. Let active downloads finish before installing. It also prints a generated admin token; paste it into the Health & settings tab before saving paths or updating tools. The token is kept in the browser tab only.

To deploy from another computer:

```bash
PI_HOST=pi@192.168.1.5 ./deploy.sh
```

### Deploy from VS Code

Open the project folder in VS Code and run **Terminal → Run Build Task** (`⇧⌘B` on macOS), then choose **Deploy to Raspberry Pi**. The task prompts for the SSH destination and defaults to `pi@192.168.1.5`. Deployment uses `rsync`, opens an interactive SSH terminal for the Pi's sudo password when needed, installs the update, and restarts Download Central.

TCP port 100 is privileged. The systemd unit grants only `CAP_NET_BIND_SERVICE`; the combined web app and workers still run as the unprivileged `pi` user.

## Settings behavior

Paths must be absolute and resolve below `/mnt`. Saving settings restarts the single Download Central service so all embedded workers use the new paths. Let active jobs finish before saving.

The file downloader still supports subfolders within its configured root for individual batches. The settings page changes that root itself.

## Tool updates

- **yt-dlp:** upgrades the package inside `/opt/pi-ytdlp-web/.venv`.
- **ffmpeg:** runs the Raspberry Pi OS package upgrade for `ffmpeg` through a fixed root helper.

Only two exact helper operations are allowed by sudoers. User-supplied commands are never executed.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
PORT=8080 STATE_DIR=/tmp/download-central .venv/bin/python app.py
```

Production uses `wsgi.py` to embed all three engines. For local integration testing, point `FILES_ENGINE_PATH`, `HLS_ENGINE_PATH`, and `YOUTUBE_ENGINE_PATH` at local engine source files and run `gunicorn wsgi:application`.
