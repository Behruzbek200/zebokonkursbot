#!/usr/bin/env python3
"""
Professional Telegram Bot – Webhook version (Render)
Single file: bot.py | SQLite | pyTelegramBotAPI | Python 3.12+
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
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")  # Render taqdim etgan URL (masalan: https://your-app.onrender.com)

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
            channel_username TEXT UNIQUE,
            added_date TEXT,
            added_by INTEGER
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

    # Auto‑migration: add winners_count if missing
    try:
        cur.execute("SELECT winners_count FROM prizes LIMIT 1")
    except sqlite3.OperationalError:
        cur.execute("ALTER TABLE prizes ADD COLUMN winners_count INTEGER DEFAULT 0")
        logger.info("Migration: added winners_count column.")

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
        try:
            cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
        except Exception as e:
            logger.error(f"Default setting {k}: {e}")

    try:
        cur.execute("INSERT OR IGNORE INTO admins (user_id, added_by, date) VALUES (?, ?, ?)",
                    (SUPER_ADMIN_ID, SUPER_ADMIN_ID, datetime.now().isoformat()))
    except Exception as e:
        logger.error(f"Super admin insert: {e}")

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
# SUBSCRIPTION CHECK
# ----------------------------------------------------------------------
def get_required_channels() -> List[str]:
    rows = db_execute("SELECT channel_username FROM channels", fetch=True)
    return [r["channel_username"] for r in rows] if rows else []

def check_subscription(user_id: int) -> bool:
    for ch in get_required_channels():
        try:
            member = bot.get_chat_member(ch, user_id)
            if member.status not in ["creator", "administrator", "member"]:
                return False
        except:
            return False
    return True

def send_subscription_prompt(chat_id: int):
    channels = get_required_channels()
    if not channels:
        return
    text = "🔔 Botdan foydalanish uchun kanallarga obuna bo‘ling:\n\n"
    markup = types.InlineKeyboardMarkup(row_width=1)
    for ch in channels:
        name = ch.replace("@", "")
        text += f"👉 @{name}\n"
        markup.add(types.InlineKeyboardButton(f"➕ @{name}", url=f"https://t.me/{name}"))
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

# ----------------------------------------------------------------------
# Subscription middleware
# ----------------------------------------------------------------------
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
# (Barcha yordamchi funksiyalar – profil, referal, sovrinlar, admin, etc.)
# Kodni ixchamlashtirish uchun bu yerga toʻliq funksiyalarni qoʻshmayapman,
# lekin sizda mavjud boʻlgan barcha funksiyalar avvalgidek qoladi.
# Ular orasida show_profile, show_referral_link, show_prizes, show_prizes_admin,
# manage_contest_rules, start_broadcast, support_start, ... mavjud.
# <... FUNKSIYALARNING TOʻLIQ KODI ...>
# ----------------------------------------------------------------------

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
# MAIN (webhook o'rnatish)
# ----------------------------------------------------------------------
if __name__ == "__main__":
    init_database()
    # Webhookni o'rnatish
    if WEBHOOK_URL:
        bot.remove_webhook()
        bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
        logger.info(f"Webhook set to {WEBHOOK_URL}/webhook")
    else:
        logger.warning("WEBHOOK_URL muhit oʻzgaruvchisi oʻrnatilmagan. Webhook ishlamaydi.")
    # Flask serverni ishga tushirish
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
