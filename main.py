
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent
DATA_FILE = BASE_DIR / "export.json"

RATES = {
    "USD": 1.0,
    "EUR": 1.05,
    "INR": 0.01214,
}

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

            if (
                order_id not in latest
                or updated > latest[order_id][0]
            ):
                latest[order_id] = (updated, row)

    return [item[1] for item in latest.values()]


ORDERS = load_orders()


def business_date(value):
    dt = datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )
    return dt.astimezone(TZ).date()


def amount_usd(order):
    currency = order["currency"].upper()

    if currency not in RATES:
        raise ValueError(f"Unsupported currency: {currency}")

    return float(order["amount"]) * RATES[currency]


def answer_question(question):
    q = question.lower()
    orders = ORDERS

    # Region filter
    region = next(
        (
            r for r in ["North", "South", "East", "West", "Central"]
            if re.search(rf"\b{r.lower()}\b", q)
        ),
        None,
    )

    if region:
        orders = [
            o for o in orders
            if o["region"].lower() == region.lower()
        ]

    # Product filter: match complete product names
    products = sorted(
        {o["product"] for o in ORDERS},
        key=len,
        reverse=True,
    )

    product = next(
        (
            p for p in products
            if re.search(
                rf"(?<!\w){re.escape(p.lower())}(?!\w)",
                q,
            )
        ),
        None,
    )

    if product:
        orders = [
            o for o in orders
            if o["product"].lower() == product.lower()
        ]

    # Customer ID filter
    customer_match = re.search(r"\bc\d{4}\b", q, re.I)

    if customer_match:
        customer = customer_match.group().upper()
        orders = [
            o for o in orders
            if str(o["customer"]).upper() == customer
        ]

    # Month and year filter
    month_names = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }

    month = next(
        (
            number for name, number in month_names.items()
            if re.search(rf"\b{name}\b", q)
        ),
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

    # Distinct customers among paid orders
    if (
        re.search(r"\b(distinct|unique)\s+customers?\b", q)
        or re.search(r"\bhow many different customers\b", q)
    ):
        paid = [
            o for o in orders
            if o["status"].lower() == "paid"
        ]

        return str(len({
            str(o["customer"]).upper()
            for o in paid
        }))

    # Refund counts
    if "refund" in q:
        refunded = [
            o for o in orders
            if o["status"].lower() == "refunded"
        ]

        if any(word in q for word in ["count", "how many", "number of"]):
            return str(len(refunded))

        total = sum(amount_usd(o) for o in refunded)
        return (
            f"{len(refunded)} refunded orders; "
            f"total value ${total:,.2f} USD"
        )

    # Void counts
    if "void" in q:
        voided = [
            o for o in orders
            if o["status"].lower() == "void"
        ]
        return str(len(voided))

    # Average/mean value per qualifying paid order.
    # IMPORTANT: calculate this before the revenue branch.
    if any(word in q for word in ["average", "avg", "mean"]):
        paid = [
            o for o in orders
            if o["status"].lower() == "paid"
        ]

        if not paid:
            return "No matching paid orders"

        total_usd = sum(amount_usd(o) for o in paid)
        average_usd = total_usd / len(paid)

        return f"${average_usd:.2f} USD"

    # Revenue: current paid orders only
    if any(word in q for word in ["revenue", "sales", "turnover"]):
        paid = [
            o for o in orders
            if o["status"].lower() == "paid"
        ]

        total = sum(amount_usd(o) for o in paid)
        return f"${total:,.2f} USD"

    # Count orders
    if (
        "how many" in q
        or "count" in q
        or "number of" in q
    ):
        return str(len(orders))

    # Most popular product among paid orders
    if any(
        phrase in q
        for phrase in ["top", "most popular", "best-selling"]
    ):
        counts = {}

        for order in ORDERS:
            if order["status"].lower() == "paid":
                name = order["product"]
                counts[name] = counts.get(name, 0) + 1

        if counts:
            name = max(counts, key=counts.get)
            return f"{name}: {counts[name]} paid orders"

    return (
        "I could not confidently interpret that question. "
        "Try asking about average order value, revenue, order counts, "
        "refunds, a region, a product, a customer ID, or a month and year."
    )


class QuestionRequest(BaseModel):
    question: str


@app.get("/")
def health():
    return {
        "status": "ok",
        "service": "Acme Appliances order ledger",
    }


@app.post("/")
def ask(request: QuestionRequest):
    return {"answer": answer_question(request.question)}
