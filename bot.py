#!/usr/bin/env python3
"""
Course Downloader Bot v1.0
Batch downloads ALL videos from a Study Pi course and uploads to Telegram
Deploy on Railway
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
MAX_CONCURRENT = 3

# Auth headers (from env or /auth command)
# Client-ID found from DEX analysis: 5eb393ee95fab7468a79d189
CLIENT_ID = os.getenv("CLIENT_ID", "5eb393ee95fab7468a79d189")
# Authorization/Bearer token — needs to be obtained from key.php or installed app
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "")
auth_headers = {}
if CLIENT_ID and AUTH_TOKEN:
    auth_headers = {"client-id": CLIENT_ID, "Authorization": AUTH_TOKEN}
elif CLIENT_ID:
    auth_headers = {"client-id": CLIENT_ID}


class CourseDB:
    def __init__(self, path="course_db.json"):
        self.path = Path(path)
        self.data = self._load()

    def _load(self):
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"batches": {}, "videos": {}, "downloaded": {}}

    def save(self):
        self.path.write_text(json.dumps(self.data, indent=2))

    def is_downloaded(self, video_id):
        return video_id in self.data.get("downloaded", {})

    def mark_downloaded(self, video_id, file_id, info):
        self.data.setdefault("downloaded", {})[video_id] = {
            "file_id": file_id,
            "info": info,
            "time": time.time()
        }
        self.save()


db = CourseDB()


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
        url = f"{BATCH_API}?page={page}"
        async with self.session.get(url) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("batches", [])
        return []

    async def get_batch_subjects(self, batch_id):
        await self.ensure_session()
        url = f"{API_BASE}/app.php?batch_id={batch_id}"
        async with self.session.get(url, headers=self.headers) as resp:
            if resp.status == 200:
                return await resp.json()
        return None

    async def get_video_url(self, batch_id, child_id):
        await self.ensure_session()
        url = f"{API_BASE}/api/get-video-url?batch_id={batch_id}&childId={child_id}"
        async with self.session.get(url, headers=self.headers) as resp:
            if resp.status == 200:
                return await resp.json()
        return None

    async def verify_key(self, key):
        await self.ensure_session()
        url = f"{API_BASE}/key.php/check?key={key}"
        async with self.session.get(url) as resp:
            text = await resp.text()
            try:
                return json.loads(text)
            except:
                return {"success": False, "error": text[:200]}


async def download_video(session, url, out_path, retries=3):
    for attempt in range(retries):
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=3600)) as resp:
                if resp.status == 200:
                    total = int(resp.headers.get('Content-Length', 0))
                    downloaded = 0
                    chunk_size = 1024 * 1024
                    with open(out_path, 'wb') as f:
                        async for chunk in resp.content.iter_chunked(chunk_size):
                            f.write(chunk)
                            downloaded += len(chunk)
                    if os.path.getsize(out_path) > 1000:
                        return True
                    os.remove(out_path)
        except Exception as e:
            print(f"Attempt {attempt+1} failed: {e}")
            await asyncio.sleep(2)
    return False


# ── TELEGRAM BOT ───────────────────────────────────────────────
bot = TelegramClient('downloader_bot', API_ID, API_HASH)
api = StudyPiAPI()


@bot.on(events.NewMessage(pattern='/start'))
async def start_handler(event):
    await event.reply(
        "**Course Downloader Bot**\n\n"
        "I download ALL videos from any Study Pi batch!\n\n"
        "**Commands:**\n"
        "/list - List all available batches\n"
        "/batch BATCH_ID - Download all videos from a batch\n"
        "/key YOUR_KEY - Set activation key\n"
        "/auth client_id:token - Set credentials directly\n"
        "/status - Show status\n\n"
        "First: `/key YOUR_KEY` or `/auth client_id:token`"
    )


@bot.on(events.NewMessage(pattern='/key (.+)'))
async def set_key(event):
    key = event.pattern_match.group(1).strip()
    msg = await event.reply("Verifying key...")
    result = await api.verify_key(key)
    if result.get("success"):
        await msg.edit("**Key verified!** Use `/batch BATCH_ID` to start.")
    else:
        await msg.edit("**Key rejected.** Use `/auth client_id:token` instead.")


@bot.on(events.NewMessage(pattern='/list'))
async def list_batches(event):
    msg = await event.reply("Fetching batches...")
    batches = await api.list_batches()
    if not batches:
        await msg.edit("Failed to fetch batches.")
        return
    text = "**Available Batches (448 total):**\n\n"
    for i, b in enumerate(batches[:30], 1):
        name = b.get('name', 'Unknown')
        bid = b.get('_id', '')
        price = b.get('batchPrice', 0)
        lang = b.get('language', '')
        badge = "$" if price > 0 else "F"
        text += f"{i}. [{badge}] **{name}**\n   `{bid[:16]}...` | {lang}\n"
    text += f"\n... {len(batches)-30} more\nUse `/batch FULL_BATCH_ID`"
    await msg.edit(text)


@bot.on(events.NewMessage(pattern='/batch (.+)'))
async def download_batch(event):
    batch_id = event.pattern_match.group(1).strip()
    msg = await event.reply(f"Looking up batch...")

    batches = await api.list_batches()
    batch_info = next((b for b in batches if b['_id'] == batch_id), None)
    if not batch_info:
        await msg.edit("Batch not found. Use `/list` to see IDs.")
        return

    batch_name = batch_info.get('name', 'Unknown')
    await msg.edit(f"**Downloading: {batch_name}**\n\nPhase 1: Getting subjects...")

    subjects = await api.get_batch_subjects(batch_id)
    if not subjects:
        await msg.edit("Need auth! Use `/key KEY` or `/auth client_id:token`")
        return

    progress = await event.reply(f"Found subjects! Extracting videos...")
    
    async with aiohttp.ClientSession() as session:
        for subject in subjects:
            sub_name = subject.get('name', subject.get('id', 'Unknown'))
            contents = await api.get_video_url(batch_id, subject.get('id', ''))
            if not contents or not isinstance(contents, list):
                continue

            for i, video in enumerate(contents, 1):
                video_id = video.get('id', f'v_{i}')
                video_name = video.get('name', f'Video {i}')
                video_url = video.get('url', '')

                if not video_url or db.is_downloaded(video_id):
                    continue

                out_path = DOWNLOADS_DIR / f"{video_id}.mp4"
                await progress.edit(f"**{i}/{len(contents)}** - `{video_name}`\nDownloading...")

                success = await download_video(session, video_url, out_path)
                if not success:
                    await progress.edit(f"Failed: {video_name}")
                    continue

                await progress.edit(f"Uploading: {video_name}")
                try:
                    result = await bot.send_file(
                        CHANNEL_ID or event.chat_id,
                        str(out_path),
                        caption=f"{batch_name} - {video_name}",
                        attributes=[
                            DocumentAttributeVideo(
                                duration=int(video.get('duration', 0)),
                                w=int(video.get('width', 640)),
                                h=int(video.get('height', 480)),
                                supports_streaming=True,
                            )
                        ],
                    )
                    db.mark_downloaded(video_id, result.id, {
                        "name": video_name, "batch": batch_name
                    })
                    os.remove(out_path)
                    await progress.edit(f"Done: {i}/{len(contents)} - {video_name}")
                except Exception as e:
                    await progress.edit(f"Upload error: {str(e)[:50]}")

    await progress.edit(f"**Complete!** All videos from {batch_name} processed.")


@bot.on(events.NewMessage(pattern='/auth (.+)'))
async def set_auth(event):
    global auth_headers, CLIENT_ID, AUTH_TOKEN
    auth_str = event.pattern_match.group(1).strip()
    if ':' in auth_str:
        parts = auth_str.split(':', 1)
        CLIENT_ID = parts[0].strip()
        AUTH_TOKEN = parts[1].strip()
        auth_headers = {"client-id": CLIENT_ID, "Authorization": AUTH_TOKEN}
        api.set_auth(CLIENT_ID, AUTH_TOKEN)
        await event.reply("**Auth set!** Use `/batch BATCH_ID` now.")
    else:
        await event.reply("Format: `/auth client_id:token`")


@bot.on(events.NewMessage(pattern='/status'))
async def status_handler(event):
    stats = db.data.get("downloaded", {})
    status = "Yes" if auth_headers else "No"
    await event.reply(
        f"**Bot Status**\n\n"
        f"Auth configured: {status}\n"
        f"Videos downloaded: {len(stats)}\n"
        f"DB size: {db.path.stat().st_size // 1024}KB"
    )


async def main():
    if not all([BOT_TOKEN, API_ID, API_HASH]):
        print("Missing: BOT_TOKEN, API_ID, API_HASH")
        sys.exit(1)

    print(f"Course Downloader Bot v1.0")
    print(f"Auth: {'Yes' if auth_headers else 'No'}")

    await bot.start(bot_token=BOT_TOKEN)
    me = await bot.get_me()
    print(f"Online as @{me.username}")

    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
