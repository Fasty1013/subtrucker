import sqlite3
import csv
import io
import re
from fastapi import FastAPI, Response, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from pypdf import PdfReader

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MERCHANT_MAP = {
    "E0172009": {"name": "Yandex Go (Такси)", "category": "Такси (Yandex Go)"},
    "000000010010558": {"name": "MyTaxi (Такси)", "category": "Такси (MyTaxi)"},
    "000000010009584": {"name": "Игровые сервисы / MLBB", "category": "Игры и Развлечения"},
    "000000010001882": {"name": "Uzum Market", "category": "Покупки и Магазины"},
    "000000010012305": {"name": "Оплата по QR / Покупки", "category": "Покупки и Магазины"},
}

def init_db():
    conn = sqlite3.connect("subscriptions.db")
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            price REAL NOT NULL,
            currency TEXT DEFAULT 'сум'
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
    conn.commit()
    conn.close()

init_db()

class Subscription(BaseModel):
    title: str
    price: float
    currency: str = "сум"

class TransactionItem(BaseModel):
    title: str
    price: float
    category: str

def extract_exact_amount(lines_block: list) -> float:
    for line in lines_block:
        matches = re.findall(r"\b(\d{3,9}\.\d{2})\b", line)
        for m in matches:
            val = float(m)
            if 100 <= val <= 50000000:
                return val
    return 0.0

# --- РАЗДЕЛЬНЫЕ ЭНДПОИНТЫ ОЧИСТКИ ---
@app.delete("/api/subscriptions/clear-all")
def clear_subscriptions():
    conn = sqlite3.connect("subscriptions.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM subscriptions")
    conn.commit()
    conn.close()
    return {"message": "Все подписки успешно очищены!"}

@app.delete("/api/transactions/clear-all")
def clear_transactions():
    conn = sqlite3.connect("subscriptions.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM transactions")
    conn.commit()
    conn.close()
    return {"message": "Вся история расходов успешно очищена!"}

# --- ПОДПИСКИ ---
@app.get("/api/subscriptions")
def get_subscriptions():
    conn = sqlite3.connect("subscriptions.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, price, currency FROM subscriptions")
    rows = cursor.fetchall()
    conn.close()
    return [{"id": r[0], "title": r[1], "price": r[2], "currency": r[3] if len(r) > 3 and r[3] else "сум"} for r in rows]

@app.post("/api/subscriptions")
def add_subscription(sub: Subscription):
    conn = sqlite3.connect("subscriptions.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO subscriptions (title, price, currency) VALUES (?, ?, ?)", (sub.title, sub.price, sub.currency))
    conn.commit()
    conn.close()
    return {"message": "Успешно добавлено"}

@app.delete("/api/subscriptions/{sub_id}")
def delete_subscription(sub_id: int):
    conn = sqlite3.connect("subscriptions.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM subscriptions WHERE id = ?", (sub_id,))
    conn.commit()
    conn.close()
    return {"message": "Удалено"}

# --- РАСХОДЫ ---
@app.get("/api/transactions")
def get_transactions():
    conn = sqlite3.connect("subscriptions.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, price, category, date FROM transactions ORDER BY id DESC")
    rows = cursor.fetchall()
    conn.close()
    return [{"id": r[0], "title": r[1], "price": r[2], "category": r[3], "date": r[4]} for r in rows]

@app.post("/api/transactions")
def add_transaction(item: TransactionItem):
    conn = sqlite3.connect("subscriptions.db")
    cursor = conn.cursor()
    cursor.execute("INSERT INTO transactions (title, price, category) VALUES (?, ?, ?)", (item.title, item.price, item.category))
    conn.commit()
    conn.close()
    return {"message": "Расход добавлен"}

@app.delete("/api/transactions/{tx_id}")
def delete_transaction(tx_id: int):
    conn = sqlite3.connect("subscriptions.db")
    cursor = conn.cursor()
    cursor.execute("DELETE FROM transactions WHERE id = ?", (tx_id,))
    conn.commit()
    conn.close()
    return {"message": "Удалено"}

# --- ПАРСЕР UZUM BANK ---
@app.post("/api/subscriptions/upload-statement")
async def upload_statement(file: UploadFile = File(...)):
    contents = await file.read()
    pdf_file = io.BytesIO(contents)
    reader = PdfReader(pdf_file)

    full_text = ""
    for page in reader.pages:
        full_text += page.extract_text() or ""

    lines = full_text.split("\n")
    
    conn = sqlite3.connect("subscriptions.db")
    cursor = conn.cursor()

    added_subs = 0
    added_txs = 0

    i = 0
    while i < len(lines):
        line = lines[i]
        if any(keyword in line for keyword in ["Оплата по карте", "Снятие наличных", "P2P"]):
            block_lines = lines[max(0, i-2):min(len(lines), i+6)]
            block_text = " ".join(block_lines)
            
            amount = extract_exact_amount(block_lines)

            if amount > 0:
                mid_match = re.search(r"MID\s*([A-Z0-9]+)", block_text)
                mid = mid_match.group(1) if mid_match else ""

                if mid in MERCHANT_MAP:
                    title = MERCHANT_MAP[mid]["name"]
                    category = MERCHANT_MAP[mid]["category"]
                elif "E0172009" in block_text:
                    title = "Yandex Go (Такси)"
                    category = "Такси (Yandex Go)"
                elif "10010558" in block_text:
                    title = "MyTaxi (Такси)"
                    category = "Такси (MyTaxi)"
                elif "Снятие наличных" in line or "Комиссия" in line:
                    title = "Снятие в банкомате"
                    category = "Снятие наличных"
                elif "P2P" in line:
                    title = "Перевод на карту (P2P)"
                    category = "Переводы (P2P)"
                else:
                    title = f"Оплата картой (MID {mid})" if mid else "Оплата по карте"
                    category = "Покупки и Магазины"

                cursor.execute(
                    "INSERT INTO transactions (title, price, category) VALUES (?, ?, ?)",
                    (title, amount, category)
                )
                added_txs += 1

                if amount in [1700.0, 2500.0] and ("10009584" in block_text or "E0172009" in block_text):
                    sub_title = f"{title} ({int(amount)} сум)"
                    cursor.execute("SELECT id FROM subscriptions WHERE title = ?", (sub_title,))
                    if not cursor.fetchone():
                        cursor.execute("INSERT INTO subscriptions (title, price, currency) VALUES (?, ?, ?)", (sub_title, amount, "сум"))
                        added_subs += 1
        i += 1

    conn.commit()
    conn.close()

    return {"message": f"Выписка Uzum Bank обработана! Распознано операций: {added_txs}, выявлено подписок: {added_subs}"}

@app.get("/api/subscriptions/export")
def export_csv():
    conn = sqlite3.connect("subscriptions.db")
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, price, currency FROM subscriptions")
    rows = cursor.fetchall()
    conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Название", "Цена", "Валюта"])
    for row in rows:
        writer.writerow(row)

    csv_content = "\ufeff" + output.getvalue()
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=subscriptions_report.csv"}
    )