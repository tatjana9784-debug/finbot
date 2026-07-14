import logging
import os
import sqlite3
import requests
from datetime import datetime, date

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ====== НАСТРОЙКИ ======
BOT_TOKEN = os.environ.get("BOT_TOKEN", "ВСТАВЬ_СЮДА_СВОЙ_ТОКЕН")
DB_PATH = "finance.db"
BASE_CURRENCY = "EUR"

logging.basicConfig(level=logging.INFO)

# ====== ШАГИ ДИАЛОГА ======
(
    CHOOSING_TYPE,
    ENTERING_AMOUNT,
    ENTERING_CURRENCY,
    CHOOSING_CLIENT,
    ENTERING_NEW_CLIENT,
    CHOOSING_CATEGORY,
    ENTERING_NEW_CATEGORY,
    CONFIRMING,
) = range(8)

# Типы операций
TYPE_INCOME = "income"
TYPE_EXPENSE = "expense"

# ====== БАЗА ДАННЫХ ======

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            UNIQUE(user_id, name)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            type TEXT NOT NULL,  -- 'income' или 'expense'
            UNIQUE(user_id, name, type)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,              -- 'income' или 'expense'
            amount_base REAL NOT NULL,       -- сумма в EUR (BASE_CURRENCY)
            original_amount REAL NOT NULL,
            original_currency TEXT NOT NULL,
            client_name TEXT,                -- NULL = общий расход (без клиента)
            category TEXT NOT NULL,
            note TEXT,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS rate_cache (
            currency TEXT PRIMARY KEY,
            rate REAL NOT NULL,
            fetched_date TEXT NOT NULL
        )
    """)

    # Добавляем дефолтные категории
    conn.commit()
    conn.close()

    seed_defaults()


DEFAULT_EXPENSE_CATEGORIES = [
    "Регистрации компаний",
    "Адреса",
    "Телефон",
    "Люди",
    "Документы",
    "Оплата платежек",
]

DEFAULT_INCOME_CATEGORIES = [
    "Аванс",
    "Финальная оплата",
    "Доп верифы",
    "Доп сервис",
]


def seed_user_categories(user_id: int):
    """Добавляет дефолтные категории новому пользователю, если у него ещё нет ни одной."""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM categories WHERE user_id = ?", (user_id,))
    count = cur.fetchone()[0]
    if count == 0:
        for name in DEFAULT_EXPENSE_CATEGORIES:
            cur.execute(
                "INSERT OR IGNORE INTO categories (user_id, name, type) VALUES (?, ?, ?)",
                (user_id, name, "expense"),
            )
        for name in DEFAULT_INCOME_CATEGORIES:
            cur.execute(
                "INSERT OR IGNORE INTO categories (user_id, name, type) VALUES (?, ?, ?)",
                (user_id, name, "income"),
            )
    conn.commit()
    conn.close()


def get_exchange_rate(currency: str) -> float:
    currency = currency.upper()
    if currency == BASE_CURRENCY:
        return 1.0

    today_str = date.today().isoformat()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT rate, fetched_date FROM rate_cache WHERE currency = ?", (currency,))
    row = cur.fetchone()
    if row and row[1] == today_str:
        conn.close()
        return row[0]

    resp = requests.get(
        "https://api.frankfurter.app/latest",
        params={"from": currency, "to": BASE_CURRENCY},
        timeout=10,
    )
    resp.raise_for_status()
    rate = resp.json()["rates"][BASE_CURRENCY]

    cur.execute(
        "INSERT INTO rate_cache (currency, rate, fetched_date) VALUES (?, ?, ?) "
        "ON CONFLICT(currency) DO UPDATE SET rate=excluded.rate, fetched_date=excluded.fetched_date",
        (currency, rate, today_str),
    )
    conn.commit()
    conn.close()
    return rate


def get_clients(user_id: int) -> list:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT name FROM clients WHERE user_id = ? ORDER BY name", (user_id,))
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows


def add_client(user_id: int, name: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO clients (user_id, name) VALUES (?, ?)",
        (user_id, name),
    )
    conn.commit()
    conn.close()


def get_categories(user_id: int, type_: str) -> list:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT name FROM categories WHERE user_id = ? AND type = ? ORDER BY name",
        (user_id, type_),
    )
    rows = [r[0] for r in cur.fetchall()]
    conn.close()
    return rows


def add_category(user_id: int, name: str, type_: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO categories (user_id, name, type) VALUES (?, ?, ?)",
        (user_id, name, type_),
    )
    conn.commit()
    conn.close()


def save_transaction(user_id, type_, amount_base, original_amount, original_currency,
                     client_name, category, note=None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO transactions "
        "(user_id, type, amount_base, original_amount, original_currency, client_name, category, note, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, type_, amount_base, original_amount, original_currency,
         client_name, category, note, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


# ====== КЛАВИАТУРЫ ======

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Доход от клиента", callback_data="new:income")],
        [InlineKeyboardButton("💸 Расход на клиента", callback_data="new:expense_client")],
        [InlineKeyboardButton("🧾 Общий расход (без клиента)", callback_data="new:expense_general")],
    ])


def cancel_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="cancel")]])


def clients_keyboard(clients: list, allow_new=True, allow_skip=False):
    rows = [[InlineKeyboardButton(c, callback_data=f"client:{c}")] for c in clients]
    if allow_new:
        rows.append([InlineKeyboardButton("➕ Новый клиент", callback_data="client:__new__")])
    if allow_skip:
        rows.append([InlineKeyboardButton("— Без клиента (общий)", callback_data="client:__none__")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def categories_keyboard(categories: list):
    rows = [[InlineKeyboardButton(c, callback_data=f"cat:{c}")] for c in categories]
    rows.append([InlineKeyboardButton("➕ Новая категория", callback_data="cat:__new__")])
    rows.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


# ====== /start ======

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    seed_user_categories(update.effective_user.id)
    text = (
        "Привет! Я твой рабочий финансовый помощник.\n\n"
        "Записываю доходы по клиентам, расходы (на клиента или общие) и считаю статистику.\n\n"
        "Все суммы пересчитываются в EUR по актуальному курсу.\n\n"
        "Команды:\n"
        "/new — добавить операцию\n"
        "/clients — разбивка по клиентам\n"
        "/categories — разбивка по категориям\n"
        "/month — итоги за месяц\n"
        "/balance — общий баланс\n"
    )
    await update.message.reply_text(text)


# ====== ДИАЛОГ: ДОБАВЛЕНИЕ ОПЕРАЦИИ ======

async def new_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Точка входа — /new или кнопка."""
    context.user_data.clear()
    await update.message.reply_text(
        "Что записываем?",
        reply_markup=main_menu_keyboard(),
    )
    return CHOOSING_TYPE


async def choose_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data  # new:income / new:expense_client / new:expense_general

    if data == "new:income":
        context.user_data["type"] = TYPE_INCOME
        context.user_data["has_client"] = True
    elif data == "new:expense_client":
        context.user_data["type"] = TYPE_EXPENSE
        context.user_data["has_client"] = True
    elif data == "new:expense_general":
        context.user_data["type"] = TYPE_EXPENSE
        context.user_data["has_client"] = False

    await query.edit_message_text(
        "Введи сумму. Можно с валютой:\n"
        "• `1000` — в EUR\n"
        "• `1000 USD` — в долларах, сконвертирую\n"
        "• `50000 RUB`",
        parse_mode="Markdown",
        reply_markup=cancel_keyboard(),
    )
    return ENTERING_AMOUNT


async def enter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    parts = text.split()

    try:
        amount = float(parts[0].replace(",", "."))
    except ValueError:
        await update.message.reply_text(
            "Не понял сумму. Введи число, например: `1000` или `1000 USD`",
            parse_mode="Markdown",
            reply_markup=cancel_keyboard(),
        )
        return ENTERING_AMOUNT

    currency = parts[1].upper() if len(parts) > 1 else BASE_CURRENCY

    try:
        rate = get_exchange_rate(currency)
    except Exception:
        await update.message.reply_text(
            f"Не получилось узнать курс {currency}. Проверь код валюты (3 буквы, например USD, RUB, GBP) и попробуй ещё раз.",
            reply_markup=cancel_keyboard(),
        )
        return ENTERING_AMOUNT

    context.user_data["original_amount"] = amount
    context.user_data["original_currency"] = currency
    context.user_data["amount_base"] = amount * rate
    context.user_data["rate"] = rate

    # Следующий шаг: клиент или сразу категория
    if context.user_data["has_client"]:
        user_id = update.effective_user.id
        clients = get_clients(user_id)
        if clients:
            await update.message.reply_text(
                "Выбери клиента или добавь нового:",
                reply_markup=clients_keyboard(clients),
            )
        else:
            await update.message.reply_text(
                "Клиентов пока нет. Введи имя первого клиента:",
                reply_markup=cancel_keyboard(),
            )
            context.user_data["awaiting_new_client"] = True
            return ENTERING_NEW_CLIENT
        return CHOOSING_CLIENT
    else:
        # Общий расход — сразу к категории
        return await _ask_category(update, context, is_message=True)


async def choose_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data  # client:ИмяКлиента / client:__new__ / client:__none__

    if data == "client:__new__":
        await query.edit_message_text(
            "Введи имя нового клиента:",
            reply_markup=cancel_keyboard(),
        )
        return ENTERING_NEW_CLIENT

    if data == "client:__none__":
        context.user_data["client"] = None
    else:
        context.user_data["client"] = data.replace("client:", "", 1)

    return await _ask_category(query, context, is_message=False)


async def enter_new_client(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    user_id = update.effective_user.id
    add_client(user_id, name)
    context.user_data["client"] = name
    return await _ask_category(update, context, is_message=True)


async def _ask_category(update_or_query, context, is_message: bool):
    user_id = update_or_query.effective_user.id if is_message else update_or_query.from_user.id
    type_ = context.user_data["type"]
    categories = get_categories(user_id, type_)

    text = "Выбери категорию:"
    kb = categories_keyboard(categories)

    if is_message:
        await update_or_query.message.reply_text(text, reply_markup=kb)
    else:
        await update_or_query.edit_message_text(text, reply_markup=kb)
    return CHOOSING_CATEGORY


async def choose_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data  # cat:Название / cat:__new__

    if data == "cat:__new__":
        await query.edit_message_text(
            "Введи название новой категории:",
            reply_markup=cancel_keyboard(),
        )
        return ENTERING_NEW_CATEGORY

    context.user_data["category"] = data.replace("cat:", "", 1)
    return await _confirm(query, context)


async def enter_new_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    user_id = update.effective_user.id
    type_ = context.user_data["type"]
    add_category(user_id, name, type_)
    context.user_data["category"] = name

    # Нужно отправить подтверждение через message
    return await _confirm_via_message(update, context)


async def _confirm(query, context):
    d = context.user_data
    type_label = "Доход" if d["type"] == TYPE_INCOME else "Расход"
    client_label = d.get("client") or "— (общий)"
    orig = d["original_amount"]
    cur = d["original_currency"]
    base = d["amount_base"]
    rate = d.get("rate", 1.0)

    if cur == BASE_CURRENCY:
        amount_line = f"{base:.2f} {BASE_CURRENCY}"
    else:
        amount_line = f"{orig:.2f} {cur} ≈ {base:.2f} {BASE_CURRENCY} (курс {rate:.4f})"

    text = (
        f"Проверь запись:\n\n"
        f"Тип: {type_label}\n"
        f"Сумма: {amount_line}\n"
        f"Клиент: {client_label}\n"
        f"Категория: {d['category']}\n\n"
        f"Сохранить?"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, сохранить", callback_data="confirm:yes")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ])
    await query.edit_message_text(text, reply_markup=kb)
    return CONFIRMING


async def _confirm_via_message(update, context):
    d = context.user_data
    type_label = "Доход" if d["type"] == TYPE_INCOME else "Расход"
    client_label = d.get("client") or "— (общий)"
    orig = d["original_amount"]
    cur = d["original_currency"]
    base = d["amount_base"]
    rate = d.get("rate", 1.0)

    if cur == BASE_CURRENCY:
        amount_line = f"{base:.2f} {BASE_CURRENCY}"
    else:
        amount_line = f"{orig:.2f} {cur} ≈ {base:.2f} {BASE_CURRENCY} (курс {rate:.4f})"

    text = (
        f"Проверь запись:\n\n"
        f"Тип: {type_label}\n"
        f"Сумма: {amount_line}\n"
        f"Клиент: {client_label}\n"
        f"Категория: {d['category']}\n\n"
        f"Сохранить?"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Да, сохранить", callback_data="confirm:yes")],
        [InlineKeyboardButton("❌ Отмена", callback_data="cancel")],
    ])
    await update.message.reply_text(text, reply_markup=kb)
    return CONFIRMING


async def confirm_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "confirm:yes":
        d = context.user_data
        user_id = update.effective_user.id
        save_transaction(
            user_id=user_id,
            type_=d["type"],
            amount_base=d["amount_base"],
            original_amount=d["original_amount"],
            original_currency=d["original_currency"],
            client_name=d.get("client"),
            category=d["category"],
        )
        type_label = "Доход" if d["type"] == TYPE_INCOME else "Расход"
        await query.edit_message_text(f"✅ {type_label} сохранён!\n\nДобавить ещё? /new")
    else:
        await query.edit_message_text("Операция отменена.")

    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("Операция отменена.")
    else:
        await update.message.reply_text("Операция отменена.")
    context.user_data.clear()
    return ConversationHandler.END


# ====== СТАТИСТИКА ======

def fetch_stats(user_id: int, start_date: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT type, amount_base, original_amount, original_currency, client_name, category, created_at "
        "FROM transactions WHERE user_id = ? AND created_at >= ? ORDER BY created_at",
        (user_id, start_date),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


async def cmd_clients(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    start_of_month = date.today().replace(day=1).isoformat()
    rows = fetch_stats(user_id, start_of_month)

    if not rows:
        await update.message.reply_text("В этом месяце записей ещё нет.")
        return

    # type, amount_base, orig_amount, orig_currency, client_name, category, created_at
    client_income = {}
    client_expense = {}

    for type_, amount_base, *_, client_name, category, _ in rows:
        key = client_name or "— Общие расходы"
        if type_ == TYPE_INCOME:
            client_income[key] = client_income.get(key, 0) + amount_base
        else:
            client_expense[key] = client_expense.get(key, 0) + amount_base

    all_clients = sorted(set(list(client_income.keys()) + list(client_expense.keys())))

    lines = [f"📊 Разбивка по клиентам ({date.today().strftime('%B %Y')}):\n"]
    total_income = 0
    total_expense = 0

    for client in all_clients:
        inc = client_income.get(client, 0)
        exp = client_expense.get(client, 0)
        profit = inc - exp
        total_income += inc
        total_expense += exp

        lines.append(f"👤 {client}")
        if inc:
            lines.append(f"   Доход:   +{inc:.2f} {BASE_CURRENCY}")
        if exp:
            lines.append(f"   Расход:  -{exp:.2f} {BASE_CURRENCY}")
        if inc:
            lines.append(f"   Прибыль: {profit:+.2f} {BASE_CURRENCY}")
        lines.append("")

    lines.append(f"ИТОГО:")
    lines.append(f"  Доходы:  +{total_income:.2f} {BASE_CURRENCY}")
    lines.append(f"  Расходы: -{total_expense:.2f} {BASE_CURRENCY}")
    lines.append(f"  Прибыль: {total_income - total_expense:+.2f} {BASE_CURRENCY}")

    await update.message.reply_text("\n".join(lines))


async def cmd_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    start_of_month = date.today().replace(day=1).isoformat()
    rows = fetch_stats(user_id, start_of_month)

    if not rows:
        await update.message.reply_text("В этом месяце записей ещё нет.")
        return

    income_by_cat = {}
    expense_by_cat = {}

    for type_, amount_base, *_, client_name, category, _ in rows:
        if type_ == TYPE_INCOME:
            income_by_cat[category] = income_by_cat.get(category, 0) + amount_base
        else:
            expense_by_cat[category] = expense_by_cat.get(category, 0) + amount_base

    lines = [f"📊 Разбивка по категориям ({date.today().strftime('%B %Y')}):\n"]

    if income_by_cat:
        lines.append("💰 Доходы:")
        for cat, amt in sorted(income_by_cat.items(), key=lambda x: -x[1]):
            lines.append(f"   {cat}: +{amt:.2f} {BASE_CURRENCY}")
        lines.append(f"   Итого: +{sum(income_by_cat.values()):.2f} {BASE_CURRENCY}")
        lines.append("")

    if expense_by_cat:
        lines.append("💸 Расходы:")
        for cat, amt in sorted(expense_by_cat.items(), key=lambda x: -x[1]):
            lines.append(f"   {cat}: -{amt:.2f} {BASE_CURRENCY}")
        lines.append(f"   Итого: -{sum(expense_by_cat.values()):.2f} {BASE_CURRENCY}")

    await update.message.reply_text("\n".join(lines))


async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    start_of_month = date.today().replace(day=1).isoformat()
    rows = fetch_stats(user_id, start_of_month)

    if not rows:
        await update.message.reply_text("В этом месяце записей ещё нет.")
        return

    total_income = sum(a for t, a, *_ in rows if t == TYPE_INCOME)
    total_expense = sum(a for t, a, *_ in rows if t == TYPE_EXPENSE)
    count = len(rows)

    lines = [
        f"📅 Итоги за {date.today().strftime('%B %Y')}:\n",
        f"Операций: {count}",
        f"Доходы:   +{total_income:.2f} {BASE_CURRENCY}",
        f"Расходы:  -{total_expense:.2f} {BASE_CURRENCY}",
        f"Прибыль:  {total_income - total_expense:+.2f} {BASE_CURRENCY}",
        "",
        "Подробнее:",
        "/clients — по клиентам",
        "/categories — по категориям",
    ]
    await update.message.reply_text("\n".join(lines))


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT type, SUM(amount_base) FROM transactions WHERE user_id = ? GROUP BY type",
        (user_id,),
    )
    rows = dict(cur.fetchall())
    conn.close()

    income = rows.get(TYPE_INCOME, 0)
    expense = rows.get(TYPE_EXPENSE, 0)

    await update.message.reply_text(
        f"💼 Общий баланс (за всё время):\n\n"
        f"Доходы:  +{income:.2f} {BASE_CURRENCY}\n"
        f"Расходы: -{expense:.2f} {BASE_CURRENCY}\n"
        f"Прибыль: {income - expense:+.2f} {BASE_CURRENCY}"
    )


# ====== ЗАПУСК ======

def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("new", new_cmd)],
        states={
            CHOOSING_TYPE: [CallbackQueryHandler(choose_type, pattern="^new:")],
            ENTERING_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_amount),
                CallbackQueryHandler(cancel, pattern="^cancel$"),
            ],
            CHOOSING_CLIENT: [
                CallbackQueryHandler(choose_client, pattern="^client:"),
                CallbackQueryHandler(cancel, pattern="^cancel$"),
            ],
            ENTERING_NEW_CLIENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_new_client),
                CallbackQueryHandler(cancel, pattern="^cancel$"),
            ],
            CHOOSING_CATEGORY: [
                CallbackQueryHandler(choose_category, pattern="^cat:"),
                CallbackQueryHandler(cancel, pattern="^cancel$"),
            ],
            ENTERING_NEW_CATEGORY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_new_category),
                CallbackQueryHandler(cancel, pattern="^cancel$"),
            ],
            CONFIRMING: [
                CallbackQueryHandler(confirm_save, pattern="^confirm:"),
                CallbackQueryHandler(cancel, pattern="^cancel$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern="^cancel$"),
        ],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(conv)
    app.add_handler(CommandHandler("clients", cmd_clients))
    app.add_handler(CommandHandler("categories", cmd_categories))
    app.add_handler(CommandHandler("month", cmd_month))
    app.add_handler(CommandHandler("balance", cmd_balance))

    print("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
