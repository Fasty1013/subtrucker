import sqlite3
import csv
import io
import re
import os
import datetime
from typing import Optional, List
from fastapi import FastAPI, Response, UploadFile, File, Query
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pypdf import PdfReader

app = FastAPI(title="SubTracker API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = "subscriptions.db"

# Карта мерчантов и категорий
MERCHANT_MAP = {
    "E0172009": {"name": "Yandex Go (Такси)", "category": "Такси (Yandex Go)"},
    "000000010010558": {"name": "MyTaxi (Такси)", "category": "Такси (MyTaxi)"},
    "000000010009584": {"name": "Игровые сервисы / MLBB", "category": "Игры и Развлечения"},
    "000000010009581": {"name": "Google Play / Игры", "category": "Игры и Развлечения"},
    "000000010001882": {"name": "Uzum Market", "category": "Покупки и Магазины"},
    "000000010012305": {"name": "Оплата по QR / Покупки", "category": "Покупки и Магазины"},
}

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            price REAL NOT NULL,
            currency TEXT DEFAULT 'сум',
            category TEXT DEFAULT 'Подписки',
            billing_cycle TEXT DEFAULT 'месяц'
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            price REAL NOT NULL,
            category TEXT NOT NULL,
            date TEXT DEFAULT ''
        )
    """)
    
    # Миграция колонок, если старая БД
    cursor.execute("PRAGMA table_info(subscriptions)")
    sub_cols = [c[1] for c in cursor.fetchall()]
    if "category" not in sub_cols:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN category TEXT DEFAULT 'Подписки'")
    if "billing_cycle" not in sub_cols:
        cursor.execute("ALTER TABLE subscriptions ADD COLUMN billing_cycle TEXT DEFAULT 'месяц'")

    cursor.execute("PRAGMA table_info(transactions)")
    tx_cols = [c[1] for c in cursor.fetchall()]
    if "date" not in tx_cols:
        cursor.execute("ALTER TABLE transactions ADD COLUMN date TEXT DEFAULT ''")

    conn.commit()
    conn.close()

init_db()

# --- МОДЕЛИ ---
class Subscription(BaseModel):
    title: str
    price: float
    currency: str = "сум"
    category: str = "Подписки"
    billing_cycle: str = "месяц"

class TransactionItem(BaseModel):
    title: str
    price: float
    category: str
    date: Optional[str] = ""

class UpdateDate(BaseModel):
    date: str

class ApplyCandidate(BaseModel):
    title: str
    price: float
    currency: str = "сум"
    category: str = "Подписки"
    billing_cycle: str = "месяц"

# --- ХЕЛПЕРЫ ДЛЯ ПАРСИНГА ---
def extract_exact_amount(lines_block: list) -> float:
    for line in lines_block:
        # Ищем суммы типа 35273.00, 1700.00 или 35273
        matches = re.findall(r"\b(\d{3,9}(?:\.\d{2})?)\b", line)
        for m in matches:
            val = float(m)
            # Отсекаем номера карт и аномалии
            if 100 <= val <= 50000000:
                return val
    return 0.0

def extract_date(lines_block: list) -> str:
    for line in lines_block:
        # DD.MM.YYYY HH:MM:SS или DD.MM.YYYY HH:MM или DD.MM.YYYY
        m = re.search(r"\b(\d{2}\.\d{2}\.\d{4}(?:\s+\d{2}:\d{2}(?::\d{2})?)?)\b", line)
        if m:
            return m.group(1)
        # YYYY-MM-DD
        m2 = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", line)
        if m2:
            return m2.group(1)
    return ""

def categorize_transaction(block_text: str, line: str, mid: str) -> tuple:
    """Определяет понятное название и категорию транзакции"""
    text_lower = (block_text + " " + line).lower()

    if mid in MERCHANT_MAP:
        return MERCHANT_MAP[mid]["name"], MERCHANT_MAP[mid]["category"]

    if "e0172009" in block_text or "yandex go" in text_lower or "yandex.go" in text_lower or "яндекс go" in text_lower or "яндекс такси" in text_lower:
        return "Yandex Go (Такси)", "Такси (Yandex Go)"
    if "10010558" in block_text or "mytaxi" in text_lower:
        return "MyTaxi (Такси)", "Такси (MyTaxi)"
    if "10009584" in block_text or "mlbb" in text_lower or "mobile legends" in text_lower:
        return "Игровые сервисы / MLBB", "Игры и Развлечения"
    if "10009581" in block_text or "google play" in text_lower or "google *services" in text_lower:
        return "Google Play / Сервисы", "Игры и Развлечения"
    if "10001882" in block_text or "uzum market" in text_lower:
        return "Uzum Market", "Покупки и Магазины"
    if "10012305" in block_text or "оплата по qr" in text_lower:
        return "Оплата по QR / Покупки", "Покупки и Магазины"

    # Анализ по ключевым словам
    if any(k in text_lower for k in ["снятие наличных", "снятие в банкомате", "банкомат", "комиссия"]):
        return "Снятие в банкомате", "Снятие наличных"
    if any(k in text_lower for k in ["p2p", "перевод на карту", "перевод с карты", "перевод средств"]):
        return "Перевод на карту (P2P)", "Переводы (P2P)"
    if any(k in text_lower for k in ["yandex plus", "яндекс плюс"]):
        return "Yandex Plus (Подписка)", "Медиа и Подписки"
    if any(k in text_lower for k in ["telegram premium", "telegram"]):
        return "Telegram Premium", "Медиа и Подписки"
    if any(k in text_lower for k in ["spotify"]):
        return "Spotify", "Медиа и Подписки"
    if any(k in text_lower for k in ["netflix"]):
        return "Netflix", "Медиа и Подписки"
    if any(k in text_lower for k in ["youtube"]):
        return "YouTube Premium", "Медиа и Подписки"
    if any(k in text_lower for k in ["steam", "playstation", "xbox", "epic games", "riot"]):
        return "Игры и Развлечения", "Игры и Развлечения"
    if any(k in text_lower for k in ["korzinka", "корзинка", "makro", "макро", "havas", "хавас", "carrefour"]):
        return "Супермаркет / Продукты", "Продукты и Супермаркеты"
    if any(k in text_lower for k in ["yandex eats", "яндекс еда", "express24", "kfc", "evos", "oqtepa"]):
        return "Доставка / Кафе", "Кафе и Рестораны"
    if any(k in text_lower for k in ["beeline", "ucell", "mobiuz", "uztelecom"]):
        return "Мобильная связь", "Связь и Интернет"

    if mid:
        return f"Оплата картой (MID {mid})", "Покупки и Магазины"
    return "Оплата по карте", "Покупки и Магазины"

# --- ГЛАВНАЯ СТРАНИЦА ---
@app.get("/", response_class=HTMLResponse)
def get_index():
    if os.path.exists("index.html"):
        with open("index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse("<h1>index.html не найден</h1>")

# --- ПОДПИСКИ ---
@app.get("/api/subscriptions")
def get_subscriptions():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, price, currency, category, billing_cycle FROM subscriptions ORDER BY id DESC")
    rows = cursor.fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "price": r["price"],
            "currency": r["currency"] or "сум",
            "category": r["category"] or "Подписки",
            "billing_cycle": r["billing_cycle"] or "месяц",
        }
        for r in rows
    ]

@app.post("/api/subscriptions")
def add_subscription(sub: Subscription):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO subscriptions (title, price, currency, category, billing_cycle) VALUES (?, ?, ?, ?, ?)",
        (sub.title, sub.price, sub.currency, sub.category, sub.billing_cycle)
    )
    conn.commit()
    sub_id = cursor.lastrowid
    conn.close()
    return {"message": "Подписка успешно добавлена", "id": sub_id}

@app.delete("/api/subscriptions/clear-all")
def clear_subscriptions():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM subscriptions")
    conn.commit()
    conn.close()
    return {"message": "Все подписки успешно очищены!"}

@app.delete("/api/subscriptions/clean-taxi")
def clean_taxi_subscriptions():
    """Удаляет случайно добавленные поездки на такси из подписок"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM subscriptions WHERE category LIKE '%Такси%' OR title LIKE '%Такси%' OR title LIKE '%Yandex Go%' OR title LIKE '%MyTaxi%'")
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return {"message": f"Удалено {deleted} поездок такси из подписок"}

@app.delete("/api/subscriptions/{sub_id}")
def delete_subscription(sub_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
    conn.commit()
    conn.close()
    return {"message": "Подписка удалена"}

# --- ДЕТЕКТОР ПОДПИСОК (ПОИСК В ИСТОРИИ) ---
@app.get("/api/subscriptions/detect")
def detect_subscriptions():
    """
    Анализирует таблицу транзакций и находит регулярные списания / кандидаты в подписки:
    1. Повторяющиеся суммы по одинаковым сервисам
    2. Известные сервисы подписок (Yandex, MLBB, Spotify, etc.)
    """
    conn = get_db()
    cursor = conn.cursor()

    # Уже существующие подписки
    cursor.execute("SELECT title, price FROM subscriptions")
    existing_subs = {(r["title"].lower().strip(), r["price"]) for r in cursor.fetchall()}

    # Группируем повторяющиеся операции
    cursor.execute("""
        SELECT title, category, price, COUNT(*) as count, SUM(price) as total_spent
        FROM transactions
        GROUP BY title, price
        HAVING count >= 2
        ORDER BY count DESC, total_spent DESC
    """)
    rows = cursor.fetchall()
    conn.close()

    candidates = []
    # Категории, которые обычно НЕ являются подписками (если не специфичный случай)
    excluded_categories = ["Снятие наличных", "Переводы (P2P)"]

    for r in rows:
        title = r["title"]
        category = r["category"]
        price = r["price"]
        count = r["count"]
        total_spent = r["total_spent"]

        # Игнорируем обычные переводы, банкоматы и ЛЮБОЕ ТАКСИ (Yandex Go, MyTaxi и т.д.)
        if (
            category in excluded_categories
            or "Такси" in category
            or "такси" in title.lower()
            or "yandex go" in title.lower()
            or "mytaxi" in title.lower()
        ):
            continue

        is_already_added = any(
            (title.lower() in existing_title or existing_title in title.lower()) and abs(p - price) < 1.0
            for existing_title, p in existing_subs
        )

        reason = f"Повторяющийся платеж ({count} раз)"
        if "MLBB" in title or "10009584" in title:
            reason = f"Игровой пропуск / регулярный донат ({count} платежей)"
        elif "Yandex" in title or "Google" in title:
            reason = f"Регулярная оплата цифрового сервиса ({count} раз)"

        candidates.append({
            "title": title,
            "category": category,
            "price": price,
            "currency": "сум",
            "count": count,
            "total_spent": total_spent,
            "reason": reason,
            "already_added": is_already_added
        })

    return {"candidates": candidates, "total_found": len(candidates)}

@app.post("/api/subscriptions/detect/apply")
def apply_detected_subscriptions(items: List[ApplyCandidate]):
    """Добавляет выбранные найденные подписки в трекер"""
    conn = get_db()
    cursor = conn.cursor()
    added_count = 0
    for item in items:
        # Проверяем на дубликат
        cursor.execute("SELECT id FROM subscriptions WHERE title = ? AND price = ?", (item.title, item.price))
        if not cursor.fetchone():
            cursor.execute(
                "INSERT INTO subscriptions (title, price, currency, category, billing_cycle) VALUES (?, ?, ?, ?, ?)",
                (item.title, item.price, item.currency, item.category, item.billing_cycle)
            )
            added_count += 1
    conn.commit()
    conn.close()
    return {"message": f"Добавлено подписок: {added_count}", "added_count": added_count}

# --- РАСХОДЫ И ТРАНЗАКЦИИ ---
@app.get("/api/transactions")
def get_transactions(
    search: Optional[str] = None,
    category: Optional[str] = None,
    sort_by: Optional[str] = "date_desc",
    limit: Optional[int] = 1000
):
    conn = get_db()
    cursor = conn.cursor()

    query = "SELECT id, title, price, category, date FROM transactions WHERE 1=1"
    params = []

    if search:
        query += " AND (title LIKE ? OR category LIKE ? OR date LIKE ?)"
        wildcard = f"%{search}%"
        params.extend([wildcard, wildcard, wildcard])

    if category and category != "all":
        query += " AND category = ?"
        params.append(category)

    if sort_by == "price_desc":
        query += " ORDER BY price DESC"
    elif sort_by == "price_asc":
        query += " ORDER BY price ASC"
    elif sort_by == "date_asc":
        query += " ORDER BY id ASC"
    else:
        # date_desc
        query += " ORDER BY id DESC"

    if limit:
        query += f" LIMIT {int(limit)}"

    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    return [
        {
            "id": r["id"],
            "title": r["title"],
            "price": r["price"],
            "category": r["category"],
            "date": r["date"] or "",
        }
        for r in rows
    ]

@app.post("/api/transactions")
def add_transaction(item: TransactionItem):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO transactions (title, price, category, date) VALUES (?, ?, ?, ?)",
        (item.title, item.price, item.category, item.date or "")
    )
    conn.commit()
    tx_id = cursor.lastrowid
    conn.close()
    return {"message": "Расход успешно добавлен", "id": tx_id}

@app.delete("/api/transactions/clear-all")
def clear_transactions():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM transactions")
    conn.commit()
    conn.close()
    return {"message": "Вся история расходов успешно очищена!"}

@app.delete("/api/transactions/{tx_id}")
def delete_transaction(tx_id: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
    conn.commit()
    conn.close()
    return {"message": "Расход удален"}

@app.post("/api/transactions/{tx_id}/make-subscription")
def make_transaction_subscription(tx_id: int):
    """Превращает конкретную транзакцию в ежемесячную подписку"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT title, price, category FROM transactions WHERE id = ?", (tx_id,))
    tx = cursor.fetchone()
    if not tx:
        conn.close()
        return Response(status_code=404, content="Транзакция не найдена")

    title = tx["title"]
    price = tx["price"]
    category = tx["category"]

    # Проверяем нет ли уже такой подписки
    cursor.execute("SELECT id FROM subscriptions WHERE title = ? AND price = ?", (title, price))
    if cursor.fetchone():
        conn.close()
        return {"message": f"Подписка «{title}» уже есть в вашем списке!"}

    cursor.execute(
        "INSERT INTO subscriptions (title, price, currency, category, billing_cycle) VALUES (?, ?, ?, ?, ?)",
        (title, price, "сум", category, "месяц")
    )
    conn.commit()
    conn.close()
    return {"message": f"Расход «{title}» ({int(price)} сум) добавлен в подписки!"}

@app.patch("/api/transactions/{tx_id}/date")
def update_transaction_date(tx_id: int, item: UpdateDate):
    """Обновляет дату транзакции"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE transactions SET date = ? WHERE id = ?", (item.date, tx_id))
    conn.commit()
    conn.close()
    return {"message": "Дата успешно обновлена", "date": item.date}

# --- СВОДНАЯ АНАЛИТИКА (СКОЛЬКО И НА ЧТО БЫЛО ПОТРАЧЕНО) ---
@app.get("/api/analytics/summary")
def get_analytics_summary():
    conn = get_db()
    cursor = conn.cursor()

    # Транзакции
    cursor.execute("SELECT COUNT(*) as cnt, SUM(price) as total, AVG(price) as avg_price, MAX(price) as max_price FROM transactions")
    tx_stat = cursor.fetchone()
    total_tx_count = tx_stat["cnt"] or 0
    total_spent = tx_stat["total"] or 0.0
    avg_price = tx_stat["avg_price"] or 0.0
    max_price = tx_stat["max_price"] or 0.0

    # Самая крупная транзакция
    max_tx_title = ""
    if max_price > 0:
        cursor.execute("SELECT title FROM transactions WHERE price = ? LIMIT 1", (max_price,))
        max_row = cursor.fetchone()
        if max_row:
            max_tx_title = max_row["title"]

    # Топ категория
    cursor.execute("SELECT category, SUM(price) as total FROM transactions GROUP BY category ORDER BY total DESC LIMIT 1")
    top_cat_row = cursor.fetchone()
    top_category = top_cat_row["category"] if top_cat_row else "—"
    top_category_sum = top_cat_row["total"] if top_cat_row else 0.0

    # Подписки
    cursor.execute("SELECT COUNT(*) as cnt, SUM(price) as total FROM subscriptions")
    sub_stat = cursor.fetchone()
    sub_count = sub_stat["cnt"] or 0
    sub_monthly = sub_stat["total"] or 0.0

    conn.close()

    return {
        "total_spent": total_spent,
        "total_count": total_tx_count,
        "avg_price": avg_price,
        "max_price": max_price,
        "max_tx_title": max_tx_title,
        "top_category": top_category,
        "top_category_sum": top_category_sum,
        "top_category_pct": round((top_category_sum / total_spent * 100), 1) if total_spent > 0 else 0,
        "sub_count": sub_count,
        "sub_monthly": sub_monthly,
        "sub_yearly": sub_monthly * 12,
    }

@app.get("/api/analytics/categories")
def get_category_analytics():
    """Возвращает распределение трат по всем категориям с суммами, процентами и количеством"""
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT SUM(price) as grand_total FROM transactions")
    grand_total_row = cursor.fetchone()
    grand_total = grand_total_row["grand_total"] or 0.0

    cursor.execute("""
        SELECT category, COUNT(*) as count, SUM(price) as total, MIN(price) as min_price, MAX(price) as max_price
        FROM transactions
        GROUP BY category
        ORDER BY total DESC
    """)
    rows = cursor.fetchall()
    conn.close()

    # Палитра цветов для категорий
    palette = [
        "#6366F1", "#0EA5E9", "#10B981", "#F59E0B", "#EC4899", 
        "#8B5CF6", "#14B8A6", "#F97316", "#EF4444", "#84CC16"
    ]

    icons = {
        "Такси (Yandex Go)": "🚕",
        "Такси (MyTaxi)": "🚕",
        "Игры и Развлечения": "🎮",
        "Снятие наличных": "🏧",
        "Переводы (P2P)": "💸",
        "Покупки и Магазины": "🛍️",
        "Медиа и Подписки": "📺",
        "Продукты и Супермаркеты": "🛒",
        "Кафе и Рестораны": "🍔",
        "Связь и Интернет": "📱",
    }

    result = []
    for i, r in enumerate(rows):
        cat_name = r["category"]
        total = r["total"] or 0.0
        pct = round((total / grand_total * 100), 1) if grand_total > 0 else 0
        result.append({
            "category": cat_name,
            "total": total,
            "count": r["count"],
            "percentage": pct,
            "min_price": r["min_price"],
            "max_price": r["max_price"],
            "color": palette[i % len(palette)],
            "icon": icons.get(cat_name, "💳")
        })

    return {"categories": result, "grand_total": grand_total}

# --- УЛУЧШЕННЫЙ ПАРСЕР ВЫПИСКИ (PDF) ---
@app.post("/api/subscriptions/upload-statement")
async def upload_statement(file: UploadFile = File(...)):
    try:
        contents = await file.read()
        pdf_file = io.BytesIO(contents)
        reader = PdfReader(pdf_file)

        full_text = ""
        for page in reader.pages:
            full_text += (page.extract_text() or "") + "\n"

        lines = [line.strip() for line in full_text.split("\n") if line.strip()]

        conn = get_db()
        cursor = conn.cursor()

        added_txs = 0
        added_subs = 0

        # Ключевые слова начала транзакции в выписке Uzum Bank и других
        trigger_keywords = [
            "Оплата по карте", "Снятие наличных", "P2P", "Комиссия",
            "Покупка", "Платеж", "Списание", "Оплата услуг"
        ]

        i = 0
        while i < len(lines):
            line = lines[i]
            if any(keyword in line for keyword in trigger_keywords):
                block_lines = lines[max(0, i - 3):min(len(lines), i + 7)]
                block_text = " ".join(block_lines)

                amount = extract_exact_amount(block_lines)
                date_str = extract_date(block_lines)
                if not date_str:
                    date_str = datetime.datetime.now().strftime("%d.%m.%Y %H:%M")

                if amount > 0:
                    mid_match = re.search(r"MID\s*([A-Z0-9]+)", block_text)
                    mid = mid_match.group(1) if mid_match else ""

                    title, category = categorize_transaction(block_text, line, mid)

                    cursor.execute(
                        "INSERT INTO transactions (title, price, category, date) VALUES (?, ?, ?, ?)",
                        (title, amount, category, date_str)
                    )
                    added_txs += 1

                    # Автоматическое определение подписок:
                    # ВАЖНО: Такси (Yandex Go, MyTaxi), P2P и банкоматы НИКОГДА не считаются подписками!
                    is_sub = False
                    is_taxi = "такси" in category.lower() or "такси" in title.lower() or "yandex go" in title.lower() or "mytaxi" in title.lower() or mid in ["E0172009", "000000010010558"]
                    is_excluded = is_taxi or category in ["Снятие наличных", "Переводы (P2P)"]

                    if not is_excluded:
                        # 1. Фиксированные суммы донатов / игровых сервисов
                        if amount in [1700.0, 2500.0] and ("10009584" in block_text or "10009581" in block_text or "MLBB" in title):
                            is_sub = True
                        # 2. Яндекс Плюс (только если прямо указано Plus/Плюс в выписке, а НЕ такси)
                        elif any(s in block_text.lower() for s in ["yandex plus", "яндекс плюс", "plus.yandex"]):
                            is_sub = True
                        # 3. Другие цифровые подписки
                        elif any(s in block_text.lower() for s in ["telegram premium", "spotify", "netflix", "youtube premium"]):
                            is_sub = True

                    if is_sub:
                        sub_title = f"{title} ({int(amount)} сум)"
                        cursor.execute("SELECT id FROM subscriptions WHERE title = ?", (sub_title,))
                        if not cursor.fetchone():
                            cursor.execute(
                                "INSERT INTO subscriptions (title, price, currency, category, billing_cycle) VALUES (?, ?, ?, ?, ?)",
                                (sub_title, amount, "сум", category, "месяц")
                            )
                            added_subs += 1

            i += 1

        conn.commit()
        conn.close()

        return {
            "success": True,
            "message": f"Выписка успешно обработана! Распознано операций: {added_txs}, выявлено подписок: {added_subs}",
            "transactions_count": added_txs,
            "subscriptions_detected": added_subs
        }
    except Exception as e:
        return Response(status_code=400, content=f"Ошибка обработки PDF: {str(e)}")

# --- ЭКСПОРТ CSV ---
@app.get("/api/subscriptions/export")
def export_csv():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, price, currency, category, billing_cycle FROM subscriptions")
    rows = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Название", "Цена", "Валюта", "Категория", "Период"])
    for r in rows:
        writer.writerow([r["id"], r["title"], r["price"], r["currency"], r["category"], r["billing_cycle"]])

    csv_content = "\ufeff" + output.getvalue()
    return Response(
        content=csv_content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=subscriptions_report.csv"}
    )

@app.get("/api/transactions/export")
def export_transactions_csv():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, date, title, price, category FROM transactions ORDER BY id DESC")
    rows = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Дата", "Операция", "Сумма (сум)", "Категория"])
    for r in rows:
        writer.writerow([r["id"], r["date"], r["title"], r["price"], r["category"]])

    csv_content = "\ufeff" + output.getvalue()
    return Response(
        content=csv_content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=transactions_report.csv"}
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
