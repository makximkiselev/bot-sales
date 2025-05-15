import sqlite3
from gspread import authorize
from oauth2client.service_account import ServiceAccountCredentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ConversationHandler
from telegram.ext.filters import Text  # Новый импорт фильтров

# Настройка подключения к Google Sheets
scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
creds = ServiceAccountCredentials.from_json_keyfile_name('credentials.json', scope)
client = authorize(creds)
sheet = client.open_by_key('1OTHRzo4OAH_bWuplxYjwqpyJm857mYm0kaG0RHhHcOY').worksheet('Прайс')

# Подключение к SQLite
conn = sqlite3.connect('bot_database.db')
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

ORDER_PROVIDER, ORDER_ITEM, ORDER_QUANTITY, ORDER_COST_PER_UNIT, ORDER_SERIAL_NUMBERS = range(5)

def start_new_order(update: Update, context):
    context.bot.send_message(chat_id=update.effective_chat.id, text="Введите наименование поставщика:")
    return ORDER_PROVIDER

def choose_provider(update: Update, context):
    provider_name = update.message.text
    cursor.execute("SELECT id FROM providers WHERE name=?", (provider_name,))
    result = cursor.fetchone()
    if result is not None:
        context.user_data['provider_id'] = result[0]
        context.bot.send_message(chat_id=update.effective_chat.id, text="Введите наименование товара:")
        return ORDER_ITEM
    else:
        context.bot.send_message(chat_id=update.effective_chat.id, text="Поставщик не найден. Хотите создать нового?")
        return ORDER_PROVIDER

def create_provider(update: Update, context):
    provider_name = update.message.text
    cursor.execute("INSERT INTO providers(name) VALUES (?)", (provider_name,))
    conn.commit()
    context.bot.send_message(chat_id=update.effective_chat.id, text="Поставщик успешно создан. Введите наименование товара:")
    return ORDER_ITEM

def choose_item(update: Update, context):
    item_name = update.message.text
    items = sheet.col_values(6)  # Шестой столбец — наименования товаров
    codes = sheet.col_values(5)  # Пятый столбец — коды товаров
    if item_name in items:
        index = items.index(item_name)
        context.user_data['item_id'] = codes[index]
        quantity_keyboard = [[InlineKeyboardButton(str(i), callback_data=i) for i in range(1, 11)], ["Больше"]]
        reply_markup = InlineKeyboardMarkup(quantity_keyboard)
        context.bot.send_message(chat_id=update.effective_chat.id, text="Выберите количество товара:", reply_markup=reply_markup)
        return ORDER_QUANTITY
    else:
        context.bot.send_message(chat_id=update.effective_chat.id, text="Товар не найден. Попробуйте ещё раз.")
        return ORDER_ITEM

def choose_quantity(update: Update, context):
    query = update.callback_query
    quantity = query.data
    if quantity.isdigit():  # Цифровой выбор
        context.user_data['quantity'] = int(quantity)
        context.bot.send_message(chat_id=update.effective_chat.id, text="Введите стоимость единицы товара:")
        return ORDER_COST_PER_UNIT
    elif quantity == "Больше":
        context.bot.send_message(chat_id=update.effective_chat.id, text="Введите количество вручную:")
        return ORDER_QUANTITY_MANUAL

def enter_cost_per_unit(update: Update, context):
    cost_per_unit = update.message.text
    context.user_data['cost_per_unit'] = float(cost_per_unit)
    context.bot.send_message(chat_id=update.effective_chat.id, text="Введите серийные номера (каждый номер на новой строке):")
    return ORDER_SERIAL_NUMBERS

def enter_serial_numbers(update: Update, context):
    serial_numbers = update.message.text.split("\n")
    if len(serial_numbers) != context.user_data['quantity']:
        context.bot.send_message(chat_id=update.effective_chat.id, text="Количество серийных номеров не совпадает с количеством товара. Введите корректные данные.")
        return ORDER_SERIAL_NUMBERS
    else:
        context.user_data['serial_numbers'] = ", ".join(serial_numbers)
        total_cost = context.user_data['cost_per_unit'] * context.user_data['quantity']
        cursor.execute("INSERT INTO orders(provider_id, item_id, quantity, serial_numbers, cost_per_unit, total_cost) VALUES (?, ?, ?, ?, ?, ?)",
                      (context.user_data['provider_id'], context.user_data['item_id'], context.user_data['quantity'],
                       context.user_data['serial_numbers'], context.user_data['cost_per_unit'], total_cost))
        conn.commit()
        context.bot.send_message(chat_id=update.effective_chat.id, text="Заказ успешно создан!")
        return ConversationHandler.END

def cancel(update: Update, context):
    context.bot.send_message(chat_id=update.effective_chat.id, text="Действие отменено.")
    return ConversationHandler.END

updater = Updater(token='7957981552:AAE33m7eOVbce18vdk5BNjxQbwqKyZcMQH4', use_context=True)
dispatcher = updater.dispatcher

conv_handler = ConversationHandler(
    entry_points=[CommandHandler('new_order', start_new_order)],
    states={
        ORDER_PROVIDER: [MessageHandler(Filters.text & ~Filters.command, choose_provider)],
        ORDER_ITEM: [MessageHandler(Filters.text & ~Filters.command, choose_item)],
        ORDER_QUANTITY: [CallbackQueryHandler(choose_quantity)],
        ORDER_COST_PER_UNIT: [MessageHandler(Filters.text & ~Filters.command, enter_cost_per_unit)],
        ORDER_SERIAL_NUMBERS: [MessageHandler(Filters.text & ~Filters.command, enter_serial_numbers)]
    },
    fallbacks=[CommandHandler('cancel', cancel)]
)

dispatcher.add_handler(conv_handler)

updater.start_polling()
updater.idle() 