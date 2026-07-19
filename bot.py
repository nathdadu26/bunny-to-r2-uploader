"""
bot.py
Telegram bot (Pyrogram / MTProto) that accepts videos sent directly to it in
DM and pushes them through the same migration pipeline as the web app.

Why Pyrogram instead of the plain Bot API: the HTTP Bot API caps file
downloads at 20MB for bots, which is unusable for real video files.
Pyrogram talks MTProto directly, so a bot account can download files up to
Telegram's normal size limit (2GB, 4GB for premium-enabled bots).

Behaviour (per spec):
  - Only /start command is registered (no other commands).
  - Only video messages are accepted — images/GIFs get a polite rejection.
  - After the file finishes downloading locally, the original DM message is
    deleted immediately (before Bunny upload/transcode even starts).
  - Status updates are edited into a single reply message as the pipeline
    progresses; the final message contains the streaming link.

A tiny Flask health endpoint runs alongside it (same health_check.py used by
app.py) so this can also be deployed as its own always-on Koyeb service.
"""

import os
import uuid
import asyncio
import logging
import threading

from flask import Flask
from dotenv import load_dotenv

load_dotenv()

from pyrogram import Client, filters
from pyrogram.types import Message

import pipeline
from health_check import register_health_check

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

TELEGRAM_API_ID_RAW = os.environ.get("TELEGRAM_API_ID", "0").strip()
try:
    TELEGRAM_API_ID = int(TELEGRAM_API_ID_RAW)
except ValueError:
    raise SystemExit(
        f"TELEGRAM_API_ID must be a plain number, got {TELEGRAM_API_ID_RAW!r}. "
        "Get it from https://my.telegram.org -> API development tools."
    )
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "").strip()
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

STAGE_LABELS = {
    "uploading_bunny": "⬆️ Uploading to Bunny Stream...",
    "transcoding": "🎞️ Transcoding...",
    "downloading": "📦 Downloading processed files...",
    "uploading_r2": "☁️ Uploading to R2...",
    "saving": "💾 Saving record...",
    "notifying": "📣 Posting to channel...",
    "done": "✅ Done!",
    "error": "❌ Failed",
}

bot = Client(
    "bunny_r2_bot",
    api_id=TELEGRAM_API_ID,
    api_hash=TELEGRAM_API_HASH,
    bot_token=TELEGRAM_BOT_TOKEN,
    workdir=BASE_DIR,
)


@bot.on_message(filters.command("start") & filters.private)
async def start_cmd(client, message: Message):
    logger.info("Received /start from user_id=%s", message.from_user.id if message.from_user else "?")
    await message.reply_text(
        "Send me a video file and I'll upload it, transcode it, and give you a streaming link.\n"
        "Only video files are supported (no photos or GIFs)."
    )


@bot.on_message(filters.private, group=1)
async def debug_log_all(client, message: Message):
    # Runs after the more specific handlers above (group=1 = lower priority).
    # Purely for debugging "bot not responding" — shows up in Koyeb logs
    # even if no other handler matched the message.
    logger.info(
        "Incoming private message id=%s from=%s type=%s",
        message.id,
        message.from_user.id if message.from_user else "?",
        message.media or "text",
    )


@bot.on_message((filters.photo | filters.animation) & filters.private)
async def reject_non_video(client, message: Message):
    await message.reply_text("Only video files are supported — please send a video.")


@bot.on_message(filters.video & filters.private)
async def handle_video(client, message: Message):
    loop = asyncio.get_event_loop()
    status_msg = await message.reply_text("⬇️ Downloading from Telegram...")

    original_name = message.video.file_name or f"telegram_{message.id}.mp4"
    local_path = os.path.join(UPLOAD_DIR, f"{uuid.uuid4()}_{original_name}")

    try:
        await client.download_media(message, file_name=local_path)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Telegram download failed")
        await status_msg.edit_text(f"❌ Download from Telegram failed: {exc}")
        return

    # Delete the original message right after the download completes, as requested.
    try:
        await message.delete()
    except Exception:  # noqa: BLE001
        logger.warning("Could not delete original message %s", message.id)

    await status_msg.edit_text("✅ Downloaded. Starting migration...")

    def on_stage(stage, progress):
        label = STAGE_LABELS.get(stage, stage)
        try:
            asyncio.run_coroutine_threadsafe(
                status_msg.edit_text(f"{label} ({progress}%)"), loop
            )
        except Exception:  # noqa: BLE001
            pass

    def run():
        return pipeline.process_video(
            local_path,
            original_name,
            source="telegram",
            work_dir=os.path.join(UPLOAD_DIR, f"{uuid.uuid4()}_work"),
            on_stage=on_stage,
            extra_meta={"telegram_user_id": message.from_user.id if message.from_user else None},
        )

    try:
        record = await loop.run_in_executor(None, run)
        await status_msg.edit_text(
            f"✅ Done!\n\n{record['title']}\n{record['streaming_link']}"
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Pipeline failed for %s", original_name)
        await status_msg.edit_text(f"❌ Failed: {exc}")


def run_bot_blocking():
    """Starts the bot and blocks forever (until the process exits). Safe to
    call from any thread.

    IMPORTANT: this must keep pyrogram's asyncio event loop actively
    pumping — a plain time.sleep() here would block the thread without
    yielding to the loop, which stops pyrogram's background network/
    dispatcher tasks from ever running (the connection looks "alive" but
    no update ever gets dispatched to a handler). So we create a dedicated
    event loop for this thread and keep it alive with an async wait
    instead of pyrogram's idle() (which needs the main thread for signal
    handlers and won't work from a background thread anyway)."""
    if not (TELEGRAM_API_ID and TELEGRAM_API_HASH and TELEGRAM_BOT_TOKEN):
        logger.warning(
            "TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_BOT_TOKEN not fully "
            "configured — Telegram bot will not start (web app continues normally)."
        )
        return

    async def _runner():
        async with bot:
            me = await bot.get_me()
            logger.info("Bot logged in successfully as @%s (id=%s)", me.username, me.id)
            logger.info("Waiting for messages... send /start to @%s on Telegram", me.username)

            admin_chat_id = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "").strip()
            if admin_chat_id:
                try:
                    await bot.send_message(
                        int(admin_chat_id),
                        f"✅ Bot restarted and is online as @{me.username}.\n"
                        "If you're seeing this but /start still doesn't reply, "
                        "sending works but receiving/dispatch is the problem.",
                    )
                    logger.info("Startup ping sent to TELEGRAM_ADMIN_CHAT_ID=%s", admin_chat_id)
                except Exception:
                    logger.exception(
                        "Failed to send startup ping to TELEGRAM_ADMIN_CHAT_ID=%s "
                        "(make sure that user has started a chat with the bot first)",
                        admin_chat_id,
                    )

            await asyncio.Event().wait()  # never set -> keeps the loop alive/pumping forever

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_runner())
    except Exception:
        logger.exception(
            "Bot crashed — check TELEGRAM_API_ID / TELEGRAM_API_HASH / TELEGRAM_BOT_TOKEN"
        )
        raise


def start_bot_background():
    """Starts the bot in a daemon thread. Used when app.py imports this
    module so a single container/Dockerfile can serve the web app and the
    Telegram bot together. Returns the thread, or None if not configured."""
    if not (TELEGRAM_API_ID and TELEGRAM_API_HASH and TELEGRAM_BOT_TOKEN):
        logger.info(
            "Telegram bot not configured (TELEGRAM_API_ID/HASH/BOT_TOKEN not set) — skipping."
        )
        return None
    t = threading.Thread(target=run_bot_blocking, daemon=True, name="telegram-bot")
    t.start()
    return t


def _run_health_server():
    """Minimal Flask app just for /health, /ping + the self-ping loop, for
    running bot.py as its own standalone process (not needed when it's
    imported by app.py, which already exposes /health itself)."""
    health_app = Flask(__name__)
    register_health_check(health_app)
    port = int(os.environ.get("PORT", "8000"))
    health_app.run(host="0.0.0.0", port=port, use_reloader=False)


if __name__ == "__main__":
    if not (TELEGRAM_API_ID and TELEGRAM_API_HASH and TELEGRAM_BOT_TOKEN):
        raise SystemExit(
            "TELEGRAM_API_ID, TELEGRAM_API_HASH and TELEGRAM_BOT_TOKEN must all be set. "
            "Get api_id/api_hash from https://my.telegram.org and the bot token from @BotFather."
        )
    threading.Thread(target=_run_health_server, daemon=True).start()
    logger.info("Starting Telegram bot (standalone mode)...")
    run_bot_blocking()
