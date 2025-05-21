import asyncio  # Добавьте этот импорт
import logging
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
import sqlite3
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from cachetools import TTLCache
from typing import List, Tuple
from aiogram.exceptions import TelegramAPIError

# ----------------- CONFIGURATION ------------------
GOOGLE_SHEET_KEY = '1OTHRzo4OAH_bWuplxYjwqpyJm857mYm0kaG0RHhHcOY'

CREDENTIALS_FILE = 'credentials.json'

# ----------------- GOOGLE SHEET SETUP ------------------
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive"
]
creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
client = gspread.authorize(creds)
sheet = client.open_by_key(GOOGLE_SHEET_KEY).sheet1

# Кэш на 10 минут
products_cache = TTLCache(maxsize=1, ttl=600)

def find_product_by_code_or_name(query):
    # Очищаем кэш при изменении данных
    if "force_refresh" in query:
        products_cache.clear()

    cached_data = products_cache.get("products")
    if not cached_data:
        data = sheet.get_all_values()
        products_cache["products"] = data 
        cached_data = data
    
    results = []
    for row in cached_data[1:]:
        if len(row) >= 6:
            code = row[4].strip()
            name = row[5].strip()
            if query.lower() in code.lower() or query.lower() in name.lower():
                results.append((code, name))
    return results

# ----------------- DATABASE SETUP ------------------
conn = sqlite3.connect("data.db", check_same_thread=False)
c = conn.cursor()
c.execute("""
    CREATE TABLE IF NOT EXISTS supplier_orders (
        order_id TEXT,
        date TEXT,
        supplier TEXT,
        product_name TEXT,
        product_code TEXT,
        quantity INTEGER,
        unit_price REAL,
        total_price REAL,
        serials TEXT
    )
""")

c.execute("""
    CREATE TABLE IF NOT EXISTS client_orders (
        order_id TEXT,
        date TEXT,
        client TEXT,
        product_name TEXT,
        quantity INTEGER,
        unit_price REAL,
        total_price REAL,
        serials TEXT,
        supplier_order_id TEXT
    )
""")

c.execute("""
    CREATE TABLE IF NOT EXISTS warehouse (
        serial TEXT PRIMARY KEY,
        product_name TEXT,
        supplier_order_id TEXT,
        client_order_id TEXT,
        unit_price REAL
    )
""")

c.execute("CREATE INDEX IF NOT EXISTS idx_serial ON warehouse(serial)")

c.execute("""
    CREATE TABLE IF NOT EXISTS cash (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        cash REAL DEFAULT 0,
        expense REAL DEFAULT 0,
        comment TEXT
    )
""")
conn.commit()

# ---------------- KEYBOARDS ------------------
def date_choice_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сегодня", callback_data="today")],
        [InlineKeyboardButton(text="Другая дата", callback_data="other_date")]
    ])

def confirm_add_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Добавить товар", callback_data="add_more")],
        [InlineKeyboardButton(text="Завершить заказ", callback_data="finish")]
    ])

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

def main_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📦 Создать заказ поставщику", callback_data="create_order_supplier"),
            InlineKeyboardButton(text="📤 Создать заказ клиенту", callback_data="create_order_client")
        ],
        [
            InlineKeyboardButton(text="🏭 Склад", callback_data="warehouse"),
            InlineKeyboardButton(text="🔍 Поиск заказа", callback_data="order_search")
        ],
        [
            InlineKeyboardButton(text="📊 Отчет", callback_data="report"),
            InlineKeyboardButton(text="💰 Касса", callback_data="cashbox")
        ]
    ])

def cancel_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")]
    ])

def order_actions_kb(order_id: str, order_type: str, has_serials: bool):
    buttons = [
        [
            InlineKeyboardButton(
                text="✏️ Редактировать", 
                callback_data=f"edit_{order_type}_{order_id}"
            ),
            InlineKeyboardButton(
                text="🗑️ Удалить", 
                callback_data=f"delete_{order_type}_{order_id}"
            )
        ]
    ]
    
    # Добавляем кнопку только если нет серийников
    if not has_serials:
        buttons.insert(0, [
            InlineKeyboardButton(
                text="➕ Добавить серийники",
                callback_data=f"add_serials_{order_type}_{order_id}"
            )
        ])
    
    # Кнопка возврата в меню
    buttons.append([
        InlineKeyboardButton(
            text="🏠 Главное меню", 
            callback_data="main_menu"
        )
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


from telegram.ext import CallbackQueryHandler, filters

def cancel_callback(update, context):
    # Твой код для обработки отмены
    update.callback_query.answer()
    update.callback_query.edit_message_text("Отмена выполнена.")

cancel_handler = CallbackQueryHandler(
    cancel_callback,
    pattern='^cancel$'  # фильтр для callback_data с точным значением "cancel"
)

# Добавить в раздел KEYBOARDS
def confirm_delete_kb(order_id: str, order_type: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ Да", 
                callback_data=f"confirm_delete_{order_type}_{order_id}"
            ),
            InlineKeyboardButton(
                text="❌ Нет", 
                callback_data="cancel_delete"
            )
        ]
    ])


# ---------------- STATES ------------------
class OrderSupplier(StatesGroup):
    choosing_date = State()
    entering_custom_date = State()
    choosing_supplier = State()
    entering_product = State()
    entering_quantity = State()
    entering_price = State()
    entering_serials = State()
    confirming_add = State()
    choosing_serial_input = State()

class OrderClient(StatesGroup):
    choosing_date = State()
    entering_custom_date = State()
    entering_client = State()
    entering_product = State()
    entering_quantity = State()
    entering_price = State()
    entering_serials = State()
    choosing_supplier_order = State()
    confirming_add = State()

class WarehouseState(StatesGroup):
    viewing = State()

class SearchOrderState(StatesGroup):
    choosing_type = State()
    entering_query = State()
    entering_serial = State() 
    entering_product = State()

class ReportState(StatesGroup):
    choosing_type = State()
    entering_period = State()
    entering_date_end = State()

class CashState(StatesGroup):
    choosing_action = State()
    entering_income = State()
    entering_expense_date = State()
    entering_expense_reason = State()
    entering_expense_amount = State()
    entering_report_date = State()

class DeleteOrderState(StatesGroup):
    confirm_delete = State()

class OrderClientType(StatesGroup):
    choosing_type = State()

class EditingOrder(StatesGroup):
    choosing_field = State()
    changing_date = State()
    changing_counterparty = State()
    choosing_product_action = State()
    selecting_product = State()
    changing_quantity = State()
    removing_serials = State()
    adding_serials = State()
    changing_price = State()
    confirming_changes = State()


class AddSerialState(StatesGroup):
    entering_serials = State()

# ---------------- STORAGE ------------------
temp_storage = {}

# ---------------- ROUTER ------------------
router = Router()

@router.message(F.text.lower() == "отмена")
async def cancel(message: Message, state: FSMContext):
    await state.clear()
    temp_storage.pop(message.from_user.id, None)
    await message.answer("Действие отменено. Вы в главном меню.", reply_markup=main_menu_kb())

@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer("🏠 Главное меню:", reply_markup=main_menu_kb())

from aiogram import Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Update

@router.errors()
async def error_handler(update: Update, exception: Exception):  # Добавлен параметр exception
    error_message = (
        f"⚠️ Произошла ошибка при обработке обновления:\n"
        f"• Тип: {type(exception).__name__}\n"
        f"• Сообщение: {str(exception)}"
    )
    
    if update.message:
        await update.message.answer("❌ Произошла ошибка. Возвращаемся в главное меню.", reply_markup=main_menu_kb())
    elif update.callback_query:
        await update.callback_query.message.answer("❌ Произошла ошибка. Возвращаемся в главное меню.", reply_markup=main_menu_kb())
    
    logging.error(error_message)
    return True


@router.callback_query(F.data == "cancel")
async def cancel_handler(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    temp_storage.pop(user_id, None)  # Очищаем временные данные
    await state.clear()
    await callback.message.edit_text("❌ Действие отменено.", reply_markup=main_menu_kb())

from datetime import datetime

# Функция получения текущей даты
def get_current_date():
    return datetime.now().strftime("%d.%m.%Y")

# Обработчик создания заказа поставщику
@router.callback_query(F.data == "create_order_supplier")
async def create_supplier_order(callback: CallbackQuery, state: FSMContext):
    # Определение текущего дня
    current_date = get_current_date()
    
    # Проверяем, вызвана ли эта команда из заказа клиента
    client_order_data = await state.get_data()
    if "client_order_data" in client_order_data:
        # Автоматически используем текущую дату
        await state.update_data(date=current_date)
        await callback.message.edit_text(f"Дата заказа поставщику автоматически установлена на {current_date}")
    else:
        # Ручной выбор даты
        await callback.message.edit_text("Выберите дату заказа:", reply_markup=date_choice_kb())
        await state.set_state(OrderSupplier.choosing_date)

@router.callback_query(F.data == "create_order_client")
async def create_client_order(callback: CallbackQuery, state: FSMContext):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="По серийному номеру", callback_data="client_by_serial"),
         InlineKeyboardButton(text="Вручную", callback_data="client_manual")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])
    await callback.message.edit_text("🔘 Выберите тип заказа клиента:", reply_markup=keyboard)
    await state.set_state(OrderClientType.choosing_type)

# Обработчики для разных типов заказов
@router.callback_query(F.data == "client_by_serial", OrderClientType.choosing_type)
async def handle_client_by_serial(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OrderClient.entering_serials)
    await callback.message.answer("Введите серийные номера через запятую:")

@router.message(OrderClient.entering_serials)
async def process_serials(message: Message, state: FSMContext):
    serials = [s.strip() for s in message.text.split(",")]
    
    # Проверяем наличие серийников
    c.execute("""
        SELECT product_name, supplier_order_id 
        FROM warehouse 
        WHERE serial IN ({}) 
        AND client_order_id IS NULL
    """.format(",".join(["?"]*len(serials))), serials)
    
    items = c.fetchall()
    
    if len(items) != len(serials):
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📦 Создать заказ поставщику", callback_data="create_supplier_from_client")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
        ])
        await message.answer("Некоторые серийники не найдены или уже проданы", reply_markup=keyboard)
        await state.update_data(saved_serials=serials)  # Сохраняем введенные серийники
        return
    
    # Группируем по товарам и поставщикам
    products = {}
    for product, supplier_id in items:
        if product not in products:
            products[product] = {}
        if supplier_id not in products[product]:
            products[product][supplier_id] = 0
        products[product][supplier_id] += 1
    
    # Сохраняем данные
    order_data = {
        "items": [],
        "serials": serials
    }
    for product, suppliers in products.items():
        for sid, qty in suppliers.items():
            order_data["items"].append({
                "product_name": product,
                "quantity": qty,
                "supplier_order_id": sid,
                "serials": [s for s in serials if s in [i[0] for i in items if i[1] == sid]]
            })
    
    await state.update_data(**order_data)
    await message.answer("Введите цену за единицу:")
    await state.set_state(OrderClient.entering_price)

@router.callback_query(F.data == "client_manual", OrderClientType.choosing_type)
async def handle_client_manual(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OrderClient.choosing_date)
    order_id = "OC" + datetime.now().strftime("%Y%m%d%H%M%S")
    user_id = callback.from_user.id
    temp_storage[user_id] = {"order_id": order_id, "items": []}
    await callback.message.edit_text("📅 Выберите дату заказа клиента:", reply_markup=date_choice_kb())

@router.callback_query(F.data == "warehouse")
async def show_warehouse(callback: CallbackQuery):
    c.execute("""
        SELECT product_name, COUNT(*) FROM warehouse
        WHERE client_order_id IS NULL
        GROUP BY product_name
    """)
    rows = c.fetchall()
    
    if not rows:
        await callback.message.edit_text("📦 Склад пуст", reply_markup=main_menu_kb())
        return
    
    text = "📦 Остатки на складе:\n" + "\n".join(
        f"- {product}: {count} шт." for product, count in rows
    )
    await callback.message.edit_text(text, reply_markup=main_menu_kb())

@router.callback_query(F.data == "order_search")
async def search_order(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SearchOrderState.choosing_type)
    await callback.message.edit_text(
        "🔍 Выберите тип поиска:", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔢 По номеру", callback_data="search_by_id"),
             InlineKeyboardButton(text="📅 По дате", callback_data="search_by_date")],
            [InlineKeyboardButton(text="🏷️ По серийному номеру", callback_data="search_by_serial"),
             InlineKeyboardButton(text="📦 По товару", callback_data="search_by_product")],
            [InlineKeyboardButton(text="🚫 Без серийника", callback_data="search_no_serial")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
        ])
    )
@router.callback_query(F.data == "order_search")
async def back_to_search(callback: CallbackQuery, state: FSMContext):
    await search_order(callback, state)  # Повторно вызываем обработчик поиска

@router.callback_query(F.data == "search_no_serial")
async def search_no_serial(callback: CallbackQuery):
    c.execute("""
        SELECT 'supplier', order_id, date, supplier, product_name 
        FROM supplier_orders 
        WHERE serials = '' OR serials IS NULL
        UNION
        SELECT 'client', order_id, date, client, product_name 
        FROM client_orders 
        WHERE serials = '' OR serials IS NULL
    """)
    results = c.fetchall()
    
    if not results:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="↩️ Назад", callback_data="order_search")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
        ])
        await callback.message.edit_text("🚫 Нет заказов без серийных номеров", reply_markup=keyboard)
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"{otype} {oid}",
            callback_data=f"open_{otype}_{oid}"
        )] for otype, oid, *_ in results
    ])
    await callback.message.edit_text("Заказы без серийных номеров:", reply_markup=keyboard)

@router.callback_query(F.data == "report")
async def generate_report(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ReportState.choosing_type)
    await callback.message.edit_text("📊 Выберите тип отчета:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="За период", callback_data="report_period")],
        [InlineKeyboardButton(text="За сегодня", callback_data="report_today")]
    ]))

@router.callback_query(F.data == "cashbox")
async def handle_cashbox(callback: CallbackQuery, state: FSMContext):
    await cashbox_menu(callback, state)

async def cashbox_menu(callback: CallbackQuery, state: FSMContext):
    # Расчет сводки за сегодня
    today = datetime.now().strftime("%d.%m.%Y")
    
    # Наличные (приход - расход)
    c.execute("SELECT SUM(cash) - SUM(expense) FROM cash WHERE date = ?", (today,))
    cash_balance = c.fetchone()[0] or 0
    
    # В товаре (сумма unit_price на складе)
    c.execute("SELECT SUM(unit_price) FROM warehouse WHERE client_order_id IS NULL")
    in_stock = c.fetchone()[0] or 0
    
    # Расходы за день
    c.execute("SELECT SUM(expense) FROM cash WHERE date = ?", (today,))
    expenses = c.fetchone()[0] or 0
    
    # Итого
    total = cash_balance + in_stock - expenses
    
    text = (
        f"💰 Касса на {today}:\n\n"
        f"• Наличные: {cash_balance}₽\n"
        f"• В товаре: {in_stock}₽\n"
        f"• Расходы: {expenses}₽\n"
        f"➖ Итого: {total}₽"
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Внести расход", callback_data="cash_expense")],
            [InlineKeyboardButton(text="📅 Отчет за день", callback_data="cash_day_report")],
            [InlineKeyboardButton(text="📝 Указать наличные", callback_data="set_cash_balance")],
            [InlineKeyboardButton(text="📆 Отчет по расходам", callback_data="cash_month_report")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
        ])
    )
    await state.set_state(CashState.choosing_action)

@router.callback_query(F.data == "set_cash_balance")
async def ask_cash_amount(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите сумму наличных на начало дня:")
    await state.set_state(CashState.setting_cash)

@router.message(CashState.setting_cash)
async def save_cash_amount(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
        date_str = datetime.now().strftime("%d.%m.%Y")

        # Здесь можно записывать в таблицу/БД
        c.execute("DELETE FROM cash WHERE date = ? AND comment = 'Наличные на начало дня'", (date_str,))
        c.execute("INSERT INTO cash (date, cash, comment) VALUES (?, ?, ?)", (date_str, amount, 'Наличные на начало дня'))
        conn.commit()

        await message.answer("✅ Наличные установлены!", reply_markup=cancel_kb())
        await state.clear()
    except Exception:
        await message.answer("❌ Ошибка. Убедитесь, что вы ввели корректное число.")

# Внесение расхода
@router.callback_query(F.data == "cash_expense")
async def start_expense(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите дату расхода в формате ДД.ММ.ГГГГ:")
    await state.set_state(CashState.entering_expense_date)

@router.message(CashState.entering_expense_date)
async def expense_date_entered(message: Message, state: FSMContext):
    try:
        datetime.strptime(message.text, "%d.%m.%Y")
        await state.update_data(expense_date=message.text)
        await message.answer("Введите цель расхода:")
        await state.set_state(CashState.entering_expense_reason)
    except ValueError:
        await message.answer("Неверный формат даты. Введите в формате ДД.ММ.ГГГГ:")

@router.message(CashState.entering_expense_reason)
async def expense_reason_entered(message: Message, state: FSMContext):
    await state.update_data(expense_reason=message.text)
    await message.answer("Введите сумму расхода:")
    await state.set_state(CashState.entering_expense_amount)

@router.message(CashState.entering_expense_amount)
async def expense_amount_entered(message: Message, state: FSMContext):
    try:
        amount = float(message.text.replace(",", "."))
        data = await state.get_data()
        c.execute("""
            INSERT INTO cash (date, expense, comment)
            VALUES (?, ?, ?)
        """, (data["expense_date"], amount, data["expense_reason"]))
        conn.commit()
        await message.answer("✅ Расход успешно добавлен!")
        await state.clear()
    except ValueError:
        await message.answer("Введите корректную сумму:")

# Отчет за день
@router.callback_query(F.data == "cash_day_report")
async def cash_day_report(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите дату отчета (ДД.ММ.ГГГГ):")
    await state.set_state(CashState.entering_report_date)

@router.message(CashState.entering_report_date)
async def cash_day_report_generate(message: Message, state: FSMContext):
    try:
        date = datetime.strptime(message.text, "%d.%m.%Y").strftime("%d.%m.%Y")
        c.execute("""
            SELECT comment, expense FROM cash 
            WHERE date = ? AND expense > 0
        """, (date,))
        expenses = c.fetchall()
        
        if not expenses:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Повторить поиск", callback_data="cash_retry_day_report")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
            ])
            await message.answer("🚫 Нет данных за этот день.", reply_markup=keyboard)
            return
            
        total = sum(e[1] for e in expenses)
        text = f"📊 Отчет по расходам за {date}:\n\n"
        text += "\n".join([f"• {e[0]}: {e[1]}₽" for e in expenses])
        text += f"\n\n💸 Итого: {total}₽"
        
        await message.answer(text)
    except ValueError:
        await message.answer("Неверный формат даты.")

@router.callback_query(F.data == "cash_retry_day_report")
async def retry_day_report(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите дату отчета (ДД.ММ.ГГГГ):")
    await state.set_state(CashState.entering_report_date)
    await callback.answer()

# Отчет по месяцам
@router.callback_query(F.data == "cash_month_report")
async def cash_month_report(callback: CallbackQuery):
    c.execute("""
        SELECT strftime('%Y-%m', date) as month 
        FROM cash 
        WHERE expense > 0 
        GROUP BY month
    """)
    months = c.fetchall()
    if not months:
        await callback.answer("Нет данных о расходах")
        return
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{m[0]}", callback_data=f"cash_month_{m[0]}")] for m in months
    ])
    await callback.message.edit_text("Выберите месяц:", reply_markup=kb)

@router.callback_query(F.data.startswith("cash_month_"))
async def cash_month_report_details(callback: CallbackQuery):
    month = callback.data.split("_")[-1]
    c.execute("""
        SELECT date, comment, expense 
        FROM cash 
        WHERE strftime('%Y-%m', date) = ? AND expense > 0
    """, (month,))
    expenses = c.fetchall()
    
    text = f"📆 Отчет за {month}:\n\n"
    total = 0
    for date, reason, amount in expenses:
        text += f"• {date}: {reason} — {amount}₽\n"
        total += amount
    text += f"\n💸 Итого: {total}₽"
    
    await callback.message.edit_text(text, reply_markup=main_menu_kb())

@router.callback_query(F.data.startswith("add_serial_"))
async def start_adding_serial(callback: CallbackQuery, state: FSMContext):
    try:
        _, order_type, order_id = callback.data.split("_", 2)
        # Исправленный запрос для поиска заказа
        table = "supplier_orders" if order_type == "supplier" else "client_orders"
        c.execute(f"SELECT 1 FROM {table} WHERE order_id = ?", (order_id,))
        if not c.fetchone():
            await callback.answer("❌ Заказ не найден в базе")
            return
        await state.update_data(order_type=order_type, order_id=order_id)
        await callback.message.answer("Введите серийные номера через запятую:")
        await state.set_state(AddSerialState.entering_serials)
    except Exception as e:
        logging.error(f"Serial add error: {str(e)}")
        await callback.answer("❌ Ошибка обработки запроса")

@router.message(AddSerialState.entering_serials)
async def process_adding_serials(message: Message, state: FSMContext):
    data = await state.get_data()
    serials = [s.strip() for s in message.text.split(",") if s.strip()]
    
    if not serials:
        await message.answer("❌ Список серийников пуст!")
        return

    # Проверка формата серийников
    if any(not sn.isalnum() for sn in serials):
        await message.answer("❌ Неверный формат серийников!")
        return
    
    # Проверка существования в других заказах
    conflicts = check_serials_usage(serials)
    if conflicts:
        await message.answer(f"❌ Серийники используются в заказах: {', '.join(conflicts)}")
        return
    
    # Обновление базы данных
    try:
        if data["order_type"] == "supplier":
            # Логика для поставщика
            c.execute("UPDATE supplier_orders SET serials = ? WHERE order_id = ?",
                     (",".join(serials), data["order_id"]))
            for sn in serials:
                c.execute("""INSERT OR REPLACE INTO warehouse 
                           VALUES (?, ?, ?, NULL, ?)""",
                         (sn, "", data["order_id"], 0.0))
        else:
            # Логика для клиента
            c.execute("UPDATE client_orders SET serials = ? WHERE order_id = ?",
                     (",".join(serials), data["order_id"]))
        
        conn.commit()
        await message.answer("✅ Серийники успешно добавлены!")
    except Exception as e:
        await message.answer(f"❌ Ошибка базы данных: {str(e)}")
    
    await state.clear()

# Добавить обработчик для отмены удаления
@router.callback_query(F.data == "cancel_delete")
async def cancel_delete(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Удаление отменено", reply_markup=main_menu_kb())

# ---------------- Создание заказа поставщику ------------------

@router.message(F.text == "Создать заказ поставщику")
async def start_supplier_order(message: Message, state: FSMContext):
    order_id = "OP" + datetime.now().strftime("%Y%m%d%H%M%S")
    temp_storage[message.from_user.id] = {"order_id": order_id, "items": []}
    await state.set_state(OrderSupplier.choosing_date)
    await message.answer("Выберите дату заказа:", reply_markup=date_choice_kb())

@router.callback_query(F.data.in_({"today", "other_date"}), OrderSupplier.choosing_date)
async def supplier_order_date_choice(callback: CallbackQuery, state: FSMContext):
    if callback.data == "today":
        date_str = datetime.now().strftime("%d.%m.%Y")
        await state.update_data(date=date_str)
        await callback.message.edit_text(f"Дата заказа: {date_str}")
        await callback.message.answer("Введите имя поставщика:", reply_markup=cancel_kb())
        await state.set_state(OrderSupplier.choosing_supplier)
    else:
        await callback.message.edit_text("Введите дату заказа в формате дд.мм.гггг:", reply_markup=cancel_kb())
        await state.set_state(OrderSupplier.entering_custom_date)

@router.message(OrderSupplier.entering_custom_date)
async def custom_date_entered(message: Message, state: FSMContext):
    try:
        datetime.strptime(message.text, "%d.%m.%Y")
        await state.update_data(date=message.text)
        await message.answer(f"Дата заказа установлена: {message.text}\nВведите имя поставщика:", reply_markup=cancel_kb())
        await state.set_state(OrderSupplier.choosing_supplier)
    except ValueError:
        await message.answer("Неверный формат даты. Введите в формате дд.мм.гггг:")

@router.message(OrderSupplier.choosing_supplier)
async def supplier_name_entered(message: Message, state: FSMContext):
    await state.update_data(supplier=message.text.strip())
    await message.answer("Введите код или название товара:", reply_markup=cancel_kb())
    await state.set_state(OrderSupplier.entering_product)

@router.message(OrderSupplier.entering_product)
async def product_entered(message: Message, state: FSMContext):
    query = message.text.strip()
    results = find_product_by_code_or_name(query)
    if not results:
        await message.answer("Товар не найден. Попробуйте ввести код или название заново:")
        return
    if len(results) > 1:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"{code} - {name}", callback_data=f"product_{code}")]
                for code, name in results
            ]
        )
        await message.answer("Найдены товары. Выберите:", reply_markup=keyboard)
        return
    code, name = results[0]
    await state.update_data(product_code=code, product_name=name)
    await message.answer(f"Вы выбрали товар: {name} (код: {code})\nВведите количество товара:", reply_markup=cancel_kb())
    await state.set_state(OrderSupplier.entering_quantity)

@router.callback_query(F.data.startswith("product_"), OrderSupplier.entering_product)
async def product_selected(callback: CallbackQuery, state: FSMContext):
    code = callback.data[8:]
    results = find_product_by_code_or_name(code)
    if not results:
        await callback.message.edit_text("Товар не найден, попробуйте заново ввести код или название:")
        await state.set_state(OrderSupplier.entering_product)
        return
    _, name = results[0]
    await state.update_data(product_code=code, product_name=name)
    await callback.message.edit_text(f"Вы выбрали товар: {name} (код: {code})\nВведите количество товара:", reply_markup=cancel_kb())
    await state.set_state(OrderSupplier.entering_quantity)

@router.message(OrderSupplier.entering_quantity)
async def quantity_entered(message: Message, state: FSMContext):
    try:
        qty = int(message.text.strip())
        if qty < 1:
            raise ValueError()
        await state.update_data(quantity=qty)
        await message.answer("Введите стоимость за штуку (₽):", reply_markup=cancel_kb())
        await state.set_state(OrderSupplier.entering_price)
    except ValueError:
        await message.answer("Введите корректное количество (целое число > 0):")

@router.message(OrderSupplier.entering_price)
async def price_entered(message: Message, state: FSMContext):
    try:
        price = float(message.text.strip().replace(",", "."))
        if price <= 0:
            raise ValueError()
        await state.update_data(unit_price=price)
        
        # Предлагаем выбор ввода серийников
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Ввести сейчас", callback_data="enter_serials_now")],
            [InlineKeyboardButton(text="Добавить позже", callback_data="enter_serials_later")]
        ])
        await message.answer("Хотите ввести серийные номера сейчас?", reply_markup=keyboard)
        await state.set_state(OrderSupplier.choosing_serial_input)
        
    except ValueError:
        await message.answer("Введите корректную цену (число > 0):")

@router.callback_query(F.data.in_(["enter_serial_now", "enter_serial_later"]), OrderSupplier.choosing_serial_input)
async def handle_serial_choice(callback: CallbackQuery, state: FSMContext):
    # Получаем идентификатор пользователя и данные из FSM
    user_id = callback.from_user.id
    data = await state.get_data()
    
    # Получаем временные данные пользователя или создаем пустой словарь
    user_data = temp_storage.get(user_id, {})
    
    # Проверяем, создается ли заказ поставщика в рамках клиентского заказа
    is_embedded = "client_order_data" in user_data
    
    if callback.data == "enter_serial_now" or is_embedded:
        # Режим принудительного ввода серийников (для вложенных заказов)
        
        # Инициализируем структуру для ввода серийников
        temp_storage[user_id] = {
            "current_serials": [], # Текущие введенные серийники
            "expected_serials": data["quantity"], # Ожидаемое количество
            "items": user_data.get("items", []) # Существующие позиции заказа
        }
        
        # Запрашиваем ввод серийников у пользователя
        await callback.message.answer(
            f"Введите серийные номера (осталось {data['quantity']}):",
            reply_markup=cancel_kb() # Клавиатура с кнопкой отмены
        )
        
        # Переводим в состояние ввода серийников
        await state.set_state(OrderSupplier.entering_serials)
        
    else:
        # Режим добавления без серийников (обычный сценарий)
        
        # Создаем позицию заказа без серийников
        item = {
            "product_code": data["product_code"],
            "product_name": data["product_name"],
            "quantity": data["quantity"],
            "unit_price": data["unit_price"],
            "total_price": data["quantity"] * data["unit_price"],
            "serials": [] # Пустой список серийников
        }
        
        # Добавляем позицию во временное хранилище
        temp_storage[user_id]["items"].append(item)
        
        # Отправляем подтверждение и предлагаем продолжить
        await callback.message.answer(
            "Товар добавлен без серийников.", 
            reply_markup=confirm_add_kb() # Клавиатура с опциями добавления
        )
        
        # Переводим в состояние подтверждения заказа
        await state.set_state(OrderSupplier.confirming_add)

@router.callback_query(F.data.in_(["enter_serials_now", "enter_serials_later"]), OrderSupplier.choosing_serial_input)
async def handle_serial_choice(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    user_id = callback.from_user.id
    
    # Для вложенных заказов принудительно сохраняем базовые данные
    if "client_order_data" in temp_storage.get(user_id, {}):
        temp_storage[user_id].update({
            "order_id": temp_storage[user_id].get("order_id"),
            "date": datetime.now().strftime("%d.%m.%Y"),
            "supplier": data.get("supplier", "")
        })
    
    if callback.data == "enter_serials_now":
        # Инициализируем структуру для ввода серийников
        temp_storage[user_id] = {
            "current_serials": [],
            "expected_serials": data["quantity"],
            "items": temp_storage.get(user_id, {}).get("items", [])
        }
        await callback.message.answer(
            f"Введите серийные номера (осталось {data['quantity']}):",
            reply_markup=cancel_kb()
        )
        await state.set_state(OrderSupplier.entering_serials)
    else:
        # Логика для "Добавить позже"
        item = {
            "product_code": data["product_code"],
            "product_name": data["product_name"],
            "quantity": data["quantity"],
            "unit_price": data["unit_price"],
            "total_price": data["quantity"] * data["unit_price"],
            "serials": []
        }
        temp_storage[user_id]["items"].append(item)
        await callback.message.answer("Товар добавлен без серийников.", reply_markup=confirm_add_kb())
        await state.set_state(OrderSupplier.confirming_add)

@router.message(OrderSupplier.entering_serials)
async def process_serial_input(message: Message, state: FSMContext):
    user_id = message.from_user.id
    user_data = temp_storage[user_id]
    sn = message.text.strip()
    
    # Проверка 1: Дубликат в текущем вводе
    if sn in user_data["current_serials"]:
        await message.answer("❌ Этот серийник уже был введен. Введите другой:")
        return
    
    # Проверка 2: Существование в системе
    c.execute("""
        SELECT serial FROM warehouse 
        WHERE serial = ? 
        UNION 
        SELECT serials FROM (
            SELECT serials FROM supplier_orders 
            UNION 
            SELECT serials FROM client_orders
        ) 
        WHERE ',' || serials || ',' LIKE '%,' || ? || ',%'
    """, (sn, sn))
    
    if c.fetchone():
        await message.answer("❌ Этот серийник уже существует в системе. Введите другой:")
        return
    
    # Добавляем серийник
    user_data["current_serials"].append(sn)
    remaining = user_data["expected_serials"] - len(user_data["current_serials"])
    
    if remaining > 0:
        await message.answer(f"✅ Принято. Осталось ввести: {remaining}")
    else:
        # Сохраняем товар с серийниками
        data = await state.get_data()
        item = {
            "product_code": data["product_code"],
            "product_name": data["product_name"],
            "quantity": data["quantity"],
            "unit_price": data["unit_price"],
            "total_price": data["quantity"] * data["unit_price"],
            "serials": user_data["current_serials"]
        }
        user_data["items"].append(item)
        
        # Сбрасываем временные данные
        del user_data["current_serials"]
        del user_data["expected_serials"]
        
        await message.answer("✅ Все серийники добавлены. Продолжайте или завершите заказ.", 
                           reply_markup=confirm_add_kb())
        await state.set_state(OrderSupplier.confirming_add)

@router.message(OrderSupplier.entering_price)
async def process_price(message: Message, state: FSMContext):
    try:
        price = float(message.text.replace(",", ".").strip())
        if price <= 0:
            await message.answer("Цена должна быть положительным числом. Пожалуйста, введите корректную сумму.")
            return
            
        # Сохраняем цену в состоянии
        await state.update_data(price=price)
        
        # Получаем данные из состояния для расчета общей суммы
        data = await state.get_data()
        quantity = data.get("quantity", 0)
        
        # Рассчитываем общую сумму
        total_price = price * quantity
        await state.update_data(total_price=total_price)
        
        # Переходим к следующему состоянию или подтверждению заказа
        order_summary = (
            f"📋 Информация о заказе:\n"
            f"Поставщик: {data.get('supplier', 'Не указан')}\n"
            f"Товар: {data.get('product_name', 'Не указан')}\n"
            f"Количество: {quantity}\n"
            f"Цена за единицу: {price}₽\n"
            f"Общая сумма: {total_price}₽\n\n"
            f"Подтвердите заказ:"
        )
        
        await message.answer(
            order_summary,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_order"),
                    InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_order")
                ]
            ])
        )
        
        # Переходим к состоянию подтверждения заказа
        await state.set_state(OrderSupplier.confirming_add)
        
    except ValueError:
        await message.answer("Пожалуйста, введите корректную цену в виде числа (например, 1500 или 1500.50).")


@router.callback_query(F.data == "add_more", OrderSupplier.confirming_add)
async def add_more_product(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OrderSupplier.entering_product)
    await callback.message.edit_text("Введите код или название товара:")

@router.callback_query(F.data == "finish", OrderSupplier.confirming_add)
async def finish_supplier_order(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    order_data = temp_storage.get(user_id, {})
    data = await state.get_data()
    required_fields = ["date", "supplier", "product_code"]
    
    # Добавляем проверку на существование заказа клиента
    client_order_data = order_data.get("client_order_data")
    if client_order_data:
        order_data.update({
            "order_id": order_data.get("order_id"),
            "items": order_data.get("items", []),
            "date": datetime.now().strftime("%d.%m.%Y"),  # Автоматическая дата
            "supplier": order_data.get("supplier", "")
        })
    
    # Гарантируем наличие минимальных данных
    if not order_data.get("order_id") or not order_data.get("items", []):
        await callback.message.edit_text(
            "❌ Основные данные заказа отсутствуют. Возврат в меню.",
            reply_markup=main_menu_kb()
        )
        await state.clear()
        return

    data = await state.get_data()
    date = data.get("date", "Не указана")
    supplier = data.get("supplier", "Не указан")
    order_id = order_data["order_id"]

    total_order_price = 0
    for item in order_data["items"]:
        serials_str = ",".join(item["serials"])
        total_order_price += item["total_price"]
        c.execute("""
            INSERT INTO supplier_orders (order_id, date, supplier, product_name, product_code, quantity, unit_price, total_price, serials)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (order_id, date, supplier, item["product_name"], item["product_code"], item["quantity"], item["unit_price"], item["total_price"], serials_str))
        for sn in item["serials"]:
            c.execute("""
                INSERT OR IGNORE INTO warehouse (serial, product_name, supplier_order_id, client_order_id, unit_price)
                VALUES (?, ?, ?, NULL, ?)
            """, (sn, item["product_name"], order_id, item["unit_price"]))
    conn.commit()

    text = f"✅ Заказ поставщику создан:\n\nДата заказа: {date}\nНомер заказа: {order_id}\nПоставщик: {supplier}\n\n"
    for idx, item in enumerate(order_data["items"], 1):
        text += (f"{idx}. {item['product_name']} ({item['product_code']})\n"
                 f"   Цена за шт: {item['unit_price']}₽\n"
                 f" Кол-во: {item['quantity']}\n"
                 f" Стоимость: {item['total_price']}₽\n"
                 f" Серийные номера: {', '.join(item['serials'])}\n\n")
        text += f"<b>Общая сумма заказа:</b> {total_order_price}₽"
        temp_storage.pop(user_id, None)
    await state.clear()
    await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=main_menu_kb())

# ---------------- Создание заказа клиенту ------------------

@router.message(F.text == "Создать заказ клиенту")
async def start_client_order(message: Message, state: FSMContext):
    order_id = "OC" + datetime.now().strftime("%Y%m%d%H%M%S")
    temp_storage[message.from_user.id] = {"order_id": order_id, "items": []}
    await state.set_state(OrderClient.choosing_date)
    await message.answer("Выберите дату заказа клиента:", reply_markup=date_choice_kb())

@router.callback_query(F.data.in_({"today", "other_date"}), OrderClient.choosing_date)
async def client_order_date_choice(callback: CallbackQuery, state: FSMContext):
    if callback.data == "today":
        date_str = datetime.now().strftime("%d.%m.%Y")
        await state.update_data(date=date_str)
        await callback.message.edit_text(f"Дата заказа клиента: {date_str}")
        await callback.message.answer("Введите имя клиента:", reply_markup=cancel_kb())
        await state.set_state(OrderClient.entering_client)
    else:
        await callback.message.edit_text("Введите дату заказа клиента в формате дд.мм.гггг:", reply_markup=cancel_kb())
        await state.set_state(OrderClient.entering_custom_date)

@router.message(OrderClient.entering_custom_date)
async def client_custom_date_entered(message: Message, state: FSMContext):
    try:
        datetime.strptime(message.text, "%d.%m.%Y")
        await state.update_data(date=message.text)
        await message.answer(f"Дата заказа клиента установлена: {message.text}\nВведите имя клиента:", reply_markup=cancel_kb())
        await state.set_state(OrderClient.entering_client)
    except ValueError:
        await message.answer("Неверный формат даты. Введите в формате дд.мм.гггг:")

@router.message(OrderClient.entering_client)
async def client_name_entered(message: Message, state: FSMContext):
    await state.update_data(client=message.text.strip())
    await message.answer("Введите код или название товара:", reply_markup=cancel_kb())
    await state.set_state(OrderClient.entering_product)

@router.message(OrderClient.entering_product)
async def client_product_entered(message: Message, state: FSMContext):
    query = message.text.strip()
    results = find_product_by_code_or_name(query)
    if not results:
        await message.answer(
            "❌ Товар не найден. Хотите создать заказ поставщику?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📦 Создать заказ поставщику", callback_data="create_order_supplier")],
                [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
            ])
        )
        return
    if len(results) > 1:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=f"{code} - {name}", callback_data=f"client_product_{code}")]
                for code, name in results
            ]
        )
        await message.answer("Найдены товары. Выберите:", reply_markup=keyboard)
        return
    code, name = results[0]
    await state.update_data(product_code=code, product_name=name)
    await message.answer(f"Вы выбрали товар: {name} (код: {code})\nВведите количество товара:", reply_markup=cancel_kb())
    await state.set_state(OrderClient.entering_quantity)

@router.callback_query(F.data.startswith("client_product_"), OrderClient.entering_product)
async def client_product_selected(callback: CallbackQuery, state: FSMContext):
    code = callback.data[15:]
    results = find_product_by_code_or_name(code)
    if not results:
        await callback.message.edit_text("Товар не найден, попробуйте заново ввести код или название:")
        await state.set_state(OrderClient.entering_product)
        return
    _, name = results[0]
    await state.update_data(product_code=code, product_name=name)
    await callback.message.edit_text(f"Вы выбрали товар: {name} (код: {code})\nВведите количество товара:", reply_markup=cancel_kb())
    await state.set_state(OrderClient.entering_quantity)

# Пример для обработчика ввода количества
@router.message(OrderClient.entering_quantity)
async def client_quantity_entered(message: Message, state: FSMContext):
    try:
        qty = int(message.text.strip())
        if qty < 1:
            raise ValueError()

        await state.update_data(quantity=qty)

        # Формируем клавиатуру вручную, не используя .append на inline_keyboard
        kb = InlineKeyboardMarkup(inline_keyboard=
            cancel_kb().inline_keyboard +
            [[InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]]
        )

        await message.answer("Введите стоимость за штуку (₽):", reply_markup=kb)
        await state.set_state(OrderClient.entering_price)

    except ValueError:
        await message.answer(
            "❌ Некорректное количество. Введите целое число больше 0:",
            reply_markup=cancel_kb()
        )


async def client_price_entered(message: Message, state: FSMContext):
    try:
        price = float(message.text.replace(",", ".").strip())
        if price <= 0:
            raise ValueError()

        await state.update_data(unit_price=price)
        data = await state.get_data()

        # Считаем общую сумму позиции и добавляем в хранилище
        item = {
            "product_name": data.get("product_name"),
            "quantity": data.get("quantity"),
            "unit_price": price,
            "total_price": data.get("quantity") * price,
            "serials": [],
            "supplier_order_id": ""
        }

        user_id = message.from_user.id
        if user_id in temp_storage:
            temp_storage[user_id]["items"].append(item)
        else:
            temp_storage[user_id] = {
                "order_id": "OC" + datetime.now().strftime("%Y%m%d%H%M%S"),
                "items": [item],
                "date": data.get("date", datetime.now().strftime("%d.%m.%Y")),
                "client": data.get("client", "Не указан")
            }

        supplier_link = f"open_supplier_{item['supplier_order_id']}" if item['supplier_order_id'] else "Нет связи"

        # Подтверждение и сводка
        text = (
            f"📋 Сводка по заказу клиента:\n"
            f"Дата: {temp_storage[user_id]['date']}\n"
            f"Клиент: {temp_storage[user_id]['client']}\n"
            f"Товар: {item['product_name']}\n"
            f"Кол-во: {item['quantity']} шт.\n"
            f"Цена за единицу: {item['unit_price']}₽\n"
            f"Итого: {item['total_price']}₽\n"
            f"Связанный заказ поставщика: {'🔗 Показать' if item['supplier_order_id'] else '—'}"
        )

        keyboard = [
            [InlineKeyboardButton(text="✅ Завершить заказ", callback_data="finish")],
            [InlineKeyboardButton(text="➕ Добавить еще", callback_data="add_more")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
        ]
        if item['supplier_order_id']:
            keyboard.insert(1, [InlineKeyboardButton(text="🔗 Показать заказ поставщика", callback_data=supplier_link)])

        await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))
        await state.set_state(OrderClient.confirming_add)

    except ValueError:
        await message.answer("Введите корректную цену в формате 1000.00", reply_markup=cancel_kb())

@router.callback_query(F.data.startswith("choose_supplier_"), OrderClient.choosing_supplier_order)
async def client_choose_supplier(callback: CallbackQuery, state: FSMContext):
    supplier_order_id = callback.data[16:]
    user_id = callback.from_user.id
    data = await state.get_data()
    qty = data.get("quantity")
    product_name = data.get("product_name")
    unit_price = data.get("unit_price")
    client_name = data.get("client")
    date = data.get("date")
    order_id = temp_storage[user_id]["order_id"]

    # Проверяем, есть ли на складе достаточное кол-во товара у выбранного поставщика
    c.execute("""
        SELECT serial FROM warehouse
        WHERE product_name=? AND supplier_order_id=? AND client_order_id IS NULL
        LIMIT ?
    """, (product_name, supplier_order_id, qty))
    rows = c.fetchall()
    if len(rows) < qty:
        await callback.message.answer(f"На складе недостаточно товара у поставщика {supplier_order_id}. Выберите другого поставщика.")
        return

    serials = [r[0] for r in rows]
    total_price = qty * unit_price
    item = {
        "product_name": product_name,
        "quantity": qty,
        "unit_price": unit_price,
        "total_price": total_price,
        "serials": serials,
        "supplier_order_id": supplier_order_id
    }
    temp_storage[user_id]["items"].append(item)

    await callback.message.edit_text("Товар добавлен в заказ клиента.", reply_markup=confirm_add_kb())
    await state.set_state(OrderClient.confirming_add)

@router.callback_query(F.data == "add_more", OrderClient.confirming_add)
async def client_add_more_product(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OrderClient.entering_product)
    await callback.message.edit_text("Введите код или название товара:")

@router.callback_query(F.data == "create_supplier_from_client")
async def create_supplier_from_client(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    data = await state.get_data()
    temp_storage[user_id] = {
        "order_id": "OP" + datetime.now().strftime("%Y%m%d%H%M%S"),
        "items": [],
        "date": datetime.now().strftime("%d.%m.%Y"),  # Авто-дата
        "client_order_data": (await state.get_data()).copy()
    }
    await state.set_state(OrderSupplier.choosing_supplier)
    await callback.message.answer("Введите имя поставщика:")
    
    # Инициализируем запись, если не существует
    if user_id not in temp_storage:
        temp_storage[user_id] = {}
        
    # Сохраняем текущее состояние клиентского заказа
    data = await state.get_data()
    temp_storage[user_id]["client_order_data"] = data
    
    # Запускаем процесс создания заказа поставщику
    order_id = "OP" + datetime.now().strftime("%Y%m%d%H%M%S")
    temp_storage[user_id] = {
        "order_id": order_id,
        "items": [],
        "date": datetime.now().strftime("%d.%m.%Y"),
        "client_order_data": data
    }

    await state.update_data(date=temp_storage[user_id]["date"])
    await callback.message.answer("Введите имя поставщика:", reply_markup=cancel_kb())
    await state.set_state(OrderSupplier.choosing_supplier)

@router.callback_query(F.data == "finish", OrderClient.confirming_add)
async def finish_client_order(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    order_data = temp_storage.get(user_id)
    if not order_data or not order_data.get("items"):
        await callback.message.edit_text("Нет товаров в заказе, невозможно сохранить.")
        await state.clear()
        return

    data = await state.get_data()
    date = data.get("date")
    client = data.get("client")
    order_id = order_data["order_id"]

    total_order_price = 0
    for item in order_data["items"]:
        serials_str = ",".join(item["serials"])
        total_order_price += item["total_price"]
        c.execute("""
            INSERT INTO client_orders (order_id, date, client, product_name, quantity, unit_price, total_price, serials, supplier_order_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (order_id, date, client, item["product_name"], item["quantity"], item["unit_price"], item["total_price"], serials_str, item["supplier_order_id"]))

        # Обновляем склад: связываем серийники с клиентским заказом (продажей)
        for sn in item["serials"]:
            c.execute("""
                UPDATE warehouse SET client_order_id=?
                WHERE serial=?
            """, (order_id, sn))
    conn.commit()

    text = f"✅ Заказ клиенту создан:\n\nДата заказа: {date}\nНомер заказа: {order_id}\nКлиент: {client}\n\n"
    for idx, item in enumerate(order_data["items"], 1):
        text += (f"{idx}. {item['product_name']}\n"
                 f"   Цена за шт: {item['unit_price']}₽\n"
                 f"   Кол-во: {item['quantity']}\n"
                 f"   Стоимость: {item['total_price']}₽\n"
                 f"   Серийные номера: {', '.join(item['serials'])}\n"
                 f"   Поставщик заказа: {item['supplier_order_id']}\n\n")
    text += f"<b>Общая сумма заказа:</b> {total_order_price}₽"

    temp_storage.pop(user_id, None)
    await state.clear()
    await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=main_menu_kb())

# ================ DATABASE HELPERS ================
async def get_order_from_db(order_id: str, order_type: str) -> dict:
    """Улучшенная функция получения заказа"""
    try:
        if order_type == "supplier":
            c.execute("""SELECT * FROM supplier_orders 
                      WHERE order_id = ?""", (order_id,))
        else:
            c.execute("""SELECT * FROM client_orders 
                      WHERE order_id = ?""", (order_id,))
        
        order = dict(zip([col[0] for col in c.description], c.fetchone()))
        order["items"] = []
        
        # Получение связанных серийников
        c.execute("""SELECT serial FROM warehouse 
                  WHERE {} = ?""".format(
                      "supplier_order_id" if order_type == "supplier" 
                      else "client_order_id"
                  ), (order_id,))
        order["serials"] = [row[0] for row in c.fetchall()]
        return order
    except Exception as e:
        logging.error(f"Order fetch error: {str(e)}")
        return None

def update_supplier_order(order: dict):
    """Обновление заказа поставщика в БД"""
    with conn:
        # Обновляем основную запись
        c.execute("""UPDATE supplier_orders SET
            date = ?,
            supplier = ?,
            product_name = ?,
            product_code = ?,
            quantity = ?,
            unit_price = ?,
            total_price = ?,
            serials = ?
            WHERE order_id = ?""",
            (order['date'], order['supplier'], order['product_name'],
             order['product_code'], order['quantity'], order['unit_price'],
             order['total_price'], ','.join(order['serials']), order['order_id']))
        
        # Обновляем склад
        c.execute("DELETE FROM warehouse WHERE supplier_order_id = ?", (order['order_id'],))
        for item in order['items']:
            c.execute("""INSERT INTO warehouse 
                (serial, product_name, supplier_order_id, client_order_id, unit_price)
                VALUES (?, ?, ?, ?, ?)""",
                (item['serial'], item['product_name'], order['order_id'],
                 None, item['unit_price']))

def update_client_order(order: dict):
    """Обновление заказа клиента в БД"""
    with conn:
        # Обновляем основную запись
        c.execute("""UPDATE client_orders SET
            date = ?,
            client = ?,
            product_name = ?,
            quantity = ?,
            unit_price = ?,
            total_price = ?,
            serials = ?,
            supplier_order_id = ?
            WHERE order_id = ?""",
            (order['date'], order['client'], order['product_name'],
             order['quantity'], order['unit_price'], order['total_price'],
             ','.join(order['serials']), order['supplier_order_id'], order['order_id']))
        
        # Обновляем склад
        c.execute("UPDATE warehouse SET client_order_id = NULL WHERE client_order_id = ?", 
                 (order['order_id'],))
        for serial in order['serials']:
            c.execute("UPDATE warehouse SET client_order_id = ? WHERE serial = ?",
                     (order['order_id'], serial))

def check_serials_usage(serials: list) -> list:
    """Проверка использования серийных номеров в других заказах"""
    conflicting = []
    for sn in serials:
        c.execute("SELECT client_order_id FROM warehouse WHERE serial = ?", (sn,))
        result = c.fetchone()
        if result and result[0]:
            conflicting.append(result[0])
    return list(set(conflicting))

def compare_orders(original: dict, modified: dict) -> list:
    """Сравнение двух версий заказа"""
    diff = []
    
    # Сравнение основных полей
    for field in ['date', 'supplier', 'client']:
        if original.get(field) != modified.get(field):
            diff.append((
                field.capitalize(),
                original.get(field, 'N/A'),
                modified.get(field, 'N/A')
            ))
    
    # Сравнение товаров
    orig_items = {(i['product_name'], i['serial']): i for i in original['items']}
    mod_items = {(i['product_name'], i['serial']): i for i in modified['items']}
    
    # Удаленные товары
    for key in orig_items.keys() - mod_items.keys():
        diff.append(('Удален товар', f"{key[0]} ({key[1]})", 'Удален'))
    
    # Добавленные товары
    for key in mod_items.keys() - orig_items.keys():
        diff.append(('Добавлен товар', 'Новый', f"{key[0]} ({key[1]})"))
    
    # Измененные товары
    for key in orig_items.keys() & mod_items.keys():
        orig = orig_items[key]
        mod = mod_items[key]
        if orig['unit_price'] != mod['unit_price']:
            diff.append((
                f"Цена {key[0]}",
                f"{orig['unit_price']}₽",
                f"{mod['unit_price']}₽"
            ))
    
    return diff

# ----------------- EDITING LOGIC ------------------
@router.callback_query(F.data.startswith("edit_"))
async def start_editing_order(callback: CallbackQuery, state: FSMContext):
    try:
        # Более надежное разделение данных
        parts = callback.data.split("_")
        if len(parts) != 3:
            raise ValueError
        order_type = parts[1]
        order_id = parts[2]
        
        order = get_order_from_db(order_id, order_type)
        if not order:
            await callback.answer("Заказ не найден")
            return
        
        await state.update_data(
            original_order=order,
            order_type=order_type,
            order_id=order_id,
            modified_order=order.copy()
        )
        await show_editing_menu(callback.message, state)
    except Exception as e:
        logging.error(f"Edit error: {str(e)}")
        await callback.answer("❌ Ошибка формата данных заказа")

async def show_editing_menu(message: Message, state: FSMContext):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Изменить дату", callback_data="edit_date")],
        [InlineKeyboardButton(text="🏢 Изменить контрагента", callback_data="edit_counterparty")],
        [InlineKeyboardButton(text="📦 Редактировать товары", callback_data="edit_products")],
        [InlineKeyboardButton(text="💵 Изменить стоимость", callback_data="edit_prices")],
        [InlineKeyboardButton(text="✅ Завершить редактирование", callback_data="finish_editing")]
    ])
    
    await message.answer("Выберите параметр для редактирования:", reply_markup=keyboard)
    await state.set_state(EditingOrder.choosing_field)

# ------ Редактирование даты ------
@router.callback_query(F.data == "edit_date", EditingOrder.choosing_field)
async def change_date(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите новую дату в формате ДД.ММ.ГГГГ:")
    await state.set_state(EditingOrder.changing_date)

@router.message(EditingOrder.changing_date)
async def process_new_date(message: Message, state: FSMContext):
    # Проверка формата даты
    try:
        datetime.strptime(message.text, "%d.%m.%Y")
    except ValueError:
        await message.answer("❌ Неверный формат даты! Попробуйте снова:")
        return
    
    data = await state.get_data()
    data["modified_order"]["date"] = message.text
    await state.set_data(data)
    
    await message.answer("✅ Дата успешно изменена!")
    await show_editing_menu(message, state)

# ------ Редактирование контрагента ------
@router.callback_query(F.data == "edit_counterparty", EditingOrder.choosing_field)
async def change_counterparty(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    counterparty_type = "поставщика" if data["order_type"] == "supplier" else "клиента"
    
    await callback.message.answer(f"Введите новое имя {counterparty_type}:")
    await state.set_state(EditingOrder.changing_counterparty)

@router.message(EditingOrder.changing_counterparty)
async def process_new_counterparty(message: Message, state: FSMContext):
    data = await state.get_data()
    field = "supplier" if data["order_type"] == "supplier" else "client"
    
    data["modified_order"][field] = message.text
    await state.set_data(data)
    
    await message.answer("✅ Данные контрагента обновлены!")
    await show_editing_menu(message, state)

# ------ Редактирование товаров ------
@router.callback_query(F.data == "edit_products", EditingOrder.choosing_field)
async def edit_products_menu(callback: CallbackQuery, state: FSMContext):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="add_product")],
        [InlineKeyboardButton(text="➖ Удалить товар", callback_data="remove_product")],
        [InlineKeyboardButton(text="✏️ Изменить количество", callback_data="change_quantity")],
        [InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_editing")]
    ])
    
    await callback.message.edit_text("Выберите действие с товарами:", reply_markup=keyboard)
    await state.set_state(EditingOrder.choosing_product_action)

# ------ Добавление товара ------
@router.callback_query(F.data == "add_product", EditingOrder.choosing_product_action)
async def add_new_product(callback: CallbackQuery, state: FSMContext):
    # Используем существующую логику из создания заказа
    await callback.message.answer("Введите код или название товара:")
    await state.set_state(OrderSupplier.entering_product)  # Используем состояния из создания заказа

# ------ Удаление товара ------
@router.callback_query(F.data == "remove_product", EditingOrder.choosing_product_action)
async def remove_product_menu(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=item["product_name"], callback_data=f"remove_{idx}")]
        for idx, item in enumerate(data["modified_order"]["items"])
    ] + [[InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_products")]])
    
    await callback.message.edit_text("Выберите товар для удаления:", reply_markup=keyboard)

@router.callback_query(F.data.startswith("remove_"))
async def confirm_product_removal(callback: CallbackQuery, state: FSMContext):
    product_idx = int(callback.data.split("_")[1])
    data = await state.get_data()
    product = data["modified_order"]["items"][product_idx]
    
    # Проверка серийных номеров
    conflicted_orders = check_serials_usage(product["serials"])  # Ваша функция проверки
    
    if conflicted_orders:
        conflict_text = "\n".join([f"- {order_id}" for order_id in conflicted_orders])
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_removal")],
            [InlineKeyboardButton(text="🗑️ Удалить связанные заказы", callback_data=f"force_remove_{product_idx}")]
        ])
        
        await callback.message.edit_text(
            f"⚠️ Невозможно удалить товар. Серийные номера используются в заказах:\n{conflict_text}",
            reply_markup=keyboard
        )
        return
    
    await perform_product_removal(callback, state, product_idx)

def order_actions_kb(order_id: str, order_type: str, has_serials: bool):
    keyboard = [
        [
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"edit_{order_type}_{order_id}"),
            InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"delete_{order_type}_{order_id}")
        ],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ]
    
    if not has_serials:
        keyboard.insert(1, [InlineKeyboardButton(
            text="➕ Добавить серийный номер", 
            callback_data=f"add_serial_{order_type}_{order_id}"
        )])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

async def perform_product_removal(callback: CallbackQuery, state: FSMContext, product_idx: int):
    data = await state.get_data()
    
    # Удаляем из modified_order
    removed_item = data["modified_order"]["items"].pop(product_idx)
    
    # Удаляем серийники из warehouse
    for sn in removed_item["serials"]:
        c.execute("DELETE FROM warehouse WHERE serial = ?", (sn,))
    
    conn.commit()
    
    await callback.message.answer("✅ Товар успешно удален!")
    await edit_products_menu(callback, state)

# ------ Изменение количества ------
@router.callback_query(F.data == "change_quantity", EditingOrder.choosing_product_action)
async def select_product_for_quantity(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=item["product_name"], callback_data=f"change_qty_{idx}")]
        for idx, item in enumerate(data["modified_order"]["items"])
    ] + [[InlineKeyboardButton(text="↩️ Назад", callback_data="back_to_products")]])
    
    await callback.message.edit_text("Выберите товар для изменения количества:", reply_markup=keyboard)

@router.callback_query(F.data.startswith("change_qty_"))
async def input_new_quantity(callback: CallbackQuery, state: FSMContext):
    product_idx = int(callback.data.split("_")[2])
    await state.update_data(editing_product_idx=product_idx)
    
    await callback.message.answer("Введите новое количество товара:")
    await state.set_state(EditingOrder.changing_quantity)

@router.message(EditingOrder.changing_quantity)
async def process_new_quantity(message: Message, state: FSMContext):
    try:
        new_qty = int(message.text)
        if new_qty < 1:
            raise ValueError
    except ValueError:
        await message.answer("❌ Некорректное количество! Введите целое число больше 0:")
        return
    
    data = await state.get_data()
    product_idx = data["editing_product_idx"]
    product = data["modified_order"]["items"][product_idx]
    
    if new_qty > product["quantity"]:
        # Добавление серийников
        await state.update_data(required_serials=new_qty - product["quantity"])
        await message.answer(f"Введите {new_qty - product["quantity"]} новых серийных номеров:")
        await state.set_state(EditingOrder.adding_serials)
    elif new_qty < product["quantity"]:
        # Удаление серийников
        await state.update_data(serials_to_remove=product["quantity"] - new_qty)
        await show_serial_removal_menu(message, state, product["serials"])
    else:
        await message.answer("✅ Количество не изменилось")
        await show_editing_menu(message, state)

# ------ Обработка серийных номеров ------
async def show_serial_removal_menu(message: Message, state: FSMContext, serials: list):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=sn, callback_data=f"remove_sn_{sn}")]
        for sn in serials
    ])
    
    await message.answer("Выберите серийные номера для удаления:", reply_markup=keyboard)
    await state.set_state(EditingOrder.removing_serials)

@router.callback_query(F.data.startswith("remove_sn_"), EditingOrder.removing_serials)
async def process_serial_removal(callback: CallbackQuery, state: FSMContext):
    sn_to_remove = callback.data.split("_")[2]
    data = await state.get_data()
    
    # Обновляем данные
    data["modified_order"]["items"][data["editing_product_idx"]]["serials"].remove(sn_to_remove)
    data["serials_to_remove"] -= 1
    
    # Удаляем из БД
    c.execute("DELETE FROM warehouse WHERE serial = ?", (sn_to_remove,))
    conn.commit()
    
    if data["serials_to_remove"] > 0:
        await show_serial_removal_menu(callback.message, state, 
            data["modified_order"]["items"][data["editing_product_idx"]]["serials"])
    else:
        await callback.message.answer("✅ Серийные номера успешно удалены!")
        await show_editing_menu(callback.message, state)

# ------ Редактирование цен ------
@router.callback_query(F.data == "edit_prices", EditingOrder.choosing_field)
async def select_product_for_price(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    
    if len(data["modified_order"]["items"]) == 1:
        await state.update_data(editing_product_idx=0)
        await callback.message.answer("Введите новую цену за единицу:")
        await state.set_state(EditingOrder.changing_price)
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=item["product_name"], callback_data=f"change_price_{idx}")]
            for idx, item in enumerate(data["modified_order"]["items"])
        ])
        await callback.message.edit_text("Выберите товар для изменения цены:", reply_markup=keyboard)

@router.callback_query(F.data.startswith("change_price_"))
async def input_new_price(callback: CallbackQuery, state: FSMContext):
    product_idx = int(callback.data.split("_")[2])
    await state.update_data(editing_product_idx=product_idx)
    
    await callback.message.answer("Введите новую цену за единицу:")
    await state.set_state(EditingOrder.changing_price)

@router.message(EditingOrder.changing_price)
async def process_new_price(message: Message, state: FSMContext):
    try:
        new_price = float(message.text.replace(",", "."))
        if new_price <= 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Некорректная цена! Введите положительное число:")
        return
    
    data = await state.get_data()
    product_idx = data["editing_product_idx"]
    
    # Обновляем цену
    data["modified_order"]["items"][product_idx]["unit_price"] = new_price
    data["modified_order"]["items"][product_idx]["total_price"] = (
        new_price * data["modified_order"]["items"][product_idx]["quantity"]
    )
    
    await state.set_data(data)
    await message.answer("✅ Цена успешно обновлена!")
    await show_editing_menu(message, state)

# ------ Финализация изменений ------
@router.callback_query(F.data == "finish_editing", EditingOrder.choosing_field)
async def confirm_changes(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    
    diff = compare_orders(data["original_order"], data["modified_order"])  # Ваша функция сравнения
    
    text = "Изменения:\n" + "\n".join(
        f"{field}: {old} → {new}" 
        for field, old, new in diff
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_edit")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_edit")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard)
    await state.set_state(EditingOrder.confirming_changes)

@router.callback_query(F.data == "confirm_edit")
async def save_changes(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    
    try:
        if data["order_type"] == "supplier":
            update_supplier_order(data["modified_order"])
        else:
            update_client_order(data["modified_order"])
        
        # Обновляем кассу при изменении цен
        if any('Цена' in change[0] for change in compare_orders(data["original_order"], data["modified_order"])):
            update_cash_records(data["modified_order"])
        
        await callback.message.edit_text("✅ Изменения успешно сохранены!\n" + get_order_summary(data["modified_order"]))
        await state.clear()
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка сохранения: {str(e)}")
        await state.set_state(EditingOrder.confirming_changes)

def update_cash_records(order: dict):
    """Обновление записей в кассе при изменении цен"""
    if order['order_type'] == 'client':
        total_diff = order['total_price'] - get_order_from_db(order['order_id'], 'client')['total_price']
        if total_diff != 0:
            c.execute("""INSERT INTO cash (date, cash, comment)
                      VALUES (?, ?, ?)""",
                      (datetime.now().strftime("%d.%m.%Y"), 
                       total_diff, 
                       f"Корректировка заказа {order['order_id']}"))
            conn.commit()

def get_order_summary(order: dict) -> str:
    """Формирование сводки по заказу"""
    summary = []
    if order['order_type'] == 'supplier':
        summary.append(f"📦 Заказ поставщику {order['order_id']}")
        summary.append(f"📅 Дата: {order['date']}")
        summary.append(f"🏭 Поставщик: {order['supplier']}")
    else:
        summary.append(f"📤 Заказ клиенту {order['order_id']}")
        summary.append(f"📅 Дата: {order['date']}")
        summary.append(f"👤 Клиент: {order['client']}")
    
    summary.append("\n📋 Состав заказа:")
    for item in order['items']:
        summary.append(f"• {item['product_name']} ({item['serial']})")
        summary.append(f"  Кол-во: {item['quantity']} шт.")
        summary.append(f"  Цена: {item['unit_price']}₽")
        summary.append(f"  Сумма: {item['total_price']}₽\n")
    
    summary.append(f"💵 Итого: {sum(i['total_price'] for i in order['items'])}₽")
    return "\n".join(summary)



# ---------------- Просмотр склада ------------------

@router.message(F.text == "Склад")
async def show_warehouse(message: Message):
    c.execute("""
        SELECT product_name, COUNT(*) FROM warehouse
        WHERE client_order_id IS NULL
        GROUP BY product_name
    """)
    rows = c.fetchall()
    if not rows:
        await message.answer("Склад пуст.")
        return
    text = "📦 Остатки на складе:\n"
    for product_name, count in rows:
        text += f"- {product_name}: {count} шт.\n"
    await message.answer(text)

# ---------------- Поиск заказа ------------------

@router.message(F.text == "Поиск заказа")
async def search_order_start(message: Message, state: FSMContext):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="По номеру заказа", callback_data="search_by_id")],
        [InlineKeyboardButton(text="По дате заказа", callback_data="search_by_date")]
    ])
    await message.answer("Выберите тип поиска заказа:", reply_markup=keyboard)
    await state.set_state(SearchOrderState.choosing_type)

@router.callback_query(F.data == "search_by_id", SearchOrderState.choosing_type)
async def search_by_id_choice(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите номер заказа:")
    await state.set_state(SearchOrderState.entering_query)

@router.callback_query(F.data == "search_by_date", SearchOrderState.choosing_type)
async def search_by_date_choice(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите дату заказа в формате дд.мм.гггг:")
    await state.set_state(SearchOrderState.entering_query)

@router.callback_query(F.data == "search_by_serial", SearchOrderState.choosing_type)
async def search_by_serial_choice(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("🔢 Введите серийный номер:")
    await state.set_state(SearchOrderState.entering_serial)

@router.callback_query(F.data == "search_by_product", SearchOrderState.choosing_type)
async def search_by_product_choice(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("📦 Введите название товара:")
    await state.set_state(SearchOrderState.entering_product)

@router.message(SearchOrderState.entering_serial)
async def handle_serial_search(message: Message, state: FSMContext):
    serial = message.text.strip()
    await search_order_execute(message, state, search_type="serial", query=serial)

@router.message(SearchOrderState.entering_product)
async def handle_product_search(message: Message, state: FSMContext):
    product = message.text.strip()
    await search_order_execute(message, state, search_type="product", query=product)

# Удалите старый обработчик SearchOrderState.entering_query
# И замените его на эти функции:

async def search_order_execute(message: Message, state: FSMContext, search_type: str, query: str):
    if search_type == "serial":
        await process_search_query(message, state, search_type, query)
    elif search_type == "product":
        await process_search_query(message, state, search_type, query)
    elif search_type == "id":
        await process_search_by_id_or_date(message, query)
    elif search_type == "date":
        await process_search_by_id_or_date(message, query)
    else:
        await message.answer("❌ Неизвестный тип поиска")
    await state.clear()

@router.message(SearchOrderState.entering_serial)
async def handle_serial_search(message: Message, state: FSMContext):
    serial = message.text.strip()
    await search_order_execute(message, state, "serial", serial)

@router.message(SearchOrderState.entering_product)
async def handle_product_search(message: Message, state: FSMContext):
    product = message.text.strip()
    await search_order_execute(message, state, "product", product)

@router.message(SearchOrderState.entering_query)
async def handle_id_or_date_search(message: Message, state: FSMContext):
    query = message.text.strip()
    data = await state.get_data()
    search_type = data.get("search_type", "id")
    await search_order_execute(message, state, search_type, query)

# ----------------- ОБНОВЛЕННАЯ ЛОГИКА ПОИСКА ------------------
async def process_search_query(message: Message, state: FSMContext, search_type: str, query: str):
    results = []
    
    if search_type == "serial":
        c.execute("""
            SELECT 
                CASE 
                    WHEN w.client_order_id IS NOT NULL THEN 'Клиент'
                    ELSE 'Поставщик'
                END as order_type,
                COALESCE(s.order_id, c.order_id) as order_id,
                COALESCE(s.date, c.date) as date,
                COALESCE(s.supplier, c.client) as counterparty,
                w.product_name,
                1 as quantity,
                COALESCE(s.unit_price, c.unit_price) as unit_price
            FROM warehouse w
            LEFT JOIN supplier_orders s ON w.supplier_order_id = s.order_id
            LEFT JOIN client_orders c ON w.client_order_id = c.order_id
            WHERE w.serial = ?
        """, (query,))
        results = c.fetchall()
    
    elif search_type == "product":
        c.execute("""
            SELECT 'Поставщик', order_id, date, supplier, product_name, quantity, total_price 
            FROM supplier_orders 
            WHERE product_name LIKE ?
            UNION
            SELECT 'Клиент', order_id, date, client, product_name, quantity, total_price 
            FROM client_orders 
            WHERE product_name LIKE ?
        """, (f"%{query}%", f"%{query}%"))
        results = c.fetchall()
    
    # Остальная логика отображения результатов
    if not results:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Новый поиск", callback_data="order_search")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
        ])
        await message.answer("🔍 Ничего не найдено", reply_markup=kb)
        await state.clear()
        return
    
    text = "🔍 Результаты поиска:\n\n"
    for idx, (otype, oid, date, name, product, qty, total) in enumerate(results, 1):
        text += (f"{idx}. {otype}\n"
                 f"   Номер: {oid}\n"
                 f"   Дата: {date}\n"
                 f"   Товар: {product}\n"
                 f"   Кол-во: {qty}\n"
                 f"   Сумма: {total}₽\n\n")
    
    await message.answer(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"📄 Открыть заказ {oid}", 
                callback_data=f"open_{otype.lower()}_{oid}"
            )] for _, oid, *_ in results
        ] + [[InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]])
    )
    await state.clear()

async def process_search_by_id_or_date(message: Message, query: str):
    # Поиск по номеру заказа
    c.execute("""
        SELECT 'Поставщик', order_id, date, supplier, product_name, quantity, total_price 
        FROM supplier_orders WHERE order_id = ?
        UNION
        SELECT 'Клиент', order_id, date, client, product_name, quantity, total_price 
        FROM client_orders WHERE order_id = ?
    """, (query, query))
    
    results = c.fetchall()
    
    if not results:
        # Если не найден по ID, пробуем поиск по дате
        try:
            datetime.strptime(query, "%d.%m.%Y")
            c.execute("""
                SELECT 'Поставщик', order_id, date, supplier, product_name, quantity, total_price
                FROM supplier_orders WHERE date = ?
                UNION
                SELECT 'Клиент', order_id, date, client, product_name, quantity, total_price
                FROM client_orders WHERE date = ?
            """, (query, query))
            results = c.fetchall()
        except ValueError:
            pass
    
    if not results:
        await message.answer("🚫 Заказы не найдены")
        return
    
    text = "🔍 Результаты поиска:\n\n"
    for idx, (otype, oid, date, name, product, qty, total) in enumerate(results, 1):
        text += (f"{idx}. {otype}\n"
                 f"   Номер: {oid}\n"
                 f"   Дата: {date}\n"
                 f"   Контрагент: {name}\n"
                 f"   Товар: {product}\n"
                 f"   Кол-во: {qty}\n"
                 f"   Сумма: {total}₽\n\n")
    
    await message.answer(
    text,
    reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=f"📄 Открыть заказ {oid}",
                callback_data=f"open_{otype.lower()}_{oid}"
            )] 
            for _, oid, *_ in results
        ] + [
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
        ]
    )
)


# ----------------- ОБРАБОТКА УДАЛЕНИЯ ЗАКАЗА ------------------
@router.callback_query(F.data.startswith("delete_"))
async def delete_order_start(callback: CallbackQuery, state: FSMContext):
    _, order_type, order_id = callback.data.split("_")
    await state.update_data(order_type=order_type, order_id=order_id)
    await callback.message.edit_text(
        f"⚠️ Вы уверены, что хотите удалить заказ {order_id}?",
        reply_markup=confirm_delete_kb(order_id, order_type)
    )
    await state.set_state(DeleteOrderState.confirm_delete)

@router.callback_query(F.data.startswith("confirm_delete_"), DeleteOrderState.confirm_delete)
async def confirm_delete_order(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    order_id = data["order_id"]
    order_type = data["order_type"]

    try:
        conn.execute("BEGIN")
        
        if order_type == "supplier":
            # Проверка на использование в клиентских заказах
            c.execute("""
                SELECT client_order_id FROM warehouse 
                WHERE supplier_order_id = ? AND client_order_id IS NOT NULL
                LIMIT 1
            """, (order_id,))
            client_order = c.fetchone()
            
            if client_order:
                await callback.message.answer(
                    f"❌ Заказ не может быть удален. Товар используется в заказе клиента: "
                    f"<a href='t.me/your_bot?start=order_{client_order[0]}'>{client_order[0]}</a>",
                    parse_mode=ParseMode.HTML
                )
                return

            # Удаление из warehouse
            c.execute("DELETE FROM warehouse WHERE supplier_order_id = ?", (order_id,))
            # Удаление из supplier_orders
            c.execute("DELETE FROM supplier_orders WHERE order_id = ?", (order_id,))
        
        elif order_type == "client":
            # Обновление warehouse
            c.execute("""
                UPDATE warehouse 
                SET client_order_id = NULL 
                WHERE client_order_id = ?
            """, (order_id,))
            # Удаление из client_orders
            c.execute("DELETE FROM client_orders WHERE order_id = ?", (order_id,))

        conn.commit()
        await callback.message.edit_text(f"✅ Заказ {order_id} успешно удален", reply_markup=main_menu_kb())
    
    except sqlite3.Error as e:
        conn.rollback()
        await callback.message.answer(f"❌ Ошибка при удалении: {e}")
    
    await state.clear()

@router.callback_query(F.data.startswith("back_to_order_"))
async def back_to_order(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split("_")[-1]
    # Логика для повторного отображения заказа
    await show_order_details(callback.message, order_id)
    await state.clear()

# ----------------- ПОКАЗ ДЕТАЛЕЙ ЗАКАЗА ------------------
async def show_order_details(message: Message, order_id: str):
    # Получаем полные данные о заказе
    c.execute("""
        SELECT 
            order_type,
            order_id, 
            date, 
            counterparty,
            product_name,
            quantity,
            total_price,
            serials
        FROM (
            SELECT 
                'supplier' as order_type, 
                order_id, 
                date, 
                supplier as counterparty, 
                product_name, 
                quantity, 
                total_price,
                serials
            FROM supplier_orders 
            WHERE order_id = ?
            UNION
            SELECT 
                'client' as order_type, 
                order_id, 
                date, 
                client as counterparty, 
                product_name, 
                quantity, 
                total_price,
                serials
            FROM client_orders 
            WHERE order_id = ?
        )
    """, (order_id, order_id))
    
    order_data = c.fetchone()
    
    if not order_data:
        await message.answer("🚫 Заказ не найден")
        return

    # Распаковываем данные
    (order_type, oid, date, counterparty, 
     product, qty, total, serials) = order_data
     
    # Проверяем наличие серийников
    has_serials = bool(serials and serials.strip())
    
    # Формируем текст
    order_type_text = "поставщику" if order_type == "supplier" else "клиенту"
    text = (
        f"📄 Заказ {order_type_text}:\n\n"
        f"🔢 Номер: {oid}\n"
        f"📅 Дата: {date}\n"
        f"👤 Контрагент: {counterparty}\n"
        f"📦 Товар: {product}\n"
        f"🔢 Количество: {qty}\n"
        f"💰 Сумма: {total}₽\n"
        f"🏷️ Серийники: {serials if has_serials else 'отсутствуют'}"
    )

    # Создаем клавиатуру с учетом наличия серийников
    await message.answer(
        text,
        reply_markup=order_actions_kb(
            order_id=oid,
            order_type=order_type,
            has_serials=has_serials
        )
    )

# ----------------- ОБНОВЛЕННЫЕ КОМАНДЫ ------------------
@router.callback_query(F.data.startswith("open_"))
async def open_order(callback: CallbackQuery):
    _, order_type, order_id = callback.data.split("_")
    await show_order_details(callback.message, order_id)
    await callback.answer()

@router.callback_query(F.data == "main_menu")
async def return_to_main_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("🏠 Главное меню:", reply_markup=main_menu_kb())

from aiogram import F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.enums import ParseMode
from datetime import datetime

# Команда "Отчёт"
@router.message(F.text == "Отчёт")
async def report_start(message: Message, state: FSMContext):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="За период", callback_data="report_period")],
        [InlineKeyboardButton(text="За сегодня", callback_data="report_today")],
    ])
    await message.answer("Выберите тип отчёта:", reply_markup=keyboard)
    await state.set_state(ReportState.choosing_type)

# Отчёт за сегодня
@router.callback_query(F.data == "report_today", ReportState.choosing_type)
async def report_today(callback: CallbackQuery, state: FSMContext):
    today = datetime.now().strftime("%d.%m.%Y")
    await send_report(callback.message, state, today, today)
    await state.clear()

# Выбор периода
@router.callback_query(F.data == "report_period", ReportState.choosing_type)
async def report_period(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите период отчёта в формате дд.мм.гггг - дд.мм.гггг:")
    await state.set_state(ReportState.entering_period)

# Ввод периода
@router.message(ReportState.entering_period)
async def report_period_entered(message: Message, state: FSMContext):
    try:
        start_str, end_str = map(str.strip, message.text.split("-"))
        datetime.strptime(start_str, "%d.%m.%Y")
        datetime.strptime(end_str, "%d.%m.%Y")
        await send_report(message, state, start_str, end_str)
        await state.clear()
    except Exception:
        await message.answer("Неверный формат. Введите период в формате дд.мм.гггг - дд.мм.гггг:")

# Генерация отчёта
async def send_report(message, state, start_date, end_date):
    c.execute("""
        SELECT 'Поставщик', date, product_name, SUM(quantity), SUM(total_price)
        FROM supplier_orders
        WHERE date BETWEEN ? AND ?
        GROUP BY product_name, date
    """, (start_date, end_date))
    supplier_data = c.fetchall()

    c.execute("""
        SELECT 'Клиент', date, product_name, SUM(quantity), SUM(total_price)
        FROM client_orders
        WHERE date BETWEEN ? AND ?
        GROUP BY product_name, date
    """, (start_date, end_date))
    client_data = c.fetchall()

    text = f"📊 Отчёт с {start_date} по {end_date}:\n\n"

    total_supplier_qty = 0
    total_supplier_sum = 0
    if supplier_data:
        text += "📥 Поставки:\n"
        for _, date, product, qty, total in supplier_data:
            total_supplier_qty += qty
            total_supplier_sum += total
            text += f"{date}: {product} — {qty} шт, сумма {total}₽\n"
        text += f"\nИтого по поставкам: {total_supplier_qty} шт на {total_supplier_sum}₽\n"
    else:
        text += "📥 Поставки: нет данных\n"

    total_client_qty = 0
    total_client_sum = 0
    if client_data:
        text += "\n📤 Продажи:\n"
        for _, date, product, qty, total in client_data:
            total_client_qty += qty
            total_client_sum += total
            text += f"{date}: {product} — {qty} шт, сумма {total}₽\n"
        text += f"\nИтого по продажам: {total_client_qty} шт на {total_client_sum}₽\n"
    else:
        text += "\n📤 Продажи: нет данных\n"

    # Подсчёт чистой прибыли
    net_profit = total_client_sum - total_supplier_sum
    text += f"\n💰 Чистая прибыль: {net_profit}₽"

    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=main_menu_kb())

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
dp.include_router(router)

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())