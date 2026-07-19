# Bunny Stream → Cloudflare R2 Uploader

Web app: PC se video upload karo (drag & drop, multi-file, ya poori folder) →
har video Bunny Stream par upload hoke transcode hota hai → transcode ke baad
uske saare HLS files (playlist.m3u8 + resolution playlists + .ts segments)
download hoke Cloudflare R2 bucket me same folder structure me upload ho jaate
hain.

## Features
- Drag & drop upload zone (files ya poori folder, dono support)
- Multiple files ek saath queue ho jaate hain (2 parallel process hote hain by default)
- Per-file progress bar with stage (`Uploading to Bunny → Transcoding → Downloading HLS → Uploading to R2 → Done`)
- `/health` and `/ping` routes + built-in self-ping loop (`health_check.py`) so the
  Koyeb free-tier instance doesn't sleep
- Dockerfile ready for Koyeb deploy

## Project layout
```
app.py              Flask app, upload endpoint, job orchestration
bunny_client.py      Bunny Stream API (create/upload/poll)
hls_downloader.py     Downloads generated HLS tree (m3u8 + segments)
r2_client.py          Uploads files to Cloudflare R2 (boto3/S3 API)
health_check.py       /health, /ping + self-ping background thread
templates/index.html  Upload UI
static/script.js       Drag/drop, folder traversal, XHR upload + polling
static/style.css       Styling
Dockerfile
requirements.txt
.env.example
```

## 1. Environment variables

Copy `.env.example` to `.env` locally, ya Koyeb dashboard me "Environment
variables" section me set karo:

| Variable | Description |
|---|---|
| `BUNNY_LIBRARY_ID` | Bunny Stream video library ID |
| `BUNNY_API_KEY` | Library ka API key (Bunny dashboard → Stream → your library → API) |
| `BUNNY_PULL_ZONE_HOSTNAME` | Pull zone hostname jaha se HLS serve hota hai, e.g. `vz-xxxxxxxx-xxx.b-cdn.net` (library settings me milega) |
| `R2_ACCOUNT_ID` | Cloudflare account ID |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | R2 API token (Cloudflare dashboard → R2 → Manage API Tokens) |
| `R2_BUCKET_NAME` | Target R2 bucket name |
| `R2_KEY_PREFIX` | (optional) sab uploads ek folder prefix ke andar daalne ke liye |
| `MAX_CONCURRENT_JOBS` | Ek saath kitne videos process honge (default 2) |
| `DELETE_LOCAL_AFTER_UPLOAD` | Local temp files cleanup karo ya nahi (default true) |
| `DELETE_FROM_BUNNY_AFTER_SUCCESS` | R2 upload success hone ke baad Bunny se video delete karo ya nahi (default false) |
| `SELF_URL` | Apna Koyeb public URL, e.g. `https://your-app.koyeb.app` — self-ping ke liye |
| `SELF_PING_INTERVAL` | Seconds (default 240 = 4 min) |
| `SELF_PING_ENABLED` | `true`/`false` |

## 2. Local test (Cloud Shell)

```bash
pip install -r requirements.txt
cp .env.example .env   # fill values
python app.py
```

App `http://localhost:8000` par khulega.

## 3. Deploy on Koyeb (Docker)

1. Is folder ko GitHub repo me push karo (ya zip Koyeb me directly build karo).
2. Koyeb dashboard → **Create Service** → **Docker** → apna repo/Dockerfile select karo.
3. Port `8000` set karo.
4. Step 1 wale saare environment variables Koyeb service settings me add karo.
5. Deploy hone ke baad jo subdomain milega (e.g. `https://xxxx.koyeb.app`), usko
   `SELF_URL` env var me daalkar service ko **redeploy** karo — isse self-ping
   sahi URL par hit karega aur free tier sleep nahi hoga.
6. Subdomain khol ke seedha upload UI dikhega — drag & drop ya "Choose Files"
   / "Choose Folder" button se videos select karo.

## Notes / limitations
- Job status in-memory rakha jaata hai — agar container restart ho gaya to
  in-progress jobs ka status reset ho jaayega (video Bunny par safe rehta hai,
  bas UI progress reset hoga).
- Free tier par RAM/CPU limited hote hain, isliye bahut badi files (multi-GB)
  ya bahut zyada parallel jobs slow ho sakte hain — `MAX_CONCURRENT_JOBS` ko
  free tier ke resources ke hisaab se 1-2 hi rakhna better hai.
- `BUNNY_PULL_ZONE_HOSTNAME` galat hoga to HLS download fail hoga — ye
  Bunny Stream library settings me "Pull Zone" ke Hostname field se milta hai.
