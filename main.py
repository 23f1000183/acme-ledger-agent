import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "export.json"

RATES = {"USD": 1.0, "EUR": 1.05, "INR": 0.01214}
TZ = ZoneInfo("Asia/Kolkata")

app = FastAPI(title="Acme Appliances Order Ledger Agent")


def load_orders():
    latest = {}

    with DATA_FILE.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue

            row = json.loads(line)
            updated = datetime.fromisoformat(
                row["updated_at"].replace("Z", "+00:00")
            )
            order_id = row["id"]

            if order_id not in latest or updated > latest[order_id][0]:
                latest[order_id] = (updated, row)

    return [item[1] for item in latest.values()]


ORDERS = load_orders()


def business_date(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt.astimezone(TZ).date()


def amount_usd(order):
    return float(order["amount"]) * RATES[order["currency"]]


def answer_question(question):
    q = question.lower()
    orders = ORDERS

    # Identify an explicitly mentioned region.
    region = next(
        (r for r in ["North", "South", "East", "West", "Central"]
         if re.search(rf"\b{r.lower()}\b", q)),
        None,
    )
    if region:
        orders = [o for o in orders if o["region"].lower() == region.lower()]

    # Identify an explicitly mentioned product.
    products = sorted({o["product"] for o in ORDERS}, key=len, reverse=True)
    product = next((p for p in products if p.lower() in q), None)
    if product:
        orders = [o for o in orders if o["product"].lower() == product.lower()]

    # Identify an explicitly mentioned customer ID.
    customer_match = re.search(r"\bc\d{4}\b", q, re.I)
    if customer_match:
        customer = customer_match.group().upper()
        orders = [o for o in orders if o["customer"].upper() == customer]

    # Filter by month and year, when both are mentioned.
    month_names = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    month = next(
        (num for name, num in month_names.items()
         if re.search(rf"\b{name}\b", q)),
        None,
    )
    year_match = re.search(r"\b(20\d{2})\b", q)
    year = int(year_match.group(1)) if year_match else None

    if month and year:
        orders = [
            o for o in orders
            if business_date(o["created_at"]).month == month
            and business_date(o["created_at"]).year == year
        ]

    # Revenue only counts orders whose current status is paid.
    if any(word in q for word in ["revenue", "sales", "turnover"]):
        paid = [o for o in orders if o["status"] == "paid"]
        total = sum(amount_usd(o) for o in paid)
        return f"${total:,.2f} USD"

    if "refund" in q:
        refunded = [o for o in orders if o["status"] == "refunded"]
        total = sum(amount_usd(o) for o in refunded)
        return f"{len(refunded)} refunded orders; total value ${total:,.2f} USD"

    if "void" in q:
        voided = [o for o in orders if o["status"] == "void"]
        return f"{len(voided)} void orders"

    if "how many" in q or "count" in q or "number of" in q:
        return str(len(orders))

    if "top" in q or "most popular" in q or "best-selling" in q:
        counts = {}
        for o in orders:
            if o["status"] == "paid":
                counts[o["product"]] = counts.get(o["product"], 0) + 1
        if counts:
            name = max(counts, key=counts.get)
            return f"{name}: {counts[name]} paid orders"

    return (
        "I could not confidently interpret that question. "
        "Try asking about revenue, order counts, refunds, a region, "
        "a product, a customer ID, or a month and year."
    )


class QuestionRequest(BaseModel):
    question: str


@app.get("/")
def health():
    return {"status": "ok", "service": "Acme Appliances order ledger"}


@app.post("/")
def ask(request: QuestionRequest):
    return {"answer": answer_question(request.question)}
