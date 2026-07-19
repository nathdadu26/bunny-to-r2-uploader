"""
hls_downloader.py
Downloads the full HLS output (master playlist + variant playlists + .ts/.m4s
segments + thumbnail) that Bunny Stream generates for a video, mirroring the
same relative folder structure so it can be re-uploaded to R2 as-is.
"""

import os
import requests
from urllib.parse import urljoin, urlparse

TIMEOUT = 30


class DownloadError(Exception):
    pass


def _pull_zone_base_url(pull_zone_hostname, video_id):
    hostname = pull_zone_hostname.strip().rstrip("/")
    if not hostname.startswith("http"):
        hostname = f"https://{hostname}"
    return f"{hostname}/{video_id}/"


def _relative_key(root_url, file_url):
    """Returns the path of file_url relative to root_url (the videoId/ folder)."""
    root_path = urlparse(root_url).path
    file_path = urlparse(file_url).path
    if file_path.startswith(root_path):
        rel = file_path[len(root_path):]
    else:
        rel = os.path.basename(file_path)
    return rel.lstrip("/")


def _download_file(url, local_path):
    os.makedirs(os.path.dirname(local_path), exist_ok=True)
    resp = requests.get(url, stream=True, timeout=TIMEOUT)
    if resp.status_code != 200:
        raise DownloadError(f"GET {url} failed ({resp.status_code})")
    with open(local_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 256):
            if chunk:
                f.write(chunk)
    return local_path


def _process_playlist(url, root_url, dest_dir, collected, visited, on_progress=None):
    if url in visited:
        return
    visited.add(url)

    rel_key = _relative_key(root_url, url)
    local_path = os.path.join(dest_dir, rel_key)
    _download_file(url, local_path)
    collected.append((local_path, rel_key))
    if on_progress:
        on_progress(len(collected))

    with open(local_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Reference to another playlist or a segment file
        ref_url = urljoin(url, line)
        if line.endswith(".m3u8"):
            _process_playlist(ref_url, root_url, dest_dir, collected, visited, on_progress)
        else:
            ref_key = _relative_key(root_url, ref_url)
            ref_local = os.path.join(dest_dir, ref_key)
            if ref_local not in [c[0] for c in collected]:
                _download_file(ref_url, ref_local)
                collected.append((ref_local, ref_key))
                if on_progress:
                    on_progress(len(collected))


def download_hls(video_id, pull_zone_hostname, dest_dir, on_progress=None, include_thumbnail=True):
    """Downloads the complete HLS tree for a Bunny video.

    Returns a list of (local_path, relative_key) tuples, where relative_key
    is the path under the videoId/ folder (e.g. "playlist.m3u8",
    "720p/video.m3u8", "720p/seg_0.ts").
    """
    root_url = _pull_zone_base_url(pull_zone_hostname, video_id)
    master_url = urljoin(root_url, "playlist.m3u8")

    collected = []
    visited = set()
    _process_playlist(master_url, root_url, dest_dir, collected, visited, on_progress)

    if include_thumbnail:
        for thumb_name in ("thumbnail.jpg", "preview.webp"):
            try:
                thumb_url = urljoin(root_url, thumb_name)
                local_path = os.path.join(dest_dir, thumb_name)
                _download_file(thumb_url, local_path)
                collected.append((local_path, thumb_name))
            except DownloadError:
                pass  # thumbnail is best-effort

    if not collected:
        raise DownloadError(f"No HLS files found for video {video_id} at {root_url}")

    return collected
