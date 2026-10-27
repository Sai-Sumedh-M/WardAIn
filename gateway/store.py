"""All gateway.db reads and writes."""
import json
import time
import uuid

from common.sqlite import Database

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(agent_id TEXT PRIMARY KEY, role TEXT, public_key TEXT, status TEXT,
  created_at REAL);
CREATE TABLE IF NOT EXISTS tokens(jti TEXT PRIMARY KEY, agent_id TEXT, expires_at REAL, revoked INTEGER);
CREATE TABLE IF NOT EXISTS nonces(nonce TEXT PRIMARY KEY, ts REAL);
CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY, ts REAL, run_id TEXT, step_id TEXT,
  agent_id TEXT, type TEXT, tool TEXT, detail_json TEXT);
CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, scenario TEXT, mode TEXT, customer_id TEXT,
  amount REAL, reason TEXT, plan_json TEXT, status TEXT, started_at REAL, ended_at REAL);
CREATE TABLE IF NOT EXISTS steps(step_id TEXT PRIMARY KEY, run_id TEXT, agent_id TEXT, role TEXT,
  tool TEXT, args_json TEXT, result_json TEXT, parent_step_ids_json TEXT, plan_index INTEGER,
  deviation INTEGER, side_effect INTEGER, tainted INTEGER, ts REAL);
CREATE TABLE IF NOT EXISTS compensations(id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT,
  step_id TEXT, tool TEXT, undo_tool TEXT, undo_args_json TEXT, applied INTEGER, applied_at REAL);
CREATE TABLE IF NOT EXISTS idempotency(key TEXT PRIMARY KEY, run_id TEXT, step_id TEXT,
  status_code INTEGER, response_json TEXT);
CREATE TABLE IF NOT EXISTS approvals(approval_id TEXT PRIMARY KEY, run_id TEXT, step_id TEXT,
  agent_id TEXT, tool TEXT, envelope_json TEXT, status TEXT, response_json TEXT, created_at REAL,
  decided_at REAL, kind TEXT DEFAULT 'action', payload_json TEXT);
CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value_json TEXT);
"""

DEFAULT_CONFIG = {
    "mode": "on",
    "scenario": {"name": "none", "chaos": [], "seed": 42},
    "approval_mode": "auto_reject",
    "monitor_enabled": True,
    "global_halt": False,
    "nvidia_guard": "off",
    "hitl": "off",
}


class GatewayStore:
    def __init__(self, db_path: str):
        """Open the database, create tables and default config."""
        self.db = Database(db_path)
        self.db.executescript(SCHEMA)
        cols = {r["name"] for r in self.db.query("PRAGMA table_info(approvals)")}
        for col, ddl in (("kind", "TEXT DEFAULT 'action'"), ("payload_json", "TEXT")):
            if col not in cols:   # database created before Phase 9
                self.db.execute(f"ALTER TABLE approvals ADD COLUMN {col} {ddl}")
        for key, value in DEFAULT_CONFIG.items():
            self.db.execute("INSERT OR IGNORE INTO config VALUES(?,?)", (key, json.dumps(value)))

    # ---- config ----
    def get_config(self, key: str):
        """Return one config value."""
        row = self.db.query_one("SELECT value_json FROM config WHERE key=?", (key,))
        return json.loads(row["value_json"]) if row else DEFAULT_CONFIG.get(key)

    def all_config(self) -> dict:
        """Return all config keys."""
        return {r["key"]: json.loads(r["value_json"]) for r in self.db.query("SELECT * FROM config")}

    def set_config(self, key: str, value) -> None:
        """Upsert one config value."""
        self.db.execute("INSERT OR REPLACE INTO config VALUES(?,?)", (key, json.dumps(value)))

    # ---- events ----
    def add_event(self, type: str, run_id: str | None = None, step_id: str | None = None,
                  agent_id: str | None = None, tool: str | None = None, detail: dict | None = None) -> str:
        """Insert an event and return its id."""
        event_id = uuid.uuid4().hex
        self.db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?)",
                        (event_id, time.time(), run_id, step_id, agent_id, type, tool,
                         json.dumps(detail or {})))
        return event_id

    def get_events(self, run_id: str | None = None, since_ts: float = 0.0, limit: int = 500) -> list[dict]:
        """Events ascending by ts: from since_ts onward, or the most recent `limit` if since_ts is 0."""
        sql, params = "SELECT * FROM events WHERE ts > ?", [since_ts]
        if run_id:
            sql += " AND run_id=?"
            params.append(run_id)
        order = "ASC" if since_ts else "DESC"
        rows = self.db.query(sql + f" ORDER BY ts {order}, rowid {order} LIMIT ?", tuple(params + [limit]))
        if not since_ts:
            rows.reverse()
        return [{**{k: v for k, v in r.items() if k != "detail_json"},
                 "detail": json.loads(r["detail_json"])} for r in rows]

    # ---- agents / tokens / nonces ----
    def get_agent(self, agent_id: str) -> dict | None:
        """Return the agent row."""
        return self.db.query_one("SELECT * FROM agents WHERE agent_id=?", (agent_id,))

    def list_agents(self) -> list[dict]:
        """All agent rows."""
        return self.db.query("SELECT * FROM agents ORDER BY created_at")

    def upsert_agent(self, agent_id: str, role: str, public_key: str) -> None:
        """Create or replace an active agent."""
        self.db.execute("INSERT OR REPLACE INTO agents VALUES(?,?,?,?,?)",
                        (agent_id, role, public_key, "active", time.time()))

    def set_agent_status(self, agent_id: str, status: str) -> None:
        """Update agent status."""
        self.db.execute("UPDATE agents SET status=? WHERE agent_id=?", (status, agent_id))

    def add_token(self, jti: str, agent_id: str, expires_at: float) -> None:
        """Record an issued token."""
        self.db.execute("INSERT INTO tokens VALUES(?,?,?,0)", (jti, agent_id, expires_at))

    def is_token_revoked(self, jti: str) -> bool:
        """True if the token is revoked."""
        row = self.db.query_one("SELECT revoked FROM tokens WHERE jti=?", (jti,))
        return bool(row and row["revoked"])

    def revoke_agent_tokens(self, agent_id: str) -> None:
        """Revoke every token of an agent."""
        self.db.execute("UPDATE tokens SET revoked=1 WHERE agent_id=?", (agent_id,))

    def add_nonce(self, nonce: str, now: float) -> bool:
        """Insert a nonce (pruning old ones); False if it was already seen."""
        self.db.execute("DELETE FROM nonces WHERE ts < ?", (now - 120,))
        if self.db.query_one("SELECT 1 AS x FROM nonces WHERE nonce=?", (nonce,)):
            return False
        self.db.execute("INSERT INTO nonces VALUES(?,?)", (nonce, now))
        return True

    # ---- runs ----
    def add_run(self, run_id: str, scenario: str, mode: str, customer_id: str, amount: float,
                reason: str) -> None:
        """Insert a running run."""
        self.db.execute("INSERT OR REPLACE INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (run_id, scenario, mode, customer_id, amount, reason, None, "running",
                         time.time(), None))

    def get_run(self, run_id: str) -> dict | None:
        """Return the run row."""
        return self.db.query_one("SELECT * FROM runs WHERE run_id=?", (run_id,))

    def list_runs(self, limit: int = 100) -> list[dict]:
        """Newest runs first."""
        return self.db.query("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))

    def set_run_plan(self, run_id: str, plan: list[dict]) -> None:
        """Store the plan on the run."""
        self.db.execute("UPDATE runs SET plan_json=? WHERE run_id=?", (json.dumps(plan), run_id))

    def finish_run(self, run_id: str, status: str) -> None:
        """Set final run status and end time."""
        self.db.execute("UPDATE runs SET status=?, ended_at=? WHERE run_id=?", (status, time.time(), run_id))

    # ---- steps ----
    def add_step(self, step: dict) -> None:
        """Insert a step row (JSON fields given as Python objects)."""
        self.db.execute(
            "INSERT OR REPLACE INTO steps VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (step["step_id"], step["run_id"], step["agent_id"], step["role"], step["tool"],
             json.dumps(step["args"], sort_keys=True), json.dumps(step["result"]),
             json.dumps(step["parent_step_ids"]), step.get("plan_index"), step.get("deviation", 0),
             step.get("side_effect", 0), step.get("tainted", 0), step["ts"]))

    def get_steps(self, run_id: str) -> list[dict]:
        """Steps of a run ordered by ts, JSON decoded."""
        rows = self.db.query("SELECT * FROM steps WHERE run_id=? ORDER BY ts ASC, rowid ASC", (run_id,))
        return [{**{k: v for k, v in r.items() if not k.endswith("_json")},
                 "args": json.loads(r["args_json"]), "result": json.loads(r["result_json"]),
                 "parent_step_ids": json.loads(r["parent_step_ids_json"])} for r in rows]

    def set_step_plan(self, step_id: str, plan_index: int | None, deviation: int) -> None:
        """Set the matched plan index / deviation flag of a step."""
        self.db.execute("UPDATE steps SET plan_index=?, deviation=? WHERE step_id=?",
                        (plan_index, deviation, step_id))

    def mark_tainted(self, step_ids: list[str]) -> None:
        """Mark steps as tainted."""
        for sid in step_ids:
            self.db.execute("UPDATE steps SET tainted=1 WHERE step_id=?", (sid,))

    def set_run_status(self, run_id: str, status: str) -> None:
        """Set the run status without touching ended_at."""
        self.db.execute("UPDATE runs SET status=? WHERE run_id=?", (status, run_id))

    # ---- compensations ----
    def add_compensation(self, run_id: str, step_id: str, tool: str, undo_tool: str, undo_args: dict) -> None:
        """Record how to undo a side-effect step."""
        self.db.execute("INSERT INTO compensations(run_id,step_id,tool,undo_tool,undo_args_json,applied,"
                        "applied_at) VALUES(?,?,?,?,?,0,NULL)",
                        (run_id, step_id, tool, undo_tool, json.dumps(undo_args)))

    def pending_compensations(self, run_id: str, step_ids: list[str]) -> list[dict]:
        """Unapplied compensations for the given steps, newest first."""
        rows = self.db.query("SELECT * FROM compensations WHERE run_id=? AND applied=0 ORDER BY id DESC",
                             (run_id,))
        return [{**r, "undo_args": json.loads(r["undo_args_json"])} for r in rows if r["step_id"] in step_ids]

    def mark_compensation_applied(self, comp_id: int) -> None:
        """Flag a compensation as applied."""
        self.db.execute("UPDATE compensations SET applied=1, applied_at=? WHERE id=?", (time.time(), comp_id))

    # ---- event queries ----
    def count_events(self, agent_id: str, types: list[str], since_ts: float) -> int:
        """Count events of the given types for an agent at or after since_ts."""
        marks = ",".join("?" * len(types))
        row = self.db.query_one(f"SELECT COUNT(*) AS n FROM events WHERE agent_id=? AND ts>=? "
                                f"AND type IN ({marks})", (agent_id, since_ts, *types))
        return row["n"]

    def last_event_ts(self, agent_id: str, type: str) -> float | None:
        """Timestamp of the agent's latest event of a type."""
        row = self.db.query_one("SELECT MAX(ts) AS ts FROM events WHERE agent_id=? AND type=?",
                                (agent_id, type))
        return row["ts"] if row else None

    # ---- idempotency ----
    def get_idempotency(self, key: str) -> dict | None:
        """Stored (status_code, response) for a key, or None."""
        row = self.db.query_one("SELECT * FROM idempotency WHERE key=?", (key,))
        return {"status_code": row["status_code"], "response": json.loads(row["response_json"])} if row else None

    def put_idempotency(self, key: str, run_id: str, step_id: str, status_code: int, response: dict) -> None:
        """Store the response of an executed call."""
        self.db.execute("INSERT OR REPLACE INTO idempotency VALUES(?,?,?,?,?)",
                        (key, run_id, step_id, status_code, json.dumps(response)))

    # ---- approvals ----
    def add_approval(self, approval_id: str, call: dict, kind: str = "action",
                     payload: dict | None = None) -> None:
        """Insert a pending approval holding the envelope (and the data-review payload, if any)."""
        self.db.execute("INSERT INTO approvals(approval_id,run_id,step_id,agent_id,tool,envelope_json,status,"
                        "response_json,created_at,decided_at,kind,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (approval_id, call["run_id"], call["step_id"], call["agent_id"], call["tool"],
                         json.dumps(call), "pending", None, time.time(), None, kind,
                         json.dumps(payload) if payload is not None else None))

    def _approval(self, row: dict | None) -> dict | None:
        """Decode JSON columns of an approval row."""
        if row is None:
            return None
        resp = json.loads(row["response_json"]) if row["response_json"] else None
        return {**{k: v for k, v in row.items() if k not in ("envelope_json", "response_json", "payload_json")},
                "kind": row.get("kind") or "action",
                "payload": json.loads(row["payload_json"]) if row.get("payload_json") else None,
                "envelope": json.loads(row["envelope_json"]),
                "status_code": resp["status_code"] if resp else None,
                "response": resp["response"] if resp else None}

    def get_approval(self, approval_id: str) -> dict | None:
        """Return one approval (status_code/response decoded)."""
        return self._approval(self.db.query_one("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)))

    def pending_approvals(self) -> list[dict]:
        """All pending approvals, oldest first."""
        rows = self.db.query("SELECT * FROM approvals WHERE status='pending' ORDER BY created_at")
        return [self._approval(r) for r in rows]

    def claim_approval(self, approval_id: str, status: str) -> bool:
        """Atomically move a pending approval to approved|rejected; False if not pending."""
        with self.db.lock:
            cur = self.db.conn.execute(
                "UPDATE approvals SET status=?, decided_at=? WHERE approval_id=? AND status='pending'",
                (status, time.time(), approval_id))
            self.db.conn.commit()
            return cur.rowcount == 1

    def set_approval_response(self, approval_id: str, status_code: int, response: dict) -> None:
        """Store the final status code and body of a decided approval."""
        self.db.execute("UPDATE approvals SET response_json=? WHERE approval_id=?",
                        (json.dumps({"status_code": status_code, "response": response}), approval_id))

    # ---- reset ----
    def reset_state(self) -> None:
        """Delete agents, tokens, nonces, idempotency and approvals (events/runs/steps are kept)."""
        for table in ("agents", "tokens", "nonces", "idempotency", "approvals"):
            self.db.execute(f"DELETE FROM {table}")
