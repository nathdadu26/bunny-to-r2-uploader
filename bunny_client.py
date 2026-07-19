"""
bunny_client.py
Wrapper around the Bunny Stream (video.bunnycdn.com) REST API.

Docs: https://docs.bunny.net/reference/video_createvideo
"""

import os
import time
import requests

BUNNY_API_BASE = "https://video.bunnycdn.com/library"

BUNNY_LIBRARY_ID = os.environ.get("BUNNY_LIBRARY_ID", "")
BUNNY_API_KEY = os.environ.get("BUNNY_API_KEY", "")

# Bunny video status codes
STATUS_CREATED = 0
STATUS_UPLOADED = 1
STATUS_PROCESSING = 2
STATUS_TRANSCODING = 3
STATUS_FINISHED = 4
STATUS_ERROR = 5
STATUS_UPLOAD_FAILED = 6

FAILED_STATUSES = (STATUS_ERROR, STATUS_UPLOAD_FAILED)


class BunnyError(Exception):
    pass


def _headers():
    if not BUNNY_API_KEY:
        raise BunnyError("BUNNY_API_KEY is not configured")
    return {"AccessKey": BUNNY_API_KEY, "accept": "application/json"}


class ProgressFileReader:
    """File-like wrapper that reports read progress via a callback.
    Lets requests stream the PUT body straight from disk without
    loading the whole video into memory, while we still get a
    percentage for the UI.
    """

    def __init__(self, path, on_progress=None, chunk_size=1024 * 1024):
        self._path = path
        self._size = os.path.getsize(path)
        self._read = 0
        self._chunk_size = chunk_size
        self._on_progress = on_progress
        self._fh = open(path, "rb")

    def __len__(self):
        return self._size

    def read(self, size=-1):
        size = self._chunk_size if size in (-1, None) else size
        chunk = self._fh.read(size)
        if chunk:
            self._read += len(chunk)
            if self._on_progress:
                pct = min(100, int(self._read * 100 / self._size)) if self._size else 100
                self._on_progress(pct, self._read, self._size)
        return chunk

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass


def create_video(title, library_id=None):
    """Creates a video entry in the Bunny Stream library and returns its videoId (guid)."""
    library_id = library_id or BUNNY_LIBRARY_ID
    if not library_id:
        raise BunnyError("BUNNY_LIBRARY_ID is not configured")
    url = f"{BUNNY_API_BASE}/{library_id}/videos"
    resp = requests.post(url, json={"title": title}, headers={**_headers(), "content-type": "application/*+json"})
    if resp.status_code not in (200, 201):
        raise BunnyError(f"create_video failed ({resp.status_code}): {resp.text}")
    data = resp.json()
    video_id = data.get("guid") or data.get("videoId") or data.get("id")
    if not video_id:
        raise BunnyError(f"create_video: no guid in response: {data}")
    return video_id


def upload_video(video_id, file_path, library_id=None, on_progress=None):
    """Uploads the raw video bytes for an already-created video entry."""
    library_id = library_id or BUNNY_LIBRARY_ID
    url = f"{BUNNY_API_BASE}/{library_id}/videos/{video_id}"
    reader = ProgressFileReader(file_path, on_progress=on_progress)
    try:
        headers = {**_headers(), "content-type": "application/octet-stream"}
        # requests needs Content-Length to stream a file-like object with PUT
        headers["Content-Length"] = str(len(reader))
        resp = requests.put(url, data=reader, headers=headers)
    finally:
        reader.close()
    if resp.status_code not in (200, 201):
        raise BunnyError(f"upload_video failed ({resp.status_code}): {resp.text}")
    return True


def get_video(video_id, library_id=None):
    library_id = library_id or BUNNY_LIBRARY_ID
    url = f"{BUNNY_API_BASE}/{library_id}/videos/{video_id}"
    resp = requests.get(url, headers=_headers())
    if resp.status_code != 200:
        raise BunnyError(f"get_video failed ({resp.status_code}): {resp.text}")
    return resp.json()


def wait_for_transcode(video_id, library_id=None, poll_interval=8, timeout=3600, on_status=None):
    """Polls Bunny until the video finishes transcoding (status 4),
    fails (status 5/6), or the timeout is hit."""
    start = time.time()
    while True:
        info = get_video(video_id, library_id=library_id)
        status = info.get("status")
        pct = info.get("encodeProgress", 0)
        if on_status:
            on_status(status, pct)
        if status == STATUS_FINISHED:
            return info
        if status in FAILED_STATUSES:
            raise BunnyError(f"Bunny transcoding failed for {video_id} (status={status})")
        if time.time() - start > timeout:
            raise BunnyError(f"Timed out waiting for Bunny transcode of {video_id}")
        time.sleep(poll_interval)


def delete_video(video_id, library_id=None):
    library_id = library_id or BUNNY_LIBRARY_ID
    url = f"{BUNNY_API_BASE}/{library_id}/videos/{video_id}"
    resp = requests.delete(url, headers=_headers())
    return resp.status_code in (200, 204)
