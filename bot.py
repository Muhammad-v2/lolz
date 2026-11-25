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
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from config import ADMIN_ID, BOT_TOKEN
import db
from lolz_client import LolzClient, LolzError

log = logging.getLogger("bot")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
dp.message.filter(F.from_user.id == ADMIN_ID)
dp.callback_query.filter(F.from_user.id == ADMIN_ID)

_lolz_client: LolzClient | None = None


def _get_lolz_client() -> LolzClient:
    global _lolz_client
    if _lolz_client is None:
        _lolz_client = LolzClient()
    return _lolz_client


MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Статус"), KeyboardButton(text="🛒 Очередь покупок")],
        [KeyboardButton(text="📥 Цена покупки")],
        [KeyboardButton(text="📤 Цена продажи"), KeyboardButton(text="📝 Шаблон заголовка")],
        [KeyboardButton(text="⏱ Интервал поиска"), KeyboardButton(text="🎛 Фильтры поиска")],
        [KeyboardButton(text="📦 На продаже"), KeyboardButton(text="✅ Проданные")],
        [KeyboardButton(text="⚠️ Проблемные")],
        [KeyboardButton(text="⏸ Пауза"), KeyboardButton(text="▶️ Запустить")],
        [KeyboardButton(text="❌ Отмена")],
    ],
    resize_keyboard=True,
)

FILTERS_MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🚫 Спам-блок"), KeyboardButton(text="🔒 Пароль")],
        [KeyboardButton(text="⏳ Возраст сессии")],
        [KeyboardButton(text="🔍 Проверка через SpamBot")],
        [KeyboardButton(text="⬅️ Назад в меню")],
    ],
    resize_keyboard=True,
)

YES_NO_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Да"), KeyboardButton(text="Нет"), KeyboardButton(text="Неважно")],
        [KeyboardButton(text="❌ Отмена")],
    ],
    resize_keyboard=True,
)

CANCEL_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отмена")]],
    resize_keyboard=True,
)


class Form(StatesGroup):
    edit_item_price = State()
    buy_count = State()
    buy_price = State()
    sell_price = State()
    title_template = State()
    interval = State()
    filter_spam = State()
    filter_password = State()
    session_age = State()


async def send_log(text: str):
    try:
        await bot.send_message(ADMIN_ID, text)
    except Exception:
        log.exception("Не удалось отправить сообщение админу")


@dp.message(Command("start", "help"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Это бот автопокупки и перепродажи Telegram-аккаунтов на Lolz Market.\n"
        "Выбирай действие кнопками ниже:",
        reply_markup=MAIN_MENU,
    )


@dp.message(F.text == "❌ Отмена")
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=MAIN_MENU)


@dp.message(F.text == "📊 Статус")
async def cmd_status(message: Message):
    s = await db.get_settings()
    enabled = await db.is_enabled()
    queue = await db.get_queue()
    spent = await db.get_today_spent()
    count = await db.get_today_count()
    recent = await db.recent_purchases(5)

    try:
        balance = await _get_lolz_client().get_balance()
        bal_str = f"{balance}₽"
    except LolzError as e:
        bal_str = f"ошибка ({e})"

    lines = [
        f"Состояние: {'▶️ Работает' if enabled else '⏸ На паузе'}",
        f"Баланс Lolz: {bal_str}",
        f"В очереди на покупку: {queue} шт.",
        f"Куплено сегодня: {count} шт. на {spent:.2f}₽",
        f"Цена покупки: {s['buy_min_price']}–{s['buy_max_price']}₽",
        f"Цена продажи: {s['sell_price']}₽",
        f"Интервал поиска: {s['search_interval_sec']} сек",
        "",
        "Последние покупки:",
    ]
    if not recent:
        lines.append("— пока нет")
    else:
        for p in recent:
            lines.append(
                f"#{p['item_id']} | {p['price']}₽ | {p['phone'] or '—'} | {p['status']}"
            )
    await message.answer("\n".join(lines), reply_markup=MAIN_MENU)


@dp.message(F.text == "🛒 Очередь покупок")
async def ask_queue(message: Message, state: FSMContext):
    current = await db.get_queue()
    await state.set_state(Form.buy_count)
    await message.answer(
        f"Сейчас в очереди: {current} шт.\n"
        "Напиши число:\n"
        "• просто 5 — установить 5\n"
        "• +3 или -2 — прибавить/убавить",
        reply_markup=CANCEL_KB,
    )


@dp.message(StateFilter(Form.buy_count))
async def set_queue(message: Message, state: FSMContext):
    txt = message.text.strip()
    if txt.startswith(("+", "-")) and txt[1:].isdigit():
        new_val = await db.add_queue(int(txt))
    elif txt.isdigit():
        await db.set_queue(int(txt))
        new_val = int(txt)
    else:
        await message.answer("Нужно целое число (например 5, +2 или -1).")
        return
    await state.clear()
    await message.answer(f"Очередь обновлена: {new_val} шт. ✅", reply_markup=MAIN_MENU)


@dp.message(F.text == "⏸ Пауза")
async def pause_worker(message: Message):
    await db.set_enabled(False)
    await message.answer("Поставил на паузу ⏸ (бот работает, цикл стоит)", reply_markup=MAIN_MENU)


@dp.message(F.text == "▶️ Запустить")
async def resume_worker(message: Message):
    await db.set_enabled(True)
    await message.answer("Поиск и покупка снова работают ▶️", reply_markup=MAIN_MENU)


@dp.message(F.text == "📥 Цена покупки")
async def ask_buy_price(message: Message, state: FSMContext):
    mn = await db.get_setting("buy_min_price")
    mx = await db.get_setting("buy_max_price")
    await state.set_state(Form.buy_price)
    await message.answer(
        f"Текущая цена покупки: {mn}–{mx}₽\n"
        "Напиши новый диапазон через пробел, например: 5 7",
        reply_markup=CANCEL_KB,
    )


@dp.message(StateFilter(Form.buy_price))
async def set_buy_price(message: Message, state: FSMContext):
    parts = message.text.split()
    if len(parts) != 2 or not all(p.replace(".", "", 1).isdigit() for p in parts):
        await message.answer("Напиши два числа через пробел, например: 5 7")
        return
    await db.set_setting("buy_min_price", parts[0])
    await db.set_setting("buy_max_price", parts[1])
    await state.clear()
    await message.answer(f"Цена покупки: {parts[0]}–{parts[1]}₽ ✅", reply_markup=MAIN_MENU)


@dp.message(F.text == "📤 Цена продажи")
async def ask_sell_price(message: Message, state: FSMContext):
    current = await db.get_setting("sell_price")
    await state.set_state(Form.sell_price)
    await message.answer(f"Текущая цена продажи: {current}₽\nНапиши новую цену.", reply_markup=CANCEL_KB)


@dp.message(StateFilter(Form.sell_price))
async def set_sell_price(message: Message, state: FSMContext):
    if not message.text.replace(".", "", 1).isdigit():
        await message.answer("Нужно число, например 11.")
        return
    await db.set_setting("sell_price", message.text)
    await state.clear()
    await message.answer(f"Цена продажи: {message.text}₽ ✅", reply_markup=MAIN_MENU)


@dp.message(F.text == "📝 Шаблон заголовка")
async def ask_title(message: Message, state: FSMContext):
    current = await db.get_setting("title_template")
    await state.set_state(Form.title_template)
    await message.answer(
        f"Текущий шаблон:\n{current}\n\n"
        "Напиши новый. Используй {code} для кода страны и {country} для названия.\n"
        "Пример: +{code} {country} | Чистый | Не трогал",
        reply_markup=CANCEL_KB,
    )


@dp.message(StateFilter(Form.title_template))
async def set_title(message: Message, state: FSMContext):
    await db.set_setting("title_template", message.text)
    await state.clear()
    await message.answer("Шаблон заголовка обновлён ✅", reply_markup=MAIN_MENU)


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


YES_NO_MAP = {"да": "yes", "нет": "no", "неважно": "nomatter"}
YES_NO_LABEL = {"yes": "да", "no": "нет", "nomatter": "неважно"}


@dp.message(F.text == "🎛 Фильтры поиска")
async def open_filters(message: Message, state: FSMContext):
    await state.clear()
    spam = await db.get_setting("filter_spam")
    password = await db.get_setting("filter_password")
    sess_v = await db.get_setting("session_age_value")
    sess_p = await db.get_setting("session_age_period")
    await message.answer(
        "Текущие фильтры поиска:\n"
        f"🚫 Спам-блок: {YES_NO_LABEL.get(spam, spam)}\n"
        f"🔒 Пароль: {YES_NO_LABEL.get(password, password)}\n"
        f"⏳ Возраст сессии: больше {sess_v} {sess_p}\n"
        f"🔍 Проверка через SpamBot: {'включена (дольше, точнее)' if (await db.get_setting('check_spam_block')) != '0' else 'выключена (быстрее)'}\n\n"
        "Выбери, что изменить:",
        reply_markup=FILTERS_MENU,
    )


@dp.message(F.text == "🔍 Проверка через SpamBot")
async def toggle_spam_check(message: Message):
    current = (await db.get_setting("check_spam_block")) != "0"
    new_value = "0" if current else "1"
    await db.set_setting("check_spam_block", new_value)
    label = "включена" if new_value == "1" else "выключена"
    await message.answer(
        f"Проверка спам-блока через @SpamBot теперь {label}.",
        reply_markup=FILTERS_MENU,
    )


@dp.message(F.text == "⬅️ Назад в меню")
async def back_to_main(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=MAIN_MENU)


@dp.message(F.text == "🚫 Спам-блок")
async def ask_spam(message: Message, state: FSMContext):
    await state.set_state(Form.filter_spam)
    await message.answer(
        "Искать аккаунты со спам-блоком?\n"
        "«Да» — только со спам-блоком\n«Нет» — только без него\n«Неважно» — любые",
        reply_markup=YES_NO_KB,
    )


@dp.message(StateFilter(Form.filter_spam))
async def set_spam(message: Message, state: FSMContext):
    value = YES_NO_MAP.get(message.text.strip().lower())
    if not value:
        await message.answer("Выбери один из вариантов на клавиатуре.")
        return
    await db.set_setting("filter_spam", value)
    await state.clear()
    await message.answer(f"Спам-блок: {YES_NO_LABEL[value]} ✅", reply_markup=FILTERS_MENU)


@dp.message(F.text == "🔒 Пароль")
async def ask_password(message: Message, state: FSMContext):
    await state.set_state(Form.filter_password)
    await message.answer(
        "Фильтр облачного пароля (2FA):",
        reply_markup=YES_NO_KB,
    )


@dp.message(StateFilter(Form.filter_password))
async def set_password(message: Message, state: FSMContext):
    value = YES_NO_MAP.get(message.text.strip().lower())
    if not value:
        await message.answer("Выбери один из вариантов на клавиатуре.")
        return
    await db.set_setting("filter_password", value)
    await state.clear()
    await message.answer(f"Пароль: {YES_NO_LABEL[value]} ✅", reply_markup=FILTERS_MENU)


@dp.message(F.text == "⏳ Возраст сессии")
async def ask_session_age(message: Message, state: FSMContext):
    await state.set_state(Form.session_age)
    await message.answer(
        "Напиши минимальный возраст сессии, например: 1 day, 12 hour, 7 day",
        reply_markup=CANCEL_KB,
    )


@dp.message(StateFilter(Form.session_age))
async def set_session_age(message: Message, state: FSMContext):
    parts = message.text.strip().lower().split()
    if len(parts) != 2 or not parts[0].isdigit() or parts[1] not in ("hour", "day", "week", "month"):
        await message.answer("Формат: <число> <hour|day|week|month>, например: 1 day")
        return
    await db.set_setting("session_age_value", parts[0])
    await db.set_setting("session_age_period", parts[1])
    await state.clear()
    await message.answer(f"Возраст сессии: {parts[0]} {parts[1]} ✅", reply_markup=FILTERS_MENU)


SPAM_LABEL = {
    "free": "🟢 чистый",
    "limited": "🔴 спам-блок",
    "unknown": "⚪️ не проверено",
}


def _items_list_keyboard(purchases, prefix: str) -> InlineKeyboardMarkup:
    rows = []
    for p in purchases[:20]:
        rows.append([
            InlineKeyboardButton(
                text=f"#{p['item_id']} · {p['phone'] or '—'} · {p['sell_price'] or p['price']}₽",
                callback_data=f"{prefix}:{p['item_id']}",
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.message(F.text == "📦 На продаже")
async def list_listed_items(message: Message, state: FSMContext):
    await state.clear()
    purchases = await db.list_listed_purchases()
    if not purchases:
        await message.answer("Сейчас нет товаров на продаже.", reply_markup=MAIN_MENU)
        return
    lines = ["Товары на продаже:\n"]
    for i, p in enumerate(purchases, start=1):
        lines.append(f"{i}. #{p['item_id']} — {p['sell_price'] or '—'}₽ ({p['phone'] or '—'})")
    await message.answer("\n".join(lines), reply_markup=_items_list_keyboard(purchases, "item"))


@dp.message(F.text == "✅ Проданные")
async def list_sold_items(message: Message, state: FSMContext):
    await state.clear()
    purchases = await db.list_sold_purchases()
    if not purchases:
        await message.answer(
            "Проданных товаров пока нет (статус 'sold' ставится вручную).",
            reply_markup=MAIN_MENU,
        )
        return
    lines = ["Проданные товары:\n"]
    for i, p in enumerate(purchases, start=1):
        lines.append(
            f"{i}. #{p['item_id']} — куплен за {p['price']}₽, продан за {p['sell_price'] or '—'}₽"
        )
    await message.answer(
        "\n".join(lines),
        reply_markup=_items_list_keyboard(purchases, "item"),
    )


@dp.message(F.text == "⚠️ Проблемные")
async def list_problem_items(message: Message, state: FSMContext):
    await state.clear()
    purchases = await db.list_problem_purchases()
    if not purchases:
        await message.answer("Проблемных аккаунтов нет 👍", reply_markup=MAIN_MENU)
        return
    lines = ["Аккаунты с доступом, но без выставления на продажу:\n"]
    for i, p in enumerate(purchases, start=1):
        lines.append(f"{i}. #{p['item_id']} — статус: {p['status']}")
    await message.answer(
        "\n".join(lines),
        reply_markup=_items_list_keyboard(purchases, "item"),
    )


@dp.callback_query(F.data.startswith("item:"))
async def show_item_card(callback: CallbackQuery):
    item_id = int(callback.data.split(":", 1)[1])
    p = await db.get_purchase(item_id)
    if not p:
        await callback.answer("Не найдено", show_alert=True)
        return

    spam_label = SPAM_LABEL.get(p["spam_status"] or "unknown", "⚪️ не проверено")
    hex_key = p["hex_key"] or "—"
    hex_short = (hex_key[:20] + "…") if len(hex_key) > 20 else hex_key

    text = (
        f"📱 Аккаунт #{item_id}\n\n"
        f"Телефон: {p['phone'] or '—'}\n"
        f"User ID: {p['telegram_user_id'] or '—'}\n"
        f"Страна: {p['country'] or '—'} ({p['country_code'] or '—'})\n"
        f"Спам-блок: {spam_label}\n\n"
        f"Куплен за: {p['price']}₽\n"
        f"Продаётся за: {p['sell_price'] or '—'}₽\n"
        f"Статус: {p['status']}\n\n"
        f"HEX: {hex_short}\n"
        f"DC: {p['dc_id'] or '—'}\n\n"
        f"🔗 Лот покупки: {p['buy_url'] or f'https://lzt.market/{item_id}/'}\n"
        f"🔗 Лот продажи: {p['sell_url'] or '—'}"
    )

    buttons = []
    if p["status"] == "listed":
        buttons.append([InlineKeyboardButton(text="✏️ Изменить цену", callback_data=f"editprice:{item_id}")])
        buttons.append([InlineKeyboardButton(text="✅ Пометить проданным", callback_data=f"marksold:{item_id}")])
    elif p["hex_key"] and p["status"] not in ("sold",):
        buttons.append([InlineKeyboardButton(text="🔁 Повторить выставление", callback_data=f"relist:{item_id}")])
    buttons.append([InlineKeyboardButton(text="📋 Полный HEX (в личку)", callback_data=f"fullhex:{item_id}")])

    await callback.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@dp.callback_query(F.data.startswith("fullhex:"))
async def send_full_hex(callback: CallbackQuery):
    item_id = int(callback.data.split(":", 1)[1])
    p = await db.get_purchase(item_id)
    if not p or not p["hex_key"]:
        await callback.answer("Нет данных", show_alert=True)
        return
    await callback.message.answer(f"HEX для #{item_id}:\n`{p['hex_key']}`\nDC: {p['dc_id']}")
    await callback.answer()


@dp.callback_query(F.data.startswith("relist:"))
async def retry_listing(callback: CallbackQuery):
    item_id = int(callback.data.split(":", 1)[1])
    p = await db.get_purchase(item_id)
    if not p or not p["hex_key"] or not p["dc_id"]:
        await callback.answer("Нет сохранённых HEX/DC для повтора", show_alert=True)
        return

    sell_price = float(await db.get_setting("sell_price"))
    title_tpl = await db.get_setting("title_template")
    clean_code = (p["country_code"] or "?").lstrip("+")
    try:
        title = title_tpl.format(code=clean_code, country=p["country"] or "—")
    except Exception:
        title = f"+{clean_code} {p['country'] or '—'} | Чистый | Не трогал"

    after_purchase_note = await db.get_setting("sell_description")
    buyer_note = await db.get_setting("sell_buyer_note")
    information = f"{after_purchase_note}\n{buyer_note}".strip()

    try:
        client = _get_lolz_client()
        sell_result = await client.list_for_sale(
            title=title,
            price=sell_price,
            description="",
            information=information,
            hex_key=p["hex_key"],
            dc_id=p["dc_id"],
        )
    except LolzError as e:
        await callback.message.answer(f"❌ Снова не получилось: {e}")
        await callback.answer()
        return

    sell_item_id = None
    for key in ("item_id", "itemId", "id"):
        val = sell_result.get(key) if isinstance(sell_result, dict) else None
        if val:
            sell_item_id = int(val)
            break

    await db.update_purchase_status(item_id, "listed")
    await db.set_purchase_extra(
        item_id,
        sell_item_id=sell_item_id,
        sell_price=sell_price,
        sell_url=f"https://lzt.market/{sell_item_id}/" if sell_item_id else None,
    )
    await callback.message.answer(f"✅ Аккаунт #{item_id} выставлен на продажу за {sell_price}₽")
    await callback.answer()


@dp.callback_query(F.data.startswith("marksold:"))
async def mark_item_sold(callback: CallbackQuery):
    item_id = int(callback.data.split(":", 1)[1])
    await db.mark_sold(item_id)
    await callback.message.answer(f"Аккаунт #{item_id} помечен как проданный ✅")
    await callback.answer()


@dp.callback_query(F.data.startswith("editprice:"))
async def ask_new_item_price(callback: CallbackQuery, state: FSMContext):
    item_id = int(callback.data.split(":", 1)[1])
    await state.set_state(Form.edit_item_price)
    await state.update_data(edit_item_id=item_id)
    await callback.message.answer(
        f"Напиши новую цену для #{item_id} (в рублях):",
        reply_markup=CANCEL_KB,
    )
    await callback.answer()


@dp.message(StateFilter(Form.edit_item_price))
async def set_new_item_price(message: Message, state: FSMContext):
    if not message.text.replace(".", "", 1).isdigit():
        await message.answer("Нужно число, например 12.")
        return
    data = await state.get_data()
    item_id = data.get("edit_item_id")
    new_price = float(message.text)

    p = await db.get_purchase(item_id)
    target_item_id = (p["sell_item_id"] if p and p["sell_item_id"] else item_id)

    try:
        client = _get_lolz_client()
        await client.edit_item_price(int(target_item_id), new_price)
    except LolzError as e:
        await message.answer(f"❌ Не удалось изменить цену: {e}", reply_markup=MAIN_MENU)
        await state.clear()
        return

    await db.set_purchase_extra(item_id, sell_price=new_price)
    await state.clear()
    await message.answer(f"Цена #{item_id} изменена на {new_price}₽ ✅", reply_markup=MAIN_MENU)
