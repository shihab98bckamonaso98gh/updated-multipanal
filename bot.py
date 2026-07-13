# -*- coding: utf-8 -*-
"""
Flex SMS -> Telegram OTP bot (Railway-ready, lightweight, fast)
- Auto-solves the "What is X + Y = ?" login captcha
- Single fast global poll of the SMSCDRStats ajax endpoint
- Routes each new SMS to the number owner (no overlap)
- Admin: add/remove country buttons, upload unlimited-size number txt files
- Tiny health server on $PORT so Railway keeps it alive
"""

import os
import re
import json
import html
import asyncio
import logging
import tempfile
import datetime as dt
from typing import Optional

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# --------------------------------------------------------------------------- #
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.strip().isdigit()}
SITE_BASE = os.getenv("SITE_BASE", "http://168.119.13.175").rstrip("/")
SITE_USERNAME = os.getenv("SITE_USERNAME", "")
SITE_PASSWORD = os.getenv("SITE_PASSWORD", "")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "1"))
DATA_DIR = os.getenv("DATA_DIR", ".").rstrip("/")
DATA_FILE = os.path.join(DATA_DIR, "data.json")
PORT = int(os.getenv("PORT", "8080"))

LOGIN_URL = f"{SITE_BASE}/ints/login"
SIGNIN_URL = f"{SITE_BASE}/ints/signin"
STATS_URL = f"{SITE_BASE}/ints/agent/SMSCDRStats"
DATA_URL = f"{SITE_BASE}/ints/agent/res/data_smscdr.php"

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
for noisy in ("httpx", "httpcore", "telegram", "apscheduler", "aiohttp.access"):
    logging.getLogger(noisy).setLevel(logging.WARNING)
log = logging.getLogger("otpbot")

os.makedirs(DATA_DIR, exist_ok=True)

# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #
class Store:
    def __init__(self, path):
        self.path = path
        self.lock = asyncio.Lock()
        self.data = {"buttons": [], "numbers": {}, "assignments": {}}
        if os.path.exists(path):
            try:
                self.data = json.load(open(path, encoding="utf-8"))
            except Exception as e:
                log.warning("read %s failed: %s", path, e)
        for k in ("buttons", "numbers", "assignments"):
            self.data.setdefault(k, [] if k == "buttons" else {})

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def buttons(self):
        return self.data["buttons"]

    def get_button(self, bid):
        return next((b for b in self.data["buttons"] if b["id"] == bid), None)

    async def add_button(self, name):
        async with self.lock:
            bid = "b" + str(int(dt.datetime.now().timestamp() * 1000))
            self.data["buttons"].append({"id": bid, "name": name})
            self.data["numbers"].setdefault(bid, [])
            self._save()
            return bid

    async def remove_button(self, bid):
        async with self.lock:
            self.data["buttons"] = [b for b in self.data["buttons"] if b["id"] != bid]
            self.data["numbers"].pop(bid, None)
            self._save()

    async def add_numbers(self, bid, numbers):
        async with self.lock:
            pool = self.data["numbers"].setdefault(bid, [])
            existing = set(pool)
            added = 0
            for n in numbers:
                if n and n not in existing:
                    pool.append(n); existing.add(n); added += 1
            self._save()
            return added

    async def take_number(self, bid, user_id, name):
        async with self.lock:
            pool = self.data["numbers"].get(bid, [])
            if not pool:
                return None
            number = pool.pop(0)
            self.data["assignments"][number] = {"user_id": user_id, "button_id": bid, "button_name": name}
            self._save()
            return number

    async def release_number(self, number):
        async with self.lock:
            self.data["assignments"].pop(number, None)
            self._save()

    def assignment(self, number):
        return self.data["assignments"].get(number)


store = Store(DATA_FILE)

# --------------------------------------------------------------------------- #
# Site client (keep-alive connector, session reuse = fast + reliable)
# --------------------------------------------------------------------------- #
class SmsClient:
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.csstr: Optional[str] = None
        self.lock = asyncio.Lock()
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Referer": LOGIN_URL,
        }

    async def start(self):
        if self.session is None or self.session.closed:
            jar = aiohttp.CookieJar(unsafe=True)  # allow cookies from bare-IP host
            connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300, keepalive_timeout=60)
            self.session = aiohttp.ClientSession(
                headers=self.headers,
                cookie_jar=jar,
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=20),
            )

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    @staticmethod
    def _solve(page):
        m = re.search(r"What is\s*(\d+)\s*([+\-*x])\s*(\d+)", page, re.I)
        if not m:
            raise RuntimeError("captcha question not found on login page")
        a, op, b = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        return str(a - b if op == "-" else a * b if op in "*x" else a + b)

    async def login(self):
        await self.start()
        async with self.lock:
            log.info("Logging into site...")
            self.session.cookie_jar.clear()
            async with self.session.get(LOGIN_URL) as r:
                page = await r.text()
            answer = self._solve(page)
            payload = {"username": SITE_USERNAME, "password": SITE_PASSWORD, "capt": answer}
            async with self.session.post(SIGNIN_URL, data=payload, allow_redirects=True) as r:
                body = await r.text()
                final = str(r.url)
            if "signin" in final or "/login" in final or 'name="password"' in body:
                raise RuntimeError("login failed (bad credentials or captcha)")
            await self._refresh_csstr()
            log.info("Login OK. csstr=%s", self.csstr)

    async def _refresh_csstr(self):
        async with self.session.get(STATS_URL) as r:
            page = await r.text()
        m = re.search(r"csstr=([A-Za-z0-9]+)", page)
        if not m:
            raise RuntimeError("csstr token not found (login likely failed)")
        self.csstr = m.group(1)

    async def ensure(self):
        if self.session is None or self.session.closed or not self.csstr:
            await self.login()

    async def fetch_rows(self):
        await self.ensure()
        today = dt.date.today().strftime("%Y-%m-%d")
        params = {
            "fdate1": f"{today} 00:00:00", "fdate2": f"{today} 23:59:59",
            "frange": "", "fclient": "", "fnum": "", "fcli": "",
            "fgdate": "", "fgmonth": "", "fgrange": "", "fgclient": "",
            "fgnumber": "", "fgcli": "", "fg": "0", "csstr": self.csstr,
            "sEcho": "1", "iDisplayStart": "0", "iDisplayLength": "100",
        }
        try:
            async with self.session.get(
                DATA_URL, params=params, headers={"X-Requested-With": "XMLHttpRequest"}
            ) as r:
                text = await r.text()
        except Exception as e:
            log.warning("fetch error: %s", e)
            await self.login()
            return []

        if not text.lstrip().startswith("{"):
            log.info("Session expired, re-login...")
            await self.login()
            return []
        try:
            payload = json.loads(text)
        except Exception:
            await self.login()
            return []

        rows = payload.get("aaData") or payload.get("data") or []
        out = []
        for row in rows:
            if not isinstance(row, list) or len(row) < 6:
                continue
            date = str(row[0])
            if not re.match(r"^\d{4}-\d{2}-\d{2}", date):
                continue
            out.append({
                "date": date,
                "range": str(row[1]),
                "number": re.sub(r"\D", "", str(row[2])),
                "cli": str(row[3]),
                "client": str(row[4]),
                "sms": html.unescape(re.sub(r"<[^>]+>", "", str(row[5]))),
            })
        return out


client = SmsClient()

# --------------------------------------------------------------------------- #
# Monitor
# --------------------------------------------------------------------------- #
class Monitor:
    def __init__(self):
        self.seen = set()

    @staticmethod
    def _key(r):
        return f"{r['date']}|{r['number']}|{r['sms']}"

    @staticmethod
    def otp(text):
        m = re.search(r"(\d[\d\s\-]{3,}\d)", text)
        return re.sub(r"\D", "", m.group(1)) if m else ""

    async def prime(self):
        try:
            for r in await client.fetch_rows():
                self.seen.add(self._key(r))
        except Exception as e:
            log.warning("prime failed: %s", e)
        log.info("Primed with %d rows", len(self.seen))

    async def loop(self, bot):
        log.info("Monitor started (interval=%.1fs)", POLL_INTERVAL)
        while True:
            try:
                rows = await client.fetch_rows()
                for r in reversed(rows):  # oldest first
                    k = self._key(r)
                    if k in self.seen:
                        continue
                    self.seen.add(k)
                    await self._dispatch(bot, r)
                if len(self.seen) > 5000:
                    self.seen = set(list(self.seen)[-2000:])
            except Exception as e:
                log.warning("tick error: %s", e)
            await asyncio.sleep(POLL_INTERVAL)

    async def _dispatch(self, bot, r):
        a = store.assignment(r["number"])
        if not a:
            return
        otp = self.otp(r["sms"])
        msg = (
            f"📩 {html.escape(a['button_name'])} Message Received!\n\n"
            f"📞 Number : +{html.escape(r['number'])}\n\n"
            f"🔑 OTP Code: <code>{html.escape(otp)}</code>\n\n"
            f"💬 Full Message:\n{html.escape(r['sms'])}"
        )
        try:
            await bot.send_message(chat_id=a["user_id"], text=msg, parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning("send to %s failed: %s", a["user_id"], e)


monitor = Monitor()

# --------------------------------------------------------------------------- #
# Keyboards
# --------------------------------------------------------------------------- #
def main_kb(uid):
    rows = [["Get number"]]
    if uid in ADMIN_IDS:
        rows.append(["Admin panel"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def admin_kb():
    return ReplyKeyboardMarkup([["Add/remove Button"], ["Upload"], ["Back"]], resize_keyboard=True)

def addremove_kb():
    return ReplyKeyboardMarkup([["Add", "Remove"], ["Back"]], resize_keyboard=True)

def inline_for(action):
    btns = [[InlineKeyboardButton(b["name"], callback_data=f"{action}|{b['id']}")] for b in store.buttons()]
    return InlineKeyboardMarkup(btns) if btns else None

def is_admin(uid):
    return uid in ADMIN_IDS

# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
async def start(update, ctx):
    ctx.user_data.clear()
    ctx.user_data["menu"] = "main"
    await update.message.reply_text("Welcome! Choose an option:", reply_markup=main_kb(update.effective_user.id))

async def on_text(update, ctx):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    if ctx.user_data.get("await") == "add_button" and is_admin(uid):
        await store.add_button(text)
        ctx.user_data.pop("await", None)
        await update.message.reply_text(f"✅ Button created: {text}", reply_markup=addremove_kb())
        return

    if text == "Get number":
        kb = inline_for("getnum")
        await update.message.reply_text("Select a country:" if kb else "No numbers available yet.", reply_markup=kb)
        return

    if text == "Admin panel":
        if not is_admin(uid):
            await update.message.reply_text("Not authorized."); return
        ctx.user_data["menu"] = "admin"
        await update.message.reply_text("Admin panel:", reply_markup=admin_kb())
        return

    if text == "Add/remove Button" and is_admin(uid):
        ctx.user_data["menu"] = "addremove"
        await update.message.reply_text("Add or remove:", reply_markup=addremove_kb())
        return

    if text == "Add" and is_admin(uid):
        ctx.user_data["await"] = "add_button"
        await update.message.reply_text("Send the button name (e.g. 🇬🇭 Ghana):")
        return

    if text == "Remove" and is_admin(uid):
        kb = inline_for("del")
        await update.message.reply_text("Tap a button to delete:" if kb else "No buttons to remove.", reply_markup=kb)
        return

    if text == "Upload" and is_admin(uid):
        kb = inline_for("upl")
        await update.message.reply_text("Pick a button to upload numbers into:" if kb else "Create a button first.", reply_markup=kb)
        return

    if text == "Back":
        menu = ctx.user_data.get("menu", "main")
        ctx.user_data.pop("await", None)
        if menu == "addremove":
            ctx.user_data["menu"] = "admin"
            await update.message.reply_text("Admin panel:", reply_markup=admin_kb())
        else:
            ctx.user_data["menu"] = "main"
            await update.message.reply_text("Main menu:", reply_markup=main_kb(uid))
        return

    await update.message.reply_text("Use the menu below.", reply_markup=main_kb(uid))

async def on_document(update, ctx):
    uid = update.effective_user.id
    awaiting = ctx.user_data.get("await")
    if not (isinstance(awaiting, tuple) and awaiting[0] == "upload" and is_admin(uid)):
        return
    bid = awaiting[1]
    # stream to a temp file, then parse line-by-line -> low memory, big files OK
    tmp_path = os.path.join(tempfile.gettempdir(), f"upl_{uid}.txt")
    file = await update.message.document.get_file()
    await file.download_to_drive(tmp_path)
    numbers = []
    with open(tmp_path, "r", encoding="utf-8", errors="ignore") as f:
        for ln in f:
            n = re.sub(r"\D", "", ln)
            if n:
                numbers.append(n)
    try:
        os.remove(tmp_path)
    except OSError:
        pass
    added = await store.add_numbers(bid, numbers)
    ctx.user_data.pop("await", None)
    btn = store.get_button(bid)
    await update.message.reply_text(
        f"✅ Uploaded {added} numbers into {btn['name'] if btn else bid}.", reply_markup=admin_kb()
    )

async def on_callback(update, ctx):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    parts = q.data.split("|")
    action = parts[0]

    if action == "getnum":
        btn = store.get_button(parts[1])
        if not btn:
            await q.edit_message_text("That button no longer exists."); return
        number = await store.take_number(btn["id"], uid, btn["name"])
        if not number:
            await q.edit_message_text(f"No numbers left for {btn['name']}."); return
        await _send_assigned(q, btn, number)
        return

    if action == "chg":
        old, bid = parts[1], parts[2]
        await store.release_number(old)
        btn = store.get_button(bid)
        if not btn:
            await q.edit_message_text("That button no longer exists."); return
        number = await store.take_number(bid, uid, btn["name"])
        if not number:
            await q.edit_message_text(f"No numbers left for {btn['name']}."); return
        await _send_assigned(q, btn, number)
        return

    if action == "del" and is_admin(uid):
        btn = store.get_button(parts[1])
        await store.remove_button(parts[1])
        await q.edit_message_text(f"🗑 Deleted: {btn['name'] if btn else parts[1]}", reply_markup=inline_for("del"))
        return

    if action == "upl" and is_admin(uid):
        btn = store.get_button(parts[1])
        ctx.user_data["await"] = ("upload", parts[1])
        await q.edit_message_text(f"Send the .txt file for {btn['name'] if btn else parts[1]} now.")
        return

async def _send_assigned(q, btn, number):
    text = (
        "Number Assigned Successfully !\n\n"
        f"Country : {html.escape(btn['name'])}\n"
        f"Number : <code>+{html.escape(number)}</code>\n\n"
        "Code will be received automatically here."
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔄 Change number", callback_data=f"chg|{number}|{btn['id']}")]])
    await q.edit_message_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)

# --------------------------------------------------------------------------- #
# Health server (Railway needs a bound port)
# --------------------------------------------------------------------------- #
async def _health(request):
    return web.Response(text="ok")

async def start_health_server():
    app = web.Application()
    app.router.add_get("/", _health)
    app.router.add_get("/health", _health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Health server on :%d", PORT)

# --------------------------------------------------------------------------- #
async def on_startup(app):
    await start_health_server()
    await client.start()
    await client.login()
    await monitor.prime()
    app.create_task(monitor.loop(app.bot))

async def on_shutdown(app):
    await client.close()

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN missing")
    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).post_shutdown(on_shutdown).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Bot starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()