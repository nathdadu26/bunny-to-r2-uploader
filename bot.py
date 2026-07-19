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

TELEGRAM_API_ID = int(os.environ.get("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

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
    await message.reply_text(
        "Send me a video file and I'll upload it, transcode it, and give you a streaming link.\n"
        "Only video files are supported (no photos or GIFs)."
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


def _run_health_server():
    """Minimal Flask app just for /health, /ping + the self-ping loop,
    so this service can be deployed the same way as app.py on Koyeb."""
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
    logger.info("Starting Telegram bot...")
    bot.run()
