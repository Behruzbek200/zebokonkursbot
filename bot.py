#!/usr/bin/env python3
"""
Professional Telegram Bot – Webhook version.
Maxfiy kanalga so‘rov yuborgan foydalanuvchilarga ruxsat beradi (kanalga qo‘shmaydi).
Barcha funksiyalar to‘liq.
"""

import os
import logging
import sqlite3
import urllib.parse
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, List, Any

import telebot
from telebot import types
from telebot.util import quick_markup
from flask import Flask, request, abort

# ----------------------------------------------------------------------
# CONFIGURATION (via environment variables)
# ----------------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
SUPER_ADMIN_ID = int(os.environ.get("SUPER_ADMIN_ID", "123456789"))
DB_NAME = os.environ.get("DB_NAME", "bot.db")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

# ----------------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger("bot")

# ----------------------------------------------------------------------
# Flask app
# ----------------------------------------------------------------------
app = Flask(__name__)

# ----------------------------------------------------------------------
# DATABASE INITIALIZATION & MIGRATION
# ----------------------------------------------------------------------
def init_database() -> None:
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()

    cur.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            last_name TEXT,
            points INTEGER DEFAULT 0,
            referred_by INTEGER,
            registered_date TEXT,
            banned INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_username TEXT,
            chat_id INTEGER,
            invite_link TEXT,
            added_date TEXT,
            added_by INTEGER
        );
        CREATE TABLE IF NOT EXISTS join_requests (
            user_id INTEGER,
            chat_id INTEGER,
            request_date TEXT,
            PRIMARY KEY (user_id, chat_id)
        );
        CREATE TABLE IF NOT EXISTS prizes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            description TEXT,
            image_file_id TEXT,
            sort_order INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS contest_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_text TEXT
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            added_by INTEGER,
            date TEXT
        );
    """)

    # migration
    try:
        cur.execute("SELECT winners_count FROM prizes LIMIT 1")
    except:
        cur.execute("ALTER TABLE prizes ADD COLUMN winners_count INTEGER DEFAULT 0")

    defaults = {
        "bot_name": "My Awesome Bot",
        "start_message": "Assalomu alaykum! Botimizga xush kelibsiz.",
        "referral_points": "1",
        "contest_active": "0",
        "contest_end": "",
        "contest_referral_points": "1",
        "admin_username": ""
    }
    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    cur.execute("INSERT OR IGNORE INTO admins (user_id, added_by, date) VALUES (?, ?, ?)",
                (SUPER_ADMIN_ID, SUPER_ADMIN_ID, datetime.now().isoformat()))

    conn.commit()
    conn.close()
    logger.info("Database ready.")

# ----------------------------------------------------------------------
# DB HELPERS
# ----------------------------------------------------------------------
def db_execute(query: str, params: Tuple = (), fetch: bool = False, fetchone: bool = False) -> Any:
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    try:
        cur.execute(query, params)
        conn.commit()
        if fetchone:
            row = cur.fetchone()
            return dict(row) if row else None
        if fetch:
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        logger.error(f"DB error: {e} | {query} | {params}")
        conn.rollback()
        return None
    finally:
        conn.close()

def get_setting(key: str) -> Optional[str]:
    r = db_execute("SELECT value FROM settings WHERE key = ?", (key,), fetchone=True)
    return r["value"] if r else None

def set_setting(key: str, value: str) -> bool:
    try:
        db_execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
        return True
    except Exception as e:
        logger.error(f"set_setting: {e}")
        return False

# ----------------------------------------------------------------------
# FSM
# ----------------------------------------------------------------------
user_states: Dict[int, Tuple[str, Dict]] = {}

def set_state(uid: int, state: str, ctx: Dict = None) -> None:
    user_states[uid] = (state, ctx or {})

def get_state(uid: int) -> Optional[Tuple[str, Dict]]:
    return user_states.get(uid)

def clear_state(uid: int) -> None:
    user_states.pop(uid, None)

# ----------------------------------------------------------------------
# BOT INSTANCE
# ----------------------------------------------------------------------
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ----------------------------------------------------------------------
# JOIN REQUEST HANDLER – faqat yozib qo‘yish, tasdiqlamaslik
# ----------------------------------------------------------------------
@bot.chat_join_request_handler()
def handle_join_request(join_request: types.ChatJoinRequest):
    try:
        db_execute(
            "INSERT OR IGNORE INTO join_requests (user_id, chat_id, request_date) VALUES (?, ?, ?)",
            (join_request.from_user.id, join_request.chat.id, datetime.now().isoformat())
        )
        logger.info(f"Join request recorded for user {join_request.from_user.id} in chat {join_request.chat.id}")
    except Exception as e:
        logger.error(f"Failed to record join request: {e}")

# ----------------------------------------------------------------------
# SUBSCRIPTION CHECK (endi join_requests ham tekshiriladi)
# ----------------------------------------------------------------------
def get_required_channels() -> List[Dict]:
    rows = db_execute("SELECT channel_username, chat_id, invite_link FROM channels", fetch=True)
    return rows if rows else []

def check_subscription(user_id: int) -> bool:
    for ch in get_required_channels():
        if ch.get("chat_id"):
            # avval a'zolikni tekshir
            try:
                member = bot.get_chat_member(ch["chat_id"], user_id)
                if member.status in ["creator", "administrator", "member"]:
                    continue  # a'zo, keyingi kanalga o't
            except:
                pass
            # a'zo bo'lmasa, join request yuborganligini tekshir
            req = db_execute(
                "SELECT * FROM join_requests WHERE user_id = ? AND chat_id = ?",
                (user_id, ch["chat_id"]), fetchone=True
            )
            if not req:
                return False
        elif ch.get("channel_username"):
            try:
                member = bot.get_chat_member(ch["channel_username"], user_id)
                if member.status not in ["creator", "administrator", "member"]:
                    return False
            except:
                return False
    return True

def send_subscription_prompt(chat_id: int):
    channels = get_required_channels()
    if not channels:
        return
    text = "🔔 Botdan foydalanish uchun quyidagi kanallarga obuna bo‘ling:\n\n"
    markup = types.InlineKeyboardMarkup(row_width=1)
    for ch in channels:
        if ch.get("invite_link"):
            text += "👉 Maxfiy kanal (so‘rov yuborish kifoya)\n"
            markup.add(types.InlineKeyboardButton("➕ Kanalga qo‘shilish", url=ch["invite_link"]))
        elif ch.get("channel_username"):
            name = ch["channel_username"].replace("@", "")
            text += f"👉 @{name}\n"
            markup.add(types.InlineKeyboardButton(f"➕ @{name}", url=f"https://t.me/{name}"))
        elif ch.get("chat_id"):
            text += f"👉 Maxfiy kanal (ID: {ch['chat_id']})\n"
    markup.add(types.InlineKeyboardButton("✅ Tekshirish", callback_data="check_subscription"))
    bot.send_message(chat_id, text, reply_markup=markup, disable_web_page_preview=True)

# ----------------------------------------------------------------------
# KEYBOARDS
# ----------------------------------------------------------------------
def main_menu_keyboard(is_admin: bool = False) -> types.ReplyKeyboardMarkup:
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        "👤 Profil", "🔗 Referal havolam", "📤 Do‘stlarni taklif qilish",
        "👥 Takliflarim", "🏆 TOP 10", "🎯 Mening o‘rnim",
        "🎁 Sovrinlar", "📜 Konkurs shartlari",
        "⏳ Konkurs vaqti", "ℹ️ Yordam"
    ]
    if is_admin:
        buttons.append("📊 Admin")
    markup.add(*buttons)
    return markup

def admin_menu_keyboard() -> types.ReplyKeyboardMarkup:
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        "📊 Statistika", "👥 Foydalanuvchilar", "📢 Xabar yuborish",
        "📢 Majburiy obuna", "🏆 Konkurs", "🎁 Sovrin boshqaruvi",
        "📜 Shart boshqaruvi", "🏅 Reyting",
        "⚙️ Sozlamalar", "🔒 Adminlar", "🏠 Asosiy menyu"
    ]
    markup.add(*buttons)
    return markup

# ----------------------------------------------------------------------
# UTILS
# ----------------------------------------------------------------------
def is_admin(uid: int) -> bool:
    return db_execute("SELECT user_id FROM admins WHERE user_id = ?", (uid,), fetchone=True) is not None

def is_super_admin(uid: int) -> bool:
    return uid == SUPER_ADMIN_ID

def get_user_rank(uid: int) -> int:
    rows = db_execute("SELECT user_id FROM users WHERE banned=0 ORDER BY points DESC", fetch=True)
    if not rows:
        return 0
    for i, r in enumerate(rows, 1):
        if r["user_id"] == uid:
            return i
    return 0

def get_top_users(limit: int = 10) -> List[Dict]:
    return db_execute("SELECT * FROM users WHERE banned=0 ORDER BY points DESC LIMIT ?", (limit,), fetch=True) or []

def get_referral_count(uid: int) -> int:
    r = db_execute("SELECT COUNT(*) as cnt FROM users WHERE referred_by = ?", (uid,), fetchone=True)
    return r["cnt"] if r else 0

def get_remaining_time() -> Optional[timedelta]:
    if get_setting("contest_active") != "1":
        return None
    end_str = get_setting("contest_end")
    if not end_str:
        return None
    try:
        end = datetime.fromisoformat(end_str)
        now = datetime.now()
        return end - now if end > now else None
    except:
        return None

# ----------------------------------------------------------------------
# REGISTRATION & REFERRAL
# ----------------------------------------------------------------------
def register_user(user: types.User, referrer_id: Optional[int] = None) -> bool:
    uid = user.id
    if db_execute("SELECT user_id FROM users WHERE user_id = ?", (uid,), fetchone=True):
        return False
    if referrer_id == uid:
        referrer_id = None
    if referrer_id:
        ref = db_execute("SELECT user_id FROM users WHERE user_id=? AND banned=0", (referrer_id,), fetchone=True)
        if not ref:
            referrer_id = None

    now = datetime.now().isoformat()
    db_execute(
        "INSERT INTO users (user_id, username, first_name, last_name, points, referred_by, registered_date) VALUES (?,?,?,?,?,?,?)",
        (uid, user.username, user.first_name, user.last_name, 0, referrer_id, now)
    )
    if referrer_id:
        pts = int(get_setting("contest_referral_points") if get_setting("contest_active")=="1" else get_setting("referral_points") or 1)
        db_execute("UPDATE users SET points = points + ? WHERE user_id = ?", (pts, referrer_id))
    return True

# ----------------------------------------------------------------------
# HANDLERS
# ----------------------------------------------------------------------
@bot.message_handler(commands=["start"])
def start_cmd(message: types.Message):
    chat_id = message.chat.id
    user = message.from_user
    args = message.text.split()
    ref = None
    if len(args) > 1:
        try:
            ref = int(args[1])
        except:
            pass
    register_user(user, ref)
    if not check_subscription(chat_id):
        send_subscription_prompt(chat_id)
        return
    start_msg = get_setting("start_message") or "Xush kelibsiz!"
    bot.send_message(chat_id, start_msg, reply_markup=main_menu_keyboard(is_admin(chat_id)))

def subscription_required(func):
    def wrapper(msg: types.Message):
        if not check_subscription(msg.chat.id):
            send_subscription_prompt(msg.chat.id)
            return
        return func(msg)
    return wrapper

@bot.message_handler(func=lambda m: m.text in [
    "👤 Profil", "🔗 Referal havolam", "📤 Do‘stlarni taklif qilish",
    "👥 Takliflarim", "🏆 TOP 10", "🎯 Mening o‘rnim",
    "🎁 Sovrinlar", "📜 Konkurs shartlari",
    "⏳ Konkurs vaqti", "ℹ️ Yordam", "📊 Admin",
    "📊 Statistika", "👥 Foydalanuvchilar", "📢 Xabar yuborish",
    "📢 Majburiy obuna", "🏆 Konkurs",
    "🎁 Sovrin boshqaruvi", "📜 Shart boshqaruvi",
    "🏅 Reyting", "⚙️ Sozlamalar", "🔒 Adminlar",
    "🏠 Asosiy menyu"
])
@subscription_required
def main_menu_handler(message: types.Message):
    txt = message.text
    cid = message.chat.id
    uid = message.from_user.id

    if is_admin(uid):
        if txt == "📊 Statistika": show_stats(cid); return
        if txt == "👥 Foydalanuvchilar": show_users_list(cid); return
        if txt == "📢 Xabar yuborish": start_broadcast(cid); return
        if txt == "📢 Majburiy obuna": show_sub_management(cid); return
        if txt == "🏆 Konkurs": show_contest_panel(cid); return
        if txt == "🎁 Sovrin boshqaruvi": show_prizes_admin(cid); return
        if txt == "📜 Shart boshqaruvi": manage_contest_rules(cid); return
        if txt == "🏅 Reyting": show_rating_panel(cid); return
        if txt == "⚙️ Sozlamalar": settings_menu(cid); return
        if txt == "🔒 Adminlar": admin_management(cid); return
        if txt == "📊 Admin":
            bot.send_message(cid, "Admin panel:", reply_markup=admin_menu_keyboard())
            return

    if txt == "👤 Profil": show_profile(cid, uid)
    elif txt == "🔗 Referal havolam": show_referral_link(cid, uid)
    elif txt == "📤 Do‘stlarni taklif qilish": show_invite(cid, uid)
    elif txt == "👥 Takliflarim": show_my_referrals(cid, uid)
    elif txt == "🏆 TOP 10": show_top10(cid)
    elif txt == "🎯 Mening o‘rnim": show_my_rank(cid, uid)
    elif txt == "🎁 Sovrinlar": show_prizes(cid)
    elif txt == "📜 Konkurs shartlari": show_contest_rules(cid)
    elif txt == "⏳ Konkurs vaqti": show_timer(cid)
    elif txt == "ℹ️ Yordam": support_start(cid)
    elif txt == "🏠 Asosiy menyu":
        bot.send_message(cid, "Asosiy menyu:", reply_markup=main_menu_keyboard(is_admin(uid)))
    else:
        bot.send_message(cid, "Nomaʼlum buyruq.")

# ----------------------------------------------------------------------
# SUPPORT SYSTEM (Yordam)
# ----------------------------------------------------------------------
def support_start(chat_id: int):
    set_state(chat_id, "support_message")
    msg = bot.send_message(chat_id, "📩 Adminga yozmoqchi bo‘lgan xabaringizni yuboring. Bekor qilish uchun /cancel")
    bot.register_next_step_handler(msg, support_receive)

def support_receive(message: types.Message):
    cid = message.chat.id
    if message.text and message.text == "/cancel":
        clear_state(cid)
        bot.send_message(cid, "Bekor qilindi.")
        return
    for admin_id in [SUPER_ADMIN_ID] + [a["user_id"] for a in db_execute("SELECT user_id FROM admins WHERE user_id != ?", (SUPER_ADMIN_ID,), fetch=True) or []]:
        try:
            forwarded = bot.forward_message(admin_id, cid, message.message_id)
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton("💬 Javob berish", callback_data=f"support_reply:{cid}"))
            bot.send_message(admin_id, f"👤 Foydalanuvchidan xabar:\nID: {cid}", reply_markup=markup, reply_to_message_id=forwarded.message_id)
        except Exception as e:
            logger.error(f"Support forward failed to {admin_id}: {e}")
    bot.send_message(cid, "✅ Xabaringiz adminga yuborildi.")
    clear_state(cid)

@bot.callback_query_handler(func=lambda call: call.data.startswith("support_reply:"))
def support_reply_cb(call: types.CallbackQuery):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo‘q", show_alert=True)
        return
    target_uid = int(call.data.split(":")[1])
    set_state(call.message.chat.id, "support_reply", {"target": target_uid})
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, f"✍️ Javob matnini yozing (foydalanuvchi ID: {target_uid}):")
    bot.register_next_step_handler(msg, support_send_reply)

def support_send_reply(message: types.Message):
    cid = message.chat.id
    state = get_state(cid)
    if not state or state[0] != "support_reply":
        return
    target = state[1]["target"]
    text = f"📩 <b>Admin javobi:</b>\n{message.text}"
    try:
        bot.send_message(target, text)
        bot.send_message(cid, "✅ Javob yuborildi.")
    except Exception as e:
        bot.send_message(cid, f"❌ Xatolik: {e}")
    clear_state(cid)

# ----------------------------------------------------------------------
# USER VIEWS
# ----------------------------------------------------------------------
def show_profile(chat_id: int, uid: int):
    user = db_execute("SELECT * FROM users WHERE user_id=?", (uid,), fetchone=True)
    if not user:
        bot.send_message(chat_id, "Siz ro‘yxatdan o‘tmagansiz. /start")
        return
    refs = get_referral_count(uid)
    rank = get_user_rank(uid)
    text = (
        f"👤 <b>Profil</b>\n\n"
        f"<b>Ism:</b> {user['first_name']} {user.get('last_name','')}\n"
        f"<b>Username:</b> @{user.get('username','yo‘q')}\n"
        f"<b>ID:</b> <code>{uid}</code>\n"
        f"<b>Ball:</b> {user['points']}\n"
        f"<b>Taklif qilganlar:</b> {refs}\n"
        f"<b>Reyting:</b> {rank}-o‘rin\n"
        f"<b>Sana:</b> {user['registered_date'][:10] if user['registered_date'] else '?'}"
    )
    bot.send_message(chat_id, text)

def show_referral_link(chat_id, uid):
    bot_username = bot.get_me().username
    link = f"https://t.me/{bot_username}?start={uid}"
    share_text = f"Do'stlaringizni taklif qiling va ball to'plang!\n👉 {link}"
    share_url = f"https://t.me/share/url?url={urllib.parse.quote(link)}&text={urllib.parse.quote(share_text)}"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📤 Do‘stlarga ulashish", url=share_url))
    bot.send_message(chat_id, f"🔗 Sizning havolangiz:\n{link}", reply_markup=markup)

def show_invite(chat_id, uid):
    show_referral_link(chat_id, uid)

def show_my_referrals(chat_id, uid):
    count = get_referral_count(uid)
    refs = db_execute("SELECT first_name, registered_date FROM users WHERE referred_by=? ORDER BY registered_date DESC LIMIT 5", (uid,), fetch=True)
    txt = f"👥 <b>Taklif qilganlaringiz: {count}</b>\n"
    for r in refs:
        txt += f"• {r['first_name']} – {r['registered_date'][:10]}\n"
    bot.send_message(chat_id, txt)

def show_top10(chat_id):
    top = get_top_users(10)
    if not top:
        return bot.send_message(chat_id, "Hali foydalanuvchi yo‘q.")
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    txt = "🏆 <b>TOP 10</b>\n\n"
    for i, u in enumerate(top, 1):
        medal = medals.get(i, f"{i}.")
        txt += f"{medal} {u['first_name']} – {u['points']} ball\n"
    bot.send_message(chat_id, txt)

def show_my_rank(chat_id, uid):
    rank = get_user_rank(uid)
    if rank == 0:
        return bot.send_message(chat_id, "Ro‘yxatda emassiz.")
    pts = db_execute("SELECT points FROM users WHERE user_id=?", (uid,), fetchone=True)["points"]
    diff = ""
    if rank > 1:
        prev = db_execute("SELECT points FROM users WHERE banned=0 ORDER BY points DESC LIMIT 1 OFFSET ?", (rank-2,), fetchone=True)
        if prev:
            diff = f" Oldingi o‘ringa {prev['points'] - pts + 1} ball kerak."
    bot.send_message(chat_id, f"🎯 Siz {rank}-o‘rindasiz.{diff}")

def show_prizes(chat_id):
    prizes = db_execute("SELECT * FROM prizes ORDER BY sort_order", fetch=True)
    if not prizes:
        return bot.send_message(chat_id, "Hozircha sovrinlar mavjud emas.")
    for p in prizes:
        wc = p.get("winners_count", 0)
        wc_text = f" (Top {wc} g'olibga)" if wc > 0 else ""
        text = f"🎁 <b>{p['title']}{wc_text}</b>\n{p.get('description','')}"
        if p.get("image_file_id"):
            try:
                bot.send_photo(chat_id, p["image_file_id"], caption=text)
            except:
                bot.send_message(chat_id, text)
        else:
            bot.send_message(chat_id, text)

def show_contest_rules(chat_id):
    rules = db_execute("SELECT rule_text FROM contest_rules ORDER BY id", fetch=True)
    if not rules:
        bot.send_message(chat_id, "Hali shartlar kiritilmagan.")
        return
    txt = "📜 <b>Konkurs shartlari:</b>\n" + "\n".join(f"• {r['rule_text']}" for r in rules)
    bot.send_message(chat_id, txt)

def show_timer(chat_id):
    rem = get_remaining_time()
    if not rem:
        return bot.send_message(chat_id, "Hozir faol konkurs yo‘q.")
    days = rem.days
    hours, remainder = divmod(rem.seconds, 3600)
    minutes = remainder // 60
    bot.send_message(chat_id, f"⏳ Konkurs tugashiga: {days} kun, {hours} soat, {minutes} daqiqa qoldi.")

# ----------------------------------------------------------------------
# ADMIN VIEWS
# ----------------------------------------------------------------------
def show_stats(chat_id):
    total = db_execute("SELECT COUNT(*) as c FROM users", fetchone=True)["c"]
    today = datetime.now().strftime("%Y-%m-%d")
    today_users = db_execute("SELECT COUNT(*) as c FROM users WHERE registered_date LIKE ?", (f"{today}%",), fetchone=True)["c"]
    active = db_execute("SELECT COUNT(*) as c FROM users WHERE banned=0", fetchone=True)["c"]
    ref_total = db_execute("SELECT COUNT(*) as c FROM users WHERE referred_by IS NOT NULL", fetchone=True)["c"]
    top_user = db_execute("SELECT first_name, points FROM users ORDER BY points DESC LIMIT 1", fetchone=True)
    top_str = f"{top_user['first_name']} ({top_user['points']})" if top_user else "–"
    bot.send_message(chat_id, (
        f"📊 <b>Statistika</b>\n"
        f"Jami: {total}\nBugungi: {today_users}\nFaol: {active}\n"
        f"Referallar: {ref_total}\nTop: {top_str}"
    ))

def show_users_list(chat_id: int, page: int = 0):
    users = db_execute("SELECT user_id, first_name, username, points, banned FROM users ORDER BY user_id LIMIT 10 OFFSET ?", (page*10,), fetch=True)
    if not users:
        bot.send_message(chat_id, "Foydalanuvchilar topilmadi.")
        return
    text = "👥 <b>Foydalanuvchilar</b>\n\n"
    markup = types.InlineKeyboardMarkup(row_width=2)
    for u in users:
        name = u["first_name"] or "NoName"
        uname = f"@{u['username']}" if u.get("username") else ""
        b = "🚫" if u["banned"] else ""
        text += f"{b} {name} {uname} (ID: {u['user_id']}) – {u['points']} ball\n"
        markup.add(types.InlineKeyboardButton(f"⚙️ {u['user_id']}", callback_data=f"user_actions:{u['user_id']}"))
    nav = []
    if page > 0:
        nav.append(types.InlineKeyboardButton("⬅️ Oldingi", callback_data=f"users_page:{page-1}"))
    if len(users) == 10:
        nav.append(types.InlineKeyboardButton("Keyingi ➡️", callback_data=f"users_page:{page+1}"))
    if nav:
        markup.row(*nav)
    bot.send_message(chat_id, text, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("users_page:"))
def users_page_cb(call):
    if not is_admin(call.from_user.id): return
    page = int(call.data.split(":")[1])
    show_users_list(call.message.chat.id, page)
    bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("user_actions:"))
def user_actions_cb(call):
    if not is_admin(call.from_user.id): return
    target_uid = int(call.data.split(":")[1])
    user = db_execute("SELECT * FROM users WHERE user_id=?", (target_uid,), fetchone=True)
    if not user:
        bot.answer_callback_query(call.id, "User topilmadi")
        return
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("➕ Ball qo‘shish", callback_data=f"admin_add_points:{target_uid}"),
        types.InlineKeyboardButton("➖ Ball ayirish", callback_data=f"admin_sub_points:{target_uid}"),
        types.InlineKeyboardButton("🚫 Ban" if not user["banned"] else "✅ Unban", callback_data=f"admin_ban:{target_uid}"),
    )
    bot.send_message(call.message.chat.id, f"{user['first_name']} (ID: {target_uid}) amallari:", reply_markup=markup)
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_add_points:"))
def admin_add_points_cb(call):
    if not is_admin(call.from_user.id): return
    uid = int(call.data.split(":")[1])
    set_state(call.message.chat.id, "add_points", {"uid": uid})
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "Qo‘shiladigan ball:")
    bot.register_next_step_handler(msg, process_add_points)

def process_add_points(msg):
    state = get_state(msg.chat.id)
    if not state: return
    uid = state[1]["uid"]
    try:
        pts = int(msg.text)
    except:
        bot.send_message(msg.chat.id, "Son kiriting."); clear_state(msg.chat.id); return
    db_execute("UPDATE users SET points=points+? WHERE user_id=?", (pts, uid))
    bot.send_message(msg.chat.id, f"✅ {pts} ball qo‘shildi.")
    clear_state(msg.chat.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_sub_points:"))
def admin_sub_points_cb(call):
    if not is_admin(call.from_user.id): return
    uid = int(call.data.split(":")[1])
    set_state(call.message.chat.id, "sub_points", {"uid": uid})
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "Ayriladigan ball:")
    bot.register_next_step_handler(msg, process_sub_points)

def process_sub_points(msg):
    state = get_state(msg.chat.id)
    if not state: return
    uid = state[1]["uid"]
    try:
        pts = int(msg.text)
    except:
        bot.send_message(msg.chat.id, "Son kiriting."); clear_state(msg.chat.id); return
    db_execute("UPDATE users SET points=MAX(0,points-?) WHERE user_id=?", (pts, uid))
    bot.send_message(msg.chat.id, f"✅ {pts} ball ayrildi.")
    clear_state(msg.chat.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_ban:"))
def admin_ban_cb(call):
    if not is_admin(call.from_user.id): return
    uid = int(call.data.split(":")[1])
    user = db_execute("SELECT banned FROM users WHERE user_id=?", (uid,), fetchone=True)
    if not user: return
    new_ban = 0 if user["banned"] else 1
    db_execute("UPDATE users SET banned=? WHERE user_id=?", (new_ban, uid))
    bot.send_message(call.message.chat.id, f"{'🚫 Banlandi' if new_ban else '✅ Bandan chiqarildi'}: ID {uid}")
    bot.answer_callback_query(call.id)

# ----------------------------------------------------------------------
# BROADCAST
# ----------------------------------------------------------------------
def start_broadcast(chat_id):
    set_state(chat_id, "broadcast")
    msg = bot.send_message(chat_id, "Xabar (matn/rasm/video/audio/fayl) yuboring yoki /cancel:")
    bot.register_next_step_handler(msg, broadcast_content)

def broadcast_content(msg):
    cid = msg.chat.id
    if msg.text == "/cancel": clear_state(cid); bot.send_message(cid, "Bekor."); return
    ctx = {"content_type": msg.content_type}
    if msg.content_type == "text": ctx["text"] = msg.text
    elif msg.content_type == "photo": ctx["photo"] = msg.photo[-1].file_id; ctx["caption"] = msg.caption or ""
    elif msg.content_type == "video": ctx["video"] = msg.video.file_id; ctx["caption"] = msg.caption or ""
    elif msg.content_type == "audio": ctx["audio"] = msg.audio.file_id; ctx["caption"] = msg.caption or ""
    elif msg.content_type == "document": ctx["document"] = msg.document.file_id; ctx["caption"] = msg.caption or ""
    else: bot.send_message(cid, "Turi noto‘g‘ri."); bot.register_next_step_handler(msg, broadcast_content); return
    set_state(cid, "broadcast_buttons", ctx)
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Tugmalar qo‘shish", callback_data="broadcast_add_buttons"))
    markup.add(types.InlineKeyboardButton("Tugmasiz yuborish", callback_data="broadcast_send"))
    bot.send_message(cid, "Inline tugmalar kerakmi?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data in ["broadcast_add_buttons", "broadcast_send"])
def broadcast_cb(call):
    if not is_admin(call.from_user.id): return
    if call.data == "broadcast_add_buttons":
        set_state(call.message.chat.id, "broadcast_buttons_input", get_state(call.message.chat.id)[1])
        msg = bot.send_message(call.message.chat.id, "Tugmalarni quyidagicha kiriting:\n`Tugma - link`")
        bot.register_next_step_handler(msg, broadcast_buttons_input)
    else:
        execute_broadcast(call.message.chat.id)

def broadcast_buttons_input(msg):
    cid = msg.chat.id
    state = get_state(cid)
    if not state: return
    ctx = state[1]
    markup = types.InlineKeyboardMarkup()
    for line in msg.text.strip().split("\n"):
        if " - " in line:
            name, url = line.split(" - ", 1)
            markup.add(types.InlineKeyboardButton(name.strip(), url=url.strip()))
    ctx["reply_markup"] = markup
    set_state(cid, "broadcast_confirm", ctx)
    bot.send_message(cid, "Yuborishni tasdiqlaysizmi?", reply_markup=quick_markup({
        "✅ Ha": {"callback_data": "broadcast_send"}, "❌ Yo‘q": {"callback_data": "broadcast_cancel"}
    }))

def execute_broadcast(chat_id):
    state = get_state(chat_id)
    if not state: return
    ctx = state[1]
    users = db_execute("SELECT user_id FROM users WHERE banned=0", fetch=True)
    total = len(users)
    success = 0
    progress_msg = bot.send_message(chat_id, "0% yuborildi...")
    for i, u in enumerate(users):
        try:
            uid = u["user_id"]
            reply_markup = ctx.get("reply_markup")
            if ctx["content_type"] == "text": bot.send_message(uid, ctx["text"], reply_markup=reply_markup)
            elif ctx["content_type"] == "photo": bot.send_photo(uid, ctx["photo"], caption=ctx.get("caption"), reply_markup=reply_markup)
            elif ctx["content_type"] == "video": bot.send_video(uid, ctx["video"], caption=ctx.get("caption"), reply_markup=reply_markup)
            elif ctx["content_type"] == "audio": bot.send_audio(uid, ctx["audio"], caption=ctx.get("caption"), reply_markup=reply_markup)
            elif ctx["content_type"] == "document": bot.send_document(uid, ctx["document"], caption=ctx.get("caption"), reply_markup=reply_markup)
            success += 1
        except Exception as e:
            logger.error(f"Broadcast error {uid}: {e}")
        if (i+1) % 20 == 0:
            bot.edit_message_text(f"{int((i+1)/total*100)}%", chat_id=progress_msg.chat.id, message_id=progress_msg.message_id)
    bot.edit_message_text(f"✅ {success}/{total} yuborildi.", chat_id=progress_msg.chat.id, message_id=progress_msg.message_id)
    clear_state(chat_id)

@bot.callback_query_handler(func=lambda call: call.data == "broadcast_cancel")
def broadcast_cancel_cb(call):
    clear_state(call.message.chat.id)
    bot.edit_message_text("Bekor qilindi.", call.message.chat.id, call.message.message_id)

# ----------------------------------------------------------------------
# MANDATORY SUBSCRIPTION MANAGEMENT
# ----------------------------------------------------------------------
def show_sub_management(chat_id):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("➕ Kanal qo‘shish", callback_data="sub_add"))
    markup.add(types.InlineKeyboardButton("➖ Kanal o‘chirish", callback_data="sub_remove"))
    markup.add(types.InlineKeyboardButton("📋 Ro‘yxat", callback_data="sub_list"))
    bot.send_message(chat_id, "Majburiy obuna boshqaruvi:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "sub_add")
def sub_add_prompt(call):
    if not is_admin(call.from_user.id): return
    set_state(call.message.chat.id, "sub_add")
    msg = bot.send_message(call.message.chat.id,
        "Kanalni qanday qo‘shasiz?\n"
        "• @username (masalan: @kanal)\n"
        "• Chat ID (masalan: -1001234567890)\n"
        "• Invite link (https://t.me/+ABCDEF)")
    bot.register_next_step_handler(msg, sub_add_process)

def sub_add_process(msg):
    text = msg.text.strip()
    if text.startswith("@"):
        username = text
        db_execute("INSERT INTO channels (channel_username, added_date, added_by) VALUES (?,?,?)",
                   (username, datetime.now().isoformat(), msg.from_user.id))
        bot.send_message(msg.chat.id, f"✅ @{username} qo‘shildi.")
        clear_state(msg.chat.id)
    elif text.startswith("-100") and text[1:].isdigit():
        try:
            chat_id = int(text)
            chat = bot.get_chat(chat_id)
            db_execute("INSERT INTO channels (chat_id, added_date, added_by) VALUES (?,?,?)",
                       (chat_id, datetime.now().isoformat(), msg.from_user.id))
            bot.send_message(msg.chat.id, f"✅ Maxfiy kanal (ID: {chat_id}) qo‘shildi.")
            clear_state(msg.chat.id)
        except:
            bot.send_message(msg.chat.id, "Chat topilmadi yoki bot a'zo emas.")
    elif text.startswith("https://t.me/+"):
        invite_link = text
        set_state(msg.chat.id, "sub_add_chatid", {"invite_link": invite_link})
        bot.send_message(msg.chat.id, "Iltimos, shu kanalning Chat ID sini kiriting (masalan: -1001234567890):")
        bot.register_next_step_handler(msg, sub_add_chatid)
    else:
        bot.send_message(msg.chat.id, "Noto‘g‘ri format.")
        bot.register_next_step_handler(msg, sub_add_process)

def sub_add_chatid(msg):
    text = msg.text.strip()
    state = get_state(msg.chat.id)
    if not state or state[0] != "sub_add_chatid":
        return
    if not text.startswith("-100"):
        bot.send_message(msg.chat.id, "Noto‘g‘ri Chat ID. Qayta urinib ko‘ring.")
        bot.register_next_step_handler(msg, sub_add_chatid)
        return
    try:
        chat_id = int(text)
        chat = bot.get_chat(chat_id)
    except:
        bot.send_message(msg.chat.id, "Chat topilmadi yoki bot a'zo emas.")
        return
    invite_link = state[1].get("invite_link", "")
    db_execute("INSERT INTO channels (chat_id, invite_link, added_date, added_by) VALUES (?,?,?,?)",
               (chat_id, invite_link, datetime.now().isoformat(), msg.from_user.id))
    bot.send_message(msg.chat.id, "✅ Kanal qo‘shildi.")
    clear_state(msg.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "sub_remove")
def sub_remove_list(call):
    channels = db_execute("SELECT id, channel_username, chat_id, invite_link FROM channels", fetch=True)
    if not channels:
        bot.answer_callback_query(call.id, "Kanal yo‘q", show_alert=True); return
    markup = types.InlineKeyboardMarkup()
    for ch in channels:
        if ch["channel_username"]:
            label = ch["channel_username"]
        elif ch["chat_id"]:
            label = f"ID: {ch['chat_id']}"
        else:
            label = "Noma'lum"
        markup.add(types.InlineKeyboardButton(label, callback_data=f"sub_del:{ch['id']}"))
    bot.edit_message_text("O‘chirish uchun kanalni tanlang:", call.message.chat.id, call.message.message_id, reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("sub_del:"))
def sub_del(call):
    ch_id = int(call.data.split(":")[1])
    db_execute("DELETE FROM channels WHERE id=?", (ch_id,))
    bot.edit_message_text("✅ Kanal o‘chirildi.", call.message.chat.id, call.message.message_id)

@bot.callback_query_handler(func=lambda call: call.data == "sub_list")
def sub_list(call):
    channels = db_execute("SELECT channel_username, chat_id, invite_link FROM channels", fetch=True)
    if not channels:
        txt = "Kanal yo‘q."
    else:
        txt = "📋 Majburiy kanallar:\n"
        for ch in channels:
            if ch["channel_username"]:
                txt += f"• {ch['channel_username']}\n"
            elif ch["chat_id"]:
                txt += f"• Maxfiy kanal (ID: {ch['chat_id']})\n"
    bot.edit_message_text(txt, call.message.chat.id, call.message.message_id)

# ----------------------------------------------------------------------
# CONTEST MANAGEMENT
# ----------------------------------------------------------------------
def show_contest_panel(chat_id):
    active = get_setting("contest_active") == "1"
    end = get_setting("contest_end") or "belgilanmagan"
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("⏸ To‘xtatish" if active else "✅ Boshlash", callback_data="contest_toggle"),
        types.InlineKeyboardButton("⏳ Muddat belgilash", callback_data="contest_set_duration"),
        types.InlineKeyboardButton("🔢 Referal balli", callback_data="contest_ref_points"),
        types.InlineKeyboardButton("🏁 Yakunlash", callback_data="contest_finish")
    )
    bot.send_message(chat_id, f"🏆 Konkurs\nFaol: {active}\nTugash: {end}", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "contest_toggle")
def contest_toggle(call):
    if not is_admin(call.from_user.id): return
    curr = get_setting("contest_active") == "1"
    set_setting("contest_active", "0" if curr else "1")
    if not curr and not get_setting("contest_end"):
        set_setting("contest_end", (datetime.now() + timedelta(days=7)).isoformat())
    bot.answer_callback_query(call.id, "Holat o‘zgartirildi.")
    show_contest_panel(call.message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "contest_set_duration")
def contest_set_duration(call):
    if not is_admin(call.from_user.id): return
    set_state(call.message.chat.id, "contest_duration")
    msg = bot.send_message(call.message.chat.id, "Konkurs davomiyligi (kun):")
    bot.register_next_step_handler(msg, process_contest_duration)

def process_contest_duration(msg):
    try:
        days = int(msg.text)
    except:
        bot.send_message(msg.chat.id, "Son kiriting."); clear_state(msg.chat.id); return
    end = datetime.now() + timedelta(days=days)
    set_setting("contest_end", end.isoformat())
    if get_setting("contest_active") != "1":
        set_setting("contest_active", "1")
    bot.send_message(msg.chat.id, f"✅ Konkurs {days} kunga belgilandi. Tugash: {end.strftime('%Y-%m-%d %H:%M')}")
    clear_state(msg.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "contest_ref_points")
def contest_ref_points(call):
    set_state(call.message.chat.id, "contest_ref_pts")
    msg = bot.send_message(call.message.chat.id, "Yangi referal balli:")
    bot.register_next_step_handler(msg, process_contest_ref_pts)

def process_contest_ref_pts(msg):
    try:
        pts = int(msg.text)
    except:
        bot.send_message(msg.chat.id, "Son kiriting."); return
    set_setting("contest_referral_points", str(pts))
    bot.send_message(msg.chat.id, f"✅ Referal balli: {pts}")
    clear_state(msg.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "contest_finish")
def contest_finish(call):
    set_setting("contest_active", "0")
    set_setting("contest_end", "")
    bot.edit_message_text("🏁 Konkurs yakunlandi.", call.message.chat.id, call.message.message_id)

# ----------------------------------------------------------------------
# PRIZES ADMIN
# ----------------------------------------------------------------------
def show_prizes_admin(chat_id):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("➕ Qo‘shish", callback_data="prize_add"))
    prizes = db_execute("SELECT id, title, winners_count FROM prizes ORDER BY sort_order", fetch=True)
    if prizes:
        for p in prizes:
            wc = f" ({p['winners_count']} ta)" if p.get("winners_count", 0) > 0 else ""
            markup.add(types.InlineKeyboardButton(f"✏️ {p['title']}{wc}", callback_data=f"prize_edit_select:{p['id']}"))
            markup.add(types.InlineKeyboardButton(f"🗑 {p['title']}{wc}", callback_data=f"prize_delete:{p['id']}"))
    bot.send_message(chat_id, "🎁 Sovrinlar boshqaruvi", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "prize_add")
def prize_add_start(call):
    set_state(call.message.chat.id, "prize_add", {"step": "title"})
    msg = bot.send_message(call.message.chat.id, "Sovrin nomi:")
    bot.register_next_step_handler(msg, prize_title)

def prize_title(msg):
    st = get_state(msg.chat.id)
    st[1]["title"] = msg.text; st[1]["step"] = "winners"
    set_state(msg.chat.id, "prize_add", st[1])
    msg = bot.send_message(msg.chat.id, "Nechta g‘olibga beriladi? (raqam yoki 0):")
    bot.register_next_step_handler(msg, prize_winners)

def prize_winners(msg):
    try:
        wc = int(msg.text)
    except:
        bot.send_message(msg.chat.id, "Son kiriting."); return
    st = get_state(msg.chat.id)
    st[1]["winners_count"] = wc; st[1]["step"] = "desc"
    set_state(msg.chat.id, "prize_add", st[1])
    msg = bot.send_message(msg.chat.id, "Tavsif:")
    bot.register_next_step_handler(msg, prize_desc)

def prize_desc(msg):
    st = get_state(msg.chat.id)
    st[1]["description"] = msg.text; st[1]["step"] = "image"
    set_state(msg.chat.id, "prize_add", st[1])
    msg = bot.send_message(msg.chat.id, "Rasm yuboring yoki /skip:")
    bot.register_next_step_handler(msg, prize_image)

def prize_image(msg):
    st = get_state(msg.chat.id)
    if msg.content_type == "photo":
        st[1]["image"] = msg.photo[-1].file_id
    elif msg.text == "/skip":
        st[1]["image"] = None
    else:
        bot.send_message(msg.chat.id, "Rasm yoki /skip.")
        bot.register_next_step_handler(msg, prize_image)
        return
    db_execute("INSERT INTO prizes (title, description, image_file_id, winners_count) VALUES (?,?,?,?)",
               (st[1]["title"], st[1]["description"], st[1].get("image"), st[1].get("winners_count", 0)))
    bot.send_message(msg.chat.id, "✅ Sovrin qo‘shildi.")
    clear_state(msg.chat.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("prize_edit_select:"))
def prize_edit_select(call):
    pid = int(call.data.split(":")[1])
    prize = db_execute("SELECT * FROM prizes WHERE id=?", (pid,), fetchone=True)
    if not prize:
        bot.answer_callback_query(call.id, "Topilmadi"); return
    set_state(call.message.chat.id, "prize_edit", {"id": pid, "step": "title"})
    msg = bot.send_message(call.message.chat.id, f"Yangi nom (hozir: {prize['title']}):")
    bot.register_next_step_handler(msg, prize_edit_title)

def prize_edit_title(msg):
    st = get_state(msg.chat.id)
    st[1]["title"] = msg.text; st[1]["step"] = "winners"
    set_state(msg.chat.id, "prize_edit", st[1])
    msg = bot.send_message(msg.chat.id, "Yangi g‘oliblar soni:")
    bot.register_next_step_handler(msg, prize_edit_winners)

def prize_edit_winners(msg):
    try:
        wc = int(msg.text)
    except:
        bot.send_message(msg.chat.id, "Son kiriting."); return
    st = get_state(msg.chat.id)
    st[1]["winners_count"] = wc; st[1]["step"] = "desc"
    set_state(msg.chat.id, "prize_edit", st[1])
    msg = bot.send_message(msg.chat.id, "Yangi tavsif:")
    bot.register_next_step_handler(msg, prize_edit_desc)

def prize_edit_desc(msg):
    st = get_state(msg.chat.id)
    st[1]["description"] = msg.text; st[1]["step"] = "image"
    set_state(msg.chat.id, "prize_edit", st[1])
    msg = bot.send_message(msg.chat.id, "Yangi rasm yoki /skip:")
    bot.register_next_step_handler(msg, prize_edit_image)

def prize_edit_image(msg):
    st = get_state(msg.chat.id)
    img = None
    if msg.content_type == "photo":
        img = msg.photo[-1].file_id
    elif msg.text != "/skip":
        bot.send_message(msg.chat.id, "Rasm yoki /skip.")
        bot.register_next_step_handler(msg, prize_edit_image)
        return
    db_execute("UPDATE prizes SET title=?, description=?, image_file_id=?, winners_count=? WHERE id=?",
               (st[1]["title"], st[1]["description"], img, st[1].get("winners_count", 0), st[1]["id"]))
    bot.send_message(msg.chat.id, "✅ Sovrin yangilandi.")
    clear_state(msg.chat.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("prize_delete:"))
def prize_delete(call):
    pid = int(call.data.split(":")[1])
    db_execute("DELETE FROM prizes WHERE id=?", (pid,))
    bot.edit_message_text("🗑 Sovrin o‘chirildi.", call.message.chat.id, call.message.message_id)

# ----------------------------------------------------------------------
# CONTEST RULES ADMIN
# ----------------------------------------------------------------------
def manage_contest_rules(chat_id):
    rules = db_execute("SELECT id, rule_text FROM contest_rules ORDER BY id", fetch=True)
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("➕ Shart qo‘shish", callback_data="rule_add"))
    if rules:
        for r in rules:
            short = r["rule_text"][:30] + "..." if len(r["rule_text"]) > 30 else r["rule_text"]
            markup.add(types.InlineKeyboardButton(f"🗑 {short}", callback_data=f"rule_del:{r['id']}"))
    bot.send_message(chat_id, "📜 Konkurs shartlari boshqaruvi", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "rule_add")
def rule_add_prompt(call):
    if not is_admin(call.from_user.id): return
    set_state(call.message.chat.id, "rule_add")
    msg = bot.send_message(call.message.chat.id, "Yangi shart matnini yozing:")
    bot.register_next_step_handler(msg, rule_add_process)

def rule_add_process(msg):
    db_execute("INSERT INTO contest_rules (rule_text) VALUES (?)", (msg.text,))
    bot.send_message(msg.chat.id, "✅ Shart qo‘shildi.")
    clear_state(msg.chat.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("rule_del:"))
def rule_del(call):
    if not is_admin(call.from_user.id): return
    rid = int(call.data.split(":")[1])
    db_execute("DELETE FROM contest_rules WHERE id=?", (rid,))
    bot.edit_message_text("🗑 Shart o‘chirildi.", call.message.chat.id, call.message.message_id)

# ----------------------------------------------------------------------
# RATING PANEL
# ----------------------------------------------------------------------
def show_rating_panel(chat_id):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("TOP 10", callback_data="rating_top10"))
    markup.add(types.InlineKeyboardButton("TOP 100", callback_data="rating_top100"))
    bot.send_message(chat_id, "🏅 Reyting", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data in ["rating_top10", "rating_top100"])
def rating_show_cb(call):
    limit = 10 if call.data == "rating_top10" else 100
    top = get_top_users(limit)
    txt = "\n".join(f"{i}. {u['first_name']} – {u['points']}" for i,u in enumerate(top,1))
    bot.send_message(call.message.chat.id, txt[:4096])
    bot.answer_callback_query(call.id)

# ----------------------------------------------------------------------
# SETTINGS
# ----------------------------------------------------------------------
def settings_menu(chat_id):
    markup = types.InlineKeyboardMarkup(row_width=2)
    items = [
        ("Bot nomi", "set_bot_name"),
        ("Start xabari", "set_start_msg"),
        ("Referal balli", "set_ref_points"),
        ("Konkurs tugash sanasi (YYYY-MM-DD HH:MM)", "set_contest_end"),
        ("Admin username", "set_admin_username")
    ]
    for label, cb in items:
        markup.add(types.InlineKeyboardButton(label, callback_data=cb))
    bot.send_message(chat_id, "⚙️ Sozlamalar", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("set_"))
def settings_cb(call):
    key_map = {
        "set_bot_name": "bot_name",
        "set_start_msg": "start_message",
        "set_ref_points": "referral_points",
        "set_contest_end": "contest_end",
        "set_admin_username": "admin_username"
    }
    key = key_map.get(call.data)
    if not key: return
    set_state(call.message.chat.id, "settings_edit", {"key": key})
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, f"Yangi qiymatni kiriting ({key}):")
    bot.register_next_step_handler(msg, process_setting)

def process_setting(msg):
    st = get_state(msg.chat.id)
    if not st: return
    key = st[1]["key"]
    set_setting(key, msg.text)
    bot.send_message(msg.chat.id, f"✅ {key} yangilandi.")
    clear_state(msg.chat.id)

# ----------------------------------------------------------------------
# ADMINS MANAGEMENT
# ----------------------------------------------------------------------
def admin_management(chat_id):
    if not is_super_admin(chat_id):
        bot.send_message(chat_id, "Faqat superadmin boshqara oladi.")
        return
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("➕ Qo‘shish", callback_data="admin_add"))
    markup.add(types.InlineKeyboardButton("➖ O‘chirish", callback_data="admin_remove_list"))
    bot.send_message(chat_id, "🔒 Adminlar", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data == "admin_add")
def admin_add_prompt(call):
    if not is_super_admin(call.from_user.id): return
    set_state(call.message.chat.id, "admin_add_id")
    msg = bot.send_message(call.message.chat.id, "Yangi admin ID:")
    bot.register_next_step_handler(msg, admin_add_process)

def admin_add_process(msg):
    try:
        uid = int(msg.text)
    except:
        bot.send_message(msg.chat.id, "Noto‘g‘ri ID."); return
    db_execute("INSERT OR IGNORE INTO admins (user_id, added_by, date) VALUES (?,?,?)",
               (uid, msg.from_user.id, datetime.now().isoformat()))
    bot.send_message(msg.chat.id, f"✅ Admin qo‘shildi: {uid}")
    clear_state(msg.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "admin_remove_list")
def admin_remove_list(call):
    if not is_super_admin(call.from_user.id): return
    admins = db_execute("SELECT user_id FROM admins WHERE user_id != ?", (SUPER_ADMIN_ID,), fetch=True)
    if not admins:
        bot.answer_callback_query(call.id, "Boshqa admin yo‘q", show_alert=True); return
    markup = types.InlineKeyboardMarkup()
    for a in admins:
        markup.add(types.InlineKeyboardButton(str(a["user_id"]), callback_data=f"admin_rm:{a['user_id']}"))
    bot.send_message(call.message.chat.id, "O‘chirish:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_rm:"))
def admin_rm(call):
    if not is_super_admin(call.from_user.id): return
    uid = int(call.data.split(":")[1])
    db_execute("DELETE FROM admins WHERE user_id=?", (uid,))
    bot.edit_message_text(f"✅ {uid} admin o‘chirildi.", call.message.chat.id, call.message.message_id)

# ----------------------------------------------------------------------
# SUBSCRIPTION CHECK CALLBACK
# ----------------------------------------------------------------------
@bot.callback_query_handler(func=lambda call: call.data == "check_subscription")
def check_sub_cb(call):
    if check_subscription(call.message.chat.id):
        bot.delete_message(call.message.chat.id, call.message.message_id)
        bot.send_message(call.message.chat.id, "✅ Tasdiqlandi!", reply_markup=main_menu_keyboard(is_admin(call.message.chat.id)))
    else:
        bot.answer_callback_query(call.id, "Hali obuna bo‘lmagansiz.", show_alert=True)

# ----------------------------------------------------------------------
# CANCEL
# ----------------------------------------------------------------------
@bot.message_handler(commands=["cancel"])
def cancel_cmd(msg):
    clear_state(msg.chat.id)
    bot.send_message(msg.chat.id, "Bekor qilindi.", reply_markup=main_menu_keyboard(is_admin(msg.chat.id)))

# ----------------------------------------------------------------------
# FALLBACK
# ----------------------------------------------------------------------
@bot.message_handler(func=lambda m: True)
def fallback(msg):
    if get_state(msg.chat.id):
        bot.send_message(msg.chat.id, "Amal bajarilmoqda. /cancel bilan bekor qiling.")
    else:
        bot.send_message(msg.chat.id, "Menyudan foydalaning.", reply_markup=main_menu_keyboard(is_admin(msg.chat.id)))

# ----------------------------------------------------------------------
# FLASK WEBHOOK ROUTE
# ----------------------------------------------------------------------
@app.route('/webhook', methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return 'ok', 200
    else:
        abort(403)

@app.route('/')
def index():
    return "Bot is running", 200

# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------
if __name__ == "__main__":
    init_database()
    if WEBHOOK_URL:
        bot.remove_webhook()
        bot.set_webhook(url=f"{WEBHOOK_URL}/webhook", allowed_updates=["message", "callback_query", "chat_join_request"])
        logger.info(f"Webhook set to {WEBHOOK_URL}/webhook")
    else:
        logger.warning("WEBHOOK_URL not set. Webhook won't work.")
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
