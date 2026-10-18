import os
import re
import json
import sqlite3
import threading

import httpx
import pandas as pd
from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware


# --------------------------------------------------
# 1. CONFIGURATION
# --------------------------------------------------

RATES_URL = os.environ.get("RATES_URL", "").strip()
EXPORT_URL = os.environ.get("EXPORT_URL", "").strip()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


# --------------------------------------------------
# 2. DATABASE
# --------------------------------------------------

db_conn = sqlite3.connect(
    ":memory:",
    check_same_thread=False
)

db_lock = threading.Lock()


def init_database():
    """Download, process, and load the order data."""

    if not RATES_URL or not EXPORT_URL:
        raise RuntimeError(
            "Set RATES_URL and EXPORT_URL in Render Environment Variables."
        )

    with httpx.Client(timeout=60.0, follow_redirects=True) as client:

        print("Fetching currency rates from /rates...")

        resp_rates = client.get(RATES_URL, headers=HEADERS)
        resp_rates.raise_for_status()

        usd_rates = resp_rates.json()["usd_per_unit"]
        print("Currency Rates:", usd_rates)

        print("Fetching raw order ledger from /export...")

        resp_export = client.get(EXPORT_URL, headers=HEADERS)
        resp_export.raise_for_status()

        raw_lines = resp_export.text.strip().splitlines()

        rows = [
            json.loads(line)
            for line in raw_lines
            if line.strip()
        ]

        print(f"Total raw records downloaded: {len(rows)}")

    if not rows:
        raise RuntimeError("The export endpoint returned no order records.")

    df = pd.DataFrame(rows)

    # Parse timestamps into timezone-aware UTC.
    df["created_at_utc"] = pd.to_datetime(
        df["created_at"],
        format="ISO8601",
        utc=True
    )

    df["updated_at_utc"] = pd.to_datetime(
        df["updated_at"],
        format="ISO8601",
        utc=True
    )

    # Keep the most recently updated record for each order ID.
    df = (
        df.sort_values("updated_at_utc", kind="mergesort")
        .drop_duplicates(subset=["id"], keep="last")
        .copy()
    )

    print(f"Total verified active orders: {len(df)}")

    # Convert business dates to Asia/Kolkata.
    ist_created = df["created_at_utc"].dt.tz_convert("Asia/Kolkata")

    df["created_date"] = ist_created.dt.strftime("%Y-%m-%d")
    df["created_month"] = ist_created.dt.strftime("%Y-%m")
    df["created_year"] = ist_created.dt.year

    # Standardize transaction amounts into USD.
    df["rate_to_usd"] = df["currency"].map(usd_rates)

    if df["rate_to_usd"].isna().any():
        unknown = df.loc[
            df["rate_to_usd"].isna(), "currency"
        ].unique().tolist()

        raise ValueError(f"Missing USD conversion rates for: {unknown}")

    df["amount_usd"] = (
        pd.to_numeric(df["amount"], errors="raise")
        * df["rate_to_usd"]
    )

    # Remove intermediate timestamp columns before loading into SQLite.
    df_sql = df.drop(
        columns=["created_at_utc", "updated_at_utc"]
    )

    with db_lock:
        df_sql.to_sql(
            "orders",
            db_conn,
            index=False,
            if_exists="replace"
        )

    print("In-memory SQLite database ready.")


# --------------------------------------------------
# 3. QUESTION PARSING CONFIGURATION
# --------------------------------------------------

MONTH_MAP = {
    "january": "01",
    "february": "02",
    "march": "03",
    "april": "04",
    "may": "05",
    "june": "06",
    "july": "07",
    "august": "08",
    "september": "09",
    "october": "10",
    "november": "11",
    "december": "12",
}

PRODUCTS = [
    "Air Fryer",
    "Rice Cooker",
    "Juicer",
    "Toaster",
    "Mixer",
    "Kettle",
    "Blender",
    "Grinder",
]

REGIONS = [
    "North",
    "South",
    "East",
    "West",
    "Central",
]


# --------------------------------------------------
# 4. CONVERT QUESTIONS INTO SQL
# --------------------------------------------------

def parse_question_to_sql(question: str):
    q_low = question.lower()
    conditions = []

    # Status classification.
    if re.search(r"\brefund(ed|s)?\b", q_low):
        conditions.append("status = 'refunded'")

    elif re.search(r"\b(void|voided|cancel|cancelled)\b", q_low):
        conditions.append("status = 'void'")

    else:
        conditions.append("status = 'paid'")

    # Region filter.
    for reg in REGIONS:
        if re.search(rf"\b{reg.lower()}\b", q_low):
            conditions.append(f"region = '{reg}'")
            break

    # Product filter.
    for prod in PRODUCTS:
        if re.search(rf"\b{re.escape(prod.lower())}s?\b", q_low):
            conditions.append(f"product = '{prod}'")
            break

    # Customer ID filter, for example C1234.
    cust_match = re.search(r"\b(c\d{4})\b", q_low)

    if cust_match:
        conditions.append(
            f"customer = '{cust_match.group(1).upper()}'"
        )

    # Date, month, and year filters.
    year_match = re.search(r"\b(202\d)\b", q_low)

    year_str = year_match.group(1) if year_match else "2026"

    month_num = None

    for m_name, m_val in MONTH_MAP.items():
        if re.search(rf"\b{m_name}\b", q_low):
            month_num = m_val
            break

    if month_num:
        conditions.append(
            f"created_month = '{year_str}-{month_num}'"
        )

    elif year_match and "month" not in q_low:
        conditions.append(f"created_year = {year_str}")

    where_clause = (
        " AND ".join(conditions)
        if conditions
        else "1=1"
    )

    # Top-selling product.
    if any(p in q_low for p in [
        "which product",
        "what product",
        "top-selling product",
        "best-selling product",
        "product earned the most",
    ]):
        order_metric = (
            "SUM(qty)"
            if any(w in q_low for w in ["qty", "quantity", "units"])
            else "SUM(amount_usd)"
        )

        sql = (
            f"SELECT product FROM orders WHERE {where_clause} "
            f"GROUP BY product ORDER BY {order_metric} DESC LIMIT 1"
        )

        return sql, "string"

    # Top customer.
    if any(p in q_low for p in [
        "which customer",
        "what customer",
        "top customer",
        "customer spent the most",
    ]):
        sql = (
            f"SELECT customer FROM orders WHERE {where_clause} "
            "GROUP BY customer "
            "ORDER BY SUM(amount_usd) DESC LIMIT 1"
        )

        return sql, "string"

    # Top region.
    if any(p in q_low for p in [
        "which region",
        "what region",
        "top region",
        "region earned the most",
    ]):
        sql = (
            f"SELECT region FROM orders WHERE {where_clause} "
            "GROUP BY region "
            "ORDER BY SUM(amount_usd) DESC LIMIT 1"
        )

        return sql, "string"

    # Count distinct customers.
    if any(p in q_low for p in [
        "distinct customer",
        "different customer",
        "unique customer",
        "how many customer",
    ]):
        sql = (
            "SELECT COUNT(DISTINCT customer) "
            f"FROM orders WHERE {where_clause}"
        )

        return sql, "int"

    # Average order amount in USD.
    if "average" in q_low:
        sql = (
            "SELECT ROUND(COALESCE(AVG(amount_usd), 0), 2) "
            f"FROM orders WHERE {where_clause}"
        )

        return sql, "money"

    # Count orders.
    if any(p in q_low for p in [
        "how many orders",
        "count the",
        "number of orders",
        "how many refunded",
    ]):
        sql = f"SELECT COUNT(*) FROM orders WHERE {where_clause}"
        return sql, "int"

    # Total quantity.
    if any(p in q_low for p in [
        "how many units",
        "total quantity",
        "total units",
    ]):
        sql = (
            "SELECT COALESCE(SUM(qty), 0) "
            f"FROM orders WHERE {where_clause}"
        )

        return sql, "int"

    # Default: total revenue, earnings, or sales in USD.
    sql = (
        "SELECT ROUND(COALESCE(SUM(amount_usd), 0), 2) "
        f"FROM orders WHERE {where_clause}"
    )

    return sql, "money"


# --------------------------------------------------
# 5. FASTAPI APPLICATION
# --------------------------------------------------

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup_event():
    init_database()


# --------------------------------------------------
# 6. EXAM ENDPOINT
# --------------------------------------------------

@app.post("/")
async def answer_question(request: Request):
    try:
        body = await request.json()

        if not isinstance(body, dict):
            raise HTTPException(
                status_code=400,
                detail="Request body must be a JSON object."
            )

        question = body.get("question", "")

        if not isinstance(question, str) or not question.strip():
            raise HTTPException(
                status_code=400,
                detail="A non-empty question is required."
            )

        print(f"[INCOMING QUESTION] {question}")

        sql_query, ans_type = parse_question_to_sql(question)

        print(f"[SQL EXECUTED] {sql_query}")

        with db_lock:
            cursor = db_conn.cursor()
            cursor.execute(sql_query)
            row = cursor.fetchone()
            cursor.close()

        val = row[0] if row and row[0] is not None else 0

        if ans_type == "string":
            result = str(val)

        elif ans_type == "int":
            result = int(val)

        else:
            result = round(float(val), 2)

        print(f"[RESPONSE] {result}")

        return {"answer": result}

    except HTTPException:
        raise

    except Exception as exc:
        print(f"[ERROR] {exc}")

        raise HTTPException(
            status_code=500,
            detail="Unable to process the question."
        )
