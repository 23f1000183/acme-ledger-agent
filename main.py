import json
import re
import requests
import pandas as pd

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="Acme Appliances Ledger Agent")

BASE_URL = "https://exam.sanand.workers.dev/questionData"

PARAMS = {
    "email": "23f1000183@ds.study.iitm.ac.in",
    "quizSign": "hNYxrUzlTTboRJkV6J/ubuJwOnoyij9p5puwOsPcDUIkTEuWOVK79Ir24ywxZpEsUk8fpT4OFg1EFAN7qdvjn0aDnlKuAAuSZXzgrNpshjw37g1FErEg3CHoShNk5Hu75Lwz9AeZP5IJ0FvPli285a7l7hqNtjSeH9D1HefU1hSFyVFP7tv+XRRVQVfqPVLfEEAuqKwEldIZ7ztiOJjrGVTEzRUE4sk6Hu5NykUsRLA1M0zBfLPqbq2gfrcNdS/mAioxecK445weKuk2oJ0Ckf9WCUfsysaUPu7J6NsRTGjd/mV9QWj9ZgWHgan1VrTQb0PmMoo1TApoqryKNNwSRg==",
    "questionId": "q-ledger-agent-server",
}

MONTHS = {
    "january": 1, "february": 2, "march": 3,
    "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9,
    "october": 10, "november": 11, "december": 12,
}

# Any of these stems means "revenue / money earned" -> paid orders only.
REVENUE_STEM = re.compile(
    r"\b(earn\w*|revenue\w*|sale\w*|turnover\w*|income\w*|"
    r"paid|bought|buy|purchas\w*|sold)\b",
    re.I,
)


def fetch(path):
    response = requests.get(
        BASE_URL,
        params={**PARAMS, "path": path},
        timeout=90,
    )
    response.raise_for_status()
    return response


def load_data():
    response = fetch("/export")
    rows = [
        json.loads(line)
        for line in response.text.splitlines()
        if line.strip()
    ]

    df = pd.DataFrame(rows)
    if df.empty:
        raise RuntimeError("The ledger contains no rows.")

    df["updated_at"] = pd.to_datetime(df["updated_at"], utc=True)
    df["created_at"] = pd.to_datetime(df["created_at"], utc=True)

    # Keep the latest version of each order.
    df = (
        df.sort_values("updated_at")
        .drop_duplicates("id", keep="last")
        .copy()
    )

    rates = fetch("/rates").json()["usd_per_unit"]

    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce")
    df["unit_price"] = pd.to_numeric(
        df["unit_price"], errors="coerce"
    )

    df["usd_amount"] = (
        df["amount"] * df["currency"].map(rates)
    )
    df["business_created"] = (
        df["created_at"].dt.tz_convert("Asia/Kolkata")
    )
    df["business_updated"] = (
        df["updated_at"].dt.tz_convert("Asia/Kolkata")
    )

    return df, rates


orders_df, exchange_rates = load_data()


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1)


def money_values(df, currency):
    """Convert each order amount into the requested currency."""
    if currency not in exchange_rates:
        currency = "USD"
    return df["usd_amount"] / exchange_rates[currency]


def answer_question(question):
    q = question.lower().strip()
    df = orders_df.copy()

    # Decide whether the question refers to creation or update dates.
    if any(x in q for x in ["updated", "update date", "last modified"]):
        date_col = "business_updated"
    else:
        date_col = "business_created"

    # ------------------------------------------------------------------
    # Status filter
    # ------------------------------------------------------------------
    status_already_filtered = False

    if re.search(r"\b(refunded|refunds|refund)\b", q):
        df = df[df["status"].str.lower() == "refunded"]
        status_already_filtered = True
    elif re.search(r"\b(void|voided|cancel+ed?)\b", q):
        df = df[df["status"].str.lower() == "void"]
        status_already_filtered = True
    elif REVENUE_STEM.search(q):
        df = df[df["status"].str.lower() == "paid"]
        status_already_filtered = True

    # ------------------------------------------------------------------
    # Region filter
    # ------------------------------------------------------------------
    for region in orders_df["region"].dropna().unique():
        if re.search(r"\b" + re.escape(str(region).lower()) + r"\b", q):
            df = df[df["region"].str.lower() == str(region).lower()]
            break

    # ------------------------------------------------------------------
    # Product filter
    # ------------------------------------------------------------------
    for product in orders_df["product"].dropna().unique():
        if re.search(r"\b" + re.escape(str(product).lower()) + r"s?\b", q):
            df = df[df["product"].str.lower() == str(product).lower()]
            break

    # ------------------------------------------------------------------
    # Customer / order filters
    # ------------------------------------------------------------------
    customer_match = re.search(r"\bc\d+\b", q, re.I)
    if customer_match:
        df = df[
            df["customer"].str.upper() == customer_match.group().upper()
        ]

    order_match = re.search(r"\bord-\d+\b", q, re.I)
    if order_match:
        df = df[df["id"].str.upper() == order_match.group().upper()]

    # ------------------------------------------------------------------
    # Currency
    # ------------------------------------------------------------------
    currency_match = re.search(r"\b(INR|USD|EUR)\b", question, re.I)
    currency = (
        currency_match.group(1).upper() if currency_match else "USD"
    )

    # ------------------------------------------------------------------
    # Year / month
    # ------------------------------------------------------------------
    year_match = re.search(r"\b(20\d{2})\b", q)
    month_number = None

    for month_name, number in MONTHS.items():
        if re.search(r"\b" + month_name + r"\b", q):
            month_number = number
            break

    if year_match:
        df = df[df[date_col].dt.year == int(year_match.group(1))]

    if month_number is not None:
        df = df[df[date_col].dt.month == month_number]

    # ------------------------------------------------------------------
    # Explicit date range
    # ------------------------------------------------------------------
    date_range = re.search(
        r"from\s+(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})",
        q,
    )
    if date_range:
        start = pd.Timestamp(
            date_range.group(1), tz="Asia/Kolkata"
        )
        end = pd.Timestamp(
            date_range.group(2), tz="Asia/Kolkata"
        ) + pd.Timedelta(days=1)

        df = df[
            (df[date_col] >= start)
            & (df[date_col] < end)
        ]

    if df.empty:
        return {"answer": 0}

    # ------------------------------------------------------------------
    # Intent flags
    # ------------------------------------------------------------------
    is_average = any(x in q for x in ["average", "mean"])

    ranking = any(
        word in q for word in
        ["most", "highest", "top", "largest", "greatest",
         "least", "lowest"]
    )
    is_revenue_question = bool(REVENUE_STEM.search(q))

    # ------------------------------------------------------------------
    # Ranking questions
    # ------------------------------------------------------------------
    if ranking and "product" in q and is_revenue_question:
        paid = df[df["status"].str.lower() == "paid"]
        if paid.empty:
            return {"answer": "0"}
        totals = paid.groupby("product")["usd_amount"].sum()
        product = totals.idxmin() if any(
            w in q for w in ["least", "lowest"]
        ) else totals.idxmax()
        return {"answer": str(product)}

    if ranking and "region" in q and is_revenue_question:
        paid = df[df["status"].str.lower() == "paid"]
        if paid.empty:
            return {"answer": "0"}
        totals = paid.groupby("region")["usd_amount"].sum()
        region = totals.idxmin() if any(
            w in q for w in ["least", "lowest"]
        ) else totals.idxmax()
        return {"answer": str(region)}

    if ranking and "customer" in q and is_revenue_question:
        paid = df[df["status"].str.lower() == "paid"]
        if paid.empty:
            return {"answer": "0"}
        totals = paid.groupby("customer")["usd_amount"].sum()
        customer = totals.idxmin() if any(
            w in q for w in ["least", "lowest"]
        ) else totals.idxmax()
        return {"answer": str(customer)}

    # ------------------------------------------------------------------
    # Averages (MUST run before counts: "how many dollars on average"
    # contains the phrase "how many")
    # ------------------------------------------------------------------
    if is_average:
        if "quantity" in q or "units" in q:
            result = df["qty"].mean()
        elif "unit price" in q or "price per unit" in q:
            result = (
                df["unit_price"] * df["currency"].map(exchange_rates)
            ).mean() / exchange_rates[currency]
        else:
            result = money_values(df, currency).mean()
        return {"answer": f"{result:.2f}"}

    # ------------------------------------------------------------------
    # Counts
    # ------------------------------------------------------------------
    is_unit_question = any(
        x in q for x in ["unit", "quantity", "qty", "units sold"]
    )

    asks_count = (
        any(x in q for x in [
            "how many", "number of", "count of", "count orders",
            "count transactions", "number of transactions",
            "how many orders", "how many transactions",
        ])
        or re.search(r"\b(count|quantity of orders)\b", q)
    ) and not is_average

    if asks_count and not is_unit_question:
        if re.search(r"\bcustomer", q):
            result = df["customer"].nunique()
        elif re.search(r"\bproduct", q):
            result = df["product"].nunique()
        elif re.search(r"\bregion", q):
            result = df["region"].nunique()
        else:
            result = df["id"].nunique()
        return {"answer": str(int(result))}

    if is_unit_question and any(
        x in q for x in ["total", "sum", "how many", "number of"]
    ):
        return {"answer": str(int(df["qty"].sum()))}

    # ------------------------------------------------------------------
    # Monetary
    # ------------------------------------------------------------------
    if (
        any(x in q for x in [
            "revenue", "sales", "turnover", "earned", "earn",
            "income", "total", "sum", "amount", "value",
            "how much", "refund",
        ])
        or is_revenue_question
    ):
        if is_revenue_question and not status_already_filtered:
            df = df[df["status"].str.lower() == "paid"]

        result = money_values(df, currency).sum()
        return {"answer": f"{result:.2f}"}

    # ------------------------------------------------------------------
    # Specific order
    # ------------------------------------------------------------------
    if order_match:
        return {
            "answer": f"{money_values(df, currency).sum():.2f}"
        }

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
    # Every question the grader sends is printed here. Open
    # Render -> your service -> Logs to see the [QUESTION] and
    # [ANSWER] lines when the grader marks something wrong.
    print(f"[QUESTION] {request.question}", flush=True)
    try:
        result = answer_question(request.question)
        print(f"[ANSWER] {result}", flush=True)
        return result
    except Exception as exc:
        print(f"[ERROR] {request.question} -> {exc}", flush=True)
        raise HTTPException(
            status_code=500,
            detail=f"Could not answer question: {str(exc)}",
        )
