"""
r2_client.py
Thin wrapper around boto3 for uploading files to a Cloudflare R2 bucket
(S3-compatible API).
"""

import os
import mimetypes
import boto3
from botocore.config import Config

R2_ACCOUNT_ID = os.environ.get("R2_ACCOUNT_ID", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
R2_BUCKET_NAME = os.environ.get("R2_BUCKET_NAME", "")

CONTENT_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
    ".m4s": "video/iso.segment",
    ".mp4": "video/mp4",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".png": "image/png",
    ".vtt": "text/vtt",
}

_client = None


def get_client():
    global _client
    if _client is not None:
        return _client
    if not (R2_ACCOUNT_ID and R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY):
        raise RuntimeError("R2 credentials are not fully configured (R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY)")
    endpoint_url = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    _client = boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4", retries={"max_attempts": 3, "mode": "standard"}),
        region_name="auto",
    )
    return _client


def _content_type_for(path):
    ext = os.path.splitext(path)[1].lower()
    return CONTENT_TYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"


def upload_file(local_path, key, bucket_name=None, public=False):
    bucket_name = bucket_name or R2_BUCKET_NAME
    if not bucket_name:
        raise RuntimeError("R2_BUCKET_NAME is not configured")
    client = get_client()
    extra_args = {"ContentType": _content_type_for(local_path)}
    if public:
        extra_args["ACL"] = "public-read"
    client.upload_file(local_path, bucket_name, key, ExtraArgs=extra_args)
    return key


def upload_many(files, bucket_name=None, on_progress=None):
    """files: list of (local_path, key). Uploads sequentially, reporting progress."""
    done = 0
    for local_path, key in files:
        upload_file(local_path, key, bucket_name=bucket_name)
        done += 1
        if on_progress:
            on_progress(done, len(files))
    return done
