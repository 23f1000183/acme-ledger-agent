
import json
import re
import requests
import pandas as pd

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Acme Appliances Ledger Agent")

BASE_URL = "https://exam.sanand.workers.dev/questionData"

PARAMS = {
    "email": "23f1000183@ds.study.iitm.ac.in",
    "quizSign": "hNYxrUzlTTboRJkV6J/ubuJwOnoyij9p5puwOsPcDUIkTEuWOVK79Ir24ywxZpEsUk8fpT4OFg1EFAN7qdvjn0aDnlKuAAuSZXzgrNpshjw37g1FErEg3CHoShNk5Hu75Lwz9AeZP5IJ0FvPli285a7l7hqNtjSeH9D1HefU1hSFyVFP7tv+XRRVQVfqPVLfEEAuqKwEldIZ7ztiOJjrGVTEzRUE4sk6Hu5NykUsRLA1M0zBfLPqbq2gfrcNdS/mAioxecK445weKuk2oJ0Ckf9WCUfsysaUPu7J6NsRTGjd/mV9QWj9ZgWHgan1VrTQb0PmMoo1TApoqryKNNwSRg==",
    "questionId": "q-ledger-agent-server",
}


def fetch_json(path):
    params = {**PARAMS, "path": path}
    response = requests.get(BASE_URL, params=params, timeout=60)
    response.raise_for_status()
    return response


def fetch_data():
    response = fetch_json("/export")

    rows = [
        json.loads(line)
        for line in response.text.splitlines()
        if line.strip()
    ]

    df = pd.DataFrame(rows)

    if df.empty:
        raise RuntimeError("The order ledger is empty.")

    df["updated_at"] = pd.to_datetime(df["updated_at"], utc=True)
    df = (
        df.sort_values("updated_at")
        .drop_duplicates(subset="id", keep="last")
        .copy()
    )

    rates = fetch_json("/rates").json()["usd_per_unit"]

    df["usd_amount"] = (
        pd.to_numeric(df["amount"], errors="coerce")
        * df["currency"].map(rates)
    )

    # Convert timestamps to the business timezone.
    df["created_at"] = pd.to_datetime(
        df["created_at"], utc=True
    ).dt.tz_convert("Asia/Kolkata")

    df["updated_at"] = df["updated_at"].dt.tz_convert(
        "Asia/Kolkata"
    )

    return df, rates


orders_df, exchange_rates = fetch_data()


class QuestionRequest(BaseModel):
    question: str


MONTHS = {
    "january": 1, "february": 2, "march": 3,
    "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
}


def answer_question(question):
    q = question.lower().strip()
    df = orders_df.copy()

    # Filter by status when the question specifies one.
    if "refunded" in q or "refunds" in q or "refund " in q:
        df = df[df["status"].str.lower() == "refunded"]
    elif "void" in q or "voided" in q:
        df = df[df["status"].str.lower() == "void"]
    elif "paid" in q:
        df = df[df["status"].str.lower() == "paid"]
    elif any(word in q for word in [
        "revenue", "sales", "turnover", "earned", "income"
    ]):
        # Only paid orders count as revenue.
        df = df[df["status"].str.lower() == "paid"]

    # Filter by region.
    for region in df["region"].dropna().unique():
        if re.search(r"\b" + re.escape(str(region).lower()) + r"\b", q):
            df = df[df["region"].str.lower() == str(region).lower()]
            break

    # Filter by product.
    for product in df["product"].dropna().unique():
        if re.search(r"\b" + re.escape(str(product).lower()) + r"\b", q):
            df = df[df["product"].str.lower() == str(product).lower()]
            break

    # Filter by customer ID.
    customer_match = re.search(r"\bc\d+\b", q, re.IGNORECASE)
    if customer_match:
        customer = customer_match.group(0).upper()
        df = df[df["customer"].str.upper() == customer]

    # Filter by order ID.
    order_match = re.search(r"\bord-\d+\b", q, re.IGNORECASE)
    if order_match:
        order_id = order_match.group(0).upper()
        df = df[df["id"].str.upper() == order_id]

    # Filter by a specific currency, if explicitly requested.
    currency_match = re.search(r"\b(INR|USD|EUR)\b", question, re.IGNORECASE)
    requested_currency = (
        currency_match.group(1).upper() if currency_match else None
    )

    # Filter by year.
    year_match = re.search(r"\b(20\d{2})\b", q)
    if year_match:
        df = df[df["created_at"].dt.year == int(year_match.group(1))]

    # Filter by month name.
    for month_name, month_number in MONTHS.items():
        if re.search(r"\b" + month_name + r"\b", q):
            df = df[df["created_at"].dt.month == month_number]
            break

    # Filter by explicit date range: "from YYYY-MM-DD to YYYY-MM-DD".
    date_range = re.search(
        r"from\s+(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})",
        q,
    )
    if date_range:
        start = pd.Timestamp(date_range.group(1), tz="Asia/Kolkata")
        end = pd.Timestamp(date_range.group(2), tz="Asia/Kolkata")
        df = df[
            (df["created_at"] >= start)
            & (df["created_at"] < end + pd.Timedelta(days=1))
        ]

    if df.empty:
        return {"answer": "0"}

    # Determine the requested calculation.
    if any(word in q for word in [
        "average", "average order", "mean order"
    ]):
        if requested_currency == "INR":
            values = df["amount"] * df["currency"].map(exchange_rates).fillna(0) / exchange_rates["INR"]
        elif requested_currency == "EUR":
            values = df["usd_amount"] / exchange_rates["EUR"]
        else:
            values = df["usd_amount"]
        result = values.mean()
        return {"answer": f"{result:.2f}"}

    if any(word in q for word in [
        "how many", "number of orders", "count of orders",
        "count orders", "number of transactions", "how much"
    ]) and not any(word in q for word in [
        "revenue", "sales", "turnover", "total amount"
    ]):
        return {"answer": str(int(df["id"].nunique()))}

    # Revenue / sales / amount calculations.
    if requested_currency == "INR":
        values = df["amount"] * df["currency"].map(exchange_rates).fillna(0) / exchange_rates["INR"]
    elif requested_currency == "EUR":
        values = df["usd_amount"] / exchange_rates["EUR"]
    else:
        values = df["usd_amount"]

    if any(word in q for word in [
        "total", "revenue", "sales", "turnover", "earned",
        "income", "amount", "sum", "value", "refund"
    ]):
        result = values.sum()
        return {"answer": f"{result:.2f}"}

    # Default to the number of unique orders matching the filters.
    return {"answer": str(int(df["id"].nunique()))}


@app.get("/")
def health():
    return {
        "status": "ok",
        "unique_orders": int(orders_df["id"].nunique()),
        "message": "Acme Appliances Ledger Agent is running",
    }


@app.post("/")
def answer(request: QuestionRequest):
    try:
        return answer_question(request.question)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not answer question: {str(exc)}",
        )
