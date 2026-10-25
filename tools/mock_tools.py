"""Mock downstream tools service (no auth, no idempotency)."""
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from common.config import load_settings
from common.sqlite import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers(customer_id TEXT PRIMARY KEY, name TEXT, email TEXT, phone TEXT,
  balance REAL, notes TEXT);
CREATE TABLE IF NOT EXISTS payments(payment_id TEXT PRIMARY KEY, customer_id TEXT, amount REAL,
  status TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS outbox(email_id TEXT PRIMARY KEY, to_addr TEXT, subject TEXT, body TEXT,
  external INTEGER, status TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER);
"""

UPDATABLE_FIELDS = {"balance", "notes", "email", "phone"}


class ToolError(Exception):
    """Tool-level error, returned as HTTP 400."""


def seed(db: Database) -> None:
    """Delete all rows and insert the 20 seed customers and counters."""
    for table in ("customers", "payments", "outbox", "counters"):
        db.execute(f"DELETE FROM {table}")
    for i in range(1, 21):
        db.execute("INSERT INTO customers VALUES(?,?,?,?,?,?)",
                   (f"C{i:03d}", f"Customer {i}", f"customer{i}@gmail.com",
                    f"+91-98000-{i:05d}", 1000.0, "Regular customer"))
    db.execute("INSERT INTO counters VALUES('payment', 0)")
    db.execute("INSERT INTO counters VALUES('email', 0)")


def next_id(db: Database, name: str, prefix: str) -> str:
    """Increment a counter and return the formatted id."""
    db.execute("UPDATE counters SET value = value + 1 WHERE name=?", (name,))
    n = db.query_one("SELECT value FROM counters WHERE name=?", (name,))["value"]
    return f"{prefix}{n:05d}"


def get_customer_row(db: Database, customer_id: str) -> dict:
    """Fetch a customer or raise ToolError."""
    row = db.query_one("SELECT * FROM customers WHERE customer_id=?", (customer_id,))
    if row is None:
        raise ToolError(f"unknown customer {customer_id}")
    return row


def t_get_customer(db: Database, args: dict) -> dict:
    """get_customer tool."""
    return get_customer_row(db, args["customer_id"])


def t_get_payments(db: Database, args: dict) -> dict:
    """get_payments tool."""
    rows = db.query("SELECT * FROM payments WHERE customer_id=? ORDER BY payment_id", (args["customer_id"],))
    return {"payments": rows}


def t_issue_refund(db: Database, args: dict) -> dict:
    """issue_refund tool: insert payment and credit the balance."""
    cid, amount = args["customer_id"], round(float(args["amount"]), 2)
    get_customer_row(db, cid)
    pid = next_id(db, "payment", "P")
    db.execute("INSERT INTO payments VALUES(?,?,?,?,?)", (pid, cid, amount, "completed", time.time()))
    db.execute("UPDATE customers SET balance = round(balance + ?, 2) WHERE customer_id=?", (amount, cid))
    return {"payment_id": pid, "customer_id": cid, "amount": amount, "status": "completed"}


def t_send_email(db: Database, args: dict) -> dict:
    """send_email tool."""
    to = args["to"]
    external = 0 if to.endswith("@company.com") else 1
    eid = next_id(db, "email", "E")
    db.execute("INSERT INTO outbox VALUES(?,?,?,?,?,?,?)",
               (eid, to, args["subject"], args["body"], external, "sent", time.time()))
    return {"email_id": eid, "to": to, "external": external}


def t_update_record(db: Database, args: dict) -> dict:
    """update_record tool."""
    cid, field, value = args["customer_id"], args["field"], args["value"]
    if field not in UPDATABLE_FIELDS:
        raise ToolError(f"bad field {field}")
    row = get_customer_row(db, cid)
    db.execute(f"UPDATE customers SET {field}=? WHERE customer_id=?", (value, cid))
    return {"customer_id": cid, "field": field, "previous_value": row[field], "value": value}


def t_reverse_refund(db: Database, args: dict) -> dict:
    """reverse_refund tool: no-op if already reversed."""
    pay = db.query_one("SELECT * FROM payments WHERE payment_id=?", (args["payment_id"],))
    if pay is None:
        raise ToolError("unknown payment")
    if pay["status"] != "reversed":
        db.execute("UPDATE payments SET status='reversed' WHERE payment_id=?", (pay["payment_id"],))
        db.execute("UPDATE customers SET balance = round(balance - ?, 2) WHERE customer_id=?",
                   (pay["amount"], pay["customer_id"]))
    return {"payment_id": pay["payment_id"], "status": "reversed"}


def t_recall_email(db: Database, args: dict) -> dict:
    """recall_email tool."""
    if db.query_one("SELECT 1 AS x FROM outbox WHERE email_id=?", (args["email_id"],)) is None:
        raise ToolError("unknown email")
    db.execute("UPDATE outbox SET status='recalled' WHERE email_id=?", (args["email_id"],))
    return {"email_id": args["email_id"], "status": "recalled"}


TOOLS = {
    "get_customer": t_get_customer, "get_payments": t_get_payments, "issue_refund": t_issue_refund,
    "send_email": t_send_email, "update_record": t_update_record,
    "reverse_refund": t_reverse_refund, "recall_email": t_recall_email,
}


def create_tools_app(db_path: str) -> FastAPI:
    """Build the tools FastAPI app with its own database."""
    app = FastAPI()
    db = Database(db_path)
    db.executescript(SCHEMA)
    if db.query_one("SELECT 1 AS x FROM customers") is None:
        seed(db)
    app.state.db = db
    app.state.outage_until = 0.0

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.post("/tools/{name}")
    def run_tool(name: str, body: dict):
        if name not in TOOLS:
            return JSONResponse({"error": "unknown_tool"}, status_code=404)
        if time.time() < app.state.outage_until:
            return JSONResponse({"error": "service_unavailable"}, status_code=503)
        try:
            return {"result": TOOLS[name](db, body.get("args", {}))}
        except (ToolError, KeyError, ValueError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.post("/admin/reset")
    def reset() -> dict:
        seed(db)
        app.state.outage_until = 0.0
        return {"ok": True}

    @app.post("/admin/outage")
    def outage(body: dict) -> dict:
        app.state.outage_until = time.time() + float(body["seconds"])
        return {"ok": True}

    @app.get("/admin/state")
    def state() -> dict:
        return {"customers": db.query("SELECT * FROM customers ORDER BY customer_id"),
                "payments": db.query("SELECT * FROM payments ORDER BY payment_id"),
                "outbox": db.query("SELECT * FROM outbox ORDER BY email_id")}

    return app


def default_app() -> FastAPI:
    """uvicorn factory."""
    return create_tools_app(load_settings().TOOLS_DB_PATH)
