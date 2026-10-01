#!/usr/bin/env python3
"""Zenin APK Shop — Telegram Stars + Crypto Pay."""

import asyncio
import logging
import os
import shutil
import sqlite3
from contextlib import suppress
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ContentType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

# ============================ CONFIG ============================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8641853420:AAFafrUy1ZOz851jFlRTq_3CJMCHZDkE1FU")
CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN", "641055:AAjyk1j47cBTblrloklrJrqULS4xfoKguA2")
RUB_PROVIDER_TOKEN = os.getenv("RUB_PROVIDER_TOKEN", "")
CRYPTO_PAY_BASE = os.getenv("CRYPTO_PAY_BASE", "https://pay.crypt.bot/api")
ADMIN_ID = int(os.getenv("ADMIN_ID", "956327348"))
CHANNEL = os.getenv("CHANNEL", "@darknessware")
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/darknessware")
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/snexbog")
SHOP_NAME = os.getenv("SHOP_NAME", "Darkness Shop")
PRODUCT_NAME = os.getenv("PRODUCT_NAME", "Zenin 1.0")
PUBLIC_URL = os.getenv("PUBLIC_URL", "")
PORT = int(os.getenv("PORT", "8080"))

PLANS = {
    "7d": {"title": "7 дней", "stars": 200, "usdt": "2", "rub": 200},
    "30d": {"title": "30 дней", "stars": 400, "usdt": "4", "rub": 400},
    "forever": {"title": "Навсегда", "stars": 600, "usdt": "7.5", "rub": 750},
}

ROOT = Path(os.getenv("DATA_DIR", str(Path(__file__).resolve().parent)))
ROOT.mkdir(parents=True, exist_ok=True)
DB_PATH = ROOT / "zenin_shop.db"
APK_PATH = ROOT / "Zenin_1.0.apk"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
router = Router()


class AdminState(StatesGroup):
    upload_file = State()
    broadcast = State()


# ============================ DB ============================
def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db() -> None:
    with connect() as db:
        db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                created_at TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                plan TEXT NOT NULL,
                method TEXT NOT NULL,
                amount TEXT NOT NULL,
                currency TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                external_id TEXT UNIQUE,
                payload TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL,
                paid_at TEXT,
                delivered_at TEXT
            );
            """
        )
        db.commit()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_user_from_message(message: Message) -> None:
    if message.from_user:
        save_user(message.from_user.id, message.from_user.username, message.from_user.first_name)


def save_user(user_id: int, username: Optional[str], first_name: Optional[str]) -> None:
    with connect() as db:
        db.execute(
            """
            INSERT INTO users(user_id, username, first_name, created_at, last_seen)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username,
              first_name=excluded.first_name,
              last_seen=excluded.last_seen
            """,
            (user_id, username, first_name, now(), now()),
        )
        db.commit()


# ============================ KEYBOARDS ============================
def kb(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
        ]
    )


def main_keyboard(user_id: Optional[int] = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🛍 Каталог", callback_data="products")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="💬 Поддержка", url=SUPPORT_URL), InlineKeyboardButton(text="📢 Канал", url=CHANNEL_URL)],
    ]
    if user_id == ADMIN_ID:
        rows.insert(0, [InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def methods_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⭐ Telegram Stars", callback_data="method:stars")],
            [InlineKeyboardButton(text="💎 CryptoBot USDT", callback_data="method:crypto")],
            [InlineKeyboardButton(text="₽ Рубли — реселлер", url=SUPPORT_URL)],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="products")],
        ]
    )


def method_plans_keyboard(method: str) -> InlineKeyboardMarkup:
    rows = []
    for code, p in PLANS.items():
        if method == "crypto":
            price = f"{p['usdt']} USDT"
        elif method == "rub":
            price = f"{p['rub']} ₽"
        else:
            price = f"{p['stars']} ⭐"
        rows.append([InlineKeyboardButton(text=f"{p['title']} — {price}", callback_data=f"{method}:{code}")])
    rows.append([InlineKeyboardButton(text="⬅️ К способам оплаты", callback_data="product:zenin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscribe_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Подписаться", url=CHANNEL_URL)],
            [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_sub")],
        ]
    )


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"), InlineKeyboardButton(text="🧾 Заказы", callback_data="admin:orders")],
            [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users"), InlineKeyboardButton(text="📦 Товар", callback_data="admin:file")],
            [InlineKeyboardButton(text="📨 Рассылка", callback_data="admin:broadcast"), InlineKeyboardButton(text="💳 Оплаты", callback_data="admin:payments")],
            [InlineKeyboardButton(text="🛡 Проверки", callback_data="admin:health"), InlineKeyboardButton(text="🏷 Тарифы", callback_data="admin:prices")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="home")],
        ]
    )


# ============================ HELPERS ============================
async def is_subscribed(bot: Bot, user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    try:
        member = await bot.get_chat_member(CHANNEL, user_id)
        return member.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        }
    except Exception as exc:
        logging.warning("Subscription check failed: %s", exc)
        return False


async def require_subscription(event: Message | CallbackQuery, bot: Bot) -> bool:
    return True


async def crypto_call(method: str, data: Optional[dict] = None) -> dict:
    if not CRYPTO_PAY_TOKEN:
        raise RuntimeError("CRYPTO_PAY_TOKEN is not configured")
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}
    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.post(f"{CRYPTO_PAY_BASE}/{method}", json=data or {}) as response:
            result = await response.json(content_type=None)
            if not response.ok or not result.get("ok"):
                raise RuntimeError(f"Crypto Pay error: {result}")
            return result["result"]


async def deliver(bot: Bot, order_id: int) -> bool:
    with connect() as db:
        order = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order or order["status"] != "paid" or order["delivered_at"]:
        return False
    if not APK_PATH.exists():
        await bot.send_message(
            order["user_id"],
            "✅ Оплата подтверждена, но товарный файл пока не загружен. Поддержка: " + SUPPORT_URL,
        )
        await bot.send_message(ADMIN_ID, f"⚠️ Заказ #{order_id} оплачен, но товарный файл не загружен. Открой админ-панель → Товар.")
        return False
    await bot.send_document(
        order["user_id"],
        FSInputFile(APK_PATH),
        caption=f"✅ Заказ #{order_id}\n{PRODUCT_NAME}\nТариф: {PLANS[order['plan']]['title']}\n\nСпасибо за покупку!",
    )
    with connect() as db:
        db.execute("UPDATE orders SET delivered_at=? WHERE id=? AND delivered_at IS NULL", (now(), order_id))
        db.commit()
    return True


async def health_server() -> None:
    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info("Health server started on port %s", PORT)


async def keep_alive() -> None:
    if not PUBLIC_URL:
        return
    while True:
        with suppress(Exception):
            async with aiohttp.ClientSession() as session:
                await session.get(f"{PUBLIC_URL.rstrip('/')}/health", timeout=20)
        await asyncio.sleep(600)


# ============================ USER HANDLERS ============================
@router.message(CommandStart())
async def start(message: Message, bot: Bot):
    save_user_from_message(message)
    if not await require_subscription(message, bot):
        return
    await message.answer(
        f"<b>🖤 {SHOP_NAME}</b>\n\n"
        f"<b>{PRODUCT_NAME}</b> — приватный цифровой товар.\n"
        "Оплата доступна через Stars, CryptoBot и реселлера в рублях.\n\n"
        "Выберите раздел ниже:",
        reply_markup=main_keyboard(message.from_user.id),
    )


@router.callback_query(F.data == "check_sub")
async def check_sub(call: CallbackQuery, bot: Bot):
    save_user(call.from_user.id, call.from_user.username, call.from_user.first_name)
    await call.message.edit_text("✅ Доступ открыт.", reply_markup=main_keyboard(call.from_user.id))
    await call.answer()


@router.callback_query(F.data == "home")
async def home(call: CallbackQuery, bot: Bot):
    if not await require_subscription(call, bot):
        return
    await call.message.edit_text(f"<b>{SHOP_NAME}</b>\nВыберите раздел:", reply_markup=main_keyboard(call.from_user.id))
    await call.answer()


@router.callback_query(F.data == "products")
async def show_products(call: CallbackQuery, bot: Bot):
    if not await require_subscription(call, bot):
        return
    await call.message.edit_text(
        "<b>🛍 Продукты</b>\n\nВыберите продукт:",
        reply_markup=kb([[(f'📱 {PRODUCT_NAME}', 'product:zenin')], [('⬅️ Назад', 'home')]]),
    )
    await call.answer()


@router.callback_query(F.data == "product:zenin")
async def show_product(call: CallbackQuery, bot: Bot):
    if not await require_subscription(call, bot):
        return
    await call.message.edit_text(
        f"<b>📱 {PRODUCT_NAME}</b>\n\n"
        "Выберите удобный способ оплаты. После подтверждения бот автоматически выдаст товар.",
        reply_markup=methods_keyboard(),
    )
    await call.answer()


@router.callback_query(F.data.startswith("method:"))
async def choose_method_plan(call: CallbackQuery, bot: Bot):
    if not await require_subscription(call, bot):
        return
    method = call.data.split(":", 1)[1]
    if method not in {"crypto", "stars", "rub"}:
        return await call.answer("Способ оплаты не найден", show_alert=True)
    titles = {"crypto": "CryptoBot", "stars": "Telegram Stars", "rub": "Рубли"}
    title = titles[method]
    await call.message.edit_text(
        f"<b>{PRODUCT_NAME}</b>\nОплата: {title}\n\nВыберите срок доступа:",
        reply_markup=method_plans_keyboard(method),
    )
    await call.answer()


@router.callback_query(F.data.startswith("stars:"))
async def pay_stars(call: CallbackQuery, bot: Bot):
    if not await require_subscription(call, bot):
        return
    plan = call.data.split(":", 1)[1]
    if plan not in PLANS:
        return await call.answer("Тариф не найден", show_alert=True)
    p = PLANS[plan]
    payload = f"stars:{call.from_user.id}:{plan}:{int(datetime.now().timestamp() * 1000)}"
    with connect() as db:
        cur = db.execute(
            """INSERT INTO orders(user_id,plan,method,amount,currency,status,payload,created_at)
               VALUES(?,?,?,?,?,'pending',?,?)""",
            (call.from_user.id, plan, "stars", str(p["stars"]), "XTR", payload, now()),
        )
        order_id = cur.lastrowid
        db.commit()
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title=PRODUCT_NAME,
        description=f"Доступ: {p['title']} | Заказ #{order_id}",
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label=f"Zenin — {p['title']}", amount=p["stars"])],
        provider_token="",
    )
    await call.answer()


@router.callback_query(F.data.startswith("rub:"))
async def pay_rub(call: CallbackQuery, bot: Bot):
    if not await require_subscription(call, bot):
        return
    if not RUB_PROVIDER_TOKEN:
        return await call.answer(
            "Оплата рублями ещё не подключена. Нужен provider token от ЮKassa/другого провайдера.",
            show_alert=True,
        )
    plan = call.data.split(":", 1)[1]
    if plan not in PLANS:
        return await call.answer("Тариф не найден", show_alert=True)
    p = PLANS[plan]
    amount_kopecks = int(p["rub"] * 100)
    payload = f"rub:{call.from_user.id}:{plan}:{int(datetime.now().timestamp() * 1000)}"
    with connect() as db:
        cur = db.execute(
            """INSERT INTO orders(user_id,plan,method,amount,currency,status,payload,created_at)
               VALUES(?,?,?,?,?,'pending',?,?)""",
            (call.from_user.id, plan, "rub", str(amount_kopecks), "RUB", payload, now()),
        )
        order_id = cur.lastrowid
        db.commit()
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title=PRODUCT_NAME,
        description=f"Доступ: {p['title']} | Заказ #{order_id}",
        payload=payload,
        currency="RUB",
        prices=[LabeledPrice(label=f"Zenin — {p['title']}", amount=amount_kopecks)],
        provider_token=RUB_PROVIDER_TOKEN,
    )
    await call.answer()


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    with connect() as db:
        order = db.execute("SELECT * FROM orders WHERE payload=?", (query.invoice_payload,)).fetchone()
    valid = bool(
        order
        and order["status"] == "pending"
        and order["user_id"] == query.from_user.id
        and order["currency"] == query.currency
        and int(order["amount"]) == query.total_amount
    )
    await query.answer(ok=valid, error_message=None if valid else "Заказ не найден или сумма изменилась.")


@router.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT)
async def successful_stars(message: Message, bot: Bot):
    payment = message.successful_payment
    with connect() as db:
        order = db.execute("SELECT * FROM orders WHERE payload=?", (payment.invoice_payload,)).fetchone()
        if not order or order["user_id"] != message.from_user.id:
            return
        db.execute(
            "UPDATE orders SET status='paid', paid_at=?, external_id=? WHERE id=? AND status='pending'",
            (now(), payment.telegram_payment_charge_id, order["id"]),
        )
        db.commit()
    method_title = "рублями" if order["method"] == "rub" else "звёздами"
    await message.answer(f"✅ Оплата {method_title} подтверждена. Выдаю товар…")
    await deliver(bot, order["id"])
    await bot.send_message(ADMIN_ID, f"💰 Новый {order['method']}-заказ #{order['id']} от {message.from_user.id}")


@router.callback_query(F.data.startswith("crypto:"))
async def pay_crypto(call: CallbackQuery, bot: Bot):
    if not await require_subscription(call, bot):
        return
    plan = call.data.split(":", 1)[1]
    if plan not in PLANS:
        return await call.answer("Тариф не найден", show_alert=True)
    p = PLANS[plan]
    payload = f"crypto:{call.from_user.id}:{plan}:{int(datetime.now().timestamp() * 1000)}"
    try:
        invoice = await crypto_call(
            "createInvoice",
            {
                "currency_type": "crypto",
                "asset": "USDT",
                "amount": p["usdt"],
                "description": f"{PRODUCT_NAME} — {p['title']}",
                "payload": payload,
                "expires_in": 3600,
            },
        )
    except Exception as exc:
        logging.exception("createInvoice failed")
        return await call.answer(f"Crypto Pay временно недоступен: {exc}", show_alert=True)
    with connect() as db:
        cur = db.execute(
            """INSERT INTO orders(user_id,plan,method,amount,currency,status,external_id,payload,created_at)
               VALUES(?,?,?,?,?,'pending',?,?,?)""",
            (call.from_user.id, plan, "crypto", p["usdt"], "USDT", str(invoice["invoice_id"]), payload, now()),
        )
        order_id = cur.lastrowid
        db.commit()
    pay_url = invoice.get("bot_invoice_url") or invoice.get("mini_app_invoice_url") or invoice.get("web_app_invoice_url")
    await call.message.answer(
        f"🧾 Заказ #{order_id}\nСумма: {p['usdt']} USDT\nСчёт действует 1 час.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💎 Оплатить", url=pay_url)],
                [InlineKeyboardButton(text="✅ Проверить оплату", callback_data=f"verify:{order_id}")],
            ]
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith("verify:"))
async def verify_crypto(call: CallbackQuery, bot: Bot):
    if not await require_subscription(call, bot):
        return
    try:
        order_id = int(call.data.split(":", 1)[1])
    except ValueError:
        return await call.answer("Неверный заказ", show_alert=True)
    with connect() as db:
        order = db.execute("SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, call.from_user.id)).fetchone()
    if not order or order["method"] != "crypto":
        return await call.answer("Заказ не найден", show_alert=True)
    if order["status"] == "paid":
        await call.answer("Оплата уже подтверждена.", show_alert=True)
        return await deliver(bot, order_id)
    try:
        invoices = await crypto_call("getInvoices", {"invoice_ids": order["external_id"]})
        items = invoices.get("items", [])
    except Exception as exc:
        return await call.answer(f"Ошибка проверки: {exc}", show_alert=True)
    if not items or items[0].get("status") != "paid":
        return await call.answer("Оплата пока не найдена.", show_alert=True)
    inv = items[0]
    valid = (
        str(inv.get("invoice_id")) == order["external_id"]
        and inv.get("asset") == "USDT"
        and Decimal(str(inv.get("amount"))) == Decimal(order["amount"])
        and inv.get("payload") == order["payload"]
    )
    if not valid:
        await bot.send_message(ADMIN_ID, f"⚠️ Несовпадение параметров Crypto-заказа #{order_id}")
        return await call.answer("Параметры платежа не совпали. Обратитесь в поддержку.", show_alert=True)
    with connect() as db:
        db.execute("UPDATE orders SET status='paid', paid_at=? WHERE id=? AND status='pending'", (now(), order_id))
        db.commit()
    await call.answer("Оплата подтверждена!", show_alert=True)
    await deliver(bot, order_id)
    await bot.send_message(ADMIN_ID, f"💰 Новый Crypto-заказ #{order_id} от {call.from_user.id}")


@router.callback_query(F.data == "profile")
async def profile(call: CallbackQuery, bot: Bot):
    if not await require_subscription(call, bot):
        return
    with connect() as db:
        paid = db.execute("SELECT COUNT(*) FROM orders WHERE user_id=? AND status='paid'", (call.from_user.id,)).fetchone()[0]
    username = f"@{call.from_user.username}" if call.from_user.username else "не указан"
    await call.message.edit_text(
        f"<b>👤 Профиль</b>\n\nID: <code>{call.from_user.id}</code>\nUsername: {username}\nПокупок: {paid}",
        reply_markup=kb([[('📦 Мои покупки', 'purchases')], [('🆘 Поддержка', 'support')], [('⬅️ Назад', 'home')]]),
    )
    await call.answer()


@router.callback_query(F.data == "purchases")
async def purchases(call: CallbackQuery, bot: Bot):
    if not await require_subscription(call, bot):
        return
    with connect() as db:
        rows = db.execute(
            "SELECT * FROM orders WHERE user_id=? AND status='paid' ORDER BY id DESC LIMIT 10",
            (call.from_user.id,),
        ).fetchall()
    if not rows:
        text = "У вас пока нет оплаченных заказов."
    else:
        text = "<b>Ваши покупки:</b>\n" + "\n".join(
            f"#{r['id']} — {PLANS[r['plan']]['title']} — {r['method']}" for r in rows
        )
    await call.message.answer(text)
    await call.answer()


@router.callback_query(F.data == "support")
async def support(call: CallbackQuery):
    await call.message.answer("Поддержка: " + SUPPORT_URL)
    await call.answer()


# ============================ ADMIN ============================
@router.message(Command("setapk"), F.from_user.id == ADMIN_ID)
async def set_apk_command(message: Message, state: FSMContext):
    await state.set_state(AdminState.upload_file)
    await message.answer("📦 Отправьте товарный файл документом.")


@router.callback_query(F.data == "admin:file", F.from_user.id == ADMIN_ID)
async def admin_file(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.upload_file)
    status = "загружен" if APK_PATH.exists() else "не загружен"
    await call.message.edit_text(
        f"📦 <b>Товарный файл</b>\n\nСтатус: <b>{status}</b>\nОтправьте новый файл документом прямо сюда.",
        reply_markup=admin_keyboard(),
    )
    await call.answer()


@router.message(AdminState.upload_file, F.document, F.from_user.id == ADMIN_ID)
async def upload_product_file(message: Message, bot: Bot, state: FSMContext):
    temp = APK_PATH.with_suffix(".tmp")
    await bot.download(message.document, destination=temp)
    shutil.move(temp, APK_PATH)
    await state.clear()
    await message.answer(f"✅ Товарный файл сохранён: {message.document.file_name or APK_PATH.name}", reply_markup=admin_keyboard())


@router.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def admin(message: Message):
    await message.answer("⚙️ <b>Админ-панель</b>", reply_markup=admin_keyboard())


@router.callback_query(F.data == "admin:menu", F.from_user.id == ADMIN_ID)
async def admin_menu(call: CallbackQuery):
    await call.message.edit_text("⚙️ <b>Админ-панель</b>\nВыберите действие:", reply_markup=admin_keyboard())
    await call.answer()


@router.callback_query(F.data == "admin:stats", F.from_user.id == ADMIN_ID)
async def admin_stats(call: CallbackQuery):
    with connect() as db:
        users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        paid = db.execute("SELECT COUNT(*) FROM orders WHERE status='paid'").fetchone()[0]
        pending = db.execute("SELECT COUNT(*) FROM orders WHERE status='pending'").fetchone()[0]
        crypto = db.execute("SELECT COUNT(*) FROM orders WHERE status='paid' AND method='crypto'").fetchone()[0]
        stars = db.execute("SELECT COUNT(*) FROM orders WHERE status='paid' AND method='stars'").fetchone()[0]
        rub = db.execute("SELECT COUNT(*) FROM orders WHERE status='paid' AND method='rub'").fetchone()[0]
    await call.message.edit_text(
        f"📊 <b>Статистика</b>\n\n"
        f"👤 Пользователей: <b>{users}</b>\n"
        f"✅ Оплачено: <b>{paid}</b>\n"
        f"⏳ Ожидают: <b>{pending}</b>\n"
        f"💎 Crypto: <b>{crypto}</b>\n"
        f"⭐ Stars: <b>{stars}</b>\n"
        f"₽ RUB: <b>{rub}</b>\n"
        f"📦 Товар: <b>{'загружен' if APK_PATH.exists() else 'не загружен'}</b>",
        reply_markup=admin_keyboard(),
    )
    await call.answer()


@router.callback_query(F.data == "admin:orders", F.from_user.id == ADMIN_ID)
async def admin_orders(call: CallbackQuery):
    with connect() as db:
        rows = db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 10").fetchall()
    if not rows:
        text = "🧾 Заказов пока нет."
    else:
        text = "🧾 <b>Последние заказы</b>\n\n" + "\n".join(
            f"#{r['id']} | {r['user_id']} | {r['plan']} | {r['method']} | {r['status']} | {r['amount']} {r['currency']}"
            for r in rows
        )
    await call.message.edit_text(text, reply_markup=admin_keyboard())
    await call.answer()


@router.callback_query(F.data == "admin:users", F.from_user.id == ADMIN_ID)
async def admin_users(call: CallbackQuery):
    with connect() as db:
        rows = db.execute("SELECT * FROM users ORDER BY last_seen DESC LIMIT 15").fetchall()
    if not rows:
        text = "👥 Пользователей пока нет."
    else:
        text = "👥 <b>Последние пользователи</b>\n\n" + "\n".join(
            f"<code>{r['user_id']}</code> | @{r['username'] or '-'} | {r['first_name'] or '-'}" for r in rows
        )
    await call.message.edit_text(text, reply_markup=admin_keyboard())
    await call.answer()


@router.callback_query(F.data == "admin:prices", F.from_user.id == ADMIN_ID)
async def admin_prices(call: CallbackQuery):
    text = "🏷 <b>Тарифы</b>\n\n" + "\n".join(
        f"• {p['title']}: {p['stars']} ⭐ / {p['usdt']} USDT / {p['rub']} ₽"
        for p in PLANS.values()
    )
    await call.message.edit_text(text, reply_markup=admin_keyboard())
    await call.answer()


@router.callback_query(F.data == "admin:payments", F.from_user.id == ADMIN_ID)
async def admin_payments(call: CallbackQuery):
    text = (
        "💳 <b>Оплаты</b>\n\n"
        "⭐ Telegram Stars: <b>включено</b>\n"
        f"💎 CryptoBot: <b>{'включено' if CRYPTO_PAY_TOKEN else 'нет токена'}</b>\n"
        f"₽ Рубли через реселлера: <b>{SUPPORT_URL}</b>\n"
        f"₽ Telegram Payments: <b>{'включено' if RUB_PROVIDER_TOKEN else 'нет provider token'}</b>"
    )
    await call.message.edit_text(text, reply_markup=admin_keyboard())
    await call.answer()


@router.callback_query(F.data == "admin:health", F.from_user.id == ADMIN_ID)
async def admin_health(call: CallbackQuery, bot: Bot):
    me = await bot.get_me()
    channel_status = "ошибка"
    with suppress(Exception):
        member = await bot.get_chat_member(CHANNEL, me.id)
        channel_status = member.status.value
    text = (
        "🛡 <b>Проверки</b>\n\n"
        f"Бот: @{me.username}\n"
        f"Канал: {CHANNEL}\n"
        f"Статус в канале: <b>{channel_status}</b>\n"
        f"Health: <b>ok</b>\n"
        f"Файл товара: <b>{'есть' if APK_PATH.exists() else 'нет'}</b>"
    )
    await call.message.edit_text(text, reply_markup=admin_keyboard())
    await call.answer()


@router.callback_query(F.data == "admin:broadcast", F.from_user.id == ADMIN_ID)
async def admin_broadcast_entry(call: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.broadcast)
    await call.message.edit_text("📨 Отправьте текст рассылки одним сообщением.", reply_markup=admin_keyboard())
    await call.answer()


@router.message(AdminState.broadcast, F.from_user.id == ADMIN_ID)
async def admin_broadcast_send(message: Message, bot: Bot, state: FSMContext):
    text = message.html_text or message.text or ""
    if not text.strip():
        return await message.answer("Отправьте текст рассылки.")
    with connect() as db:
        ids = [r[0] for r in db.execute("SELECT user_id FROM users").fetchall()]
    ok = 0
    bad = 0
    for uid in ids:
        try:
            await bot.send_message(uid, text)
            ok += 1
            await asyncio.sleep(0.04)
        except Exception:
            bad += 1
    await state.clear()
    await message.answer(f"✅ Рассылка завершена. Доставлено: {ok}, ошибок: {bad}", reply_markup=admin_keyboard())


@router.message(Command("refund"), F.from_user.id == ADMIN_ID)
async def refund_stars(message: Message, bot: Bot):
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) != 3:
        return await message.answer("Формат: /refund USER_ID TELEGRAM_PAYMENT_CHARGE_ID")
    try:
        await bot.refund_star_payment(user_id=int(parts[1]), telegram_payment_charge_id=parts[2])
        await message.answer("✅ Возврат Stars выполнен.")
    except Exception as exc:
        await message.answer(f"Ошибка возврата: {exc}")


# ============================ RUN ============================
async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("Set BOT_TOKEN environment variable")
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_my_commands([BotCommand(command="start", description="Открыть магазин")])
    asyncio.create_task(health_server())
    asyncio.create_task(keep_alive())
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
