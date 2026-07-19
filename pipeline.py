"""
pipeline.py
The single source of truth for "take a local video file, end up with it
fully migrated" — used by both app.py (web upload UI) and bot.py (Telegram
DM uploads). Keeping this in one place means the web flow and the bot flow
can never drift apart.

process_video() is synchronous/blocking by design — callers run it in a
background thread (ThreadPoolExecutor in app.py, run_in_executor in bot.py).
"""

import os
import re
import shutil
import logging

import bunny_client
import bunny_storage_zip
import r2_client
import mongo_client
import telegram_notify

logger = logging.getLogger("pipeline")

DELETE_LOCAL_AFTER_UPLOAD = os.environ.get("DELETE_LOCAL_AFTER_UPLOAD", "true").lower() == "true"
DELETE_FROM_BUNNY_AFTER_SUCCESS = os.environ.get("DELETE_FROM_BUNNY_AFTER_SUCCESS", "false").lower() == "true"
INCLUDE_ORIGINAL_IN_R2 = os.environ.get("INCLUDE_ORIGINAL_IN_R2", "false").lower() == "true"
R2_KEY_PREFIX = os.environ.get("R2_KEY_PREFIX", "").strip().strip("/")

MP4_RE = re.compile(r"^play_(\d+)p\.mp4$")


class PipelineError(Exception):
    pass


def _noop(*args, **kwargs):
    pass


def process_video(local_path, title, source="web", work_dir=None, on_stage=None, extra_meta=None):
    """Runs the full migration for one already-downloaded-to-disk video file.

    on_stage(stage: str, progress: int) is called as the job advances; stage
    is one of: uploading_bunny, transcoding, downloading, uploading_r2,
    saving, notifying, done, error.

    Returns the saved record dict on success. Raises PipelineError (or lets
    the underlying exception through) on failure.
    """
    on_stage = on_stage or _noop
    extra_meta = extra_meta or {}
    work_dir = work_dir or (local_path + "_work")
    size = os.path.getsize(local_path)

    try:
        # 1. Create + upload to Bunny Stream
        on_stage("uploading_bunny", 10)
        video_id = bunny_client.create_video(title)

        def on_upload_progress(pct, read, total):
            on_stage("uploading_bunny", 10 + int(pct * 0.20))

        bunny_client.upload_video(video_id, local_path, on_progress=on_upload_progress)

        # 2. Wait for transcoding
        on_stage("transcoding", 30)

        def on_status(status, pct):
            on_stage("transcoding", 30 + int(min(pct, 100) * 0.30))

        bunny_client.wait_for_transcode(video_id, on_status=on_status)

        # 3. Download + extract the full data.zip (HLS + mp4 fallbacks + thumbnails)
        on_stage("downloading", 60)

        def on_download_progress(pct):
            on_stage("downloading", 60 + int(pct * 0.15))

        files, extracted_dir = bunny_storage_zip.download_and_extract(
            video_id, work_dir, on_download_progress=on_download_progress
        )

        if not INCLUDE_ORIGINAL_IN_R2:
            files = [(p, k) for p, k in files if k != "original"]

        # 4. Upload everything to R2, mirroring the folder structure
        on_stage("uploading_r2", 78)
        prefix_parts = [p for p in [R2_KEY_PREFIX, video_id] if p]
        r2_prefix = "/".join(prefix_parts)
        upload_list = [(local, f"{r2_prefix}/{rel_key}") for local, rel_key in files]

        def on_r2_progress(done, total):
            on_stage("uploading_r2", 78 + int(done * 12 / max(total, 1)))

        r2_client.upload_many(upload_list, on_progress=on_r2_progress)

        public_base = os.environ.get("R2_PUBLIC_BASE_URL", "").rstrip("/")

        def public_url(rel_key):
            if not public_base:
                return None
            return f"{public_base}/{r2_prefix}/{rel_key}"

        rel_keys = {rel_key for _, rel_key in files}
        thumbnail_key = "thumbnail.jpg" if "thumbnail.jpg" in rel_keys else None
        playlist_key = "playlist.m3u8" if "playlist.m3u8" in rel_keys else None
        mp4_urls = {}
        for rel_key in rel_keys:
            m = MP4_RE.match(rel_key)
            if m:
                mp4_urls[f"{m.group(1)}p"] = public_url(rel_key)

        # 5. Save Mongo record with a unique streaming mapping code
        on_stage("saving", 92)
        mapping = mongo_client.generate_unique_mapping()
        record = {
            "mapping": mapping,
            "bunny_video_id": video_id,
            "title": title,
            "size": size,
            "source": source,
            "r2_prefix": r2_prefix,
            "hls_playlist_url": public_url(playlist_key) if playlist_key else None,
            "thumbnail_url": public_url(thumbnail_key) if thumbnail_key else None,
            "mp4_urls": mp4_urls,
            "file_count": len(files),
            **extra_meta,
        }
        mongo_client.save_video_record(record)
        record["streaming_link"] = telegram_notify.build_streaming_link(mapping)

        # 6. Post thumbnail to the Telegram channel (best-effort)
        on_stage("notifying", 96)
        if record["thumbnail_url"]:
            telegram_notify.post_thumbnail_to_channel(record["thumbnail_url"], title, mapping)

        if DELETE_FROM_BUNNY_AFTER_SUCCESS:
            try:
                bunny_client.delete_video(video_id)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to delete bunny video %s after migration", video_id)

        on_stage("done", 100)
        return record

    finally:
        if DELETE_LOCAL_AFTER_UPLOAD:
            for path in (local_path, work_dir):
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
                    elif os.path.exists(path):
                        os.remove(path)
                except Exception:  # noqa: BLE001
                    logger.exception("Cleanup failed for %s", path)
