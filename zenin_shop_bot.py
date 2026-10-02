#!/usr/bin/env python3

import asyncio
import logging
import os
import secrets
import sqlite3
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ContentType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8641853420:AAFafrUy1ZOz851jFlRTq_3CJMCHZDkE1FU")
ADMIN_ID = int(os.getenv("ADMIN_ID", "956327348"))
SHOP_NAME = os.getenv("SHOP_NAME", "Darkness Shop")
PRODUCT_NAME = os.getenv("PRODUCT_NAME", "StandRise Rework")
SUPPORT_URL = os.getenv("SUPPORT_URL", "https://t.me/snexbog")
PUBLIC_URL = os.getenv("PUBLIC_URL", "")
PORT = int(os.getenv("PORT", "8080"))
ROOT = Path(os.getenv("DATA_DIR", str(Path(__file__).resolve().parent)))
DB_PATH = ROOT / "shop.db"

PLAN_CODE = "standrise_1d"
PLAN_TITLE = "1 день"
PLAN_STARS = 5
ACCESS_DAYS = 1

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
router = Router()


class AdminState(StatesGroup):
    group_id = State()
    broadcast = State()


def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pretty_dt(value: Optional[str]) -> str:
    if not value:
        return "—"
    with suppress(Exception):
        return datetime.fromisoformat(value).strftime("%d.%m.%Y %H:%M")
    return value


def init_db() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.executescript(
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
                product TEXT NOT NULL,
                plan TEXT NOT NULL,
                amount INTEGER NOT NULL,
                currency TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                payload TEXT UNIQUE NOT NULL,
                charge_id TEXT,
                access_key TEXT,
                invite_link TEXT,
                access_until TEXT,
                created_at TEXT NOT NULL,
                paid_at TEXT
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        con.commit()


def setting(key: str, default: str = "") -> str:
    with db() as con:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with db() as con:
        con.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        con.commit()


def save_user(user_id: int, username: Optional[str], first_name: Optional[str]) -> None:
    with db() as con:
        con.execute(
            """
            INSERT INTO users(user_id,username,first_name,created_at,last_seen)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username,
              first_name=excluded.first_name,
              last_seen=excluded.last_seen
            """,
            (user_id, username, first_name, now(), now()),
        )
        con.commit()


def save_message_user(message: Message) -> None:
    if message.from_user:
        save_user(message.from_user.id, message.from_user.username, message.from_user.first_name)


def keygen() -> str:
    return "SR-" + "-".join(secrets.token_hex(2).upper() for _ in range(4))


def main_kb(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🛍 Продукты", callback_data="products")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="💬 Поддержка", url=SUPPORT_URL)],
    ]
    if user_id == ADMIN_ID:
        rows.insert(0, [InlineKeyboardButton(text="⚙️ Админ-панель", callback_data="admin:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def products_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"🚀 {PRODUCT_NAME}", callback_data="buy:open")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="home")],
        ]
    )


def buy_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"⭐ {PLAN_TITLE} — {PLAN_STARS} звёзд", callback_data=f"pay:{PLAN_CODE}")],
            [InlineKeyboardButton(text="⬅️ В меню", callback_data="home")],
        ]
    )


def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"), InlineKeyboardButton(text="🧾 Заказы", callback_data="admin:orders")],
            [InlineKeyboardButton(text="👥 Пользователи", callback_data="admin:users"), InlineKeyboardButton(text="🔐 Группа", callback_data="admin:group")],
            [InlineKeyboardButton(text="📨 Рассылка", callback_data="admin:broadcast"), InlineKeyboardButton(text="🛡 Проверки", callback_data="admin:health")],
            [InlineKeyboardButton(text="🏠 В меню", callback_data="home")],
        ]
    )


def order_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👤 Профиль", callback_data="profile")]])


async def create_invite(bot: Bot, order_id: int) -> str:
    group_id = setting("private_group_id")
    if not group_id:
        return ""
    link = await bot.create_chat_invite_link(
        chat_id=int(group_id),
        name=f"StandRise #{order_id}",
        member_limit=1,
        creates_join_request=False,
    )
    return link.invite_link


async def deliver_order(bot: Bot, order_id: int) -> None:
    with db() as con:
        order = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order or order["status"] != "paid":
        return

    invite = order["invite_link"] or ""
    if not invite:
        with suppress(Exception):
            invite = await create_invite(bot, order_id)

    access_key = order["access_key"] or keygen()
    access_until = order["access_until"] or (datetime.now(timezone.utc) + timedelta(days=ACCESS_DAYS)).isoformat()

    with db() as con:
        con.execute(
            "UPDATE orders SET access_key=?, invite_link=?, access_until=? WHERE id=?",
            (access_key, invite, access_until, order_id),
        )
        con.commit()

    text = (
        f"✅ <b>Покупка оплачена</b>\n\n"
        f"Товар: <b>{PRODUCT_NAME}</b>\n"
        f"Доступ: <b>{PLAN_TITLE}</b>\n"
        f"Ключ: <code>{access_key}</code>\n"
        f"Активен до: <b>{pretty_dt(access_until)}</b>\n\n"
    )
    if invite:
        text += f"🔐 Приватная группа:\n{invite}\n\nСсылка одноразовая."
    else:
        text += "🔐 Ссылка в приватную группу пока не создана. Админ уже получил уведомление."
        await bot.send_message(ADMIN_ID, "⚠️ Не указана приватная группа или бот не имеет прав создавать ссылки.")

    await bot.send_message(order["user_id"], text, reply_markup=order_kb())


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


async def keep_alive() -> None:
    if not PUBLIC_URL:
        return
    while True:
        with suppress(Exception):
            async with aiohttp.ClientSession() as session:
                await session.get(f"{PUBLIC_URL.rstrip('/')}/health", timeout=20)
        await asyncio.sleep(600)


@router.message(CommandStart())
async def start(message: Message):
    save_message_user(message)
    await message.answer(
        f"<b>⚡ {SHOP_NAME}</b>\n\n"
        f"<b>{PRODUCT_NAME}</b> — доступ в приватную группу и персональный ключ после оплаты.\n\n"
        f"Тариф сейчас один: <b>{PLAN_TITLE}</b> за <b>{PLAN_STARS} ⭐</b>.",
        reply_markup=main_kb(message.from_user.id),
    )


@router.callback_query(F.data == "home")
async def home(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        f"<b>⚡ {SHOP_NAME}</b>\n\nВыберите действие:",
        reply_markup=main_kb(call.from_user.id),
    )


@router.callback_query(F.data == "products")
async def products(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        "<b>🛍 Продукты</b>\n\nДоступный товар:",
        reply_markup=products_kb(),
    )


@router.callback_query(F.data == "buy:open")
async def buy_open(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text(
        f"<b>🚀 {PRODUCT_NAME}</b>\n\n"
        f"Доступ: <b>{PLAN_TITLE}</b>\n"
        f"Цена: <b>{PLAN_STARS} Telegram Stars</b>\n\n"
        "После оплаты бот выдаст ключ и одноразовую ссылку в приватную группу.",
        reply_markup=buy_kb(),
    )


@router.callback_query(F.data == f"pay:{PLAN_CODE}")
async def pay_stars(call: CallbackQuery, bot: Bot):
    save_user(call.from_user.id, call.from_user.username, call.from_user.first_name)
    payload = f"stars:{call.from_user.id}:{PLAN_CODE}:{int(datetime.now().timestamp() * 1000)}"
    with db() as con:
        cur = con.execute(
            """
            INSERT INTO orders(user_id,product,plan,amount,currency,status,payload,created_at)
            VALUES(?,?,?,?,?,'pending',?,?)
            """,
            (call.from_user.id, PRODUCT_NAME, PLAN_CODE, PLAN_STARS, "XTR", payload, now()),
        )
        order_id = cur.lastrowid
        con.commit()
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title=PRODUCT_NAME,
        description=f"{PLAN_TITLE} | заказ #{order_id}",
        payload=payload,
        currency="XTR",
        prices=[LabeledPrice(label=f"{PRODUCT_NAME} — {PLAN_TITLE}", amount=PLAN_STARS)],
        provider_token="",
    )
    await call.answer()


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    with db() as con:
        order = con.execute("SELECT * FROM orders WHERE payload=?", (query.invoice_payload,)).fetchone()
    ok = bool(
        order
        and order["status"] == "pending"
        and order["user_id"] == query.from_user.id
        and order["currency"] == query.currency
        and order["amount"] == query.total_amount
    )
    await query.answer(ok=ok, error_message=None if ok else "Заказ не найден или сумма изменилась.")


@router.message(F.content_type == ContentType.SUCCESSFUL_PAYMENT)
async def paid(message: Message, bot: Bot):
    payment = message.successful_payment
    with db() as con:
        order = con.execute("SELECT * FROM orders WHERE payload=?", (payment.invoice_payload,)).fetchone()
        if not order or order["user_id"] != message.from_user.id:
            return
        con.execute(
            "UPDATE orders SET status='paid', paid_at=?, charge_id=? WHERE id=? AND status='pending'",
            (now(), payment.telegram_payment_charge_id, order["id"]),
        )
        con.commit()
    await message.answer("✅ Оплата получена. Готовлю доступ…")
    await deliver_order(bot, order["id"])
    await bot.send_message(ADMIN_ID, f"💰 Новый заказ #{order['id']} от {message.from_user.id} на {PLAN_STARS} ⭐")


@router.callback_query(F.data == "profile")
async def profile(call: CallbackQuery):
    await call.answer()
    save_user(call.from_user.id, call.from_user.username, call.from_user.first_name)
    with db() as con:
        total = con.execute("SELECT COUNT(*) FROM orders WHERE user_id=?", (call.from_user.id,)).fetchone()[0]
        last = con.execute(
            "SELECT * FROM orders WHERE user_id=? AND status='paid' ORDER BY id DESC LIMIT 1",
            (call.from_user.id,),
        ).fetchone()
        user = con.execute("SELECT * FROM users WHERE user_id=?", (call.from_user.id,)).fetchone()
    username = f"@{call.from_user.username}" if call.from_user.username else "—"
    active = "нет"
    key = "—"
    until = "—"
    if last:
        key = last["access_key"] or "создаётся"
        until = pretty_dt(last["access_until"])
        with suppress(Exception):
            active = "да" if datetime.fromisoformat(last["access_until"]) > datetime.now(timezone.utc) else "истёк"
    await call.message.edit_text(
        f"<b>👤 Профиль</b>\n\n"
        f"ID: <code>{call.from_user.id}</code>\n"
        f"Username: <b>{username}</b>\n"
        f"Регистрация: <b>{pretty_dt(user['created_at'] if user else now())}</b>\n\n"
        f"Заказов всего: <b>{total}</b>\n"
        f"Активный доступ: <b>{active}</b>\n"
        f"Ключ: <code>{key}</code>\n"
        f"До: <b>{until}</b>",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🛍 Продукты", callback_data="products")],
                [InlineKeyboardButton(text="⬅️ В меню", callback_data="home")],
            ]
        ),
    )


@router.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def admin_cmd(message: Message):
    await message.answer("⚙️ <b>Админ-панель</b>", reply_markup=admin_kb())


@router.callback_query(F.data == "admin:menu", F.from_user.id == ADMIN_ID)
async def admin_menu(call: CallbackQuery):
    await call.answer()
    await call.message.edit_text("⚙️ <b>Админ-панель</b>\nВыберите раздел:", reply_markup=admin_kb())


@router.callback_query(F.data == "admin:stats", F.from_user.id == ADMIN_ID)
async def admin_stats(call: CallbackQuery):
    await call.answer()
    with db() as con:
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        orders = con.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        paid_count = con.execute("SELECT COUNT(*) FROM orders WHERE status='paid'").fetchone()[0]
        pending = con.execute("SELECT COUNT(*) FROM orders WHERE status='pending'").fetchone()[0]
        stars = con.execute("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='paid'").fetchone()[0]
    await call.message.edit_text(
        f"📊 <b>Статистика</b>\n\n"
        f"Пользователей: <b>{users}</b>\n"
        f"Заказов: <b>{orders}</b>\n"
        f"Ожидают: <b>{pending}</b>\n"
        f"Заработано: <b>{stars} ⭐</b>",
        reply_markup=admin_kb(),
    )


@router.callback_query(F.data == "admin:orders", F.from_user.id == ADMIN_ID)
async def admin_orders(call: CallbackQuery):
    await call.answer()
    with db() as con:
        rows = con.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 12").fetchall()
    text = "🧾 <b>Последние заказы</b>\n\n"
    text += "\n".join(
        f"#{r['id']} | <code>{r['user_id']}</code> | {r['status']} | {r['amount']}⭐ | {pretty_dt(r['created_at'])}"
        for r in rows
    ) or "Заказов пока нет."
    await call.message.edit_text(text, reply_markup=admin_kb())


@router.callback_query(F.data == "admin:users", F.from_user.id == ADMIN_ID)
async def admin_users(call: CallbackQuery):
    await call.answer()
    with db() as con:
        rows = con.execute("SELECT * FROM users ORDER BY last_seen DESC LIMIT 15").fetchall()
    text = "👥 <b>Пользователи</b>\n\n"
    text += "\n".join(
        f"<code>{r['user_id']}</code> | @{r['username'] or '-'} | {r['first_name'] or '-'}"
        for r in rows
    ) or "Пользователей пока нет."
    await call.message.edit_text(text, reply_markup=admin_kb())


@router.callback_query(F.data == "admin:group", F.from_user.id == ADMIN_ID)
async def admin_group(call: CallbackQuery, state: FSMContext):
    await call.answer()
    current = setting("private_group_id", "не указана")
    await state.set_state(AdminState.group_id)
    await call.message.edit_text(
        f"🔐 <b>Приватная группа</b>\n\n"
        f"Текущая: <code>{current}</code>\n\n"
        "Добавь бота админом в приватную группу с правом создавать ссылки.\n"
        "Затем отправь ID группы в формате <code>-100...</code>.",
        reply_markup=admin_kb(),
    )


@router.message(AdminState.group_id, F.from_user.id == ADMIN_ID)
async def save_group(message: Message, state: FSMContext, bot: Bot):
    raw = (message.text or "").strip()
    try:
        chat_id = int(raw)
        chat = await bot.get_chat(chat_id)
    except Exception:
        return await message.answer("Не смог проверить группу. Отправь правильный ID вида <code>-100...</code> и проверь, что бот добавлен туда админом.")
    set_setting("private_group_id", str(chat_id))
    await state.clear()
    await message.answer(f"✅ Группа сохранена: <b>{chat.title}</b>\nID: <code>{chat_id}</code>", reply_markup=admin_kb())


@router.callback_query(F.data == "admin:health", F.from_user.id == ADMIN_ID)
async def admin_health(call: CallbackQuery, bot: Bot):
    await call.answer()
    me = await bot.get_me()
    group_id = setting("private_group_id")
    group_status = "не указана"
    if group_id:
        try:
            member = await bot.get_chat_member(int(group_id), me.id)
            group_status = member.status.value
        except Exception:
            group_status = "ошибка доступа"
    await call.message.edit_text(
        f"🛡 <b>Проверки</b>\n\n"
        f"Бот: @{me.username}\n"
        f"Оплата Stars: <b>включена</b>\n"
        f"Группа: <code>{group_id or 'не указана'}</code>\n"
        f"Статус бота в группе: <b>{group_status}</b>\n"
        f"Health: <b>ok</b>",
        reply_markup=admin_kb(),
    )


@router.callback_query(F.data == "admin:broadcast", F.from_user.id == ADMIN_ID)
async def broadcast_entry(call: CallbackQuery, state: FSMContext):
    await call.answer()
    await state.set_state(AdminState.broadcast)
    await call.message.edit_text("📨 Отправьте текст рассылки.", reply_markup=admin_kb())


@router.message(AdminState.broadcast, F.from_user.id == ADMIN_ID)
async def broadcast_send(message: Message, bot: Bot, state: FSMContext):
    text = message.html_text or message.text or ""
    if not text.strip():
        return await message.answer("Отправьте текст.")
    with db() as con:
        ids = [r[0] for r in con.execute("SELECT user_id FROM users").fetchall()]
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
    await message.answer(f"✅ Рассылка завершена. Доставлено: {ok}, ошибок: {bad}", reply_markup=admin_kb())


async def main() -> None:
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
