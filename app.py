"""
app.py
Flask web app:
  - Drag & drop / multi-file / folder upload UI (templates/index.html)
  - For every uploaded video: upload to Bunny Stream -> wait for
    transcoding -> download the generated HLS files -> upload them to
    Cloudflare R2, mirroring the folder structure.
  - Job status is tracked in-memory and polled by the frontend.
"""

import os
import uuid
import shutil
import logging
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv

load_dotenv()

import bunny_client
import r2_client
from hls_downloader import download_hls, DownloadError
from health_check import register_health_check

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

PULL_ZONE_HOSTNAME = os.environ.get("BUNNY_PULL_ZONE_HOSTNAME", "")
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "2"))
DELETE_LOCAL_AFTER_UPLOAD = os.environ.get("DELETE_LOCAL_AFTER_UPLOAD", "true").lower() == "true"
DELETE_FROM_BUNNY_AFTER_SUCCESS = os.environ.get("DELETE_FROM_BUNNY_AFTER_SUCCESS", "false").lower() == "true"
R2_KEY_PREFIX = os.environ.get("R2_KEY_PREFIX", "").strip().strip("/")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = None  # allow large video uploads (limited by host resources instead)

executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS)

# In-memory job store: job_id -> dict
JOBS = {}
JOBS_LOCK = threading.Lock()

STAGE_WEIGHTS = {
    "queued": 0,
    "uploading_bunny": 10,
    "transcoding": 40,
    "downloading_hls": 70,
    "uploading_r2": 90,
    "done": 100,
    "error": 0,
}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _update_job(job_id, **fields):
    with JOBS_LOCK:
        if job_id not in JOBS:
            return
        JOBS[job_id].update(fields)
        JOBS[job_id]["updated_at"] = _now()
        if "stage" in fields:
            JOBS[job_id]["progress"] = STAGE_WEIGHTS.get(fields["stage"], JOBS[job_id].get("progress", 0))


def _set_progress(job_id, progress):
    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id]["progress"] = progress
            JOBS[job_id]["updated_at"] = _now()


def process_job(job_id):
    with JOBS_LOCK:
        job = dict(JOBS[job_id])

    local_path = job["local_path"]
    title = job["original_name"]
    dest_dir = os.path.join(DOWNLOAD_DIR, job_id)

    try:
        if not PULL_ZONE_HOSTNAME:
            raise RuntimeError("BUNNY_PULL_ZONE_HOSTNAME is not configured on the server")

        # 1. Create + upload to Bunny Stream
        _update_job(job_id, stage="uploading_bunny")
        video_id = bunny_client.create_video(title)
        _update_job(job_id, bunny_video_id=video_id)

        def on_upload_progress(pct, read, total):
            # uploading_bunny occupies 10-40% of the overall bar
            _set_progress(job_id, 10 + int(pct * 0.30))

        bunny_client.upload_video(video_id, local_path, on_progress=on_upload_progress)

        # 2. Wait for transcoding
        _update_job(job_id, stage="transcoding")

        def on_status(status, pct):
            _set_progress(job_id, 40 + int(min(pct, 100) * 0.30))

        bunny_client.wait_for_transcode(video_id, on_status=on_status)

        # 3. Download the generated HLS files
        _update_job(job_id, stage="downloading_hls")
        os.makedirs(dest_dir, exist_ok=True)

        def on_download_progress(count):
            _set_progress(job_id, min(85, 70 + count))

        files = download_hls(video_id, PULL_ZONE_HOSTNAME, dest_dir, on_progress=on_download_progress)
        _update_job(job_id, file_count=len(files))

        # 4. Upload each file to R2, mirroring the folder structure
        _update_job(job_id, stage="uploading_r2")
        prefix_parts = [p for p in [R2_KEY_PREFIX, job.get("relative_dir", ""), video_id] if p]
        r2_prefix = "/".join(prefix_parts)

        upload_list = [(local, f"{r2_prefix}/{rel_key}") for local, rel_key in files]

        def on_r2_progress(done, total):
            _set_progress(job_id, 90 + int(done * 10 / max(total, 1)))

        r2_client.upload_many(upload_list, on_progress=on_r2_progress)

        r2_playlist_key = f"{r2_prefix}/playlist.m3u8"

        if DELETE_FROM_BUNNY_AFTER_SUCCESS:
            try:
                bunny_client.delete_video(video_id)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to delete bunny video %s after migration", video_id)

        _update_job(
            job_id,
            stage="done",
            progress=100,
            r2_prefix=r2_prefix,
            r2_playlist_key=r2_playlist_key,
        )

    except Exception as exc:  # noqa: BLE001
        logger.exception("Job %s failed", job_id)
        _update_job(job_id, stage="error", error=str(exc))
    finally:
        if DELETE_LOCAL_AFTER_UPLOAD:
            try:
                if os.path.exists(local_path):
                    os.remove(local_path)
                if os.path.isdir(dest_dir):
                    shutil.rmtree(dest_dir, ignore_errors=True)
            except Exception:  # noqa: BLE001
                logger.exception("Cleanup failed for job %s", job_id)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/upload", methods=["POST"])
def upload():
    """Accepts a single file per request (frontend loops for multi/folder
    uploads). Saves it to disk, creates a job, and queues background
    processing."""
    if "file" not in request.files:
        return jsonify({"error": "no file part"}), 400

    f = request.files["file"]
    if f.filename == "":
        return jsonify({"error": "empty filename"}), 400

    relative_path = request.form.get("relativePath", "").strip()
    job_id = str(uuid.uuid4())

    safe_name = f"{job_id}_{os.path.basename(f.filename)}"
    local_path = os.path.join(UPLOAD_DIR, safe_name)
    f.save(local_path)

    relative_dir = os.path.dirname(relative_path) if relative_path else ""

    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "original_name": f.filename,
            "relative_path": relative_path or f.filename,
            "relative_dir": relative_dir,
            "local_path": local_path,
            "size": os.path.getsize(local_path),
            "stage": "queued",
            "progress": 0,
            "bunny_video_id": None,
            "error": None,
            "created_at": _now(),
            "updated_at": _now(),
        }

    executor.submit(process_job, job_id)
    return jsonify({"job_id": job_id}), 202


@app.route("/api/status/<job_id>")
def status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job_id"}), 404
        return jsonify({k: v for k, v in job.items() if k != "local_path"})


@app.route("/api/jobs")
def list_jobs():
    with JOBS_LOCK:
        jobs = [{k: v for k, v in j.items() if k != "local_path"} for j in JOBS.values()]
    jobs.sort(key=lambda j: j["created_at"], reverse=True)
    return jsonify(jobs)


register_health_check(app)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
