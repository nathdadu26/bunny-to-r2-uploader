"""
health_check.py

Two jobs live in this single file, exactly as requested:

1. A /health (and /ping) route that Koyeb's own health checker can hit.
2. A background "self-ping" thread that periodically calls the app's own
   public URL so the free-tier instance never goes idle/sleeps.

Usage (from app.py):

    from health_check import register_health_check
    register_health_check(app)

Configure via env vars:
    SELF_URL              -> full public URL of this deployment
                              e.g. https://your-app-name.koyeb.app
                              (KOYEB_PUBLIC_DOMAIN is auto-detected if set)
    SELF_PING_INTERVAL     -> seconds between self-pings (default 240 = 4 min)
    SELF_PING_ENABLED      -> "true"/"false" (default "true")
"""

import os
import time
import threading
import logging
from datetime import datetime, timezone

import requests
from flask import jsonify

logger = logging.getLogger("health_check")
logging.basicConfig(level=logging.INFO)

START_TIME = time.time()
_last_ping_result = {"ok": None, "at": None, "detail": None}


def _resolve_self_url():
    explicit = os.environ.get("SELF_URL", "").strip().rstrip("/")
    if explicit:
        return explicit
    # Koyeb injects this for the default subdomain
    koyeb_domain = os.environ.get("KOYEB_PUBLIC_DOMAIN", "").strip()
    if koyeb_domain:
        return f"https://{koyeb_domain}"
    return ""


def register_health_check(app):
    """Registers the /health and /ping routes on the given Flask app,
    and starts the self-ping background thread (once)."""

    @app.route("/health")
    def health():
        uptime = round(time.time() - START_TIME, 1)
        return jsonify(
            {
                "status": "ok",
                "uptime_seconds": uptime,
                "server_time": datetime.now(timezone.utc).isoformat(),
                "last_self_ping": _last_ping_result,
            }
        ), 200

    @app.route("/ping")
    def ping():
        return "pong", 200

    _start_self_ping_thread()
    return app


def _start_self_ping_thread():
    if os.environ.get("SELF_PING_ENABLED", "true").lower() not in ("1", "true", "yes"):
        logger.info("Self-ping disabled via SELF_PING_ENABLED")
        return

    interval = int(os.environ.get("SELF_PING_INTERVAL", "240"))
    self_url = _resolve_self_url()

    if not self_url:
        logger.warning(
            "SELF_URL / KOYEB_PUBLIC_DOMAIN not set — self-ping loop will "
            "idle until one is configured. Set SELF_URL to your Koyeb "
            "subdomain to keep the free-tier instance awake."
        )

    def _loop():
        # small initial delay so the server is fully up before the first ping
        time.sleep(15)
        while True:
            url = self_url or _resolve_self_url()
            if url:
                try:
                    resp = requests.get(f"{url}/ping", timeout=15)
                    _last_ping_result["ok"] = resp.status_code == 200
                    _last_ping_result["detail"] = f"HTTP {resp.status_code}"
                except Exception as exc:  # noqa: BLE001
                    _last_ping_result["ok"] = False
                    _last_ping_result["detail"] = str(exc)
                _last_ping_result["at"] = datetime.now(timezone.utc).isoformat()
                if not _last_ping_result["ok"]:
                    logger.warning("Self-ping failed: %s", _last_ping_result["detail"])
            time.sleep(interval)

    t = threading.Thread(target=_loop, name="self-ping", daemon=True)
    t.start()
    logger.info("Self-ping thread started (interval=%ss, url=%s)", interval, self_url or "auto-detect")
