"""
bunny_storage_zip.py

Bunny Stream's pull-zone HLS URLs (https://{pullzone}.b-cdn.net/{videoId}/playlist.m3u8)
are frequently protected by token authentication or simply not reliable to
crawl file-by-file. Every video's processed files (HLS playlists, .ts
segments, MP4 fallbacks, thumbnails) are also stored in a hidden Bunny Edge
Storage zone that's linked to the video library — this is exactly what the
"Download" button in the Bunny dashboard uses (it fetches a `data.zip` of the
video's folder). This module does the same thing via the Storage API, which
is far more reliable than crawling playlists over the CDN.

Docs / reference: https://docs.bunny.net/reference/storage_api (Edge Storage)
Requires the STORAGE ZONE password (different from the Stream library
AccessKey) — found in the Bunny dashboard under the video library's linked
storage zone -> "FTP & API Access".
"""

import os
import zipfile
import requests

REGION_HOSTS = {
    "": "storage.bunnycdn.com",
    "de": "storage.bunnycdn.com",
    "ny": "ny.storage.bunnycdn.com",
    "la": "la.storage.bunnycdn.com",
    "sg": "sg.storage.bunnycdn.com",
    "syd": "syd.storage.bunnycdn.com",
    "uk": "uk.storage.bunnycdn.com",
}

BUNNY_STORAGE_ZONE_NAME = os.environ.get("BUNNY_STORAGE_ZONE_NAME", "")
BUNNY_STORAGE_PASSWORD = os.environ.get("BUNNY_STORAGE_PASSWORD", "")
BUNNY_STORAGE_REGION = os.environ.get("BUNNY_STORAGE_REGION", "").strip().lower()

TIMEOUT = 60


class StorageError(Exception):
    pass


def _storage_host():
    return REGION_HOSTS.get(BUNNY_STORAGE_REGION, "storage.bunnycdn.com")


def _check_config():
    if not BUNNY_STORAGE_ZONE_NAME or not BUNNY_STORAGE_PASSWORD:
        raise StorageError(
            "BUNNY_STORAGE_ZONE_NAME / BUNNY_STORAGE_PASSWORD not configured "
            "(dashboard -> Stream library -> linked storage zone -> FTP & API Access)"
        )


def download_zip(video_id, dest_zip_path, on_progress=None):
    """Downloads the full data.zip for a video's storage folder — the same
    file the dashboard 'Download' button produces."""
    _check_config()
    url = f"https://{_storage_host()}/{BUNNY_STORAGE_ZONE_NAME}/{video_id}/?download"
    headers = {"AccessKey": BUNNY_STORAGE_PASSWORD}

    with requests.get(url, headers=headers, stream=True, timeout=TIMEOUT) as resp:
        if resp.status_code != 200:
            raise StorageError(f"zip download failed for {video_id} ({resp.status_code}): {resp.text[:300]}")
        total = int(resp.headers.get("content-length", 0))
        written = 0
        os.makedirs(os.path.dirname(dest_zip_path), exist_ok=True)
        with open(dest_zip_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 512):
                if chunk:
                    f.write(chunk)
                    written += len(chunk)
                    if on_progress and total:
                        on_progress(min(100, int(written * 100 / total)))
    return dest_zip_path


def extract_zip(zip_path, dest_dir):
    """Extracts the zip and returns a list of (local_path, relative_key)
    for every file inside (folder structure preserved, e.g.
    '720p/video.m3u8', 'playlist.m3u8', 'thumbnail.jpg', 'play_720p.mp4')."""
    os.makedirs(dest_dir, exist_ok=True)
    files = []
    with zipfile.ZipFile(zip_path, "r") as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            zf.extract(info, dest_dir)
            rel_key = info.filename.replace("\\", "/")
            local_path = os.path.join(dest_dir, info.filename)
            files.append((local_path, rel_key))
    if not files:
        raise StorageError(f"zip {zip_path} contained no files")
    return files


def download_and_extract(video_id, work_dir, on_download_progress=None):
    """Convenience wrapper: downloads the zip to work_dir/data.zip, extracts
    to work_dir/extracted/, and removes the zip afterwards.

    Returns a list of (local_path, relative_key) tuples.
    """
    zip_path = os.path.join(work_dir, "data.zip")
    extracted_dir = os.path.join(work_dir, "extracted")
    download_zip(video_id, zip_path, on_progress=on_download_progress)
    files = extract_zip(zip_path, extracted_dir)
    try:
        os.remove(zip_path)
    except OSError:
        pass
    return files, extracted_dir
