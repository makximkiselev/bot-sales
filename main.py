# 📦 Telegram бот: создание заказа поставщику с Google Sheets
# Автор: OpenAI ChatGPT (2025)

import logging
import sqlite3
from datetime import datetime
from aiogram import Bot, Dispatcher, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.enums import ParseMode
from aiogram.filters import Command
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from aiogram import types
from aiogram.types import InputTextMessageContent
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import Update
import asyncio

# 🔧 Настройки
BOT_TOKEN = "7957981552:AAE33m7eOVbce18vdk5BNjxQbwqKyZcMQH4"
GOOGLE_SHEET_KEY = "1OTHRzo4OAH_bWuplxYjwqpyJm857mYm0kaG0RHhHcOY"
CREDENTIALS_FILE = "credentials.json"

# 📊 Google Sheets
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
gclient = gspread.authorize(creds)
sheet = gclient.open_by_key(GOOGLE_SHEET_KEY).sheet1

# 🗄️ БД
conn = sqlite3.connect("/home/ubuntu/bot-sales/data/orders.db", check_same_thread=False)
c = conn.cursor()
c.execute("""
CREATE TABLE IF NOT EXISTS supplier_orders (
    order_id TEXT,
    date TEXT,
    supplier TEXT,
    product_name TEXT,
    quantity INTEGER,
    unit_price REAL,
    total_price REAL,
    serials TEXT,
    product_code TEXT
)
""")
c.execute("""
CREATE TABLE IF NOT EXISTS warehouse (
    serial TEXT PRIMARY KEY,
    product_name TEXT,
    order_id TEXT,
    unit_price REAL
)
""")
c.execute("""
CREATE TABLE IF NOT EXISTS suppliers (
    name TEXT PRIMARY KEY
)
""")
conn.commit()
logging.info("✅ Серийники и заказ успешно обновлены и сохранены в базе данных")

# 📦 FSM состояния

class OrderState(StatesGroup):
    choosing_date = State()
    entering_custom_date = State()
    entering_supplier = State()
    confirming_supplier = State()
    searching_product = State()
    choosing_product = State()
    confirming_product = State()
    entering_quantity = State()
    choosing_serial_mode = State()
    entering_serial = State()
    editing_order = State()
    entering_price = State()
    confirming_summary = State()
    entering_serial_new = State()
    entering_serial_existing = State()

# 🧠 Память
storage = MemoryStorage()
from aiogram.client.default import DefaultBotProperties
bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=storage)
router = Router()
temp_storage = {}

# ⌨️ Клавиатуры
def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Создать заказ поставщику", callback_data="create_order")],
        [InlineKeyboardButton(text="📋 Сводка", callback_data="summary_menu")]
    ])

def date_choice_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сегодня", callback_data="today")],
        [InlineKeyboardButton(text="Иная дата", callback_data="other_date")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    ])

def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    ])

def supplier_confirm_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать нового", callback_data="create_supplier")],
        [InlineKeyboardButton(text="🔍 Поиск заново", callback_data="search_supplier_again")]
    ])

# 🔍 Поиск товара
def search_products(query):
    values = sheet.get_all_values()[1:]
    results = []
    for row in values:
        if len(row) >= 6:
            code = row[4].strip()
            name = row[5].strip()
            if query.lower() in code.lower() or query.lower() in name.lower():
                results.append((code, name))
    return results

#настройка логирования
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)s:%(message)s"
)
logging.debug("🚀 Логирование уровня DEBUG активно")

# 🟢 Старт
@router.message(Command("start"))
async def start(message: Message, state: FSMContext):
    await state.clear()
    temp_storage.pop(message.from_user.id, None)
    await message.answer("🏠 Главное меню:", reply_markup=main_menu_kb())

# 📦 Создание заказа — шаг 1: дата
@router.callback_query(F.data == "create_order")
async def create_order(callback: CallbackQuery, state: FSMContext):
    order_id = f"OS -{datetime.now().strftime('%d.%m')} - {datetime.now().strftime('%H:%M')}"
    await state.update_data(order_id=order_id)
    await callback.message.edit_text("Выберите дату заказа:", reply_markup=date_choice_kb())
    await state.set_state(OrderState.choosing_date)

@router.callback_query(F.data == "today", OrderState.choosing_date)
async def set_today(callback: CallbackQuery, state: FSMContext):
    date = datetime.now().strftime("%d.%m.%Y")
    data = await state.get_data()
    c.execute("INSERT INTO supplier_orders (order_id, date) VALUES (?, ?)", (data['order_id'], date))
    conn.commit()
    await state.update_data(date=date)
    await callback.message.edit_text("Введите имя поставщика:", reply_markup=cancel_kb())
    await state.set_state(OrderState.entering_supplier)

@router.callback_query(F.data == "other_date", OrderState.choosing_date)
async def ask_custom_date(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите дату в формате ДД.ММ.ГГ:", reply_markup=cancel_kb())
    await state.set_state(OrderState.entering_custom_date)

@router.message(OrderState.entering_custom_date)
async def set_custom_date(message: Message, state: FSMContext):
    try:
        datetime.strptime(message.text.strip(), "%d.%m.%y")
        await state.update_data(date=message.text.strip())
        await message.answer("Введите имя поставщика:", reply_markup=cancel_kb())
        await state.set_state(OrderState.entering_supplier)
    except ValueError:
        await message.answer("Неверный формат. Введите дату в формате ДД.ММ.ГГ:")

@router.message(OrderState.entering_supplier)
async def enter_supplier(message: Message, state: FSMContext):
    supplier = message.text.strip()
    data = await state.get_data()
    c.execute("UPDATE supplier_orders SET supplier = ? WHERE order_id = ?", (supplier, data['order_id']))
    conn.commit()
    await state.update_data(supplier=supplier)
    await message.answer("Введите код или название товара:", reply_markup=cancel_kb())
    await state.set_state(OrderState.searching_product)

@router.callback_query(F.data == "create_supplier", OrderState.confirming_supplier)
async def confirm_supplier(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    logging.info(f"🔎 FSM state data: {data}")
    logging.info(f"📦 Данные из FSM: {data}")
    c.execute("INSERT OR IGNORE INTO suppliers (name) VALUES (?)", (data["supplier"],))
    conn.commit()
    await callback.message.edit_text("✅ Поставщик добавлен. Введите код или название товара:", reply_markup=cancel_kb())
    await state.set_state(OrderState.searching_product)

@router.callback_query(F.data == "search_supplier_again", OrderState.confirming_supplier)
async def retry_supplier(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите имя поставщика:", reply_markup=cancel_kb())
    await state.set_state(OrderState.entering_supplier)

# 🔍 Поиск и выбор товара
@router.message(OrderState.searching_product)
async def handle_product_search(message: Message, state: FSMContext):
    query = message.text.strip()
    results = search_products(query)

    if not results:
        await message.answer("🔍 Товар не найден. Введите код или название товара снова:", reply_markup=cancel_kb())
        return

    # Если найден ровно один по коду
    if len(results) == 1 and query.lower() == results[0][0].lower():
        code, name = results[0]
        await state.update_data(product_code=code, product_name=name)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_product")],
            [InlineKeyboardButton(text="🔄 Поиск заново", callback_data="search_product_again")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
        ])
        await message.answer(f"Найден товар: <b>{name}</b> (код: {code})", reply_markup=keyboard)
        await state.set_state(OrderState.confirming_product)
    else:
        builder = InlineKeyboardBuilder()
        for code, name in results[:10]:
            builder.button(text=f"{name} ({code})", callback_data=f"choose_product:{code}")
        builder.row(InlineKeyboardButton(text="🔄 Начать поиск заново", callback_data="search_product_restart"))
        builder.adjust(1)
        builder.row(InlineKeyboardButton(text="❌ Отмена", callback_data="cancel"))
        await message.answer("🔍 Найдено несколько товаров, выберите нужный:", reply_markup=builder.as_markup())
        await state.set_state(OrderState.choosing_product)

@router.callback_query(F.data.startswith("choose_product:"), OrderState.choosing_product)
async def choose_product(callback: CallbackQuery, state: FSMContext):
    code = callback.data.split(":")[1]
    results = search_products(code)
    name = next((name for c, name in results if c == code), None)
    if name:
        await state.update_data(product_code=code, product_name=name)
        await callback.message.edit_text(f"Вы выбрали товар: <b>{name}</b> (код: {code}). Введите количество:", reply_markup=cancel_kb())
        await state.set_state(OrderState.entering_quantity)
    else:
        await callback.message.edit_text("❌ Ошибка. Товар не найден. Попробуйте ещё раз.", reply_markup=cancel_kb())
        await state.set_state(OrderState.searching_product)

@router.callback_query(F.data == "confirm_product", OrderState.confirming_product)
async def confirm_single_product(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите количество товара:", reply_markup=cancel_kb())
    await state.set_state(OrderState.entering_quantity)

@router.callback_query(F.data.in_(["search_product_again", "search_product_restart"]))
async def retry_product(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите код или название товара:", reply_markup=cancel_kb())
    await state.set_state(OrderState.searching_product)

@router.message(OrderState.entering_quantity)
async def enter_quantity(message: Message, state: FSMContext):
    try:
        qty = int(message.text.strip())
        if qty < 1:
            raise ValueError()
        await state.update_data(quantity=qty)
        await message.answer("Введите цену за единицу товара (₽):", reply_markup=cancel_kb())
        await state.set_state(OrderState.entering_price)
    except ValueError:
        await message.answer("Введите корректное количество (целое число > 0):", reply_markup=cancel_kb())

# 📥 Ввод серийных номеров
@router.callback_query(F.data == "serials_now", OrderState.choosing_serial_mode)
async def enter_serials(callback: CallbackQuery, state: FSMContext):
    await state.update_data(serials=[], context="new")
    await callback.message.edit_text("Введите 1-й серийный номер:", reply_markup=cancel_kb())
    await state.set_state(OrderState.entering_serial_new)

@router.message(OrderState.entering_serial_new)
async def handle_serial_entry_new(message: Message, state: FSMContext):
    new_serial = message.text.strip()
    data = await state.get_data()

    # Проверка обязательных данных
    required_fields = ["product_name", "product_code", "quantity", "unit_price", "date", "supplier", "order_id"]
    for field in required_fields:
        if field not in data:
            await message.answer(f"❌ Ошибка: отсутствует {field}.", reply_markup=main_menu_kb())
            return

    # Проверка дубликатов
    c.execute("SELECT serial FROM warehouse WHERE serial = ?", (new_serial,))
    if c.fetchone():
        await message.answer("❌ Такой серийный номер уже есть в системе. Введите другой:", reply_markup=cancel_kb())
        return

    serials = data.get("serials", [])
    serials.append(new_serial)
    await state.update_data(serials=serials)

    # Если ещё не ввели все
    if len(serials) < data["quantity"]:
        await message.answer(f"Введите {len(serials) + 1}-й серийный номер:", reply_markup=cancel_kb())
        return

    # ✅ Введены все серийники — сохраняем в БД
    order_id = data["order_id"]
    date = data["date"]
    supplier = data["supplier"]
    product_name = data["product_name"]
    product_code = data["product_code"]
    quantity = data["quantity"]
    unit_price = data["unit_price"]
    total_price = quantity * unit_price

    # Сохраняем заказ
    c.execute("""
    UPDATE supplier_orders SET
        product_name = ?, quantity = ?, unit_price = ?, total_price = ?,
        serials = ?, product_code = ?, supplier = ?, date = ?
    WHERE order_id = ? AND product_code = ?
""", (
    product_name, quantity, unit_price, total_price,
    ",".join(serials), product_code, supplier, date,
    order_id, product_code
))

    # Сохраняем серийники в warehouse
    for sn in serials:
        c.execute("""
            INSERT OR IGNORE INTO warehouse (
                serial, product_name, order_id, unit_price
            ) VALUES (?, ?, ?, ?)
        """, (sn, product_name, order_id, unit_price))

    conn.commit()

    await state.clear()
    await message.answer("✅ Заказ с серийными номерами успешно сохранён!", reply_markup=main_menu_kb())

@router.message(OrderState.entering_serial_existing)
async def handle_serial_entry_existing(message: Message, state: FSMContext):
    new_serial = message.text.strip()
    data = await state.get_data()

    order_id = data.get("order_id")
    product_code = data.get("serial_target")
    current = data.get("current_serials", [])

    c.execute("SELECT quantity FROM supplier_orders WHERE order_id = ? AND product_code = ?", (order_id, product_code))
    row = c.fetchone()
    if not row:
        await message.answer("❌ Ошибка: товар не найден в заказе.", reply_markup=main_menu_kb())
        return

    quantity = row[0]

    if new_serial in current:
        await message.answer("❌ Такой серийный номер уже введён. Введите другой:", reply_markup=cancel_kb())
        return

    c.execute("SELECT serial FROM warehouse WHERE serial = ?", (new_serial,))
    if c.fetchone():
        await message.answer("❌ Такой серийный номер уже есть в системе. Введите другой:", reply_markup=cancel_kb())
        return

    if len(current) >= quantity:
        await message.answer("⚠️ Введено максимум серийных номеров.", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Сохранить серийники", callback_data="save_serials")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
            ]
        ))
        return

    current.append(new_serial)
    await state.update_data(current_serials=current)

    if len(current) == quantity:
        await message.answer(f"✅ Все {quantity} серийных номеров введены.", reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Сохранить серийники", callback_data="save_serials")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
            ]
        ))
    else:
        await message.answer(f"Введите {len(current)+1}-й серийный номер (из {quantity}):", reply_markup=cancel_kb())

@router.callback_query(F.data == "serials_later", OrderState.choosing_serial_mode)
async def skip_serials(callback: CallbackQuery, state: FSMContext):
    await state.update_data(serials=[])
    await callback.message.edit_text("Серийные номера будут добавлены позже.", reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить ещё товар", callback_data="add_more")],
            [InlineKeyboardButton(text="✅ Завершить заказ", callback_data="finish_order")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
        ]
    ))
    await state.set_state(OrderState.confirming_summary)

# ✅ Завершение заказа и сводка
@router.callback_query(F.data == "add_more")
async def add_more_items(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите код или название следующего товара:", reply_markup=cancel_kb())
    await state.set_state(OrderState.searching_product)

@router.callback_query(F.data == "finish_order")
async def finalize_order(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    session = await state.get_data()
    order_id = session.get("order_id")

    if not order_id:
        logging.warning("⚠️ Отсутствует order_id в FSMContext при финализации")
        await callback.message.edit_text("❌ Заказ не найден или пуст.", reply_markup=main_menu_kb())
        return

    c.execute("SELECT product_name, quantity, unit_price, total_price, product_code FROM supplier_orders WHERE order_id = ?", (order_id,))
    rows = c.fetchall()

    if not rows:
        logging.warning(f"❌ Заказ не найден в БД: {order_id}")
        await callback.message.edit_text("❌ Заказ не найден или пуст.", reply_markup=main_menu_kb())
        return

    total_sum = sum(row[3] for row in rows)  # total_price
    date = session.get("date", "—")
    supplier = session.get("supplier", "—")

    lines = [
        f"📦 <b>Сводка заказа {order_id}</b>",
        f"Дата: {date}",
        f"Поставщик: {supplier}",
        ""
    ]
    for product_name, qty, price, total, code in rows:
        lines.append(f"{product_name} (код: {code}) x {qty} шт. по {price}₽ = {total}₽")

    lines.append(f"\n💰 Итого: {total_sum}₽")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💾 Сохранить заказ", callback_data="save_order")],
        [InlineKeyboardButton(text="✏️ Редактировать заказ", callback_data="edit_order")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="cancel")]
    ])

    await callback.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode=ParseMode.HTML)
    await state.set_state(OrderState.confirming_summary)

@router.message(OrderState.entering_price)
async def handle_price_input(message: Message, state: FSMContext):
    try:
        price = float(message.text.replace(",", ".").strip())
        session = await state.get_data()
        order_id = session.get("order_id")
        product_code = session.get("product_code")
        product_name = session.get("product_name")
        quantity = session.get("quantity")
        date = session.get("date")
        supplier = session.get("supplier")
        edit_action = session.get("edit_action")

        if not all([order_id, product_code, product_name, quantity, date, supplier]):
            await message.answer("❌ Ошибка: отсутствуют данные.", reply_markup=main_menu_kb())
            return

        total = price * quantity
        await state.update_data(unit_price=price)

        if edit_action == "edit_price":
            c.execute("""
                UPDATE supplier_orders
                SET unit_price = ?, total_price = quantity * ?
                WHERE order_id = ? AND product_code = ?
            """, (price, price, order_id, product_code))
            conn.commit()
            await message.answer("✅ Цена обновлена.", reply_markup=main_menu_kb())
            await state.clear()
            return

        # Проверка: есть ли уже такая строка в заказе
        c.execute("""
            SELECT COUNT(*) FROM supplier_orders
            WHERE order_id = ? AND product_code = ?
        """, (order_id, product_code))
        exists = c.fetchone()[0]

        if exists:
            # обновление
            c.execute("""
                UPDATE supplier_orders
                SET quantity = ?, unit_price = ?, total_price = ?, product_name = ?, supplier = ?, date = ?
                WHERE order_id = ? AND product_code = ?
            """, (quantity, price, total, product_name, supplier, date, order_id, product_code))
        else:
            # вставка
            c.execute("""
                INSERT INTO supplier_orders (
                    order_id, date, supplier, product_name,
                    quantity, unit_price, total_price, serials, product_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, '', ?)
            """, (
                order_id, date, supplier, product_name,
                quantity, price, total, product_code
            ))
        conn.commit()

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔢 Ввести серийные номера сейчас", callback_data="serials_now")],
            [InlineKeyboardButton(text="⏭ Ввести позже", callback_data="serials_later")]
        ])
        await message.answer("Вы хотите ввести серийные номера сейчас или позже?", reply_markup=kb)
        await state.set_state(OrderState.choosing_serial_mode)

    except ValueError:
        await message.answer("❌ Введите корректную цену (например: 1500.00):", reply_markup=cancel_kb())


# 💾 Сохранение заказа
@router.callback_query(F.data == "save_order", OrderState.confirming_summary)
async def save_order(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    data = await state.get_data()
    order = temp_storage.get(user_id)

    if order:
        logging.info(f"📦 Режим: temp_storage | user_id={user_id}")
        source = "temp_storage"
    else:
        logging.info(f"📦 Режим: база данных | user_id={user_id}")
        order_id = data.get("order_id")
        if not order_id:
            logging.warning("❌ Нет order_id в состоянии!")
            await callback.message.edit_text("❌ Невозможно сохранить заказ: нет данных.", reply_markup=main_menu_kb())
            return

        c.execute("SELECT DISTINCT order_id, product_name, quantity, unit_price, total_price, product_code, serials FROM supplier_orders WHERE order_id = ?", (order_id,))
        rows = c.fetchall()

        if not rows:
            logging.warning("❌ Заказ не найден в БД!")
            await callback.message.edit_text("❌ Заказ не найден в базе данных.", reply_markup=main_menu_kb())
            return

        order = {
            "order_id": order_id,
            "items": [
                {
                    "product_name": r[1],
                    "quantity": r[2],
                    "unit_price": r[3],
                    "total_price": r[4],
                    "product_code": r[5],
                    "serials": r[6].split(",") if r[6] else []
                } for r in rows
            ]
        }
        source = "db"

    if not order.get("items"):
        logging.warning(f"❌ Заказ пуст: {order}")
        await callback.message.edit_text("❌ Заказ пуст.", reply_markup=main_menu_kb())
        return

    for item in order["items"]:
        # Проверка, не существует ли уже запись
        c.execute("SELECT 1 FROM supplier_orders WHERE order_id = ? AND product_code = ?", (order["order_id"], item["product_code"]))
        exists = c.fetchone()

        if exists:
            logging.info(f"⏩ Пропускаем дубликат: {item['product_code']}")
            continue

        logging.info(f"💾 Сохраняем строку заказа: {order['order_id']}, {item['product_code']}")
        c.execute("""
            INSERT INTO supplier_orders (
                order_id, date, supplier, product_name,
                quantity, unit_price, total_price, serials, product_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            order["order_id"],
            data.get("date"),
            data.get("supplier"),
            item["product_name"],
            item["quantity"],
            item["unit_price"],
            item["total_price"],
            ",".join(item["serials"]),
            item["product_code"]
        ))

        for serial in item["serials"]:
            c.execute("""
                INSERT OR IGNORE INTO warehouse (serial, product_name, order_id, unit_price)
                VALUES (?, ?, ?, ?)
            """, (
                serial, item["product_name"], order["order_id"], item["unit_price"]
            ))

    conn.commit()
    await state.clear()
    temp_storage.pop(user_id, None)
    await callback.message.edit_text("✅ Заказ успешно сохранён!", reply_markup=main_menu_kb())


# ✏️ Редактирование заказа
@router.callback_query(F.data == "edit_order", OrderState.confirming_summary)
async def edit_order_menu(callback: CallbackQuery, state: FSMContext):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Дата заказа", callback_data="edit_date")],
        [InlineKeyboardButton(text="🏢 Поставщик", callback_data="edit_supplier")],
        [InlineKeyboardButton(text="📦 Товар", callback_data="edit_product")],
        [InlineKeyboardButton(text="🔢 Кол-во товара", callback_data="edit_quantity")],
        [InlineKeyboardButton(text="💲 Цена за ед.", callback_data="edit_price")],
        [InlineKeyboardButton(text="🔁 Серийные номера", callback_data="edit_serials")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="finish_order")]
    ])
    await callback.message.edit_text("Что вы хотите изменить?", reply_markup=keyboard)

@router.callback_query(F.data.in_(["edit_price", "edit_quantity", "edit_serials"]))
async def choose_item_to_edit(callback: CallbackQuery, state: FSMContext):
    action = callback.data  # запоминаем, что редактировать
    data = await state.get_data()
    order_id = data.get("order_id")

    c.execute("SELECT DISTINCT product_name, product_code FROM supplier_orders WHERE order_id = ?", (order_id,))
    items = c.fetchall()

    if not items:
        await callback.message.edit_text("❌ В заказе нет товаров.", reply_markup=main_menu_kb())
        return

    # сохраняем действие в FSM
    await state.update_data(edit_action=action)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{name}", callback_data=f"edit_item:{code}")]
        for name, code in items
    ])
    kb.inline_keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="cancel")])
    await callback.message.edit_text("Выберите товар для редактирования:", reply_markup=kb)

@router.callback_query(F.data.startswith("edit_item:"))
async def handle_item_selection(callback: CallbackQuery, state: FSMContext):
    product_code = callback.data.split(":")[1]
    data = await state.get_data()
    await state.update_data(product_code=product_code)

    action = data.get("edit_action")

    if action == "edit_price":
        await callback.message.edit_text("Введите новую цену за единицу (₽):", reply_markup=cancel_kb())
        await state.set_state(OrderState.entering_price)

    elif action == "edit_quantity":
        await callback.message.edit_text("Введите новое количество:", reply_markup=cancel_kb())
        await state.set_state(OrderState.entering_quantity)

    elif action == "edit_serials":
        await callback.message.edit_text("Введите первый серийный номер:", reply_markup=cancel_kb())
        await state.set_state(OrderState.entering_serial)

@router.callback_query(F.data == "edit_date")
async def edit_date(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите новую дату заказа в формате ДД.ММ.ГГ:", reply_markup=cancel_kb())
    await state.set_state(OrderState.entering_custom_date)

@router.callback_query(F.data == "edit_supplier")
async def edit_supplier(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите нового поставщика:", reply_markup=cancel_kb())
    await state.set_state(OrderState.entering_supplier)

@router.callback_query(F.data == "edit_product")
async def edit_product(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите новый код или наименование товара:", reply_markup=cancel_kb())
    await state.set_state(OrderState.searching_product)

# 📋 Сводка → Заказы без серийных номеров
@router.callback_query(F.data == "summary_menu")
async def summary_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Без серийных номеров", callback_data="no_serials")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="cancel")]
    ])
    await callback.message.edit_text("📋 Выберите раздел сводки:", reply_markup=kb)
    await state.clear()

@router.callback_query(F.data == "no_serials")
async def list_orders_without_serials(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        c.execute("SELECT DISTINCT order_id FROM supplier_orders WHERE serials IS NULL OR serials = ''")
        rows = c.fetchall()
        logging.info(f"📄 Получено строк из БД: {len(rows)}")
        if not rows:
            await callback.message.edit_text("✅ Нет заказов без серийных номеров.", reply_markup=main_menu_kb())
            return

        kb = InlineKeyboardBuilder()
        for row in rows:
            order_id = row[0]
            kb.button(text=order_id, callback_data=f"view_order:{order_id}")
        kb.adjust(1)
        kb.row(InlineKeyboardButton(text="🏠 Главное меню", callback_data="cancel"))
        await callback.message.edit_text("Выберите заказ без серийников:", reply_markup=kb.as_markup())
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка при получении заказа: {e}", reply_markup=main_menu_kb())

@router.callback_query(F.data.startswith("view_order:"))
async def show_order_summary(callback: CallbackQuery, state: FSMContext):
    logging.info(f"🔍 Получен запрос на просмотр заказа: {callback.data}")
    try:
        order_id = callback.data.split(":", 1)[1]
        logging.info(f"🔍 Извлечён order_id: {order_id}")
        logging.info(f"📦 SQL-запрос: SELECT date, supplier, product_name, quantity, product_code FROM supplier_orders WHERE order_id = {order_id}")
        c.execute("SELECT date, supplier, product_name, quantity, product_code FROM supplier_orders WHERE order_id = ?", (order_id,))
        rows = c.fetchall()
        if not rows:
            logging.warning("❌ Заказ не найден в базе данных!")
            await callback.message.edit_text("❌ Заказ не найден.", reply_markup=main_menu_kb())
            return

        date, supplier = rows[0][:2]
        lines = [
            f"📦 <b>Заказ {order_id}</b>",
            f"Дата: {date}",
            f"Поставщик: {supplier}"
        ]
        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for row in rows:
            product_name = row[2]
            quantity = row[3]
            product_code = row[4]
            lines.append(f"🛒 {product_name} (код: {product_code}) x {quantity} шт.")
            kb.inline_keyboard.append([
                InlineKeyboardButton(
                    text=f"➕ Добавить серийники: {product_code}",
                    callback_data=f"add_serials:{order_id}:{product_code}"
                )
            ])

        kb.inline_keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="cancel")])
        await callback.message.edit_text("".join(lines), reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception as e:
        await callback.message.edit_text(f"❌ Ошибка при отображении заказа: {e}", reply_markup=main_menu_kb())
    

@router.callback_query(F.data.startswith("add_serials:"))
async def start_adding_serials(callback: CallbackQuery, state: FSMContext):
    logging.info(f"🔁 Обработка add_serials: {callback.data}")

    # Извлечение параметров
    raw = callback.data[len("add_serials:"):]
    order_id, product_code = raw.rsplit(":", 1)
    order_id = order_id.strip()
    product_code = product_code.strip()
    logging.info(f"🔢 Извлечены параметры: order_id={order_id}, product_code={product_code}")

    # Запрос в БД
    logging.info("📡 Выполняем запрос к БД: supplier_orders")
    c.execute("""
        SELECT product_name, quantity, unit_price
        FROM supplier_orders
        WHERE order_id = ? AND product_code = ?
    """, (order_id, product_code))
    row = c.fetchone()
    logging.debug(f"📥 row from DB: {row} for order_id={order_id}, product_code={product_code}")

    if not row:
        logging.warning("❌ Товар не найден в БД при добавлении серийников")
        await callback.message.edit_text("❌ Товар не найден.", reply_markup=main_menu_kb())
        return

    product_name, quantity, unit_price = row
    logging.info(f"📦 Найден товар: {product_name}, {quantity} шт., {unit_price}₽")

    # Сохраняем данные во временное состояние FSM
    await state.update_data(
        order_id=order_id,
        serial_target=product_code,
        current_serials=[],
        quantity=quantity
    )

    await callback.message.edit_text("Введите серийные номера по одному:", reply_markup=cancel_kb())
    await state.set_state(OrderState.entering_serial_existing)

@router.callback_query(F.data == "save_serials")
async def save_serials(callback: CallbackQuery, state: FSMContext):
    logging.info(f"🔧 save_serials triggered by user_id={callback.from_user.id}")
    data = await state.get_data()

    serials = data.get("current_serials", [])
    order_id = data.get("order_id")
    product_code = data.get("serial_target")

    if not serials or not order_id or not product_code:
        logging.warning("⚠️ Недостаточно данных: serials, order_id или product_code отсутствуют")
        await callback.message.edit_text("❌ Недостаточно данных для сохранения.", reply_markup=main_menu_kb())
        return

    # Получаем товар
    c.execute("SELECT product_name, unit_price, serials FROM supplier_orders WHERE order_id = ? AND product_code = ?", (order_id, product_code))
    row = c.fetchone()

    if not row:
        logging.warning(f"❌ Товар не найден в заказе для сохранения серийников: {order_id}, {product_code}")
        await callback.message.edit_text("❌ Товар не найден.", reply_markup=main_menu_kb())
        return

    product_name, unit_price, existing_serials = row
    existing_serials_list = existing_serials.split(",") if existing_serials else []
    updated_serials = existing_serials_list + serials

    # Обновляем заказ
    c.execute("""
        UPDATE supplier_orders
        SET serials = ?
        WHERE order_id = ? AND product_code = ?
    """, (",".join(updated_serials), order_id, product_code))

    # Добавляем в склад
    for sn in serials:
        c.execute("""
            INSERT OR IGNORE INTO warehouse (serial, product_name, order_id, unit_price)
            VALUES (?, ?, ?, ?)
        """, (sn, product_name, order_id, unit_price))

    conn.commit()
    await state.clear()
    await callback.message.edit_text("✅ Серийные номера добавлены и сохранены!", reply_markup=main_menu_kb())

# ▶️ Запуск бота
if __name__ == "__main__":
    dp.include_router(router)
    import asyncio
    asyncio.run(dp.start_polling(bot))
 
# 🔘 Обработчик отмены
@router.callback_query(F.data == "cancel")
async def cancel_process(callback: CallbackQuery, state: FSMContext):
    session = await state.get_data()
    order_id = session.get("order_id")
    if order_id:
        c.execute("DELETE FROM supplier_orders WHERE order_id = ? AND (serials IS NULL OR serials = '')", (order_id,))
        conn.commit()
        logging.info(f"🗑 Удалён незавершённый заказ: {order_id}")

    await state.clear()
    logging.info(f"🚫 Отмена действия пользователем {callback.from_user.id}")
    try:
        await callback.message.edit_text("🏠 Главное меню:", reply_markup=main_menu_kb())
    except Exception as e:
        logging.error(f"❌ Ошибка при возврате в главное меню: {e}")
        await callback.message.answer("🏠 Главное меню:", reply_markup=main_menu_kb())