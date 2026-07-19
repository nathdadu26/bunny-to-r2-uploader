# Bunny Stream → Cloudflare R2 Uploader (+ Telegram bot)

Web app: PC se video upload karo (drag & drop, multi-file, ya poori folder) →
har video Bunny Stream par upload hoke transcode hota hai → uske baad Bunny
ke "Download" button jo `data.zip` deta hai wahi zip Storage API se fetch
karke unzip kiya jaata hai → saari files (HLS playlist + segments, MP4
fallbacks, thumbnails) Cloudflare R2 me same structure me upload ho jaati
hain → MongoDB me title/size/thumbnail/mp4/hls links ek unique `mapping`
code ke saath save hote hain (streaming ke liye) → thumbnail ek Telegram
channel me `STREAMING_LINK_BASE/{mapping}` caption ke saath post ho jaata
hai.

Ek Telegram **bot** (`bot.py`) bhi hai — usko DM me video bhejo, wo bhi
same pipeline se guzarta hai.

## Kya fix hua is version me
Pehle wala version Bunny ke **pull-zone HLS URL** (`.../playlist.m3u8`) ko
directly crawl karta tha — ye token authentication ya propagation delay ki
wajah se fail ho sakta hai. Ab hum Bunny ke **Edge Storage API** se seedha
`data.zip` download karte hain (bilkul wahi zip jo dashboard ke "Download"
button se milta hai), unzip karte hain, aur unzip ki hui files R2 me upload
karte hain. Ye zyada reliable hai.

## Features
- Drag & drop upload zone — files ya poori folder, dono support, multiple files ek saath queue
- Upload (client → server) turant "complete" dikhta hai; baaki sab
  (Bunny upload → transcode wait → zip download → unzip → R2 upload → Mongo
  save → Telegram post) background me hota hai
- Har job ka status **History** panel me hamesha ke liye dikhta hai (`/api/jobs`)
  — page refresh karne par bhi status/streaming-link dikhta rahega
- MongoDB me har video ka record: title, size, thumbnail URL, per-resolution
  MP4 URLs, HLS playlist URL, aur ek unique `mapping` code
- Naye video ka thumbnail Telegram channel me `STREAMING_LINK_BASE/{mapping}`
  link ke saath post hota hai
- `bot.py` — Telegram bot jo DM me bheja gaya video download karke isi
  pipeline se guzarta hai; sirf `/start` command; sirf video files accept
  karta hai (photo/GIF reject); download complete hote hi original DM
  message delete kar deta hai
- `/health`, `/ping` + self-ping loop (`health_check.py`) — Koyeb free tier
  sleep nahi hoga
- Dono services (`app.py` web app, `bot.py` bot) ke liye alag Dockerfile

## Project layout
```
app.py                 Flask app — upload endpoint, job history, /api/video/<mapping>
bot.py                  Telegram bot (Pyrogram) — DM video intake
pipeline.py             Shared migration pipeline used by both app.py and bot.py
bunny_client.py         Bunny Stream API (create/upload/poll transcode)
bunny_storage_zip.py    Downloads + extracts the Bunny "data.zip" via Storage API
r2_client.py            Uploads files to Cloudflare R2 (boto3/S3 API)
mongo_client.py         Saves video records + generates the unique mapping code
telegram_notify.py      Posts thumbnail + streaming link to a Telegram channel
health_check.py         /health, /ping + self-ping background thread
templates/index.html    Upload UI (queue + history)
static/script.js        Drag/drop, folder traversal, XHR upload + polling + history panel
static/style.css        Styling
Dockerfile               Deploy app.py
Dockerfile.bot           Deploy bot.py
requirements.txt
.env.example
```

## 1. Environment variables

Sab kuch `.env.example` me hai. Sabse zaroori:

| Variable | Kya hai |
|---|---|
| `BUNNY_LIBRARY_ID` / `BUNNY_API_KEY` | Stream library aur uska API key |
| `BUNNY_STORAGE_ZONE_NAME` / `BUNNY_STORAGE_PASSWORD` | Library se linked Storage Zone ka naam + password (dashboard -> Stream library -> linked storage zone -> "FTP & API Access") — **ye Stream API key se alag hai**, zip download isi se hota hai |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_NAME` | R2 credentials |
| `R2_PUBLIC_BASE_URL` | Bucket ka public URL (r2.dev ya custom domain) — Mongo/Telegram links banane ke liye zaroori |
| `MONGODB_URI` | MongoDB connection string |
| `STREAMING_LINK_BASE` | e.g. `https://mydomain.com` — final link `STREAMING_LINK_BASE/{mapping}` banta hai |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHANNEL_ID` | Channel me thumbnail post karne ke liye |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | Sirf `bot.py` (DM intake) ke liye — [my.telegram.org](https://my.telegram.org) se milega |
| `SELF_URL` | Apna Koyeb public URL — self-ping ke liye |

## 2. Bunny Storage Zone password kaha milega
1. Bunny dashboard -> Stream -> apni library kholo.
2. Library ke saath ek Storage Zone linked hoti hai (usually same naam jo
   pull zone ka hai, e.g. `vz-4a69d144-b1a`).
3. Bunny dashboard -> Storage -> wahi zone dhundo -> **FTP & API Access** ->
   yaha se "Password" milega — yahi `BUNNY_STORAGE_PASSWORD` hai.
4. Zone ka naam hi `BUNNY_STORAGE_ZONE_NAME` hai.

## 3. Local test (Cloud Shell)
```bash
pip install -r requirements.txt
cp .env.example .env   # fill values
python app.py
```
App `http://localhost:8000` par khulega.

Bot alag se test karne ke liye:
```bash
python bot.py
```

## 4. Deploy on Koyeb (Docker)

**Web app:**
1. Repo GitHub par push karo.
2. Koyeb -> **Create Service** -> **Docker** -> repo select karo -> Dockerfile path `Dockerfile`.
3. Port `8000`, saare env vars add karo.
4. Deploy hone ke baad jo subdomain mile, usko `SELF_URL` me daalkar redeploy karo.

**Telegram bot (alag service):**
1. Same repo se dusri Koyeb service banao, Dockerfile path `Dockerfile.bot`.
2. Same env vars + `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_BOT_TOKEN` add karo.
3. Port `8000` (sirf health check ke liye) + `SELF_URL` isi service ka subdomain.

## Notes / limitations
- Job/history in-memory hai — container restart hone par UI history reset ho
  jaayegi (Mongo record safe rehta hai, bas is-run ki UI history jaati hai).
- `bot.py` Pyrogram (MTProto) use karta hai, standard Bot API nahi — isliye
  bade video files (jo 20MB Bot-API-download-limit se bade hain) bhi download
  ho paate hain.
- Free tier par RAM/CPU limited — `MAX_CONCURRENT_JOBS` ko 1-2 hi rakho.
- `INCLUDE_ORIGINAL_IN_R2=false` by default — Bunny zip me jo `original`
  (raw uploaded) file hoti hai wo R2 me nahi jaati, storage bachane ke liye.
  `true` set karke original bhi upload kar sakte ho.
