"""
Бот управления с кнопками под полем ввода (reply-клавиатура).
Доступен только ADMIN_ID.
"""
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton

from config import BOT_TOKEN, ADMIN_ID
import db

log = logging.getLogger("bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
dp.message.filter(F.from_user.id == ADMIN_ID)


# ---------- Клавиатура ----------

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Статус"), KeyboardButton(text="🛒 Купить аккаунты")],
        [KeyboardButton(text="💰 Лимит в день"), KeyboardButton(text="📥 Цена покупки")],
        [KeyboardButton(text="📤 Цена продажи"), KeyboardButton(text="📝 Шаблон заголовка")],
        [KeyboardButton(text="⏱ Интервал поиска"), KeyboardButton(text="❌ Отмена")],
    ],
    resize_keyboard=True,
)

CANCEL_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отмена")]],
    resize_keyboard=True,
)


# ---------- Состояния ожидания ввода ----------

class Form(StatesGroup):
    buy_count = State()
    daily_limit = State()
    buy_price = State()
    sell_price = State()
    title_template = State()
    interval = State()


async def send_log(text: str):
    try:
        await bot.send_message(ADMIN_ID, text)
    except Exception:
        log.exception("Не удалось отправить сообщение админу")


# ---------- Старт / отмена ----------

@dp.message(Command("start", "help"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Бот управления Lolz Market.\nВыбери действие на клавиатуре ниже 👇",
        reply_markup=MAIN_MENU,
    )


@dp.message(F.text == "❌ Отмена")
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=MAIN_MENU)


# ---------- Статус ----------

@dp.message(F.text == "📊 Статус")
async def show_status(message: Message):
    settings = await db.get_settings()
    spent = await db.get_today_spent()
    remaining = await db.queue_remaining()
    purchases = await db.recent_purchases(5)

    lines = [
        f"Потрачено сегодня: {spent}₽ из {settings.get('daily_limit')}₽",
        f"В очереди на покупку: {remaining}",
        f"Цена покупки: {settings.get('buy_min_price')}–{settings.get('buy_max_price')}₽",
        f"Цена продажи: {settings.get('sell_price')}₽",
        f"Интервал поиска: {settings.get('search_interval_sec')} сек",
        "",
        "Последние покупки:",
    ]
    if purchases:
        for p in purchases:
            lines.append(f"#{p['item_id']} {p['status']} ({p['price']}₽, {p['country']})")
    else:
        lines.append("пока нет")
    await message.answer("\n".join(lines), reply_markup=MAIN_MENU)


# ---------- Купить аккаунты ----------

@dp.message(F.text == "🛒 Купить аккаунты")
async def ask_buy_count(message: Message, state: FSMContext):
    await state.set_state(Form.buy_count)
    await message.answer("Сколько аккаунтов купить? Напиши число.", reply_markup=CANCEL_KB)


@dp.message(StateFilter(Form.buy_count))
async def set_buy_count(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Нужно просто число, например 3.")
        return
    n = int(message.text)
    await db.queue_add(n)
    await state.clear()
    await message.answer(f"Добавлено в очередь на покупку: {n} ✅", reply_markup=MAIN_MENU)


# ---------- Лимит в день ----------

@dp.message(F.text == "💰 Лимит в день")
async def ask_daily_limit(message: Message, state: FSMContext):
    current = await db.get_setting("daily_limit")
    await state.set_state(Form.daily_limit)
    await message.answer(
        f"Текущий дневной лимит: {current}₽\nНапиши новое число в рублях.",
        reply_markup=CANCEL_KB,
    )


@dp.message(StateFilter(Form.daily_limit))
async def set_daily_limit(message: Message, state: FSMContext):
    if not message.text.replace(".", "", 1).isdigit():
        await message.answer("Нужно число, например 30.")
        return
    await db.set_setting("daily_limit", message.text)
    await state.clear()
    await message.answer(f"Дневной лимит установлен: {message.text}₽ ✅", reply_markup=MAIN_MENU)


# ---------- Цена покупки ----------

@dp.message(F.text == "📥 Цена покупки")
async def ask_buy_price(message: Message, state: FSMContext):
    mn = await db.get_setting("buy_min_price")
    mx = await db.get_setting("buy_max_price")
    await state.set_state(Form.buy_price)
    await message.answer(
        f"Текущая цена покупки: {mn}–{mx}₽\n"
        f"Напиши новый диапазон через пробел, например: 5 7",
        reply_markup=CANCEL_KB,
    )


@dp.message(StateFilter(Form.buy_price))
async def set_buy_price(message: Message, state: FSMContext):
    parts = message.text.split()
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        await message.answer("Напиши два числа через пробел, например: 5 7")
        return
    await db.set_setting("buy_min_price", parts[0])
    await db.set_setting("buy_max_price", parts[1])
    await state.clear()
    await message.answer(f"Цена покупки: {parts[0]}–{parts[1]}₽ ✅", reply_markup=MAIN_MENU)


# ---------- Цена продажи ----------

@dp.message(F.text == "📤 Цена продажи")
async def ask_sell_price(message: Message, state: FSMContext):
    current = await db.get_setting("sell_price")
    await state.set_state(Form.sell_price)
    await message.answer(f"Текущая цена продажи: {current}₽\nНапиши новую цену.", reply_markup=CANCEL_KB)


@dp.message(StateFilter(Form.sell_price))
async def set_sell_price(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Нужно число, например 11.")
        return
    await db.set_setting("sell_price", message.text)
    await state.clear()
    await message.answer(f"Цена продажи: {message.text}₽ ✅", reply_markup=MAIN_MENU)


# ---------- Шаблон заголовка ----------

@dp.message(F.text == "📝 Шаблон заголовка")
async def ask_title(message: Message, state: FSMContext):
    current = await db.get_setting("title_template")
    await state.set_state(Form.title_template)
    await message.answer(
        f"Текущий шаблон:\n{current}\n\n"
        f"Напиши новый. Используй {{code}} для кода страны и {{country}} для названия.\n"
        f"Пример: +{{code}} {{country}} | Чистый | Не трогал",
        reply_markup=CANCEL_KB,
    )


@dp.message(StateFilter(Form.title_template))
async def set_title(message: Message, state: FSMContext):
    await db.set_setting("title_template", message.text)
    await state.clear()
    await message.answer("Шаблон заголовка обновлён ✅", reply_markup=MAIN_MENU)


# ---------- Интервал поиска ----------

@dp.message(F.text == "⏱ Интервал поиска")
async def ask_interval(message: Message, state: FSMContext):
    current = await db.get_setting("search_interval_sec")
    await state.set_state(Form.interval)
    await message.answer(
        f"Текущий интервал: {current} сек\nНапиши новое число секунд.",
        reply_markup=CANCEL_KB,
    )


@dp.message(StateFilter(Form.interval))
async def set_interval(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Нужно число секунд, например 60.")
        return
    await db.set_setting("search_interval_sec", message.text)
    await state.clear()
    await message.answer(f"Интервал поиска: {message.text} сек ✅", reply_markup=MAIN_MENU)
