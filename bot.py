"""
АвтоМастер Про — демо Telegram-бот для автосервиса (СТО)
Библиотека: aiogram 3.x (асинхронный подход), всё в одном файле.

Быстрый старт:
    1. pip install aiogram
    2. Впишите BOT_TOKEN и ADMIN_ID ниже
    3. python bot.py

Заявки сохраняются в файл orders.csv рядом с bot.py
(откройте его в Excel / Google Таблицах).
"""

import asyncio
import csv
import logging
import os
import re
import threading
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiohttp import web

try:  # часовой пояс для даты в заявках (если недоступен — берём время сервера)
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Europe/Kyiv")
except Exception:
    TZ = None

# =====================================================================
#                              НАСТРОЙКИ
# =====================================================================
# Локально впишите значения прямо сюда. На хостинге (Render) лучше задать
# переменные окружения BOT_TOKEN и ADMIN_ID — тогда токен не попадёт в GitHub.
BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВИТЬ_ТОКЕН")   # токен от @BotFather
ADMIN_ID = int(os.getenv("ADMIN_ID", "7066723368"))      # ваш Telegram ID (число)

# Только для БЕСПЛАТНОГО аккаунта PythonAnywhere укажите:
# PROXY_URL = "http://proxy.server:3128"
# Для запуска на своём компьютере оставьте None.
PROXY_URL = None

# Данные автосервиса (демо — замените на реальные)
SERVICE_NAME = "АвтоМастер Про"
ADDRESS = "г. Киев, ул. Автомобильная, 15"
WORK_HOURS = "Пн–Сб: 09:00–19:00\nВс: 10:00–16:00"
MASTER_PHONE = "+38 (050) 123-45-67"
LATITUDE = 50.4501
LONGITUDE = 30.5234

ORDERS_FILE = Path(__file__).with_name("orders.csv")

# =====================================================================
#                        ТЕКСТЫ КНОПОК И ДАННЫЕ
# =====================================================================
BTN_BOOK = "🔧 Записаться на ремонт"
BTN_PRICE = "💰 Прайс-лист"
BTN_CONTACTS = "📍 Контакты и адрес"
BTN_SOS = "🚨 Вызвать эвакуатор / Консультация"
BTN_CANCEL = "❌ Отмена"
BTN_SEND_PHONE = "📱 Отправить мой номер"

SERVICES = {
    "oil": "Замена масла и фильтров",
    "chassis": "Диагностика ходовой",
    "tires": "Шиномонтаж",
    "computer": "Компьютерная диагностика",
    "other": "Другое",
}

DAYS = {
    "today": "Сегодня",
    "tomorrow": "Завтра",
    "weekend": "На выходных",
}

PRICE_TEXT = (
    f"💰 <b>Прайс-лист «{SERVICE_NAME}»</b>\n"
    "\n"
    "🛢 <b>Обслуживание</b>\n"
    "• Замена масла двигателя (работа) — от 300 грн\n"
    "• Замена масла + масляный фильтр — от 450 грн\n"
    "• Замена воздушного / салонного фильтра — от 150 грн\n"
    "• Замена тормозных колодок (одна ось) — от 600 грн\n"
    "\n"
    "🔩 <b>Ходовая часть</b>\n"
    "• Диагностика ходовой — 350 грн\n"
    "• Замена амортизатора (1 шт.) — от 700 грн\n"
    "• Развал-схождение — от 800 грн\n"
    "\n"
    "🛞 <b>Шиномонтаж</b>\n"
    "• Сезонная замена колёс R13–R16 (4 шт.) — от 600 грн\n"
    "• Балансировка (1 колесо) — от 80 грн\n"
    "• Ремонт прокола — от 150 грн\n"
    "\n"
    "💻 <b>Диагностика</b>\n"
    "• Компьютерная диагностика — от 300 грн\n"
    "• Сброс ошибок — 150 грн\n"
    "\n"
    "<i>Цены ориентировочные: точную стоимость назовём после осмотра авто.</i>"
)


# =====================================================================
#                      СОСТОЯНИЯ (FSM — машина состояний)
# =====================================================================
class Booking(StatesGroup):
    service = State()  # шаг 1: выбор услуги
    car = State()      # шаг 2: марка, модель, год
    day = State()      # шаг 3: удобный день
    phone = State()    # шаг 4: телефон


class Emergency(StatesGroup):
    phone = State()    # срочная заявка: ждём телефон


# =====================================================================
#                            КЛАВИАТУРЫ
# =====================================================================
def main_menu() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text=BTN_BOOK)
    kb.button(text=BTN_PRICE)
    kb.button(text=BTN_CONTACTS)
    kb.button(text=BTN_SOS)
    kb.adjust(1, 2, 1)
    return kb.as_markup(resize_keyboard=True)


def phone_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text=BTN_SEND_PHONE, request_contact=True)
    kb.button(text=BTN_CANCEL)
    kb.adjust(1)
    return kb.as_markup(
        resize_keyboard=True,
        input_field_placeholder="Или напишите номер: +380...",
    )


def services_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, title in SERVICES.items():
        kb.button(text=title, callback_data=f"svc:{key}")
    kb.adjust(1)
    return kb.as_markup()


def days_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for key, title in DAYS.items():
        kb.button(text=title, callback_data=f"day:{key}")
    kb.adjust(3)
    return kb.as_markup()


def price_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🎁 Записаться со скидкой 10%", callback_data="book_discount")
    return kb.as_markup()


# =====================================================================
#                          ВСПОМОГАТЕЛЬНОЕ
# =====================================================================
def now_str() -> str:
    return datetime.now(TZ).strftime("%d.%m.%Y %H:%M:%S")


def normalize_phone(raw: str) -> Optional[str]:
    """Приводит номер к аккуратному виду. Если это не похоже на номер — None."""
    digits = re.sub(r"\D", "", raw or "")
    if not 10 <= len(digits) <= 15:
        return None
    if raw.strip().startswith("+") or len(digits) >= 11:
        return "+" + digits
    return digits  # например, 0501234567


def extract_phone(message: Message) -> Optional[str]:
    """Достаёт номер из контакта (кнопка) или из текста (ручной ввод)."""
    if message.contact:
        return normalize_phone(message.contact.phone_number)
    return normalize_phone(message.text or "")


def user_username(message: Message) -> str:
    u = message.from_user
    return f"@{u.username}" if u and u.username else "нет"


# ---- сохранение заявок в CSV --------------------------------------------
CSV_HEADERS = [
    "Дата создания", "Имя", "Username", "Услуга",
    "Авто", "День", "Телефон", "Примечание",
]
_csv_lock = threading.Lock()


def safe_cell(value: str) -> str:
    """Защита от «формул» в Excel: текст не должен начинаться с = + - @."""
    value = str(value)
    return "'" + value if value[:1] in ("=", "+", "-", "@") else value


def _write_order(order: dict) -> None:
    with _csv_lock:
        is_new = not ORDERS_FILE.exists()
        # utf-8-sig + разделитель ";" — чтобы Excel открыл файл без «кракозябр»
        with ORDERS_FILE.open("a", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f, delimiter=";")
            if is_new:
                writer.writerow(CSV_HEADERS)
            writer.writerow([
                order["created"],
                safe_cell(order["name"]),
                order["username"],
                order["service"],
                safe_cell(order["car"]),
                order["day"],
                order["phone"],
                order["note"],
            ])


async def save_order(order: dict) -> None:
    # запись в файл — синхронная операция, выносим её в отдельный поток
    await asyncio.to_thread(_write_order, order)


# ---- уведомления администратору ---------------------------------------
def admin_text(order: dict, user_id: int, urgent: bool = False) -> str:
    title = (
        "🔥🔥 <b>СРОЧНО! Эвакуатор / консультация</b> 🔥🔥"
        if urgent
        else "🆕 <b>Новая заявка на ремонт</b>"
    )
    lines = [
        title,
        "━━━━━━━━━━━━━━━━━━",
        f"👤 <b>Клиент:</b> <a href=\"tg://user?id={user_id}\">{escape(order['name'])}</a>",
        f"✉️ <b>Username:</b> {escape(order['username'])}",
        f"🛠 <b>Услуга:</b> {escape(order['service'])}",
        f"🚗 <b>Авто:</b> {escape(order['car'])}",
        f"📅 <b>День:</b> {escape(order['day'])}",
        f"📞 <b>Телефон:</b> <code>{escape(order['phone'])}</code>",
    ]
    if order["note"]:
        lines.append(f"🎁 <b>Примечание:</b> {escape(order['note'])}")
    lines += ["━━━━━━━━━━━━━━━━━━", f"🕒 {order['created']}"]
    return "\n".join(lines)


async def notify_admin(bot: Bot, text: str) -> None:
    try:
        await bot.send_message(ADMIN_ID, text)
    except TelegramAPIError as e:
        # Самая частая причина: админ ещё не нажал /start у бота или неверный ADMIN_ID
        logging.error("Не удалось отправить уведомление админу (ADMIN_ID=%s): %s", ADMIN_ID, e)


# =====================================================================
#                              ХЕНДЛЕРЫ
# =====================================================================
# ВАЖНО: порядок регистрации имеет значение. Сначала общие команды и кнопки меню
# (они работают в любом состоянии), потом шаги FSM, в самом конце — «заглушки».
router = Router()


# ---- /start, /cancel ---------------------------------------------------
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    name = escape(message.from_user.first_name) if message.from_user else "друг"
    await message.answer(
        f"👋 Здравствуйте, <b>{name}</b>!\n\n"
        f"Вас приветствует автосервис <b>«{SERVICE_NAME}»</b> 🚗\n"
        "Ремонт, диагностика, шиномонтаж — быстро, честно и с гарантией.\n\n"
        "Выберите нужный пункт в меню ниже 👇",
        reply_markup=main_menu(),
    )


@router.message(Command("cancel"))
@router.message(F.text == BTN_CANCEL)
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Отменил. Возвращаю в главное меню 👌", reply_markup=main_menu())


# ---- 🔧 Записаться на ремонт: старт сценария ------------------------------
async def begin_booking(target: Message, state: FSMContext, discount: bool) -> None:
    await state.clear()
    await state.set_state(Booking.service)
    await state.update_data(discount=discount)
    promo = "🎁 Скидка 10% будет учтена в вашей заявке.\n\n" if discount else ""
    await target.answer(
        "🔧 <b>Запись на ремонт</b>\n\n"
        f"{promo}"
        "<b>Шаг 1 из 4.</b> Выберите услугу:",
        reply_markup=services_kb(),
    )


@router.message(F.text == BTN_BOOK)
async def menu_book(message: Message, state: FSMContext) -> None:
    await begin_booking(message, state, discount=False)


# ---- 💰 Прайс-лист ---------------------------------------------------------
@router.message(F.text == BTN_PRICE)
async def menu_price(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(PRICE_TEXT, reply_markup=price_kb())


@router.callback_query(F.data == "book_discount")
async def cb_book_discount(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await begin_booking(callback.message, state, discount=True)


# ---- 📍 Контакты и адрес ---------------------------------------------------
@router.message(F.text == BTN_CONTACTS)
async def menu_contacts(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        f"📍 <b>{SERVICE_NAME}</b>\n\n"
        f"🏠 <b>Адрес:</b> {ADDRESS}\n"
        f"🕒 <b>График работы:</b>\n{WORK_HOURS}\n\n"
        f"📞 <b>Мастер-приёмщик:</b> {MASTER_PHONE}\n\n"
        "Мы на карте 👇"
    )
    await message.answer_location(latitude=LATITUDE, longitude=LONGITUDE)


# ---- 🚨 Эвакуатор / Консультация --------------------------------------------
@router.message(F.text == BTN_SOS)
async def menu_sos(message: Message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(Emergency.phone)
    await message.answer(
        "🚨 <b>Эвакуатор / срочная консультация</b>\n\n"
        "Отправьте номер телефона — администратор перезвонит вам в ближайшие минуты.\n"
        "Нажмите кнопку ниже или напишите номер вручную 👇",
        reply_markup=phone_kb(),
    )


@router.message(Emergency.phone, F.contact)
@router.message(Emergency.phone, F.text)
async def emergency_phone(message: Message, state: FSMContext, bot: Bot) -> None:
    phone = extract_phone(message)
    if phone is None:
        await message.answer(
            "Не похоже на номер телефона 🤔\n"
            "Напишите в формате <code>+380501234567</code> или нажмите кнопку ниже.",
            reply_markup=phone_kb(),
        )
        return

    await state.clear()
    user = message.from_user
    order = {
        "created": now_str(),
        "name": user.full_name,
        "username": user_username(message),
        "service": "🔥 СРОЧНО: эвакуатор / консультация",
        "car": "—",
        "day": "Срочно, сейчас",
        "phone": phone,
        "note": "СРОЧНО",
    }
    await save_order(order)
    await notify_admin(bot, admin_text(order, user.id, urgent=True))
    await message.answer(
        "✅ Заявка принята! Администратор свяжется с вами в самое ближайшее время.\n"
        f"Если срочно — звоните: {MASTER_PHONE}",
        reply_markup=main_menu(),
    )


# ---- Шаги записи на ремонт (FSM) -------------------------------------------
# Шаг 1: услуга
@router.callback_query(Booking.service, F.data.startswith("svc:"))
async def step_service(callback: CallbackQuery, state: FSMContext) -> None:
    service = SERVICES.get(callback.data.split(":", 1)[1])
    if service is None:
        await callback.answer("Неизвестная услуга", show_alert=True)
        return
    await state.update_data(service=service)
    await state.set_state(Booking.car)
    await callback.message.edit_text(f"✅ Услуга: <b>{escape(service)}</b>")
    await callback.message.answer(
        "<b>Шаг 2 из 4.</b> Напишите марку, модель и год выпуска вашего авто.\n"
        "Например: <i>Toyota Camry, 2015</i>"
    )
    await callback.answer()


# Шаг 2: авто
@router.message(Booking.car, F.text)
async def step_car(message: Message, state: FSMContext) -> None:
    car = message.text.strip()
    if not 3 <= len(car) <= 100:
        await message.answer("Напишите, пожалуйста, марку, модель и год. Например: <i>Skoda Octavia, 2018</i>")
        return
    await state.update_data(car=car)
    await state.set_state(Booking.day)
    await message.answer(
        f"✅ Авто: <b>{escape(car)}</b>\n\n"
        "<b>Шаг 3 из 4.</b> Выберите удобный день:",
        reply_markup=days_kb(),
    )


# Шаг 3: день
@router.callback_query(Booking.day, F.data.startswith("day:"))
async def step_day(callback: CallbackQuery, state: FSMContext) -> None:
    day = DAYS.get(callback.data.split(":", 1)[1])
    if day is None:
        await callback.answer("Неизвестный день", show_alert=True)
        return
    await state.update_data(day=day)
    await state.set_state(Booking.phone)
    await callback.message.edit_text(f"✅ День: <b>{escape(day)}</b>")
    await callback.message.answer(
        "<b>Шаг 4 из 4.</b> Оставьте номер телефона для связи.\n"
        "Нажмите кнопку ниже или напишите номер вручную 👇",
        reply_markup=phone_kb(),
    )
    await callback.answer()


# Шаг 4: телефон → финал
@router.message(Booking.phone, F.contact)
@router.message(Booking.phone, F.text)
async def step_phone(message: Message, state: FSMContext, bot: Bot) -> None:
    phone = extract_phone(message)
    if phone is None:
        await message.answer(
            "Не похоже на номер телефона 🤔\n"
            "Напишите в формате <code>+380501234567</code> или нажмите кнопку ниже.",
            reply_markup=phone_kb(),
        )
        return

    data = await state.get_data()
    await state.clear()
    user = message.from_user
    order = {
        "created": now_str(),
        "name": user.full_name,
        "username": user_username(message),
        "service": data.get("service", "—"),
        "car": data.get("car", "—"),
        "day": data.get("day", "—"),
        "phone": phone,
        "note": "Скидка 10%" if data.get("discount") else "",
    }
    await save_order(order)
    await notify_admin(bot, admin_text(order, user.id))

    promo = "\n🎁 Скидка 10% закреплена за вашей заявкой." if order["note"] else ""
    await message.answer(
        f"🎉 <b>Спасибо, {escape(user.first_name)}! Заявка принята.</b>\n\n"
        f"🛠 Услуга: {escape(order['service'])}\n"
        f"🚗 Авто: {escape(order['car'])}\n"
        f"📅 День: {escape(order['day'])}\n"
        f"📞 Телефон: {escape(order['phone'])}"
        f"{promo}\n\n"
        "Мастер-приёмщик перезвонит вам, чтобы подтвердить время. Хорошего дня! 🤝",
        reply_markup=main_menu(),
    )


# ---- «Заглушки»: если клиент ответил не так, как ждёт бот --------------------
@router.message(Booking.service)
@router.message(Booking.day)
async def need_buttons(message: Message) -> None:
    await message.answer("Пожалуйста, выберите вариант кнопкой в сообщении выше 👆\nИли отправьте /cancel, чтобы отменить запись.")


@router.message(Booking.car)
async def need_car_text(message: Message) -> None:
    await message.answer("Напишите марку, модель и год авто текстом ✍️\nНапример: <i>Toyota Camry, 2015</i>")


@router.message(Booking.phone)
@router.message(Emergency.phone)
async def need_phone(message: Message) -> None:
    await message.answer(
        "Мне нужен номер телефона 📞 Нажмите кнопку ниже или напишите номер текстом.",
        reply_markup=phone_kb(),
    )


@router.callback_query()
async def stale_callback(callback: CallbackQuery) -> None:
    await callback.answer(
        "Эта кнопка уже неактуальна. Нажмите «Записаться на ремонт», чтобы начать заново.",
        show_alert=True,
    )


@router.message()
async def unknown_message(message: Message) -> None:
    await message.answer(
        "Не совсем понял 🤔 Воспользуйтесь кнопками меню ниже 👇",
        reply_markup=main_menu(),
    )


# =====================================================================
#                              ЗАПУСК
# =====================================================================
async def start_web_server() -> None:
    """
    Render ждёт, что веб-сервис слушает порт из переменной PORT.
    Поднимаем крошечный HTTP-сервер: на адрес /health он отвечает "OK".
    Этот же адрес можно «пинговать» внешним сервисом, чтобы бесплатный
    сервис не засыпал. Локально переменной PORT нет — сервер не запускается.
    """
    port = os.getenv("PORT")
    if not port:
        return

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    logging.info("🌐 Health-сервер запущен на порту %s", port)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    if BOT_TOKEN == "ВСТАВИТЬ_ТОКЕН":
        raise SystemExit("❌ Откройте bot.py и впишите свой токен в BOT_TOKEN (его выдаёт @BotFather).")

    session = AiohttpSession(proxy=PROXY_URL)  # PROXY_URL=None — работает без прокси
    bot = Bot(
        token=BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    me = await bot.get_me()
    logging.info("✅ Бот @%s запущен. Остановка: Ctrl+C", me.username)

    await start_web_server()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit) as e:
        if isinstance(e, SystemExit) and e.code:
            print(e.code)
        else:
            print("Бот остановлен.")