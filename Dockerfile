FROM python:3.11-slim

WORKDIR /app

# ca-certificates: requests/boto3/pyrogram need certs
# gcc: TgCrypto occasionally has no prebuilt wheel for a given platform/python
#      combo and needs to compile from source
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/uploads

ENV PORT=8000
EXPOSE 8000

# Single process serves everything:
#  - Flask web app (upload UI, /api/*, /health, /ping) on $PORT
#  - Telegram bot (bot.py) runs in a background thread inside the same
#    process, started from app.py — only if TELEGRAM_API_ID/HASH/BOT_TOKEN
#    are set. If they're not set, the bot simply doesn't start and only the
#    web app runs.
# Single gunicorn worker is required (not 2+): the bot thread starts once at
# module import time, so more workers would each start their own bot thread
# and duplicate/collide with the same Telegram bot token.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "600", "app:app"]
