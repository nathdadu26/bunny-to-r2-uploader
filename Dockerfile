FROM python:3.11-slim

WORKDIR /app

# System deps kept minimal; requests/boto3 need certs
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /app/uploads /app/downloads

ENV PORT=8000
EXPOSE 8000

# Single gunicorn worker is enough (jobs run in a background thread pool
# inside the process); more workers would each spin up their own self-ping
# thread and job store, which we don't want on the free tier.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "600", "app:app"]
