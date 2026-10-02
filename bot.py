import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
    Message, ReplyKeyboardMarkup, WebAppInfo
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("wheelbot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_USERNAME = os.getenv("BOT_USERNAME", "").strip().lstrip("@").strip()
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/+CGRBOztPG64wMWMy").strip()
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
DB_PATH = os.getenv("DB_PATH", "bot.db").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().lstrip("-").isdigit()}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")
if not BOT_USERNAME:
    raise RuntimeError("BOT_USERNAME is required")
if not CHANNEL_ID:
    raise RuntimeError("CHANNEL_ID is required")
if not RENDER_EXTERNAL_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL is required")

WEBAPP_URL = f"{RENDER_EXTERNAL_URL}/web/"
WEBHOOK_PATH = "/telegram/webhook"
WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"

DEFAULT_SETTINGS = {
    "chance_10": "50",
    "chance_20": "35",
    "chance_30": "12",
    "chance_50": "3",
    "refs_per_spin": "5",
    "min_withdraw": "50",
    "welcome_text": "👋 Вітаю! Підпишись на канал, щоб отримати доступ до бота.",
    "subscription_text": "📢 Щоб користуватися ботом, спочатку підпишись на канал.",
    "menu_text": "🎉 Головне меню\n\nОбирай потрібну дію нижче:",
    "ref_text": "🔗 Твоє реферальне посилання:\n{link}\n\n👥 Підтверджених рефералів: {refs}\n🎡 Spins: {spins}",
    "wheel_text": "🎡 Крути колесо та отримуй виграш. Результат визначається сервером.",
    "win_text": "🎉 Ти виграв {reward} грн!",
    "rules_text": "📜 Правила\n\n• Реферал зараховується лише після підтвердження підписки.\n• Самореферали заборонені.\n• За кожні {refs_per_spin} підтверджених рефералів — 1 безкоштовний spin.\n• Мінімальний вивід — {min_withdraw} грн.\n• Виграш рулетки визначає сервер.",
    "withdraw_text": "💰 Мінімальна сума виводу: {min_withdraw} грн.\nТвій доступний баланс: {balance} грн.",
    "earn_text": "📈 Заробити більше\n\nЗапрошуй друзів за своїм реферальним посиланням. За кожні {refs_per_spin} підтверджених рефералів отримуєш 1 spin.",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL UNIQUE,
    username TEXT,
    first_name TEXT,
    referrer_id INTEGER,
    subscribed INTEGER NOT NULL DEFAULT 0,
    referral_confirmed INTEGER NOT NULL DEFAULT 0,
    spins INTEGER NOT NULL DEFAULT 0,
    balance REAL NOT NULL DEFAULT 0,
    total_won REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    blocked INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(referrer_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id);
CREATE INDEX IF NOT EXISTS idx_users_referrer ON users(referrer_id);
CREATE INDEX IF NOT EXISTS idx_users_created ON users(created_at);
CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id INTEGER NOT NULL,
    referred_user_id INTEGER NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY(referrer_id) REFERENCES users(id),
    FOREIGN KEY(referred_user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);
CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    payment_details TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    processed_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals(status);
CREATE TABLE IF NOT EXISTS spins_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    reward REAL NOT NULL,
    request_id TEXT UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_spins_user ON spins_history(user_id);
CREATE TABLE IF NOT EXISTS spin_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    reward REAL,
    balance_after REAL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

class WithdrawalStates(StatesGroup):
    details = State()

class AdminStates(StatesGroup):
    roulette = State()
    text = State()
    channel = State()
    find_user = State()
    broadcast = State()
    withdraw_reject = State()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def money(v: float) -> str:
    return f"{v:.2f}"


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


class DB:
    def __init__(self, path: str):
        self.path = path
        self.lock = asyncio.Lock()

    async def init(self):
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            for k, v in DEFAULT_SETTINGS.items():
                await db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
            await db.commit()

    async def get_setting(self, key: str, default: str = "") -> str:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
            row = await cur.fetchone()
            return row[0] if row else default

    async def set_setting(self, key: str, value: str):
        async with self.lock:
            async with aiosqlite.connect(self.path) as db:
                await db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
                await db.commit()

    async def settings(self):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT key,value FROM settings")
            return dict(await cur.fetchall())

    async def ensure_user(self, tg_user, ref_arg: str | None = None):
        async with self.lock:
            async with aiosqlite.connect(self.path) as db:
                cur = await db.execute("SELECT id, referrer_id, referral_confirmed, subscribed, blocked FROM users WHERE telegram_id=?", (tg_user.id,))
                row = await cur.fetchone()
                if row:
                    await db.execute("UPDATE users SET username=?, first_name=? WHERE telegram_id=?", (tg_user.username, tg_user.first_name, tg_user.id))
                    await db.commit()
                    return row
                referrer_db_id = None
                if ref_arg and ref_arg.startswith("ref_"):
                    try:
                        ref_tg_id = int(ref_arg[4:])
                    except ValueError:
                        ref_tg_id = None
                    if ref_tg_id and ref_tg_id != tg_user.id:
                        cur = await db.execute("SELECT id FROM users WHERE telegram_id=?", (ref_tg_id,))
                        rr = await cur.fetchone()
                        if rr:
                            referrer_db_id = rr[0]
                cur = await db.execute("INSERT INTO users(telegram_id,username,first_name,referrer_id,created_at) VALUES(?,?,?,?,?)", (tg_user.id, tg_user.username, tg_user.first_name, referrer_db_id, now_iso()))
                uid = cur.lastrowid
                await db.commit()
                return (uid, referrer_db_id, 0, 0, 0)

    async def user_by_tg(self, tg_id: int):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM users WHERE telegram_id=?", (tg_id,))
            return await cur.fetchone()

    async def activate_subscription(self, tg_id: int):
        refs_per_spin = int(await self.get_setting("refs_per_spin", "5"))
        async with self.lock:
            async with aiosqlite.connect(self.path) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute("SELECT * FROM users WHERE telegram_id=?", (tg_id,))
                u = await cur.fetchone()
                if not u:
                    return None, 0
                await db.execute("UPDATE users SET subscribed=1 WHERE id=?", (u["id"],))
                added_spins = 0
                if u["referrer_id"] and not u["referral_confirmed"] and u["referrer_id"] != u["id"]:
                    cur = await db.execute("SELECT 1 FROM referrals WHERE referred_user_id=?", (u["id"],))
                    exists = await cur.fetchone()
                    if not exists:
                        await db.execute("INSERT INTO referrals(referrer_id,referred_user_id,created_at) VALUES(?,?,?)", (u["referrer_id"], u["id"], now_iso()))
                        await db.execute("UPDATE users SET referral_confirmed=1 WHERE id=?", (u["id"],))
                        cur = await db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (u["referrer_id"],))
                        count = (await cur.fetchone())[0]
                        if refs_per_spin > 0 and count % refs_per_spin == 0:
                            await db.execute("UPDATE users SET spins=spins+1 WHERE id=?", (u["referrer_id"],))
                            added_spins = 1
                await db.commit()
                return await self.user_by_tg(tg_id), added_spins

    async def count_refs(self, user_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (user_id,))
            return (await cur.fetchone())[0]

    async def create_withdrawal(self, tg_id: int, details: str):
        min_withdraw = float(await self.get_setting("min_withdraw", "50"))
        async with self.lock:
            async with aiosqlite.connect(self.path) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute("SELECT * FROM users WHERE telegram_id=?", (tg_id,))
                u = await cur.fetchone()
                if not u:
                    return False, "Користувача не знайдено."
                if u["balance"] < min_withdraw:
                    return False, f"Мінімальна сума виводу — {money(min_withdraw)} грн."
                amount = float(u["balance"])
                await db.execute("UPDATE users SET balance=0 WHERE id=? AND balance>=?", (u["id"], amount))
                cur = await db.execute("SELECT changes()")
                changed = (await cur.fetchone())[0]
                if changed != 1:
                    return False, "Не вдалося заблокувати баланс. Спробуйте ще раз."
                cur = await db.execute("INSERT INTO withdrawals(user_id,amount,payment_details,status,created_at) VALUES(?,?,?,?,?)", (u["id"], amount, details, "pending", now_iso()))
                wid = cur.lastrowid
                await db.commit()
                return True, (wid, amount)

    async def process_withdrawal(self, wid: int, status: str):
        async with self.lock:
            async with aiosqlite.connect(self.path) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute("SELECT * FROM withdrawals WHERE id=?", (wid,))
                w = await cur.fetchone()
                if not w or w["status"] != "pending":
                    return None
                if status == "rejected":
                    await db.execute("UPDATE users SET balance=balance+? WHERE id=?", (w["amount"], w["user_id"]))
                await db.execute("UPDATE withdrawals SET status=?,processed_at=? WHERE id=? AND status='pending'", (status, now_iso(), wid))
                await db.commit()
                cur = await db.execute("SELECT telegram_id FROM users WHERE id=?", (w["user_id"],))
                user = await cur.fetchone()
                return dict(w), user[0] if user else None

    async def get_pending_withdrawals(self):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT w.*,u.telegram_id,u.username,u.first_name FROM withdrawals w JOIN users u ON u.id=w.user_id WHERE w.status='pending' ORDER BY w.id")
            return await cur.fetchall()

    async def stats(self):
        async with aiosqlite.connect(self.path) as db:
            async def one(q, args=()):
                cur = await db.execute(q, args); return (await cur.fetchone())[0]
            today = datetime.now(timezone.utc).date().isoformat()
            return {
                "users": await one("SELECT COUNT(*) FROM users"),
                "subscribed": await one("SELECT COUNT(*) FROM users WHERE subscribed=1"),
                "today": await one("SELECT COUNT(*) FROM users WHERE created_at LIKE ?", (today+"%",)),
                "refs": await one("SELECT COUNT(*) FROM referrals"),
                "spins": await one("SELECT COUNT(*) FROM spins_history"),
                "won": await one("SELECT COALESCE(SUM(reward),0) FROM spins_history"),
                "balances": await one("SELECT COALESCE(SUM(balance),0) FROM users"),
                "pending": await one("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status='pending'"),
                "paid": await one("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status='paid'"),
                "rejected": await one("SELECT COALESCE(SUM(amount),0) FROM withdrawals WHERE status='rejected'"),
            }

    async def admin_users(self, limit=30):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT telegram_id,username,first_name,subscribed,spins,balance,blocked,created_at FROM users ORDER BY id DESC LIMIT ?", (limit,))
            return await cur.fetchall()

    async def find_user(self, query: str):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            if query.isdigit():
                cur = await db.execute("SELECT * FROM users WHERE telegram_id=?", (int(query),))
            else:
                cur = await db.execute("SELECT * FROM users WHERE username LIKE ? ORDER BY id DESC LIMIT 10", ("%"+query.lstrip("@")+"%",))
            return await cur.fetchall()

    async def set_blocked(self, tg_id: int, value: bool):
        async with self.lock:
            async with aiosqlite.connect(self.path) as db:
                await db.execute("UPDATE users SET blocked=? WHERE telegram_id=?", (int(value), tg_id)); await db.commit()

    async def all_recipients(self):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT telegram_id FROM users WHERE blocked=0")
            return [r[0] for r in await cur.fetchall()]

    async def perform_spin(self, tg_id: int, request_id: str):
        settings = await self.settings()
        chances = {10: float(settings["chance_10"]),20: float(settings["chance_20"]),30: float(settings["chance_30"]),50: float(settings["chance_50"])}
        total = sum(chances.values())
        if abs(total - 100) > 1e-9:
            raise ValueError("Некоректна конфігурація шансів")
        async with self.lock:
            async with aiosqlite.connect(self.path) as db:
                db.row_factory = aiosqlite.Row
                cur = await db.execute("SELECT * FROM users WHERE telegram_id=?", (tg_id,))
                u = await cur.fetchone()
                if not u:
                    raise PermissionError("Користувача не знайдено")
                cur = await db.execute("SELECT * FROM spin_requests WHERE request_id=?", (request_id,))
                existing = await cur.fetchone()
                if existing and existing["user_id"] != u["id"]:
                    raise PermissionError("Недійсний request_id")
                if existing and existing["reward"] is not None:
                    return float(existing["reward"]), float(existing["balance_after"])
                if u["blocked"]:
                    raise PermissionError("Акаунт заблоковано")
                if not u["subscribed"]:
                    raise PermissionError("Підпишись на канал")
                # Reserve the request id first. A duplicate request can never consume another spin.
                try:
                    await db.execute("INSERT INTO spin_requests(user_id,request_id,created_at) VALUES(?,?,?)", (u["id"], request_id, now_iso()))
                except aiosqlite.IntegrityError:
                    cur = await db.execute("SELECT reward,balance_after FROM spin_requests WHERE request_id=?", (request_id,)); e=await cur.fetchone()
                    if e and e[0] is not None: return float(e[0]), float(e[1])
                    raise RuntimeError("Повторний запит ще обробляється")
                cur = await db.execute("UPDATE users SET spins=spins-1 WHERE id=? AND spins>0", (u["id"],))
                cur = await db.execute("SELECT changes()")
                if (await cur.fetchone())[0] != 1:
                    await db.execute("DELETE FROM spin_requests WHERE request_id=?", (request_id,)); await db.commit()
                    raise PermissionError("У тебе немає доступних spin")
                r = random.random() * 100
                cumulative = 0
                reward = 50
                for amount, chance in chances.items():
                    cumulative += chance
                    if r < cumulative:
                        reward = amount; break
                await db.execute("UPDATE users SET balance=balance+?, total_won=total_won+? WHERE id=?", (reward,reward,u["id"]))
                cur = await db.execute("SELECT balance FROM users WHERE id=?", (u["id"],)); balance_after=float((await cur.fetchone())[0])
                await db.execute("INSERT INTO spins_history(user_id,reward,request_id,created_at) VALUES(?,?,?,?)", (u["id"],reward,request_id,now_iso()))
                await db.execute("UPDATE spin_requests SET reward=?,balance_after=? WHERE request_id=?", (reward,balance_after,request_id))
                await db.commit()
                return float(reward), balance_after


db = DB(DB_PATH)
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
router = Router()
dp.include_router(router)


def menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="👤 Мій кабінет"), KeyboardButton(text="🎡 Крутити колесо")],
        [KeyboardButton(text="🔗 Моє реферальне посилання"), KeyboardButton(text="💰 Вивід коштів")],
        [KeyboardButton(text="📈 Заробити більше"), KeyboardButton(text="📜 Правила")],
    ], resize_keyboard=True)


async def subscribe_kb():
    url = await db.get_setting("channel_url", CHANNEL_URL)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Підписатися на канал", url=url)],
        [InlineKeyboardButton(text="✅ Я підписався", callback_data="check_sub")],
    ])


def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats"), InlineKeyboardButton(text="👥 Користувачі", callback_data="adm:users")],
        [InlineKeyboardButton(text="💰 Виплати", callback_data="adm:withdrawals"), InlineKeyboardButton(text="🎡 Налаштування рулетки", callback_data="adm:roulette")],
        [InlineKeyboardButton(text="📢 Налаштування каналу", callback_data="adm:channel"), InlineKeyboardButton(text="✏️ Змінити тексти", callback_data="adm:texts")],
        [InlineKeyboardButton(text="🔎 Знайти користувача", callback_data="adm:find"), InlineKeyboardButton(text="🚫 Заблоковані", callback_data="adm:blocked")],
        [InlineKeyboardButton(text="📣 Розсилка", callback_data="adm:broadcast")],
    ])


def withdraw_admin_kb(wid: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Позначити PAID", callback_data=f"wd:paid:{wid}"), InlineKeyboardButton(text="❌ REJECTED", callback_data=f"wd:rejected:{wid}")]
    ])


def wheel_button():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎡 Відкрити рулетку", web_app=WebAppInfo(url=WEBAPP_URL))]])


async def subscribed(tg_id: int) -> bool:
    try:
        channel_id = await db.get_setting("channel_id", CHANNEL_ID)
        m = await bot.get_chat_member(channel_id, tg_id)
        return m.status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR} or bool(getattr(m, "is_member", False))
    except Exception as e:
        log.warning("get_chat_member failed for %s: %s", tg_id, e)
        return False


async def show_access(message: Message, ref_arg: str | None = None):
    await db.ensure_user(message.from_user, ref_arg)
    u = await db.user_by_tg(message.from_user.id)
    if u["blocked"]:
        await message.answer("🚫 Ваш акаунт заблоковано."); return False
    if not await subscribed(message.from_user.id):
        text = await db.get_setting("subscription_text", DEFAULT_SETTINGS["subscription_text"])
        await message.answer((await db.get_setting("welcome_text", DEFAULT_SETTINGS["welcome_text"]))+"\n\n"+text, reply_markup=await subscribe_kb()); return False
    u, added = await db.activate_subscription(message.from_user.id)
    if added and u and u["referrer_id"]:
        try:
            ref_user = await db.user_by_tg((await get_tg_id_by_db_id(u["referrer_id"])))
            if ref_user: await bot.send_message(ref_user["telegram_id"], "🎉 Твій реферал підтвердив підписку! Ти отримав +1 spin за виконання порогу.")
        except Exception: pass
    await message.answer(await db.get_setting("menu_text", DEFAULT_SETTINGS["menu_text"]), reply_markup=menu_kb())
    return True

async def get_tg_id_by_db_id(db_id: int):
    async with aiosqlite.connect(DB_PATH) as conn:
        cur=await conn.execute("SELECT telegram_id FROM users WHERE id=?",(db_id,)); r=await cur.fetchone(); return r[0] if r else 0


@router.message(CommandStart())
async def start(message: Message):
    args = (message.text or "").split(maxsplit=1)
    ref_arg = args[1].strip() if len(args) > 1 else None
    await show_access(message, ref_arg)

@router.callback_query(F.data == "check_sub")
async def check_sub(c: CallbackQuery):
    if not await subscribed(c.from_user.id):
        await c.answer("❌ Підписку не знайдено. Підпишись на канал і натисни ще раз.", show_alert=True); return
    await db.activate_subscription(c.from_user.id)
    await c.message.edit_text("✅ Підписку підтверджено! Тепер бот доступний.")
    await c.message.answer(await db.get_setting("menu_text", DEFAULT_SETTINGS["menu_text"]), reply_markup=menu_kb())
    await c.answer()

@router.message(F.text == "👤 Мій кабінет")
async def cabinet(message: Message):
    if not await show_access(message): return
    u=await db.user_by_tg(message.from_user.id); refs=await db.count_refs(u["id"]); need=int(await db.get_setting("refs_per_spin","5")); progress=refs%need if need else 0
    await message.answer(f"👤 <b>Мій кабінет</b>\n\n👤 Ім'я: {u['first_name'] or '—'}\n🆔 Telegram ID: <code>{u['telegram_id']}</code>\n👥 Запрошено рефералів: {refs}\n🎡 Доступних spin: {u['spins']}\n📊 Прогрес до наступного spin: {progress}/{need}\n💰 Поточний баланс: {money(u['balance'])} грн\n🏆 Всього виграно: {money(u['total_won'])} грн")

@router.message(F.text == "🎡 Крутити колесо")
async def wheel(message: Message):
    if not await show_access(message): return
    await message.answer(await db.get_setting("wheel_text", DEFAULT_SETTINGS["wheel_text"]), reply_markup=wheel_button())

@router.message(F.text == "🔗 Моє реферальне посилання")
async def referral(message: Message):
    if not await show_access(message): return
    u=await db.user_by_tg(message.from_user.id); refs=await db.count_refs(u["id"]); link=f"https://t.me/{BOT_USERNAME}?start=ref_{u['telegram_id']}"
    await message.answer((await db.get_setting("ref_text",DEFAULT_SETTINGS["ref_text"])).format(link=link,refs=refs,spins=u["spins"]))

@router.message(F.text == "📈 Заробити більше")
async def earn(message: Message):
    if not await show_access(message): return
    s=await db.settings(); await message.answer(s["earn_text"].format(refs_per_spin=s["refs_per_spin"]))

@router.message(F.text == "📜 Правила")
async def rules(message: Message):
    if not await show_access(message): return
    s=await db.settings(); await message.answer(s["rules_text"].format(refs_per_spin=s["refs_per_spin"],min_withdraw=s["min_withdraw"]))

@router.message(F.text == "💰 Вивід коштів")
async def withdrawal(message: Message, state: FSMContext):
    if not await show_access(message): return
    u=await db.user_by_tg(message.from_user.id); min_w=float(await db.get_setting("min_withdraw","50"))
    if u["balance"] < min_w:
        await message.answer(f"💰 Баланс: {money(u['balance'])} грн\nПотрібно ще {money(min_w-u['balance'])} грн до мінімального виводу."); return
    await state.set_state(WithdrawalStates.details)
    await message.answer((await db.get_setting("withdraw_text",DEFAULT_SETTINGS["withdraw_text"])).format(min_withdraw=min_w,balance=money(u["balance"]))+"\n\nНадішли дані для виплати, наприклад:\n<code>50 | Monobank 5375...</code>")

@router.message(WithdrawalStates.details)
async def save_withdrawal_details(message: Message, state: FSMContext):
    ok, result = await db.create_withdrawal(message.from_user.id, message.text.strip())
    if not ok:
        await message.answer(str(result)); await state.clear(); return
    wid, amount = result
    await state.clear()
    await message.answer(f"✅ Заявку #{wid} створено. Баланс заблоковано до рішення адміністратора.", reply_markup=menu_kb())
    for aid in ADMIN_IDS:
        try:
            u=await db.user_by_tg(message.from_user.id)
            await bot.send_message(aid, f"💰 <b>Нова виплата #{wid}</b>\nКористувач: {u['first_name']} (@{u['username'] or '—'})\nID: <code>{u['telegram_id']}</code>\nСума: {money(amount)} грн\nДані: <code>{message.text}</code>", reply_markup=withdraw_admin_kb(wid))
        except Exception as e: log.warning("notify admin: %s", e)

@router.message(Command("admin"))
async def admin(message: Message):
    if not is_admin(message.from_user.id): return
    await message.answer("⚙️ Адмін-панель", reply_markup=admin_kb())

@router.callback_query(F.data.startswith("adm:"))
async def admin_callbacks(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): await c.answer("⛔ Доступ заборонено", show_alert=True); return
    action=c.data.split(":",1)[1]
    if action=="stats":
        s=await db.stats(); await c.message.edit_text(f"📊 <b>Статистика</b>\n\nКористувачів: {s['users']}\nПідписаних: {s['subscribed']}\nНових сьогодні: {s['today']}\nРефералів: {s['refs']}\nВикористано spin: {s['spins']}\nЗагальні виграші: {money(s['won'])} грн\nБаланс користувачів: {money(s['balances'])} грн\nPending: {money(s['pending'])} грн\nPaid: {money(s['paid'])} грн\nRejected: {money(s['rejected'])} грн", reply_markup=admin_kb())
    elif action=="users":
        rows=await db.admin_users(); txt="👥 <b>Останні користувачі</b>\n\n"+"\n".join(f"{r['telegram_id']} | @{r['username'] or '—'} | spin {r['spins']} | {money(r['balance'])} грн | {'🚫' if r['blocked'] else '✅'}" for r in rows) or "Немає користувачів"; await c.message.edit_text(txt[:4000],reply_markup=admin_kb())
    elif action=="withdrawals":
        rows=await db.get_pending_withdrawals()
        if not rows: await c.message.edit_text("💰 Pending-виплат немає.",reply_markup=admin_kb())
        else:
            await c.message.edit_text("💰 Pending-виплати:")
            for r in rows: await c.message.answer(f"#{r['id']} — {money(r['amount'])} грн\nКористувач: {r['telegram_id']} (@{r['username'] or '—'})\nДані: <code>{r['payment_details']}</code>",reply_markup=withdraw_admin_kb(r['id']))
    elif action=="roulette":
        s=await db.settings(); await state.set_state(AdminStates.roulette); await c.message.edit_text(f"🎡 Надішли одним повідомленням:\n<code>10=50 20=35 30=12 50=3 refs=5 min=50</code>\n\nПоточне: 10={s['chance_10']}%, 20={s['chance_20']}%, 30={s['chance_30']}%, 50={s['chance_50']}%, refs={s['refs_per_spin']}, min={s['min_withdraw']}")
    elif action=="channel":
        await state.set_state(AdminStates.channel); s=await db.settings(); await c.message.edit_text(f"📢 Надішли: <code>channel_id={CHANNEL_ID}</code>\n<code>channel_url={CHANNEL_URL}</code>\n\nУ цій версії зміни зберігаються для відображення/конфігурації. Для зміни фактичного Telegram chat ID потрібно також змінити env CHANNEL_ID і перезапустити сервіс.")
    elif action=="texts":
        await c.message.edit_text("✏️ Вибери текст:",reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Привітання",callback_data="txt:welcome_text"),InlineKeyboardButton(text="Підписка",callback_data="txt:subscription_text")],
            [InlineKeyboardButton(text="Головне меню",callback_data="txt:menu_text"),InlineKeyboardButton(text="Реферали",callback_data="txt:ref_text")],
            [InlineKeyboardButton(text="Рулетка",callback_data="txt:wheel_text"),InlineKeyboardButton(text="Виграш",callback_data="txt:win_text")],
            [InlineKeyboardButton(text="Правила",callback_data="txt:rules_text"),InlineKeyboardButton(text="Вивід",callback_data="txt:withdraw_text")],
            [InlineKeyboardButton(text="Заробити більше",callback_data="txt:earn_text")],
            [InlineKeyboardButton(text="⬅️ Назад",callback_data="adm:back")]
        ]))
    elif action=="find":
        await state.set_state(AdminStates.find_user); await c.message.edit_text("🔎 Надішли Telegram ID або username без @")
    elif action=="blocked":
        async with aiosqlite.connect(DB_PATH) as conn:
            conn.row_factory=aiosqlite.Row; cur=await conn.execute("SELECT telegram_id,username,first_name FROM users WHERE blocked=1 ORDER BY id DESC LIMIT 50"); rows=await cur.fetchall()
        await c.message.edit_text("🚫 <b>Заблоковані</b>\n\n"+("\n".join(f"{r['telegram_id']} @{r['username'] or '—'}" for r in rows) or "Немає"),reply_markup=admin_kb())
    elif action=="broadcast":
        await state.set_state(AdminStates.broadcast); await c.message.edit_text("📣 Надішли текст розсилки. Він буде відправлений усім незаблокованим користувачам.")
    elif action=="back": await c.message.edit_text("⚙️ Адмін-панель",reply_markup=admin_kb())
    await c.answer()

@router.callback_query(F.data.startswith("txt:"))
async def choose_text(c: CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id): return
    key=c.data.split(":",1)[1]; await state.set_state(AdminStates.text); await state.update_data(text_key=key); current=await db.get_setting(key); await c.message.edit_text(f"✏️ Поточний текст <code>{key}</code>:\n\n{current}\n\nНадішли новий текст. Плейсхолдери залишай без змін."); await c.answer()

@router.callback_query(F.data.startswith("wd:"))
async def process_wd(c: CallbackQuery):
    if not is_admin(c.from_user.id): return
    _,status,wid=c.data.split(":"); result=await db.process_withdrawal(int(wid),status)
    if not result: await c.answer("Заявка вже оброблена або не існує",show_alert=True); return
    w,tg_id=result
    try: await bot.send_message(tg_id, f"💰 Ваша заявка #{wid} на {money(w['amount'])} грн: <b>{status.upper()}</b>"+("\nКошти повернені на баланс." if status=='rejected' else ""))
    except Exception: pass
    await c.message.edit_text(f"#{wid} — {money(w['amount'])} грн → {status.upper()}"); await c.answer()

@router.message(AdminStates.roulette)
async def save_roulette(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    try:
        parts={}
        for x in message.text.split():
            k,v=x.split("=",1); parts[k]=float(v)
        vals=[parts[str(x)] for x in (10,20,30,50)]; refs=int(parts["refs"]); min_w=float(parts["min"])
        if any(v<0 for v in vals) or abs(sum(vals)-100)>1e-9 or refs<1 or min_w<=0: raise ValueError
        for x in (10,20,30,50): await db.set_setting(f"chance_{x}",str(parts[str(x)]))
        await db.set_setting("refs_per_spin",str(refs)); await db.set_setting("min_withdraw",str(min_w)); await state.clear(); await message.answer("✅ Налаштування рулетки збережено.",reply_markup=admin_kb())
    except Exception: await message.answer("❌ Некоректні дані. Сума шансів має дорівнювати 100%, refs ≥ 1, min > 0.")

@router.message(AdminStates.text)
async def save_text(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    data=await state.get_data(); key=data.get("text_key")
    if key not in DEFAULT_SETTINGS: await state.clear(); await message.answer("❌ Невідомий текст."); return
    await db.set_setting(key,message.text); await state.clear(); await message.answer("✅ Текст збережено.",reply_markup=admin_kb())

@router.message(AdminStates.find_user)
async def find_user(message: Message,state: FSMContext):
    if not is_admin(message.from_user.id): return
    rows=await db.find_user(message.text.strip()); await state.clear();
    if not rows: await message.answer("🔎 Нічого не знайдено.",reply_markup=admin_kb()); return
    for r in rows: await message.answer(f"👤 {r['first_name']}\nID: <code>{r['telegram_id']}</code>\n@{r['username'] or '—'}\nПідписка: {r['subscribed']}\nSpin: {r['spins']}\nБаланс: {money(r['balance'])} грн\nЗаблокований: {r['blocked']}")

@router.message(AdminStates.channel)
async def save_channel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    vals={}
    try:
        for x in message.text.splitlines():
            if "=" in x:
                k,v=x.split("=",1); vals[k.strip()]=v.strip()
        if "channel_url" in vals and vals["channel_url"]:
            await db.set_setting("channel_url", vals["channel_url"])
        if "channel_id" in vals and vals["channel_id"]:
            await db.set_setting("channel_id", vals["channel_id"])
        await state.clear(); await message.answer("✅ Налаштування каналу збережено в SQLite. Для фактичної перевірки Telegram використовується CHANNEL_ID з environment variables.", reply_markup=admin_kb())
    except Exception as e:
        log.warning("channel settings: %s", e); await message.answer("❌ Не вдалося зберегти.")

@router.message(AdminStates.broadcast)
async def broadcast(message: Message,state: FSMContext):
    if not is_admin(message.from_user.id): return
    recipients=await db.all_recipients(); sent=0; failed=0
    for uid in recipients:
        try: await bot.send_message(uid,message.text); sent+=1
        except Exception: failed+=1
        await asyncio.sleep(.04)
    await state.clear(); await message.answer(f"📣 Розсилку завершено. Надіслано: {sent}, помилок: {failed}.",reply_markup=admin_kb())

async def validate_webapp_init_data(init_data: str):
    if not init_data: raise ValueError("Відкрий рулетку через Telegram")
    pairs=dict(parse_qsl(init_data,keep_blank_values=True)); received=pairs.pop("hash",None)
    if not received: raise ValueError("Некоректні initData")
    auth_date=int(pairs.get("auth_date","0"));
    if abs(time.time()-auth_date)>86400: raise ValueError("initData прострочені")
    check="\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret=hmac.new(b"WebAppData",BOT_TOKEN.encode(),hashlib.sha256).digest()
    expected=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,received): raise ValueError("Недійсний Telegram initData")
    user=json.loads(pairs.get("user","{}")); tg_id=int(user["id"]); return tg_id

async def api_me(request):
    try: tg_id=await validate_webapp_init_data(request.headers.get("X-Telegram-Init-Data","")); u=await db.user_by_tg(tg_id)
    except Exception as e: return web.json_response({"error":str(e)},status=401)
    if not u or u["blocked"]: return web.json_response({"error":"Доступ заборонено"},status=403)
    return web.json_response({"balance":u["balance"],"spins":u["spins"]})

async def api_spin(request):
    try: tg_id=await validate_webapp_init_data(request.headers.get("X-Telegram-Init-Data",""))
    except Exception as e: return web.json_response({"error":str(e)},status=401)
    if not await subscribed(tg_id):
        return web.json_response({"error":"Підписка на канал не підтверджена"},status=403)
    await db.activate_subscription(tg_id)
    try:
        body=await request.json() if request.can_read_body else {}
    except Exception: body={}
    request_id=str(body.get("request_id") or request.headers.get("X-Request-ID") or "")[:100]
    if not request_id: request_id=secrets.token_urlsafe(24)
    try: reward,balance=await db.perform_spin(tg_id,request_id); win_text=await db.get_setting("win_text", DEFAULT_SETTINGS["win_text"]); return web.json_response({"reward":reward,"balance":balance,"request_id":request_id,"win_text":win_text.format(reward=reward)})
    except PermissionError as e: return web.json_response({"error":str(e)},status=403)
    except ValueError as e: return web.json_response({"error":str(e)},status=409)
    except Exception as e: log.exception("spin api"); return web.json_response({"error":"Внутрішня помилка. Спробуйте ще раз."},status=500)

async def health(request): return web.json_response({"ok":True})

async def on_startup(app):
    await db.init()
    await bot.set_webhook(WEBHOOK_URL, drop_pending_updates=False, allowed_updates=dp.resolve_used_update_types())
    log.info("Webhook set: %s",WEBHOOK_URL)

async def on_cleanup(app):
    await bot.delete_webhook(drop_pending_updates=False)
    await bot.session.close()

async def webhook(request):
    try:
        data=await request.json()
        from aiogram.types import Update
        update=Update.model_validate(data)
        await dp.feed_update(bot,update)
        return web.json_response({"ok":True})
    except Exception:
        log.exception("webhook error"); return web.json_response({"ok":False},status=500)

async def main():
    app=web.Application()
    app.router.add_get("/health",health)
    app.router.add_get("/web/",lambda request:web.FileResponse(Path(__file__).parent/"web"/"index.html"))
    app.router.add_post(WEBHOOK_PATH,webhook)
    app.router.add_get("/api/me",api_me)
    app.router.add_post("/api/spin",api_spin)
    app.on_startup.append(on_startup); app.on_cleanup.append(on_cleanup)
    runner=web.AppRunner(app); await runner.setup(); site=web.TCPSite(runner,"0.0.0.0",int(os.getenv("PORT","10000"))); await site.start(); log.info("HTTP server started"); await asyncio.Event().wait()

if __name__=="__main__": asyncio.run(main())
