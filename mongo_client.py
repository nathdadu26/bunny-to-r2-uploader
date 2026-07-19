"""
mongo_client.py
Saves one record per successfully migrated video: title, size, thumbnail
link, mp4 fallback links, HLS playlist link, and a short unique "mapping"
code used to build a stable streaming URL (STREAMING_LINK_BASE/{mapping}).
"""

import os
import secrets
import string
from datetime import datetime, timezone

from pymongo import MongoClient

MONGODB_URI = os.environ.get("MONGODB_URI", "")
MONGODB_DB_NAME = os.environ.get("MONGODB_DB_NAME", "bunny_r2_uploader")
COLLECTION_NAME = "videos"

MAPPING_ALPHABET = string.ascii_lowercase + string.digits
MAPPING_LENGTH = 7

_client = None


class MongoError(Exception):
    pass


def get_collection():
    global _client
    if not MONGODB_URI:
        raise MongoError("MONGODB_URI is not configured")
    if _client is None:
        _client = MongoClient(MONGODB_URI)
    return _client[MONGODB_DB_NAME][COLLECTION_NAME]


def _random_mapping():
    return "".join(secrets.choice(MAPPING_ALPHABET) for _ in range(MAPPING_LENGTH))


def generate_unique_mapping(max_attempts=10):
    coll = get_collection()
    for _ in range(max_attempts):
        candidate = _random_mapping()
        if coll.find_one({"mapping": candidate}) is None:
            return candidate
    raise MongoError("Could not generate a unique mapping code")


def save_video_record(record):
    """record must include at least: mapping, bunny_video_id, title, size,
    hls_playlist_url, thumbnail_url, mp4_urls (dict), r2_prefix, source.
    Returns the inserted document's mapping code.
    """
    coll = get_collection()
    record = dict(record)
    record.setdefault("created_at", datetime.now(timezone.utc))
    record["updated_at"] = datetime.now(timezone.utc)
    coll.update_one({"mapping": record["mapping"]}, {"$set": record}, upsert=True)
    return record["mapping"]


def get_by_mapping(mapping):
    coll = get_collection()
    return coll.find_one({"mapping": mapping}, {"_id": 0})
