
import os
import re
import json
import sqlite3
from contextlib import asynccontextmanager

import httpx
import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware


# Set these two environment variables in Render using your own
# signed URLs from the assignment.
RATES_URL = os.getenv("RATES_URL", "")
EXPORT_URL = os.getenv("EXPORT_URL", "")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

DB = sqlite3.connect(":memory:", check_same_thread=False)

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

REGIONS = ["North", "South", "East", "West", "Central"]


def init_database():
    """Fetch the assignment's rates and ledger, then prepare SQLite."""

    if not RATES_URL or not EXPORT_URL:
        raise RuntimeError(
            "Set your own RATES_URL and EXPORT_URL in Render "
            "environment variables."
        )

    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        print("Fetching currency rates...")

        rates_response = client.get(RATES_URL, headers=HEADERS)
        rates_response.raise_for_status()

        rates_payload = rates_response.json()
        usd_rates = rates_payload["usd_per_unit"]

        print("Loaded currency rates:", usd_rates)

        print("Fetching order ledger...")

        export_response = client.get(EXPORT_URL, headers=HEADERS)
        export_response.raise_for_status()

        rows = [
            json.loads(line)
            for line in export_response.text.splitlines()
            if line.strip()
        ]

    if not rows:
        raise RuntimeError("The order ledger export returned no records.")

    df = pd.DataFrame(rows)

    required_columns = {
        "id",
        "customer",
        "region",
        "product",
        "qty",
        "amount",
        "currency",
        "status",
        "created_at",
        "updated_at",
    }

    missing = required_columns - set(df.columns)
    if missing:
        raise RuntimeError(
            f"Ledger is missing required columns: {sorted(missing)}"
        )

    # Parse all timestamps consistently as UTC.
    df["created_at_utc"] = pd.to_datetime(
        df["created_at"], utc=True, errors="coerce"
    )
    df["updated_at_utc"] = pd.to_datetime(
        df["updated_at"], utc=True, errors="coerce"
    )

    df = df.dropna(
        subset=["id", "created_at_utc", "updated_at_utc"]
    ).copy()

    # Keep the latest version of each order.
    df = (
        df.sort_values("updated_at_utc", kind="mergesort")
        .drop_duplicates(subset=["id"], keep="last")
        .copy()
    )

    # Business dates use Asia/Kolkata.
    local_created = df["created_at_utc"].dt.tz_convert("Asia/Kolkata")

    df["created_date"] = local_created.dt.strftime("%Y-%m-%d")
    df["created_month"] = local_created.dt.strftime("%Y-%m")
    df["created_year"] = local_created.dt.year

    # Normalize text fields and numeric fields.
    for column in ["customer", "region", "product", "status", "currency"]:
        df[column] = df[column].astype("string").str.strip()

    df["region"] = df["region"].str.title()
    df["product"] = df["product"].str.title()
    df["status"] = df["status"].str.lower()
    df["currency"] = df["currency"].str.upper()

    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce")

    # Use the assignment's live rates, not approximate hardcoded rates.
    df["rate_to_usd"] = df["currency"].map(usd_rates)

    if df["rate_to_usd"].isna().any():
        missing_currencies = sorted(
            df.loc[df["rate_to_usd"].isna(), "currency"]
            .dropna()
            .unique()
            .tolist()
        )
        raise RuntimeError(
            f"No USD conversion rate for currencies: {missing_currencies}"
        )

    df["amount_usd"] = df["amount"] * df["rate_to_usd"]

    df = df.drop(
        columns=["created_at_utc", "updated_at_utc"],
        errors="ignore",
    )

    df.to_sql("orders", DB, index=False, if_exists="replace")

    DB.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)"
    )
    DB.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_region ON orders(region)"
    )
    DB.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_product ON orders(product)"
    )
    DB.execute(
        "CREATE INDEX IF NOT EXISTS idx_orders_month ON orders(created_month)"
    )
    DB.commit()

    print(f"Raw records downloaded: {len(rows)}")
    print(f"Unique orders loaded: {len(df)}")
    print("SQLite database ready.")


def parse_question_to_sql(question: str):
    """Translate supported business questions into parameterized SQL."""

    q = question.lower().strip()
    conditions = []
    params = []

    # Status: questions without another status default to paid orders.
    if re.search(r"\brefund(?:ed|s)?\b", q):
        conditions.append("LOWER(status) = ?")
        params.append("refunded")
    elif re.search(r"\b(void|voided|cancel|cancelled|canceled)\b", q):
        conditions.append("LOWER(status) IN (?, ?)")
        params.extend(["void", "cancelled"])
    else:
        conditions.append("LOWER(status) = ?")
        params.append("paid")

    # Region.
    for region in REGIONS:
        if re.search(rf"\b{region.lower()}\b", q):
            conditions.append("LOWER(region) = ?")
            params.append(region.lower())
            break

    # Product.
    for product in PRODUCTS:
        pattern = rf"\b{re.escape(product.lower())}s?\b"
        if re.search(pattern, q):
            conditions.append("LOWER(product) = ?")
            params.append(product.lower())
            break

    # Customer ID, e.g. C1020.
    customer_match = re.search(r"\b(c\d{4})\b", q, re.IGNORECASE)
    if customer_match:
        conditions.append("UPPER(customer) = ?")
        params.append(customer_match.group(1).upper())

    # Month and year. Only filter by year when the question specifies one.
    year_match = re.search(r"\b(20\d{2})\b", q)
    year = int(year_match.group(1)) if year_match else None

    month_number = None
    for month_name, number in MONTH_MAP.items():
        if re.search(rf"\b{month_name}\b", q):
            month_number = number
            break

    if month_number and year:
        conditions.append("created_month = ?")
        params.append(f"{year}-{month_number}")
    elif month_number:
        conditions.append("SUBSTR(created_month, 6, 2) = ?")
        params.append(month_number)
    elif year:
        conditions.append("created_year = ?")
        params.append(year)

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    # Intent detection: specific metrics must come before total revenue.
    asks_distinct_customers = any(
        phrase in q
        for phrase in [
            "distinct customer",
            "unique customer",
            "different customer",
            "how many customers",
            "how many customer",
        ]
    )

    if asks_distinct_customers:
        sql = (
            "SELECT COUNT(DISTINCT customer) "
            f"FROM orders WHERE {where_clause}"
        )
        return sql, params, "int"

    asks_count = (
        any(
            phrase in q
            for phrase in [
                "how many",
                "count the",
                "count of",
                "number of",
                "count ",
            ]
        )
        or q.startswith("count")
    )

    if asks_count and any(word in q for word in ["order", "refund", "void"]):
        sql = f"SELECT COUNT(*) FROM orders WHERE {where_clause}"
        return sql, params, "int"

    if any(
        phrase in q
        for phrase in ["how many units", "total quantity", "total units"]
    ):
        sql = f"""
            SELECT COALESCE(SUM(qty), 0)
            FROM orders
            WHERE {where_clause}
        """
        return sql, params, "int"

    if any(
        phrase in q
        for phrase in [
            "which product",
            "what product",
            "top-selling product",
            "best-selling product",
            "product earned the most",
        ]
    ):
        metric = (
            "SUM(qty)"
            if any(word in q for word in ["quantity", "units", "qty"])
            else "SUM(amount_usd)"
        )
        sql = f"""
            SELECT product
            FROM orders
            WHERE {where_clause}
            GROUP BY product
            ORDER BY {metric} DESC, product ASC
            LIMIT 1
        """
        return sql, params, "string"

    if any(
        phrase in q
        for phrase in [
            "which customer",
            "what customer",
            "top customer",
            "customer spent the most",
        ]
    ):
        sql = f"""
            SELECT customer
            FROM orders
            WHERE {where_clause}
            GROUP BY customer
            ORDER BY SUM(amount_usd) DESC, customer ASC
            LIMIT 1
        """
        return sql, params, "string"

    if any(
        phrase in q
        for phrase in [
            "which region",
            "what region",
            "top region",
            "region earned the most",
        ]
    ):
        sql = f"""
            SELECT region
            FROM orders
            WHERE {where_clause}
            GROUP BY region
            ORDER BY SUM(amount_usd) DESC, region ASC
            LIMIT 1
        """
        return sql, params, "string"

    if "average" in q or "on average" in q:
        sql = f"""
            SELECT ROUND(COALESCE(AVG(amount_usd), 0), 2)
            FROM orders
            WHERE {where_clause}
        """
        return sql, params, "number"

    # Default: total revenue / sales / order value.
    sql = f"""
        SELECT ROUND(COALESCE(SUM(amount_usd), 0), 2)
        FROM orders
        WHERE {where_clause}
    """
    return sql, params, "number"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database()
    yield
    DB.close()


app = FastAPI(
    title="Acme Appliances Order Ledger Agent",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def health():
    return {"status": "ok", "service": "Acme Appliances Order Ledger Agent"}


@app.post("/")
async def answer_question(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    question = body.get("question", "")
    if not isinstance(question, str) or not question.strip():
        raise HTTPException(
            status_code=400,
            detail="Please provide a non-empty question.",
        )

    sql, params, answer_type = parse_question_to_sql(question)

    try:
        cursor = DB.execute(sql, params)
        row = cursor.fetchone()
        value = row[0] if row and row[0] is not None else 0

        if answer_type == "int":
            answer = int(value)
        elif answer_type == "string":
            answer = str(value)
        else:
            answer = round(float(value), 2)

        print(f"Question: {question}")
        print(f"Answer: {answer}")

        return {"answer": answer}

    except sqlite3.Error as exc:
        print(f"SQL error: {exc}")
        raise HTTPException(
            status_code=500,
            detail="Could not calculate the answer.",
        )
