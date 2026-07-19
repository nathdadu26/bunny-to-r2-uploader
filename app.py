"""
app.py
Flask web app:
  - Drag & drop / multi-file / folder upload UI (templates/index.html)
  - Client upload to server is tracked as its own quick step; once it's on
    disk the job is handed to pipeline.process_video() in a background
    thread, and the job stays in JOBS forever so the frontend's History
    panel can show it (in-memory only — resets on container restart, but
    the underlying Bunny video and Mongo record are unaffected).
"""

import os
import uuid
import logging
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv

load_dotenv()

import mongo_client
import pipeline
from health_check import register_health_check

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "2"))

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = None  # large video uploads; limited by host resources instead

executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_JOBS)

# In-memory job store: job_id -> dict. Never deleted during the process's
# lifetime, so /api/jobs doubles as upload history.
JOBS = {}
JOBS_LOCK = threading.Lock()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _update_job(job_id, **fields):
    with JOBS_LOCK:
        if job_id not in JOBS:
            return
        JOBS[job_id].update(fields)
        JOBS[job_id]["updated_at"] = _now()


def run_pipeline_job(job_id):
    with JOBS_LOCK:
        job = dict(JOBS[job_id])

    def on_stage(stage, progress):
        _update_job(job_id, stage=stage, progress=progress)

    try:
        record = pipeline.process_video(
            job["local_path"],
            job["original_name"],
            source="web",
            work_dir=os.path.join(UPLOAD_DIR, f"{job_id}_work"),
            on_stage=on_stage,
            extra_meta={"relative_path": job.get("relative_path")},
        )
        _update_job(
            job_id,
            stage="done",
            progress=100,
            mapping=record["mapping"],
            streaming_link=record["streaming_link"],
            hls_playlist_url=record.get("hls_playlist_url"),
            thumbnail_url=record.get("thumbnail_url"),
            bunny_video_id=record.get("bunny_video_id"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Job %s failed", job_id)
        _update_job(job_id, stage="error", error=str(exc))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/upload", methods=["POST"])
def upload():
    """Accepts a single file per request (frontend loops for multi/folder
    uploads). As soon as the file is fully saved to disk, the job is marked
    'uploaded' (complete, from the client's point of view) and handed off to
    the background pipeline — the response returns immediately after that."""
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

    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "original_name": f.filename,
            "relative_path": relative_path or f.filename,
            "local_path": local_path,
            "size": os.path.getsize(local_path),
            "stage": "uploaded",  # client-side upload is done; rest continues in background
            "progress": 5,
            "error": None,
            "created_at": _now(),
            "updated_at": _now(),
        }

    executor.submit(run_pipeline_job, job_id)
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
    """Full upload history (in-memory, all sessions)."""
    with JOBS_LOCK:
        jobs = [{k: v for k, v in j.items() if k != "local_path"} for j in JOBS.values()]
    jobs.sort(key=lambda j: j["created_at"], reverse=True)
    return jsonify(jobs)


@app.route("/api/video/<mapping>")
def get_video(mapping):
    try:
        record = mongo_client.get_by_mapping(mapping)
    except mongo_client.MongoError as exc:
        return jsonify({"error": str(exc)}), 500
    if not record:
        return jsonify({"error": "not found"}), 404
    return jsonify(record)


register_health_check(app)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
