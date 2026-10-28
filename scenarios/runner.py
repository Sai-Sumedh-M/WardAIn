"""Scenario runner CLI: python -m scenarios.runner --scenario <name|all> --mode <on|off|both>."""
import argparse
import json
import threading
import time
import uuid
from pathlib import Path

import httpx
import yaml

from agents.admin import AdminClient
from agents.client import AgentClient
from agents.workflow import Behaviour, RunOutcome, run_workflow
from common.config import load_settings
from common.envelope import RunRequest
from scenarios import scorecard
from scenarios.attacks import RogueClient, replay

DEFINITIONS = Path(__file__).parent / "definitions"
ROLES = ["planner", "retriever", "executor", "auditor"]


def load_scenario(name: str) -> dict:
    """Load one scenario YAML."""
    return yaml.safe_load((DEFINITIONS / f"{name}.yaml").read_text())


def all_scenarios() -> list[str]:
    """Names of every scenario file."""
    return sorted(p.stem for p in DEFINITIONS.glob("*.yaml"))


def actuals(state: dict, customer_id: str, outcome_status: str, halt_ts: float | None) -> dict:
    """Ground-truth numbers computed from the tools state (the expect vocabulary)."""
    payments, outbox = state["payments"], state["outbox"]
    target = next(c for c in state["customers"] if c["customer_id"] == customer_id)
    mine = [p for p in payments if p["customer_id"] == customer_id and p["status"] == "completed"]
    return {
        "target_completed_refunds": len(mine),
        "target_completed_amount": round(sum(p["amount"] for p in mine), 2),
        "other_customer_refunds": len([p for p in payments if p["customer_id"] != customer_id
                                       and p["status"] == "completed"]),
        "attacker_emails": len([e for e in outbox if e["status"] == "sent"
                                and not e["to_addr"].endswith("@company.com")
                                and e["to_addr"] != target["email"]]),
        "customer_emails": len([e for e in outbox if e["status"] == "sent" and e["to_addr"] == target["email"]]),
        "target_balance_delta": round(target["balance"] - 1000.0, 2),
        "run_status": outcome_status,
        "payments_after_halt": len([p for p in payments if halt_ts and p["created_at"] > halt_ts]),
    }


def evaluate(expect: dict, state: dict, customer_id: str, outcome_status: str,
             halt_ts: float | None) -> tuple[dict, bool]:
    """Compare ground truth from tool state with the scenario expectations."""
    actual = actuals(state, customer_id, outcome_status, halt_ts)
    detail = {k: {"expected": v, "actual": actual[k]} for k, v in expect.items()}
    passed = all(round(d["expected"], 2) == round(d["actual"], 2) if isinstance(d["expected"], float)
                 else d["expected"] == d["actual"] for d in detail.values())
    return detail, passed


def build_hooks(attack: dict | None, agents: dict[str, AgentClient], run_id: str, settings,
                record: dict, admin: AdminClient) -> dict:
    """Create workflow hooks for the scenario attack (impersonate | replay | outage)."""
    if not attack or attack["type"] not in ("impersonate", "replay", "outage"):
        return {}

    def outage() -> None:
        record["outage_start_ts"] = time.time()
        admin.outage(attack["params"]["seconds"])

    if attack["type"] == "outage":
        return {attack["hook"]: outage}

    def fire() -> None:
        victim = agents["executor"]
        if attack["type"] == "impersonate":
            codes = RogueClient(settings.GATEWAY_URL).impersonate(run_id, victim)
        else:
            codes = replay(victim, attack["params"]["count"])
        record["attack_attempts"] = len(codes)
        record["attack_blocked"] = len([c for c in codes if c != 200])

    return {attack["hook"]: fire}


def run_loop(admin: AdminClient, executor: AgentClient, run_id: str, params: dict, customer_id: str,
             amount: float) -> tuple[float | None, float | None]:
    """Drive the runaway executor loop and halt everything after N completed payments."""
    def spam() -> None:
        for n in range(params["max_calls"]):
            step = round(amount + n * 0.01, 2) if params.get("vary_amount") else amount
            res = executor.call("issue_refund", {"customer_id": customer_id, "amount": step}, run_id,
                                idempotency_key=uuid.uuid4().hex)
            if res.status_code == 403:
                return
            time.sleep(params["interval_ms"] / 1000.0)

    thread = threading.Thread(target=spam)
    thread.start()
    halt_ts = halt_resp_ts = None
    while thread.is_alive():
        done = [p for p in admin.tool_state()["payments"] if p["status"] == "completed"]
        if len(done) >= params["after_successes"]:
            halt_ts = time.time()
            admin.halt_all()
            halt_resp_ts = time.time()
            break
        time.sleep(0.02)
    thread.join()
    return halt_ts, halt_resp_ts


def get_engine(engine: str):
    """Return the workflow function for the engine; langgraph is imported lazily."""
    if engine == "langgraph":
        from agents.langgraph_workflow import run_workflow_langgraph
        return run_workflow_langgraph
    return run_workflow


def run_once(admin: AdminClient, settings, sc: dict, mode: str, i: int, step_delay: float,
             engine: str = "plain", approval_mode: str = "auto_reject", hitl: str = "") -> dict:
    """Run one scenario iteration and return the raw result record."""
    name = sc["name"]
    admin.reset(mode=mode, scenario={"name": name, "chaos": sc.get("chaos") or [], "seed": 42 + i},
                approval_mode=approval_mode, monitor_enabled=sc.get("monitor_enabled", True),
                nvidia_guard=sc.get("nvidia_guard", "off"), hitl=hitl or sc.get("hitl", "off"))
    agents: dict[str, AgentClient] = {}
    counters = {r: 0 for r in ROLES}

    def make_agent(role: str) -> AgentClient:
        """Register a new agent f"{role}-{n}" with n incremented."""
        counters[role] += 1
        a = AgentClient(f"{role}-{counters[role]}", role, settings.GATEWAY_URL, settings.ADMIN_TOKEN)
        a.register()
        return a

    for role in ROLES:
        agents[role] = make_agent(role)
    run_id = f"{name}-{mode}-{int(time.time())}-{i:03d}"
    rq = sc["request"]
    req = RunRequest(run_id=run_id, scenario=name, customer_id=rq["customer_id"], amount=rq["amount"],
                     reason=rq["reason"])
    record = {"attack_attempts": 0, "attack_blocked": 0}
    hooks = build_hooks(sc.get("attack"), agents, run_id, settings, record, admin)
    outcome: RunOutcome = get_engine(engine)(admin, agents, req, Behaviour(**sc.get("behaviour", {})),
                                       hooks=hooks, step_delay=step_delay, make_agent=make_agent)
    halt_ts = halt_resp_ts = None
    if outcome.status == "loop_ready":
        halt_ts, halt_resp_ts = run_loop(admin, agents["executor"], run_id, sc["attack"]["params"],
                                         req.customer_id, req.amount)
        outcome.status = "halted"
    state = admin.tool_state()
    actual = actuals(state, req.customer_id, outcome.status, halt_ts)
    detail, passed = evaluate(sc.get("expect", {}), state, req.customer_id, outcome.status, halt_ts)
    events = admin.events(run_id=run_id)
    if halt_ts:
        seen = {e["event_id"] for e in events}
        events += [e for e in admin.events(since_ts=halt_ts) if e["event_id"] not in seen]
    return {"run_id": run_id, "scenario": name, "mode": mode, "engine": engine,
            "outcome": outcome.__dict__, "expect_passed": passed, "expect_detail": detail,
            "attack_attempts": record["attack_attempts"],
            "attack_blocked": record["attack_blocked"], "outage_start_ts": record.get("outage_start_ts"),
            "halt_ts": halt_ts, "halt_resp_ts": halt_resp_ts,
            "tool_state_summary": {**actual, "payments": len(state["payments"]),
                                   "outbox": len(state["outbox"])},
            "events": events}


def wait_for_gateway(url: str, timeout: float = 30.0) -> None:
    """Block until GET /health answers, up to timeout seconds."""
    deadline = time.time() + timeout
    while True:
        try:
            if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if time.time() > deadline:
            raise SystemExit(f"gateway not healthy at {url} after {timeout:.0f}s")
        time.sleep(0.5)


DEMO_DIR = Path("data/demo_runs")


def raw_dir_for(settings, demo: bool) -> Path:
    """Where raw results go: data/demo_runs for --demo, else results/raw (official)."""
    return DEMO_DIR if demo else Path(settings.RESULTS_DIR) / "raw"


def demo_review_settings(admin: AdminClient) -> tuple[str, str]:
    """For --demo: keep the console's Human review toggle (manual approvals + hitl) across the reset."""
    try:
        cfg = admin.get_config()
    except Exception:
        return "auto_reject", ""
    if cfg.get("approval_mode") == "manual" and cfg.get("hitl") == "ambiguous":
        return "manual", "ambiguous"
    return "auto_reject", ""


def main(argv: list[str] | None = None) -> None:
    """CLI entry point."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario")
    ap.add_argument("--scenario-file", help="load the scenario from this YAML path (use with --demo)")
    ap.add_argument("--mode", choices=["on", "off", "both"], required=True)
    ap.add_argument("--runs", type=int)
    ap.add_argument("--step-delay", type=float, default=0.0)
    ap.add_argument("--engine", choices=["plain", "langgraph"], default="plain")
    ap.add_argument("--demo", action="store_true",
                    help="write raw output to data/demo_runs/ and leave results/scorecard.* untouched")
    args = ap.parse_args(argv)
    if not args.scenario and not args.scenario_file:
        ap.error("--scenario or --scenario-file is required")
    if args.scenario_file and not args.demo:
        ap.error("--scenario-file requires --demo (official results come only from scenarios/definitions)")
    settings = load_settings()
    wait_for_gateway(settings.GATEWAY_URL)
    admin = AdminClient(settings.GATEWAY_URL, settings.ADMIN_TOKEN)
    console = demo_review_settings(admin) if args.demo else ("auto_reject", "")
    names = all_scenarios() if args.scenario == "all" and not args.scenario_file else [args.scenario or "file"]
    modes = ["off", "on"] if args.mode == "both" else [args.mode]
    raw_dir = raw_dir_for(settings, args.demo)
    raw_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        sc = (yaml.safe_load(Path(args.scenario_file).read_text()) if args.scenario_file
              else load_scenario(name))
        name = sc["name"]
        for mode in modes:
            out = raw_dir / f"{name}-{mode}.jsonl"
            out.unlink(missing_ok=True)
            passed = 0
            runs = args.runs or sc["runs"]
            for i in range(runs):
                rec = run_once(admin, settings, sc, mode, i, args.step_delay, args.engine, *console)
                passed += rec["expect_passed"]
                with out.open("a") as f:
                    f.write(json.dumps(rec) + "\n")
            print(f"{name} [{mode}]: {passed}/{runs} successful")
    if not args.demo:
        scorecard.main()


if __name__ == "__main__":
    main()
