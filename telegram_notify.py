"""
telegram_notify.py
Posts a video's thumbnail to a Telegram channel once it's live, with a
caption that's just the player URL: {TELEGRAM_CAPTION_TEMPLATE}/{mapping}.
Uses plain Bot API HTTP calls (no bot framework needed for this one-way post).
"""

import os
import logging
import requests

logger = logging.getLogger("telegram_notify")

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")  # e.g. @mychannel or -100xxxxxxxxxx
STREAMING_LINK_BASE = os.environ.get("STREAMING_LINK_BASE", "").rstrip("/")
# Player domain used specifically for the channel post caption, e.g.
# "https://domain.com" -> caption becomes "https://domain.com/{mapping}"
PLAYER_DOMAIN = os.environ.get("TELEGRAM_CAPTION_TEMPLATE", "").rstrip("/")

API_BASE = "https://api.telegram.org"


def build_streaming_link(mapping):
    if not STREAMING_LINK_BASE:
        return mapping
    return f"{STREAMING_LINK_BASE}/{mapping}"


def post_thumbnail_to_channel(thumbnail_url, title, mapping):
    """Best-effort: logs and returns False on failure instead of raising,
    since a notify failure shouldn't fail the whole migration job."""
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID):
        logger.info("Telegram channel posting not configured, skipping")
        return False

    caption = f"{PLAYER_DOMAIN}/{mapping}" if PLAYER_DOMAIN else mapping

    url = f"{API_BASE}/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    try:
        resp = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHANNEL_ID, "caption": caption, "photo": thumbnail_url},
            timeout=30,
        )
        if resp.status_code != 200:
            logger.warning("sendPhoto failed (%s): %s", resp.status_code, resp.text[:300])
            return False
        return True
    except Exception:  # noqa: BLE001
        logger.exception("Failed to post thumbnail to Telegram channel")
        return False
