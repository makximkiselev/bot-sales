import sqlite3
import logging
from gspread import authorize
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    ContextTypes
)

# Настройка логгирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Настройки подключения
GOOGLE_SHEET_KEY = '1OTHRzo4OAH_bWuplxYjwqpyJm857mYm0kaG0RHhHcOY'
BOT_TOKEN = '7957981552:AAE33m7eOVbce18vdk5BNjxQbwqKyZcMQH4'

# Состояния ConversationHandler
(
    START, ORDER_PROVIDER, ORDER_ITEM_SEARCH, ORDER_ITEM_PAGE,
    ORDER_QUANTITY, ORDER_COST_PER_UNIT, ORDER_SERIAL_INPUT,
    ORDER_SERIAL_CONFIRM, ORDER_QUANTITY_MANUAL, CONFIRM_DELETE
) = range(10)

# Константы
ITEMS_PER_PAGE = 5  # Количество товаров на странице

# Инициализация Google Sheets
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_name('credentials.json', scope)
client = authorize(creds)
sheet = client.open_by_key(GOOGLE_SHEET_KEY).worksheet('Прайс')

# Подключение к SQLite
conn = sqlite3.connect('bot_database.db', check_same_thread=False)
cursor = conn.cursor()

# Создание таблиц
cursor.execute('''
CREATE TABLE IF NOT EXISTS providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE
);
''')

cursor.execute('''
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id INTEGER,
    item_id TEXT,
    quantity INTEGER,
    serial_numbers TEXT,
    cost_per_unit REAL,
    total_cost REAL,
    FOREIGN KEY(provider_id) REFERENCES providers(id)
);
''')
conn.commit()

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Глобальный обработчик ошибок"""
    logger.error(msg="Exception while handling update:", exc_info=context.error)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="⚠ Произошла ошибка. Попробуйте еще раз."
    )

async def get_products():
    """Получение списка товаров из Google Sheets"""
    try:
        codes = sheet.col_values(5)[1:]  # Пятый столбец (коды)
        names = sheet.col_values(6)[1:]  # Шестой столбец (наименования)
        return dict(zip(names, codes))
    except Exception as e:
        logger.error(f"Ошибка при получении товаров: {str(e)}")
        return {}

async def search_products(search_term: str):
    """Поиск товаров по частичному совпадению"""
    products = await get_products()
    return {name: code for name, code in products.items() if search_term.lower() in name.lower()}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /start"""
    keyboard = [
        [InlineKeyboardButton("📦 Новый заказ", callback_data='new_order')],
        [InlineKeyboardButton("✏️ Редактировать заказ", callback_data='edit_order'),
         InlineKeyboardButton("🗑️ Удалить заказ", callback_data='delete_order')]
    ]
    await update.message.reply_text(
        "🏪 Магазин электроники\nВыберите действие:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return START

async def new_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало создания нового заказа"""
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text(text="Введите название поставщика:")
    return ORDER_PROVIDER

async def process_provider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка поставщика"""
    provider_name = update.message.text
    context.user_data['provider_name'] = provider_name
    
    cursor.execute("SELECT id FROM providers WHERE name=?", (provider_name,))
    provider = cursor.fetchone()
    
    if provider:
        context.user_data['provider_id'] = provider[0]
        await update.message.reply_text("Введите часть названия товара для поиска:")
        return ORDER_ITEM_SEARCH
    else:
        keyboard = [[InlineKeyboardButton("✅ Создать нового", callback_data="create_provider")]]
        await update.message.reply_text(
            "🔍 Поставщик не найден:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ORDER_PROVIDER

async def create_provider(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создание нового поставщика"""
    query = update.callback_query
    await query.answer()
    provider_name = context.user_data.get('provider_name')
    
    try:
        with conn:
            cursor.execute("INSERT INTO providers(name) VALUES (?)", (provider_name,))
            context.user_data['provider_id'] = cursor.lastrowid
        await query.edit_message_text(f"✅ Создан новый поставщик: {provider_name}\nВведите часть названия товара:")
        return ORDER_ITEM_SEARCH
    except sqlite3.IntegrityError:
        await query.edit_message_text("⚠️ Этот поставщик уже существует!")
        return ORDER_PROVIDER

async def process_item_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка поискового запроса"""
    search_term = update.message.text
    context.user_data['current_search'] = search_term
    context.user_data['current_page'] = 0
    
    products = await search_products(search_term)
    context.user_data['search_results'] = list(products.items())
    
    return await show_products_page(update, context)

async def show_products_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отображение страницы с результатами поиска"""
    search_results = context.user_data.get('search_results', [])
    page = context.user_data.get('current_page', 0)
    total_pages = (len(search_results) + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    
    keyboard = []
    start_idx = page * ITEMS_PER_PAGE
    end_idx = start_idx + ITEMS_PER_PAGE
    
    # Добавление товаров
    for name, code in search_results[start_idx:end_idx]:
        keyboard.append([InlineKeyboardButton(name, callback_data=f"item_{code}")])
    
    # Пагинация
    pagination = []
    if page > 0:
        pagination.append(InlineKeyboardButton("⬅️ Назад", callback_data="prev_page"))
    if end_idx < len(search_results):
        pagination.append(InlineKeyboardButton("Вперёд ➡️", callback_data="next_page"))
    
    if pagination:
        keyboard.append(pagination)
    
    # Управление поиском
    keyboard.append([
        InlineKeyboardButton("🔍 Новый поиск", callback_data="new_search"),
        InlineKeyboardButton("❌ Отмена", callback_data="cancel")
    ])
    
    text = f"🔍 Результаты поиска ({page+1}/{total_pages or 1}):"
    
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard))
    
    return ORDER_ITEM_PAGE

async def handle_product_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка действий на странице товаров"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "new_search":
        await query.edit_message_text("Введите новый поисковый запрос:")
        return ORDER_ITEM_SEARCH
    
    if query.data == "cancel":
        await cancel(update, context)
        return ConversationHandler.END
    
    if query.data in ["prev_page", "next_page"]:
        page = context.user_data.get('current_page', 0)
        if query.data == "prev_page" and page > 0:
            context.user_data['current_page'] = page - 1
        elif query.data == "next_page":
            context.user_data['current_page'] = page + 1
        
        return await show_products_page(update, context)
    
    if query.data.startswith("item_"):
        context.user_data['item_id'] = query.data.split('_')[1]
        await query.edit_message_text("Введите количество товара:")
        return ORDER_QUANTITY
    
    return ORDER_ITEM_PAGE

async def process_quantity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка количества товара"""
    try:
        quantity = int(update.message.text)
        if quantity <= 0:
            raise ValueError
        context.user_data['quantity'] = quantity
        await update.message.reply_text("Введите стоимость за единицу товара:")
        return ORDER_COST_PER_UNIT
    except ValueError:
        await update.message.reply_text("⚠️ Введите целое положительное число:")
        return ORDER_QUANTITY

async def process_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка стоимости"""
    try:
        cost = float(update.message.text)
        context.user_data['cost_per_unit'] = cost
        context.user_data['serials'] = []
        await update.message.reply_text(
            f"🔢 Введите серийные номера ({context.user_data['quantity']} шт.):\n"
            "Введите первый серийный номер:"
        )
        return ORDER_SERIAL_INPUT
    except ValueError:
        await update.message.reply_text("⚠️ Введите число:")
        return ORDER_COST_PER_UNIT

async def process_serial_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка ввода серийного номера"""
    serial = update.message.text.strip()
    serials = context.user_data.get('serials', [])
    
    if serial in serials:
        await update.message.reply_text("⚠️ Этот номер уже введен. Введите другой:")
        return ORDER_SERIAL_INPUT
    
    serials.append(serial)
    context.user_data['serials'] = serials
    
    keyboard = [
        [InlineKeyboardButton("❌ Удалить последний", callback_data="delete_last")],
        [InlineKeyboardButton("✅ Завершить", callback_data="finish_serials")]
    ] if len(serials) >= context.user_data['quantity'] else [
        [InlineKeyboardButton("❌ Удалить последний", callback_data="delete_last")],
        [InlineKeyboardButton("➡️ Продолжить", callback_data="continue_serials")]
    ]
    
    status = f"📋 Введено: {len(serials)}/{context.user_data['quantity']}\n"
    status += "\n".join([f"{i+1}. {s}" for i, s in enumerate(serials)])
    
    await update.message.reply_text(
        f"{status}\n\nВыберите действие:",
        reply_markup=InlineKeyboardMarkup(keyboard))
    return ORDER_SERIAL_CONFIRM

async def process_serial_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка подтверждения серийных номеров"""
    query = update.callback_query
    await query.answer()
    
    if query.data == "delete_last":
        serials = context.user_data['serials']
        if serials:
            serials.pop()
            context.user_data['serials'] = serials
            await query.edit_message_text("Введите следующий номер:")
            return ORDER_SERIAL_INPUT
    
    elif query.data == "finish_serials":
        if len(context.user_data['serials']) != context.user_data['quantity']:
            await query.edit_message_text(f"⚠️ Необходимо ввести {context.user_data['quantity']} номеров!")
            return ORDER_SERIAL_INPUT
        
        try:
            with conn:
                cursor.execute('''
                    INSERT INTO orders(provider_id, item_id, quantity, serial_numbers, cost_per_unit, total_cost)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    context.user_data['provider_id'],
                    context.user_data['item_id'],
                    context.user_data['quantity'],
                    ",".join(context.user_data['serials']),
                    context.user_data['cost_per_unit'],
                    context.user_data['cost_per_unit'] * context.user_data['quantity']
                ))
                order_id = cursor.lastrowid
                await query.edit_message_text(f"🎉 Заказ №{order_id} успешно создан!")
        except Exception as e:
            logger.error(f"Ошибка создания заказа: {str(e)}")
            await query.edit_message_text("⚠️ Ошибка при создании заказа!")
        
        return ConversationHandler.END
    
    await query.edit_message_text("Введите следующий серийный номер:")
    return ORDER_SERIAL_INPUT

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена текущей операции"""
    await update.message.reply_text("❌ Операция отменена")
    return ConversationHandler.END

def main():
    """Запуск бота"""
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_error_handler(error_handler)

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            START: [CallbackQueryHandler(new_order, pattern='^new_order$')],
            ORDER_PROVIDER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, process_provider),
                CallbackQueryHandler(create_provider, pattern='^create_provider$')
            ],
            ORDER_ITEM_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_item_search)],
            ORDER_ITEM_PAGE: [CallbackQueryHandler(handle_product_page)],
            ORDER_QUANTITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_quantity)],
            ORDER_COST_PER_UNIT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_cost)],
            ORDER_SERIAL_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_serial_input)],
            ORDER_SERIAL_CONFIRM: [CallbackQueryHandler(process_serial_confirmation)]
        },
        fallbacks=[CommandHandler('cancel', cancel)]
    )

    app.add_handler(conv_handler)
    app.run_polling()

if __name__ == '__main__':
    main()