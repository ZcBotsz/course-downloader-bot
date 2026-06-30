#!/usr/bin/env python3
"""
Course Downloader Bot v2.0
Batch downloads ALL videos from Study Pi, uploads to Telegram with proper captions & thumbnails
"""

import os, sys, json, asyncio, time, re, hashlib, subprocess
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

# Temp file for pending OTP login
PENDING_LOGIN = {}


class CourseDB:
    def __init__(self, path="course_db.json"):
        self.path = Path(path)
        self.data = self._load()

    def _load(self):
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"batches": {}, "videos": {}, "downloaded": {}, "auth_token": ""}

    def save(self):
        self.path.write_text(json.dumps(self.data, indent=2))

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


db = CourseDB()

# Restore saved auth token
saved_token = db.get_auth_token()
if saved_token and not AUTH_TOKEN:
    AUTH_TOKEN = saved_token
    auth_headers = {"client-id": CLIENT_ID, "Authorization": f"Bearer {saved_token}"}


class StudyPiAPI:
    def __init__(self):
        self.session = None
        self.headers = dict(auth_headers)

    async def ensure_session(self):
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                headers={"User-Agent": "Mozilla/5.0 (Android 14; Mobile)"}
            )

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    def set_auth(self, client_id, token):
        self.headers = {"client-id": client_id, "Authorization": token}

    async def list_batches(self, page=1):
        await self.ensure_session()
        async with self.session.get(f"{BATCH_API}?page={page}") as resp:
            if resp.status == 200:
                return (await resp.json()).get("batches", [])
        return []

    async def get_batch_subjects(self, batch_id):
        await self.ensure_session()
        url = f"{API_BASE}/app.php?batch_id={batch_id}"
        async with self.session.get(url, headers=self.headers) as resp:
            if resp.status == 200:
                return await resp.json()
        return None

    async def get_contents(self, batch_id, subject_id):
        await self.ensure_session()
        url = f"{API_BASE}/contents.php"
        params = {"batch_id": batch_id, "subject_id": subject_id, "contentType": "LECTURES"}
        async with self.session.get(url, params=params, headers=self.headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("data", data.get("lectures", data))
        return None

    async def get_video_url(self, batch_id, child_id):
        await self.ensure_session()
        url = f"{API_BASE}/api/get-video-url"
        params = {"batch_id": batch_id, "childId": child_id}
        async with self.session.get(url, params=params, headers=self.headers) as resp:
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


async def extract_thumbnail(video_path, thumb_path):
    """Extract the ORIGINAL first frame as thumbnail using ffmpeg"""
    cmd = [FFMPEG, "-y", "-i", str(video_path),
           "-frames:v", "1", "-q:v", "2", str(thumb_path)]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()
    if thumb_path.exists() and thumb_path.stat().st_size > 500:
        return str(thumb_path)
    return None


async def download_video(session, url, out_path, retries=3):
    for attempt in range(retries):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=3600)) as resp:
                if resp.status == 200:
                    with open(out_path, 'wb') as f:
                        async for chunk in resp.content.iter_chunked(1024*1024):
                            f.write(chunk)
                    if out_path.stat().st_size > 1000:
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
        "🎓 **Course Downloader Bot v2.0**\n\n"
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
        "3. Videos get uploaded with captions: `Index - 1() Topic - (name) Batch - (name) extracted by @ZCYT_2026`"
    )


@bot.on(events.NewMessage(pattern='/list'))
async def list_batches(event):
    msg = await event.reply("📚 Fetching batches...")
    batches = await api.list_batches()
    if not batches:
        await msg.edit("❌ Failed to fetch batches.")
        return

    text = "**📚 Available Batches (448 total):**\n\n"
    # Show in chunks to stay under Telegram message limit
    for i, b in enumerate(batches[:40], 1):
        name = b.get('name', 'Unknown')
        bid = b.get('_id', '')
        price = b.get('batchPrice', 0)
        lang = b.get('language', '')
        badge = "💰" if price > 0 else "🆓"
        text += f"{i}. {badge} **{name}**\n"
        text += f"   `{bid}` | {lang}\n"
    text += f"\n... {len(batches)-40} more\n\n"
    text += "To download: `/batch BATCH_ID`\n"
    text += "Copy the FULL ID above and paste it.\n\n"
    text += "Example: `/batch 622309c888d7210011887151`"

    await msg.edit(text)


@bot.on(events.NewMessage(pattern='/login ?(.*)'))
async def login_handler(event):
    phone = event.pattern_match.group(1).strip()
    if not phone:
        await event.reply(
            "📱 **Send phone number:**\n"
            "`/login 98XXXXXXXX`\n\n"
            "Make sure it's the same number you use in Study Pi app."
        )
        return

    # Validate Indian phone number
    phone = phone.strip()
    # Remove any + or spaces
    phone = phone.replace("+", "").replace(" ", "").replace("-", "")
    # Take last 10 digits if there's a country code
    if len(phone) > 10:
        phone = phone[-10:]
    if len(phone) != 10 or not phone.isdigit():
        await event.reply(
            "❌ **Invalid number.** Send your 10-digit phone number:\n"
            "`/login 9876543210`"
        )
        return
    phone_display = "+91" + phone

    msg = await event.reply(f"📱 Sending OTP to `{phone_display}`...")

    status, resp = await api.send_otp(phone)
    if status == 200 and isinstance(resp, dict) and resp.get("success"):
        PENDING_LOGIN[event.sender_id] = {"phone": phone, "phone_display": phone_display, "step": "otp"}
        await msg.edit(
            f"✅ **OTP sent to** `{phone_display}`\n\n"
            f"Enter the OTP code:\n"
            f"`/code 123456`"
        )
    else:
        error = resp.get("message", str(resp)) if isinstance(resp, dict) else str(resp)
        await msg.edit(f"❌ **OTP failed:** {error[:200]}")


@bot.on(events.NewMessage(pattern='/code ?(.*)'))
async def code_handler(event):
    otp = event.pattern_match.group(1).strip()
    if not otp or not otp.isdigit():
        await event.reply("❌ Format: `/code 123456` (the OTP you received via SMS)")
        return

    user_id = event.sender_id
    if user_id not in PENDING_LOGIN or PENDING_LOGIN[user_id].get("step") != "otp":
        await event.reply("❌ No pending OTP. Use `/login PHONE` first.")
        return

    phone = PENDING_LOGIN[user_id]["phone"]
    msg = await event.reply("🔑 Verifying OTP...")

    status, resp = await api.verify_otp(phone, otp)
    if status == 200 and isinstance(resp, dict):
        # Extract the token from response
        token = resp.get("token", resp.get("access_token", resp.get("data", "")))
        if isinstance(token, dict):
            token = token.get("token", token.get("access_token", ""))

        if token:
            global AUTH_TOKEN, auth_headers
            AUTH_TOKEN = f"Bearer {token}"
            auth_headers = {"client-id": CLIENT_ID, "Authorization": AUTH_TOKEN}
            api.set_auth(CLIENT_ID, AUTH_TOKEN)
            db.save_auth_token(token)

            await msg.edit(
                f"✅ **Login successful!**\n\n"
                f"Auth token saved permanently.\n"
                f"Now use `/batch BATCH_ID` to download videos!"
            )
            PENDING_LOGIN.pop(user_id, None)
        else:
            await msg.edit(f"⚠️ **Logged in but no token found.** Response: {json.dumps(resp)[:200]}")
    else:
        error = resp.get("message", str(resp)) if isinstance(resp, dict) else str(resp)
        await msg.edit(f"❌ **OTP verification failed:** {error[:200]}")


@bot.on(events.NewMessage(pattern='/batch (.+)'))
async def download_batch(event):
    batch_id = event.pattern_match.group(1).strip()
    msg = await event.reply(f"🔍 Looking up batch...")

    batches = await api.list_batches()
    batch_info = next((b for b in batches if b['_id'] == batch_id), None)
    if not batch_info:
        await msg.edit(
            "❌ Batch not found. Use `/list` to see all batches.\n"
            "Make sure you copy the **full batch ID** (24 characters)."
        )
        return

    batch_name = batch_info.get('name', 'Unknown')
    batch_lang = batch_info.get('language', '')
    total_videos = 0

    await msg.edit(f"📥 **{batch_name}**\nPhase 1: Getting subjects...")

    subjects_data = await api.get_batch_subjects(batch_id)
    if not subjects_data:
        await msg.edit(
            "❌ **Auth required!**\n\n"
            "Options:\n"
            "1. `/login 98XXXXXXXX` - OTP login\n"
            "2. `/auth client_id:token` - Manual auth\n"
            "3. Enter your key in Study Pi app, then read the token"
        )
        return

    subjects = subjects_data.get("data", subjects_data)
    if isinstance(subjects, dict):
        subjects = subjects.get("subjects", [subjects])

    if not subjects:
        await msg.edit(f"❌ No subjects found for this batch.")
        return

    text = f"📚 **{batch_name}**\n\nFound subjects:\n"
    for s in subjects:
        name = s.get('subject', s.get('name', '?'))
        count = s.get('lectureCount', '?')
        text += f"  📖 {name} ({count} lectures)\n"
    text += "\n🚀 Starting download... one by one..."
    await msg.edit(text)

    video_index = [0]  # mutable counter
    async with aiohttp.ClientSession() as session:
        for subject in subjects:
            sub_name = subject.get('subject', subject.get('name', 'Unknown'))
            sub_id = subject.get('subjectId', subject.get('id', ''))
            if not sub_id:
                continue

            contents = await api.get_contents(batch_id, sub_id)
            if not contents:
                # Try direct video URL method
                contents = await api.get_video_url(batch_id, sub_id)
                if contents and isinstance(contents, dict):
                    contents = [contents]

            if not contents or not isinstance(contents, list):
                continue

            for video in contents:
                video_index[0] += 1
                idx = video_index[0]
                video_id = video.get('_id', video.get('id', video.get('childId', f'v_{idx}')))
                video_name = video.get('topic', video.get('name', video.get('title', f'Lecture {idx}')))
                video_url = video.get('video', video.get('url', video.get('videoUrl', '')))
                duration = int(video.get('duration', video.get('durationInSeconds', 0)))

                if isinstance(video_url, dict):
                    video_url = video_url.get('url', video_url.get('link', ''))

                if not video_url and isinstance(video, str):
                    video_url = video

                if not video_url or db.is_downloaded(video_id):
                    continue

                out_path = DOWNLOADS_DIR / f"{video_id}.mp4"
                thumb_path = THUMBS_DIR / f"{video_id}.jpg"

                await msg.edit(f"**{idx}** - `{video_name[:40]}`\n⬇️ Downloading...")

                success = await download_video(session, video_url, out_path)
                if not success:
                    await msg.edit(f"**{idx}** - `{video_name[:40]}`\n❌ Download failed")
                    continue

                # Extract original thumbnail
                await msg.edit(f"**{idx}** - `{video_name[:40]}`\n🎬 Extracting thumbnail...")
                thumb = await extract_thumbnail(out_path, thumb_path)

                # Build caption: Index - 1() Topic - (name) Batch - (name) extracted by @ZCYT_2026
                caption = (
                    f"Index - {idx}()\n"
                    f"Topic - ({video_name})\n"
                    f"Batch - ({batch_name})\n"
                    f"{WATERMARK}"
                )

                await msg.edit(f"**{idx}** - `{video_name[:40]}`\n📤 Uploading...")

                try:
                    attrs = [DocumentAttributeVideo(
                        duration=duration or 0,
                        w=video.get('width', 640),
                        h=video.get('height', 480),
                        supports_streaming=True,
                    )]

                    if thumb:
                        result = await bot.send_file(
                            CHANNEL_ID or event.chat_id,
                            str(out_path),
                            caption=caption,
                            thumb=str(thumb),
                            attributes=attrs,
                        )
                    else:
                        result = await bot.send_file(
                            CHANNEL_ID or event.chat_id,
                            str(out_path),
                            caption=caption,
                            attributes=attrs,
                        )

                    db.mark_downloaded(video_id, result.id, {
                        "name": video_name, "batch": batch_name, "index": idx
                    })

                    # Cleanup
                    out_path.unlink(missing_ok=True)
                    thumb_path.unlink(missing_ok=True)

                except Exception as e:
                    await msg.edit(f"**{idx}** - `{video_name[:40]}`\n⚠️ Upload error: {str(e)[:60]}")

    # Final summary
    downloaded_count = len([k for k, v in db.data.get("downloaded", {}).items()
                           if v.get("info", {}).get("batch") == batch_name])
    await msg.edit(
        f"✅ **Complete!**\n"
        f"📚 {batch_name}\n"
        f"📹 Total uploaded: {downloaded_count} videos\n"
        f"🏷️ Caption format: Index - N() Topic - (name) Batch - (name) {WATERMARK}"
    )


@bot.on(events.NewMessage(pattern='/auth (.+)'))
async def set_auth(event):
    global auth_headers, CLIENT_ID, AUTH_TOKEN
    auth_str = event.pattern_match.group(1).strip()
    if ':' in auth_str:
        parts = auth_str.split(':', 1)
        CLIENT_ID = parts[0].strip()
        token = parts[1].strip()
        if not token.startswith("Bearer ") and not token.startswith("bearer "):
            AUTH_TOKEN = f"Bearer {token}"
        else:
            AUTH_TOKEN = token
        auth_headers = {"client-id": CLIENT_ID, "Authorization": AUTH_TOKEN}
        api.set_auth(CLIENT_ID, AUTH_TOKEN)
        db.save_auth_token(token if not token.startswith("Bearer ") else token[7:])
        await event.reply("✅ **Auth set!** Use `/batch BATCH_ID` now.")
    else:
        await event.reply("Format: `/auth client_id:token`")


@bot.on(events.NewMessage(pattern='/status'))
async def status_handler(event):
    stats = db.data.get("downloaded", {})
    auth_status = "✅" if (AUTH_TOKEN or db.get_auth_token()) else "❌"
    token_short = (db.get_auth_token() or AUTH_TOKEN or "")[:20]
    await event.reply(
        f"📊 **Bot Status**\n\n"
        f"🔑 Auth: {auth_status}\n"
        f"   Token: `{token_short}...`\n"
        f"📹 Videos downloaded: {len(stats)}\n"
        f"💾 DB: {db.path.stat().st_size // 1024}KB"
    )


async def main():
    if not all([BOT_TOKEN, API_ID, API_HASH]):
        print("Missing: BOT_TOKEN, API_ID, API_HASH")
        sys.exit(1)

    print("🎓 Course Downloader Bot v2.0")
    print(f"🔑 Auth: {'Yes' if AUTH_TOKEN or db.get_auth_token() else 'No'}")

    await bot.start(bot_token=BOT_TOKEN)
    me = await bot.get_me()
    print(f"✅ Online as @{me.username}")

    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
