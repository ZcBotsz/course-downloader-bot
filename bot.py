#!/usr/bin/env python3
"""
Course Downloader Bot v2.0
Batch downloads ALL videos from Study Pi, uploads to Telegram with proper captions & thumbnails
"""

import os, sys, json, asyncio, time, re, hashlib
from pathlib import Path
import aiohttp
from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeVideo

# ── CONFIG ─────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0"))

API_BASE = "https://studyuk.cfd/pw"
BATCH_API = "https://studyuk.cfd/pw/api.php"

DOWNLOADS_DIR = Path("/tmp/course_downloads")
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
THUMBS_DIR = Path("/tmp/course_thumbs")
THUMBS_DIR.mkdir(parents=True, exist_ok=True)

FFMPEG = "ffmpeg"

# Group watermark
WATERMARK = "extracted by @ZCYT_2026"

CLIENT_ID = os.getenv("CLIENT_ID", "5eb393ee95fab7468a79d189")
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "")
auth_headers = {}
if CLIENT_ID and AUTH_TOKEN:
    auth_headers = {"client-id": CLIENT_ID, "Authorization": AUTH_TOKEN}
elif CLIENT_ID:
    auth_headers = {"client-id": CLIENT_ID}

PENDING_LOGIN = {}

# ── DEFAULT DB DATA ────────────────────────────────────────────
DEFAULT_DB = {"batches": {}, "videos": {}, "downloaded": {}, "auth_token": ""}


# ── SAFE FILE HELPERS ──────────────────────────────────────────
def safe_read_json(path, default):
    """Read a JSON file safely. Returns `default` on any error."""
    try:
        p = Path(path)
        if p.exists() and p.stat().st_size > 0:
            raw = p.read_text(encoding="utf-8")
            return json.loads(raw)
    except (json.JSONDecodeError, OSError, PermissionError) as e:
        print(f"Warning: failed to read {path}, using defaults: {e}")
    return default


def safe_write_json(path, data):
    """Write a JSON file, creating parent dirs. Returns True on success."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return True
    except OSError as e:
        print(f"Warning: failed to write {path}: {e}")
        return False


def safe_exists(path):
    """Check if a path exists (handles race conditions on ephemeral fs)."""
    try:
        return Path(path).exists()
    except OSError:
        return False


def safe_stat_size(path):
    """Return file size in bytes, or 0 if the file doesn't exist / can't be read."""
    try:
        p = Path(path)
        if p.exists():
            return p.stat().st_size
    except OSError:
        pass
    return 0


# ── DATABASE ───────────────────────────────────────────────────
class CourseDB:
    def __init__(self, path="course_db.json"):
        self.path = Path(path)
        # Ensure the directory exists immediately (Railway ephemeral fs)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self):
        return safe_read_json(self.path, DEFAULT_DB)

    def save(self):
        safe_write_json(self.path, self.data)

    def is_downloaded(self, video_id):
        return video_id in self.data.get("downloaded", {})

    def mark_downloaded(self, video_id, file_id, info):
        self.data.setdefault("downloaded", {})[video_id] = {
            "file_id": file_id, "info": info, "time": time.time()
        }
        self.save()

    def save_auth_token(self, token):
        self.data["auth_token"] = token
        self.save()

    def get_auth_token(self):
        return self.data.get("auth_token", "")

    def db_size_kb(self):
        """Return the database file size in KB safely. Returns 0 on missing / error."""
        return safe_stat_size(self.path) // 1024


db = CourseDB()

# ── API CLIENT ─────────────────────────────────────────────────
class StudyPiAPI:
    def __init__(self):
        self.session = None
        self._cid = CLIENT_ID
        self._token = AUTH_TOKEN
        self.headers = dict(auth_headers)
        # Check saved token
        saved = db.get_auth_token()
        if saved and not self._token:
            self._token = f"Bearer {saved}"
            self.headers = {"client-id": self._cid, "Authorization": self._token}

    async def ensure_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"User-Agent": "Mozilla/5.0 (Android 14; Mobile)"}
            )

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    def set_auth(self, client_id, token):
        self._cid = client_id
        self._token = token
        self.headers = {"client-id": client_id, "Authorization": token}
        # Also update module vars
        global AUTH_TOKEN, auth_headers
        AUTH_TOKEN = token
        auth_headers = dict(self.headers)

    def debug_headers(self):
        return {k: v[:30] + "..." for k, v in self.headers.items()}

    async def list_batches(self, page=1):
        await self.ensure_session()
        async with self.session.get(f"{BATCH_API}?page={page}") as resp:
            if resp.status == 200:
                return (await resp.json()).get("batches", [])
        return []

    async def get_batch_subjects(self, batch_id):
        await self.ensure_session()
        url = f"{API_BASE}/app.php?batch_id={batch_id}"
        h = {**self.headers} if self.headers else {}
        if not h.get("client-id"):
            h["client-id"] = self._cid
        async with self.session.get(url, headers=h) as resp:
            if resp.status == 200:
                return await resp.json()
        return None

    async def get_contents(self, batch_id, subject_id):
        await self.ensure_session()
        url = f"{API_BASE}/contents.php"
        params = {"batch_id": batch_id, "subject_id": subject_id, "contentType": "LECTURES"}
        h = {**self.headers} if self.headers else {}
        if not h.get("client-id"):
            h["client-id"] = self._cid
        async with self.session.get(url, params=params, headers=h) as resp:
            if resp.status == 200:
                try:
                    data = await resp.json()
                except:
                    return None
                if isinstance(data, list):
                    return data
                for key in ("lectures", "data", "topics", "content", "items", "videos", "results"):
                    val = data.get(key)
                    if isinstance(val, list):
                        return val
                    if isinstance(val, dict):
                        for sub_key in ("lectures", "topics", "content", "items", "videos", "list", "data", "results"):
                            sub_val = val.get(sub_key)
                            if isinstance(sub_val, list):
                                return sub_val
                print(f"get_contents raw keys: {list(data.keys())}")
                for k, v in data.items():
                    print(f"  {k}: {type(v).__name__}")
            elif resp.status == 401:
                print("Auth 401 on contents.php")
        return None

    async def get_video_url(self, batch_id, child_id):
        await self.ensure_session()
        url = f"{API_BASE}/api/get-video-url"
        params = {"batch_id": batch_id, "childId": child_id}
        h = {**self.headers} if self.headers else {}
        if not h.get("client-id"):
            h["client-id"] = self._cid
        async with self.session.get(url, params=params, headers=h) as resp:
            if resp.status == 200:
                return await resp.json()
        return None

    async def send_otp(self, phone):
        await self.ensure_session()
        async with self.session.post(f"{API_BASE}/login/get-otp",
            json={"phone_number": phone}) as resp:
            return resp.status, await resp.json() if resp.content_type == 'application/json' else await resp.text()

    async def verify_otp(self, phone, otp):
        await self.ensure_session()
        async with self.session.post(f"{API_BASE}/login/verify-otp",
            json={"phone_number": phone, "otp": otp}) as resp:
            return resp.status, await resp.json() if resp.content_type == 'application/json' else await resp.text()


api = StudyPiAPI()
bot = TelegramClient('downloader_bot', API_ID, API_HASH)


# ── HELPERS ────────────────────────────────────────────────────
async def extract_thumbnail(video_path, thumb_path):
    """Extract the ORIGINAL first frame as thumbnail using ffmpeg"""
    thumb_path = Path(thumb_path) if not isinstance(thumb_path, Path) else thumb_path
    thumb_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [FFMPEG, "-y", "-i", str(video_path),
           "-frames:v", "1", "-q:v", "2", str(thumb_path)]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()
    if safe_stat_size(thumb_path) > 500:
        return str(thumb_path)
    return None


async def download_video(session, url, out_path, retries=3):
    for attempt in range(retries):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=3600)) as resp:
                if resp.status == 200:
                    # Ensure parent directory exists (Railway ephemeral fs may reset)
                    out_path = Path(out_path) if not isinstance(out_path, Path) else out_path
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    with open(out_path, 'wb') as f:
                        async for chunk in resp.content.iter_chunked(1024*1024):
                            f.write(chunk)
                    if safe_stat_size(out_path) > 1000:
                        return True
                    out_path.unlink(missing_ok=True)
        except Exception as e:
            print(f"DL attempt {attempt+1} failed: {e}")
            await asyncio.sleep(2)
    return False


# ── COMMANDS ───────────────────────────────────────────────────
@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    await event.reply(
        "**Course Downloader Bot v2.0**\n\n"
        "I download ALL videos from any Study Pi batch!\n\n"
        "**Commands:**\n"
        "/list - List all 448 batches\n"
        "/batch BATCH_ID - Download all videos from a batch\n"
        "/login PHONE - Login via OTP (get auth token)\n"
        "/code 123456 - Enter OTP code received via SMS\n"
        "/auth client_id:token - Set credentials manually\n"
        "/status - Show bot status\n\n"
        "**Usage:**\n"
        "1. `/login 98XXXXXXXX` or `/auth client_id:token`\n"
        "2. `/batch BATCH_ID`\n"
        "3. Captions: `Index - N() Topic - (name) Batch - (name) extracted by @ZCYT_2026`"
    )


@bot.on(events.NewMessage(pattern='/list'))
async def list_batches(event):
    msg = await event.reply("Fetching batches...")
    batches = await api.list_batches()
    if not batches:
        await msg.edit("Failed to fetch batches.")
        return
    text = "**Available Batches (448 total):**\n\n"
    for i, b in enumerate(batches[:40], 1):
        name = b.get('name', 'Unknown')
        bid = b.get('_id', '')
        price = b.get('batchPrice', 0)
        lang = b.get('language', '')
        badge = "$" if price > 0 else "F"
        text += f"{i}. [{badge}] **{name}**\n   `{bid}` | {lang}\n"
    text += f"\n... {len(batches)-40} more\n\nUse `/batch FULL_BATCH_ID`\nExample: `/batch 622309c888d7210011887151`"
    await msg.edit(text)


@bot.on(events.NewMessage(pattern='/login ?(.*)'))
async def login_handler(event):
    phone = event.pattern_match.group(1).strip()
    if not phone:
        await event.reply("Send phone: `/login 9876543210`")
        return
    phone = phone.strip().replace("+", "").replace(" ", "").replace("-", "")
    if len(phone) > 10:
        phone = phone[-10:]
    if len(phone) != 10 or not phone.isdigit():
        await event.reply("Invalid number. Send 10 digits: `/login 9876543210`")
        return
    pd = "+91" + phone
    msg = await event.reply(f"Sending OTP to `{pd}`...")
    status, resp = await api.send_otp(phone)
    if status == 200 and isinstance(resp, dict) and resp.get("success"):
        PENDING_LOGIN[event.sender_id] = {"phone": phone, "step": "otp"}
        await msg.edit(f"OTP sent to `{pd}`\n\nEnter code: `/code 123456`")
    else:
        err = resp.get("message", str(resp)) if isinstance(resp, dict) else str(resp)
        await msg.edit(f"OTP failed: {err[:200]}")


@bot.on(events.NewMessage(pattern='/code ?(.*)'))
async def code_handler(event):
    otp = event.pattern_match.group(1).strip()
    if not otp or not otp.isdigit():
        await event.reply("Format: `/code 123456`")
        return
    uid = event.sender_id
    if uid not in PENDING_LOGIN or PENDING_LOGIN[uid].get("step") != "otp":
        await event.reply("No pending OTP. Use `/login PHONE` first.")
        return
    phone = PENDING_LOGIN[uid]["phone"]
    msg = await event.reply("Verifying OTP...")
    status, resp = await api.verify_otp(phone, otp)

    if status == 200 and isinstance(resp, dict):
        token = resp.get("token", resp.get("access_token", resp.get("data", "")))
        if isinstance(token, dict):
            token = token.get("token", token.get("access_token", ""))

        if token:
            api.set_auth(CLIENT_ID, f"Bearer {token}")
            db.save_auth_token(token)
            await msg.edit("**Login successful!** Token saved. Use `/batch BATCH_ID` now.")
            PENDING_LOGIN.pop(uid, None)
        else:
            await msg.edit(f"Logged in but no token found. Response: {json.dumps(resp)[:200]}")
    else:
        err = resp.get("message", str(resp)) if isinstance(resp, dict) else str(resp)
        await msg.edit(f"OTP failed: {err[:200]}")


@bot.on(events.NewMessage(pattern='/batch (.+)'))
async def download_batch(event):
    batch_id = event.pattern_match.group(1).strip()
    msg = await event.reply("Looking up batch...")

    batches = await api.list_batches()
    batch_info = next((b for b in batches if b['_id'] == batch_id), None)
    if not batch_info:
        await msg.edit("Batch not found. Use `/list` to see IDs.")
        return

    batch_name = batch_info.get('name', 'Unknown')
    await msg.edit(f"**{batch_name}**\nGetting subjects...")

    subjects_data = await api.get_batch_subjects(batch_id)
    if not subjects_data:
        await msg.edit(
            "**Auth required!**\n\n"
            "1. `/login 98XXXXXXXX` - OTP login\n"
            "2. `/auth client_id:token` - Manual")
        return

    subjects = subjects_data.get("data", subjects_data)
    if isinstance(subjects, dict):
        subjects = subjects.get("subjects", [subjects])

    if not subjects:
        await msg.edit("No subjects found.")
        return

    text = f"**{batch_name}**\n\nSubjects:\n"
    for s in subjects:
        n = s.get('subject', s.get('name', '?'))
        c = s.get('lectureCount', '?')
        text += f"  {n} ({c})\n"
    text += "\nStarting download..."
    await msg.edit(text)

    video_index = [0]
    async with aiohttp.ClientSession() as session:
        for subject in subjects:
            sub_name = subject.get('subject', subject.get('name', 'Unknown'))
            sub_id = subject.get('subjectId', subject.get('id', ''))
            if not sub_id:
                continue

            contents = await api.get_contents(batch_id, sub_id)
            if not contents or not isinstance(contents, list):
                await event.reply(f"**{sub_name}**: No lectures (auth valid? check Railway logs)")
                continue

            await event.reply(f"**{sub_name}**: {len(contents)} lectures found")

            for video in contents:
                video_index[0] += 1
                idx = video_index[0]
                vid = video.get('_id', video.get('id', video.get('childId', f'v_{idx}')))
                vname = video.get('topic', video.get('name', video.get('title', f'Lecture {idx}')))
                vurl = video.get('video', video.get('url', video.get('videoUrl', video.get('lectureUrl', video.get('contentUrl', video.get('link', video.get('src', '')))))))
                dur = int(video.get('duration', video.get('durationInSeconds', 0)))

                if isinstance(vurl, dict):
                    vurl = vurl.get('url', vurl.get('link', ''))

                if not vurl or db.is_downloaded(vid):
                    if not vurl:
                        print(f"No URL for vid keys={list(video.keys())}")
                    continue

                out_path = DOWNLOADS_DIR / f"{vid}.mp4"
                thumb_path = THUMBS_DIR / f"{vid}.jpg"

                await msg.edit(f"**{idx}** `{vname[:40]}`\nDownloading...")
                ok = await download_video(session, vurl, out_path)
                if not ok:
                    await msg.edit(f"**{idx}** `{vname[:40]}`\nFailed")
                    continue

                await msg.edit(f"**{idx}** `{vname[:40]}`\nThumbnail...")
                thumb = await extract_thumbnail(out_path, thumb_path)

                caption = f"Index - {idx}()\nTopic - ({vname})\nBatch - ({batch_name})\n{WATERMARK}"
                await msg.edit(f"**{idx}** `{vname[:40]}`\nUploading...")

                try:
                    attrs = [DocumentAttributeVideo(
                        duration=dur or 0, w=video.get('width', 640),
                        h=video.get('height', 480), supports_streaming=True)]
                    kw = {"caption": caption, "attributes": attrs}
                    if thumb:
                        kw["thumb"] = thumb
                    result = await bot.send_file(CHANNEL_ID or event.chat_id, str(out_path), **kw)
                    db.mark_downloaded(vid, result.id, {"name": vname, "batch": batch_name, "index": idx})
                    out_path.unlink(missing_ok=True)
                    thumb_path.unlink(missing_ok=True)
                except Exception as e:
                    await msg.edit(f"**{idx}** Upload error: {str(e)[:60]}")

    total = len([k for k, v in db.data.get("downloaded", {}).items()
                 if v.get("info", {}).get("batch") == batch_name])
    await msg.edit(f"**Complete!**\nBatch: {batch_name}\nUploaded: {total} videos")


@bot.on(events.NewMessage(pattern='/auth (.+)'))
async def set_auth(event):
    s = event.pattern_match.group(1).strip()
    if ':' not in s:
        await event.reply("Format: `/auth client_id:token`")
        return
    cid, token = s.split(':', 1)
    cid = cid.strip()
    token = token.strip()
    if not token.startswith("Bearer ") and not token.startswith("bearer "):
        token = "Bearer " + token
    api.set_auth(cid, token)
    db.save_auth_token(token[7:])  # Save without "Bearer "
    await event.reply("**Auth set!** Use `/batch BATCH_ID` now.")


@bot.on(events.NewMessage(pattern='/status'))
async def status_handler(event):
    stats = db.data.get("downloaded", {})
    h = api.debug_headers()
    await event.reply(
        f"**Status**\n\n"
        f"Headers: {h}\n"
        f"Videos: {len(stats)}\n"
        f"DB: {db.db_size_kb()}KB"
    )


# ── MAIN ───────────────────────────────────────────────────────
async def main():
    if not all([BOT_TOKEN, API_ID, API_HASH]):
        print("Missing env: BOT_TOKEN, API_ID, API_HASH")
        sys.exit(1)

    # ── Startup validation: ensure all required dirs/files exist ──
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)
    # Ensure the database directory exists and DB file has valid data
    db.path.parent.mkdir(parents=True, exist_ok=True)
    if not safe_exists(db.path):
        safe_write_json(db.path, DEFAULT_DB)
        print(f"Created database: {db.path}")
    # Session file directory
    Path("downloader_bot.session").parent.mkdir(parents=True, exist_ok=True)

    print(f"Course Downloader Bot v2.0")
    print(f"Auth headers: {api.debug_headers()}")
    print(f"DB: {db.db_size_kb()}KB at {db.path}")

    await bot.start(bot_token=BOT_TOKEN)
    me = await bot.get_me()
    print(f"Online as @{me.username}")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
