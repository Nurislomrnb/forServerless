import os
import json
import time
import asyncio
import logging
import contextvars

import asyncpg
from fastapi import FastAPI, Request, HTTPException
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
)

logging.basicConfig(level=logging.INFO)

# --- ENV (Vercel Settings -> Environment Variables) ---
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_ID"])
DATABASE_URL = os.environ["DATABASE_URL"]
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]  # faqat A-Z a-z 0-9 _ - belgilari

app = FastAPI()
dp = Dispatcher()

# =====================================================================
#  DATABASE (Postgres, har so'rovga bitta ulanish)
# =====================================================================
_conn: contextvars.ContextVar = contextvars.ContextVar("conn")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id SERIAL PRIMARY KEY,
    user_id BIGINT UNIQUE,
    username TEXT,
    viewed INTEGER DEFAULT 0,
    join_date DATE DEFAULT CURRENT_DATE
);
CREATE TABLE IF NOT EXISTS films(
    id SERIAL PRIMARY KEY,
    title TEXT,
    file_id TEXT,
    code TEXT UNIQUE,
    views INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS channels(
    id SERIAL PRIMARY KEY,
    chat_id BIGINT UNIQUE,
    title TEXT,
    join_url TEXT
);
CREATE TABLE IF NOT EXISTS temp_approved_subs(
    user_id BIGINT,
    chat_id BIGINT,
    PRIMARY KEY (user_id, chat_id)
);
CREATE TABLE IF NOT EXISTS series(
    id SERIAL PRIMARY KEY,
    code TEXT,
    title TEXT,
    part INTEGER,
    file_id TEXT
);
CREATE TABLE IF NOT EXISTS admin_state(
    user_id BIGINT PRIMARY KEY,
    data TEXT
);
"""


async def q_one(sql, *args):
    return await _conn.get().fetchrow(sql, *args)


async def q_all(sql, *args):
    return await _conn.get().fetch(sql, *args)


async def q_val(sql, *args):
    return await _conn.get().fetchval(sql, *args)


async def q_exec(sql, *args):
    return await _conn.get().execute(sql, *args)


# --- Admin holati (oldingi `state` lug'ati o'rniga) ---
async def get_state(uid):
    raw = await q_val("SELECT data FROM admin_state WHERE user_id=$1", uid)
    return json.loads(raw) if raw else {}


async def set_state(uid, data):
    await q_exec(
        "INSERT INTO admin_state(user_id, data) VALUES($1,$2) "
        "ON CONFLICT (user_id) DO UPDATE SET data=EXCLUDED.data",
        uid, json.dumps(data),
    )


async def clear_state(uid):
    await q_exec("DELETE FROM admin_state WHERE user_id=$1", uid)


# Har xabar/callback oldidan admin holatini bir marta yuklaymiz -> `st`
async def state_mw(handler, event, data):
    uid = event.from_user.id if event.from_user else None
    data["st"] = await get_state(uid) if uid == ADMIN_ID else {}
    return await handler(event, data)


dp.message.outer_middleware(state_mw)
dp.callback_query.outer_middleware(state_mw)


def is_admin(m):
    return m.from_user.id == ADMIN_ID


# =====================================================================
#  KEYBOARDLAR
# =====================================================================
def admin_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🎬 Kino qo'shish")],
            [KeyboardButton(text="🗑 Kino o‘chirish")],
            [KeyboardButton(text="📊 Statistika")],
            [KeyboardButton(text="📢 Majburiy obuna kanallari")],
            [KeyboardButton(text="📤 Barchaga xabar yuborish")],
        ],
        resize_keyboard=True,
    )


def user_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🎞 Kino kodi yuborish")]],
        resize_keyboard=True,
    )


def channel_manage_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="⬅️ Orqaga")]], resize_keyboard=True
    )


def movie_type_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎬 Oddiy kino", callback_data="movie_single"),
        InlineKeyboardButton(text="📺 Serial", callback_data="movie_series"),
    ]])


def series_parts_kb(code, count):
    buttons, row = [], []
    for i in range(1, count + 1):
        row.append(InlineKeyboardButton(text=str(i), callback_data=f"series:{code}:{i}"))
        if len(row) == 5:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def join_keyboard(not_joined):
    buttons = [
        [InlineKeyboardButton(text=f"📢 {title} ga obuna bo‘lish", url=url)]
        for title, url in not_joined
    ]
    buttons.append([InlineKeyboardButton(text="✅ Obuna bo‘ldim", callback_data="check_subs")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# =====================================================================
#  MAJBURIY OBUNA
# =====================================================================
async def check_subscription(bot: Bot, user_id):
    channels = await q_all("SELECT chat_id, title, join_url FROM channels")
    not_joined = []
    for ch in channels:
        is_joined = False
        try:
            member = await bot.get_chat_member(ch["chat_id"], user_id)
            if member.status in ("member", "restricted", "administrator", "creator"):
                is_joined = True
        except Exception:
            pass

        if not is_joined:
            temp = await q_one(
                "SELECT 1 FROM temp_approved_subs WHERE user_id=$1 AND chat_id=$2",
                user_id, ch["chat_id"],
            )
            if temp:
                is_joined = True

        if not is_joined:
            not_joined.append((ch["title"], ch["join_url"]))
    return not_joined


async def require_subscription(msg: types.Message, bot: Bot) -> bool:
    """True qaytarsa — foydalanuvchi obuna bo'lgan. Aks holda xabar yuboradi."""
    not_joined = await check_subscription(bot, msg.from_user.id)
    if not_joined:
        await msg.answer(
            "📢 Iltimos, quyidagi kanallarga obuna bo‘ling:",
            reply_markup=join_keyboard(not_joined),
        )
        return False
    return True


async def deliver_code(msg: types.Message, code: str):
    movie = await q_one("SELECT title, file_id, views FROM films WHERE code=$1", code)
    if movie:
        await q_exec("UPDATE films SET views = views + 1 WHERE code=$1", code)
        await msg.answer_video(
            movie["file_id"],
            caption=f"🎬 {movie['title']}\n👁 {movie['views'] + 1} marta ko‘rilgan",
        )
        return

    series = await q_one(
        "SELECT title, COUNT(*) AS cnt FROM series WHERE code=$1 GROUP BY title", code
    )
    if series:
        await msg.answer(
            f"📺 Bu bir necha qismli kino\n\n🎞 Nomi: {series['title']}\n📀 Qismlar: {series['cnt']}",
            reply_markup=series_parts_kb(code, series["cnt"]),
        )
        return

    await msg.answer("📛 Bunday kodli kino yoki serial topilmadi.")


# =====================================================================
#  /start
# =====================================================================
@dp.message(CommandStart())
async def start_cmd(msg: types.Message, bot: Bot):
    await q_exec(
        "INSERT INTO users(user_id, username) VALUES($1,$2) ON CONFLICT DO NOTHING",
        msg.from_user.id, msg.from_user.username,
    )

    args = msg.text.split(maxsplit=1)
    if len(args) > 1:
        if not await require_subscription(msg, bot):
            return
        await deliver_code(msg, args[1].strip())
        return

    if msg.from_user.id == ADMIN_ID:
        await msg.answer("👋 Salom, admin!", reply_markup=admin_kb())
    else:
        if await require_subscription(msg, bot):
            await msg.answer("👋 Salom! Kino kodini yuboring:", reply_markup=user_kb())


@dp.callback_query(lambda c: c.data == "check_subs")
async def check_subs_again(callback: types.CallbackQuery, bot: Bot):
    not_joined = await check_subscription(bot, callback.from_user.id)
    if not_joined:
        await callback.message.edit_text(
            "❗ Siz hali ham quyidagi kanallarga obuna bo‘lmadingiz:",
            reply_markup=join_keyboard(not_joined),
        )
    else:
        await callback.message.edit_text("✅ Rahmat! Endi kodni yuborishingiz mumkin.")
        await bot.send_message(
            callback.from_user.id, "🎟 Endi kino kodini yuboring:", reply_markup=user_kb()
        )


@dp.chat_join_request()
async def track_join_request(request: types.ChatJoinRequest):
    exists = await q_one("SELECT 1 FROM channels WHERE chat_id=$1", request.chat.id)
    if exists:
        await q_exec(
            "INSERT INTO temp_approved_subs(user_id, chat_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
            request.from_user.id, request.chat.id,
        )
        logging.info("Join request %s -> %s yozib qo'yildi", request.from_user.id, request.chat.id)


# =====================================================================
#  ADMIN: MAJBURIY OBUNA KANALLARI
# =====================================================================
@dp.message(lambda m: m.text == "📢 Majburiy obuna kanallari" and is_admin(m))
async def manage_channels(msg: types.Message):
    rows = await q_all("SELECT id, title, join_url FROM channels")
    if rows:
        txt = "📢 Hozirgi kanallar:\n" + "\n".join(
            f"{r['id']}. {r['title']} - {r['join_url']}" for r in rows
        )
    else:
        txt = "📭 Hozircha kanal yo‘q."
    await msg.answer(
        txt + "\n\n➕ Yangi kanal qo‘shish uchun kanal/guruhdan biror xabarni forward qiling.\n"
        "❌ O‘chirish uchun /del ID yuboring (ID ro'yxatda ko'rsatilgan).",
        reply_markup=channel_manage_kb(),
    )
    await set_state(msg.from_user.id, {"step": "manage_channels"})


@dp.message(lambda m, st: is_admin(m) and st.get("step") == "manage_channels")
async def add_or_delete_channel(msg: types.Message, st: dict):
    uid = msg.from_user.id
    text = msg.text.strip() if msg.text else None

    if text == "⬅️ Orqaga":
        await clear_state(uid)
        await msg.answer("🔙 Orqaga qaytdingiz.", reply_markup=admin_kb())
        return

    if text and text.startswith("/del"):
        try:
            del_id = int(text.replace("/del", "").strip())
            await q_exec("DELETE FROM channels WHERE id=$1", del_id)
            await msg.answer(f"❌ Kanal o‘chirildi (ID: {del_id}).")
        except ValueError:
            await msg.answer("⚠️ /del ID formatida yuboring, ID raqam bo'lishi kerak.")
        return

    # Yangi Bot API: forward_origin; eski: forward_from_chat
    chat = None
    origin = getattr(msg, "forward_origin", None)
    if origin is not None:
        chat = getattr(origin, "chat", None) or getattr(origin, "sender_chat", None)
    if chat is None:
        chat = getattr(msg, "forward_from_chat", None)

    if chat:
        if chat.username:
            join_url = f"https://t.me/{chat.username}"
            await q_exec(
                "INSERT INTO channels(chat_id, title, join_url) VALUES($1,$2,$3) ON CONFLICT DO NOTHING",
                chat.id, chat.title, join_url,
            )
            await msg.answer(f"✅ Kanal qo‘shildi: {chat.title} ({join_url})")
        else:
            st.update({"step": "invite_link", "chat_id": chat.id, "title": chat.title})
            await set_state(uid, st)
            await msg.answer("⚠️ Bu private kanal/guruh. Endi invite linkini yuboring (https://t.me/+...):")
    else:
        await msg.answer("⚠️ Kanal/guruhdan xabar forward qiling yoki /del ID yuboring.")


@dp.message(lambda m, st: is_admin(m) and st.get("step") == "invite_link")
async def get_invite_link(msg: types.Message, st: dict):
    join_url = (msg.text or "").strip()
    if not (join_url.startswith("https://t.me/+") or join_url.startswith("https://t.me/joinchat/")):
        await msg.answer("⚠️ Invite link https://t.me/+ yoki https://t.me/joinchat/ bilan boshlanishi kerak.")
        return
    await q_exec(
        "INSERT INTO channels(chat_id, title, join_url) VALUES($1,$2,$3) ON CONFLICT DO NOTHING",
        st["chat_id"], st["title"], join_url,
    )
    await set_state(msg.from_user.id, {"step": "manage_channels"})
    await msg.answer(f"✅ Kanal qo‘shildi: {st['title']} ({join_url})")


# =====================================================================
#  ADMIN: BROADCAST
# =====================================================================
@dp.message(lambda m: m.text == "📤 Barchaga xabar yuborish" and is_admin(m))
async def broadcast_start(msg: types.Message):
    await set_state(msg.from_user.id, {"step": "broadcast_text"})
    await msg.answer(
        "📤 Barchaga yubormoqchi bo'lgan xabaringizni yuboring (matn, rasm, video yoki hujjat):\n\n"
        "Yuborganingizdan keyin avtomatik barchaga jo'natiladi.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="❌ Bekor qilish")]], resize_keyboard=True
        ),
    )


@dp.message(lambda m, st: is_admin(m) and st.get("step") == "broadcast_text")
async def broadcast_process(msg: types.Message, bot: Bot):
    if msg.text and msg.text.strip() == "❌ Bekor qilish":
        await clear_state(msg.from_user.id)
        await msg.answer("❌ Bekor qilindi.", reply_markup=admin_kb())
        return

    await clear_state(msg.from_user.id)
    users = [r["user_id"] for r in await q_all("SELECT user_id FROM users")]
    total = len(users)
    await msg.answer(f"📤 Xabar yuborilmoqda... Jami {total} ta foydalanuvchi.")

    started = time.monotonic()
    success = 0
    sent = 0

    async def send_one(uid):
        try:
            await bot.copy_message(uid, msg.chat.id, msg.message_id)
            return True
        except Exception as e:
            logging.warning("Xabar yuborishda xato %s: %s", uid, e)
            return False

    # Telegram limiti ~30 xabar/soniya -> 25 tadan paketlab yuboramiz
    for i in range(0, total, 25):
        if time.monotonic() - started > 45:  # serverless vaqt limiti uchun
            break
        batch = users[i:i + 25]
        results = await asyncio.gather(*(send_one(u) for u in batch))
        success += sum(results)
        sent += len(batch)
        await asyncio.sleep(1)

    note = "" if sent == total else f"\n⚠️ Vaqt limiti tufayli {total - sent} ta foydalanuvchiga yetib bormadi."
    await msg.answer(f"✅ Xabar yuborildi! Muvaffaqiyatli: {success}/{total}{note}", reply_markup=admin_kb())


# =====================================================================
#  ADMIN: STATISTIKA
# =====================================================================
@dp.message(lambda m: m.text == "📊 Statistika" and is_admin(m))
async def show_stats(msg: types.Message):
    users_count = await q_val("SELECT COUNT(*) FROM users")
    films_count = await q_val("SELECT COUNT(*) FROM films")
    total_views = await q_val("SELECT COALESCE(SUM(views),0) FROM films")
    today_joins = await q_val("SELECT COUNT(*) FROM users WHERE join_date = CURRENT_DATE")
    await msg.answer(
        f"📊 <b>Statistika</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{users_count}</b>\n"
        f"🆕 Bugun qo'shilganlar: <b>{today_joins}</b>\n"
        f"🎬 Kinolar: <b>{films_count}</b>\n"
        f"📺 Jami ko‘rilgan: <b>{total_views}</b> marta",
        parse_mode="HTML",
    )


# =====================================================================
#  ADMIN: KINO QO'SHISH
# =====================================================================
@dp.message(lambda m: m.text == "🎬 Kino qo'shish" and is_admin(m))
async def add_movie_start(msg: types.Message):
    await set_state(msg.from_user.id, {"step": "choose_type"})
    await msg.answer("🎬 Qanaqa kino joylamoqchisiz?", reply_markup=movie_type_kb())


@dp.callback_query(lambda c: c.data == "movie_single" and c.from_user.id == ADMIN_ID)
async def single_movie_start(call: types.CallbackQuery):
    await set_state(call.from_user.id, {"step": "single_title"})
    await call.message.answer("🎞 Kino nomini yuboring:")
    await call.answer()


@dp.message(lambda m, st: is_admin(m) and st.get("step") == "single_title")
async def single_movie_title(msg: types.Message, st: dict):
    st.update({"title": msg.text, "step": "single_video"})
    await set_state(msg.from_user.id, st)
    await msg.answer("🎥 Kinoning videosini yuboring:")


@dp.message(lambda m, st: m.video and is_admin(m) and st.get("step") == "single_video")
async def single_movie_video(msg: types.Message, st: dict):
    st.update({"file_id": msg.video.file_id, "step": "single_code"})
    await set_state(msg.from_user.id, st)
    await msg.answer("📟 Kino kodini kiriting:")


@dp.message(lambda m, st: is_admin(m) and st.get("step") == "single_code")
async def single_movie_code(msg: types.Message, st: dict):
    code = msg.text.strip()
    if await q_one("SELECT 1 FROM films WHERE code=$1", code) or await q_one(
        "SELECT 1 FROM series WHERE code=$1", code
    ):
        await msg.answer("❌ Bu kod mavjud. Boshqa kod kiriting.")
        return
    await q_exec(
        "INSERT INTO films(title, file_id, code) VALUES($1,$2,$3)",
        st["title"], st["file_id"], code,
    )
    await clear_state(msg.from_user.id)
    await msg.answer("✅ Kino muvaffaqiyatli yuklandi!", reply_markup=admin_kb())


# --- SERIAL QO'SHISH ---
@dp.callback_query(lambda c: c.data == "movie_series" and c.from_user.id == ADMIN_ID)
async def series_start(call: types.CallbackQuery):
    await set_state(call.from_user.id, {"step": "series_title"})
    await call.message.answer("🎞 Serial nomini yuboring:")
    await call.answer()


@dp.message(lambda m, st: is_admin(m) and st.get("step") == "series_title")
async def series_title(msg: types.Message, st: dict):
    st.update({"title": msg.text, "step": "series_count"})
    await set_state(msg.from_user.id, st)
    await msg.answer("📺 Serial nechta qismdan iborat?")


@dp.message(lambda m, st: is_admin(m) and st.get("step") == "series_count")
async def series_count(msg: types.Message, st: dict):
    if not msg.text or not msg.text.isdigit() or int(msg.text) < 1:
        await msg.answer("❌ Faqat son kiriting.")
        return
    st.update({"total": int(msg.text), "current": 1, "videos": [], "step": "series_video"})
    await set_state(msg.from_user.id, st)
    await msg.answer("🎥 1-qism videosini yuboring:")


@dp.message(lambda m, st: m.video and is_admin(m) and st.get("step") == "series_video")
async def series_videos(msg: types.Message, st: dict):
    st["videos"].append(msg.video.file_id)
    if st["current"] < st["total"]:
        st["current"] += 1
        await set_state(msg.from_user.id, st)
        await msg.answer(f"🎥 {st['current']}-qism videosini yuboring:")
    else:
        st["step"] = "series_code"
        await set_state(msg.from_user.id, st)
        await msg.answer("📟 Serial kodi kiriting:")


@dp.message(lambda m, st: is_admin(m) and st.get("step") == "series_code")
async def series_code(msg: types.Message, st: dict):
    code = msg.text.strip()
    if await q_one("SELECT 1 FROM series WHERE code=$1", code) or await q_one(
        "SELECT 1 FROM films WHERE code=$1", code
    ):
        await msg.answer("❌ Bu kod mavjud. Boshqa kod kiriting.")
        return
    for i, file_id in enumerate(st["videos"], start=1):
        await q_exec(
            "INSERT INTO series(code, title, part, file_id) VALUES($1,$2,$3,$4)",
            code, st["title"], i, file_id,
        )
    await clear_state(msg.from_user.id)
    await msg.answer("✅ Serial muvaffaqiyatli saqlandi!", reply_markup=admin_kb())


# --- KINO O'CHIRISH ---
@dp.message(lambda m: m.text == "🗑 Kino o‘chirish" and is_admin(m))
async def delete_movie_start(msg: types.Message):
    await set_state(msg.from_user.id, {"step": "delete"})
    await msg.answer("🗑 O‘chirmoqchi bo‘lgan kino kodini yuboring:")


@dp.message(lambda m, st: is_admin(m) and st.get("step") == "delete")
async def delete_movie_process(msg: types.Message):
    code = msg.text.strip()
    film = await q_one("SELECT title FROM films WHERE code=$1", code)
    series = await q_one("SELECT title FROM series WHERE code=$1 LIMIT 1", code)
    if not film and not series:
        await msg.answer("❌ Bunday kodli kino topilmadi.")
    else:
        title = (film or series)["title"]
        await q_exec("DELETE FROM films WHERE code=$1", code)
        await q_exec("DELETE FROM series WHERE code=$1", code)
        await msg.answer(f"✅ '{title}' (kod: {code}) o‘chirildi.", reply_markup=admin_kb())
    await clear_state(msg.from_user.id)


@dp.callback_query(lambda c: c.data and c.data.startswith("series:"))
async def send_series_part(call: types.CallbackQuery):
    _, rest = call.data.split(":", 1)
    code, part = rest.rsplit(":", 1)
    row = await q_one(
        "SELECT file_id FROM series WHERE code=$1 AND part=$2", code, int(part)
    )
    if not row:
        await call.answer("❌ Qism topilmadi", show_alert=True)
        return
    await call.message.answer_video(row["file_id"], caption=f"📺 {part}-qism")
    await call.answer()


@dp.message(lambda m: m.text == "📂 Kinolar ro'yxati" and is_admin(m))
async def show_list(msg: types.Message):
    rows = await q_all("SELECT title, code, views FROM films ORDER BY id DESC")
    if not rows:
        await msg.answer("🎞 Hozircha kinolar yo‘q.")
    else:
        txt = "\n".join(f"{r['code']} — {r['title']} ({r['views']} marta ko‘rilgan)" for r in rows)
        await msg.answer("🎬 Kinolar ro'yxati:\n" + txt)


# =====================================================================
#  FOYDALANUVCHI TOMONI
# =====================================================================
@dp.message(lambda m: m.text == "🎞 Kino kodi yuborish")
async def ask_code(msg: types.Message, bot: Bot):
    if await require_subscription(msg, bot):
        await msg.answer("🎟 Kino kodini yuboring:")


@dp.message(lambda m: m.text)
async def send_movie(msg: types.Message, bot: Bot):
    if not await require_subscription(msg, bot):
        return
    await deliver_code(msg, msg.text.strip())


# =====================================================================
#  WEBHOOK (FastAPI)
# =====================================================================
@app.post("/webhook")
async def webhook(request: Request):
    if request.headers.get("x-telegram-bot-api-secret-token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

    data = await request.json()
    bot = Bot(BOT_TOKEN)
    conn = await asyncpg.connect(DATABASE_URL, statement_cache_size=0)
    token = _conn.set(conn)
    try:
        update = types.Update.model_validate(data, context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception:
        # Xato bo'lsa ham 200 qaytaramiz, aks holda Telegram bir xil update'ni qayta yuboraveradi
        logging.exception("Update ishlashda xato")
    finally:
        _conn.reset(token)
        await conn.close()
        await bot.session.close()
    return {"ok": True}


@app.get("/setup")
async def setup(request: Request, key: str = ""):
    """Bir marta ochasiz: jadvallarni yaratadi va webhook'ni Telegramga ulaydi."""
    if key != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")

    conn = await asyncpg.connect(DATABASE_URL, statement_cache_size=0)
    try:
        await conn.execute(SCHEMA)
    finally:
        await conn.close()

    base = os.environ.get("PUBLIC_URL") or f"https://{request.headers['host']}"
    url = f"{base.rstrip('/')}/webhook"
    bot = Bot(BOT_TOKEN)
    try:
        ok = await bot.set_webhook(
            url,
            secret_token=WEBHOOK_SECRET,
            allowed_updates=dp.resolve_used_update_types(),
        )
    finally:
        await bot.session.close()
    return {"db": "ready", "webhook_set": ok, "url": url}


@app.get("/")
async def root():
    return {"status": "ok"}
