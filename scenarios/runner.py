"""Scenario runner CLI: python -m scenarios.runner --scenario <name|all> --mode <on|off|both>."""
import argparse
import json
import time
from pathlib import Path

import yaml

from agents.admin import AdminClient
from agents.client import AgentClient
from agents.workflow import Behaviour, RunOutcome, run_workflow
from common.config import load_settings
from common.envelope import RunRequest
from scenarios.attacks import RogueClient, replay

DEFINITIONS = Path(__file__).parent / "definitions"
ROLES = ["planner", "retriever", "executor", "auditor"]


def load_scenario(name: str) -> dict:
    """Load one scenario YAML."""
    return yaml.safe_load((DEFINITIONS / f"{name}.yaml").read_text())


def all_scenarios() -> list[str]:
    """Names of every scenario file."""
    return sorted(p.stem for p in DEFINITIONS.glob("*.yaml"))


def evaluate(expect: dict, state: dict, customer_id: str, outcome_status: str,
             halt_ts: float | None) -> tuple[dict, bool]:
    """Compare ground truth from tool state with the scenario expectations."""
    payments, outbox = state["payments"], state["outbox"]
    target = next(c for c in state["customers"] if c["customer_id"] == customer_id)
    mine = [p for p in payments if p["customer_id"] == customer_id and p["status"] == "completed"]
    actual = {
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
    detail = {k: {"expected": v, "actual": actual[k]} for k, v in expect.items()}
    passed = all(round(d["expected"], 2) == round(d["actual"], 2) if isinstance(d["expected"], float)
                 else d["expected"] == d["actual"] for d in detail.values())
    return detail, passed


def build_hooks(attack: dict | None, agents: dict[str, AgentClient], run_id: str, settings,
                record: dict) -> dict:
    """Create workflow hooks for the scenario attack (impersonate | replay)."""
    if not attack or attack["type"] not in ("impersonate", "replay"):
        return {}

    def fire() -> None:
        victim = agents["executor"]
        if attack["type"] == "impersonate":
            codes = RogueClient(settings.GATEWAY_URL).impersonate(run_id, victim)
        else:
            codes = replay(victim, attack["params"]["count"])
        record["attack_attempts"] = len(codes)
        record["attack_blocked"] = len([c for c in codes if c != 200])

    return {attack["hook"]: fire}


def run_once(admin: AdminClient, settings, sc: dict, mode: str, i: int, step_delay: float) -> dict:
    """Run one scenario iteration and return the raw result record."""
    name = sc["name"]
    admin.reset(mode=mode, scenario={"name": name, "chaos": sc.get("chaos") or [], "seed": 42 + i},
                approval_mode="auto_reject", monitor_enabled=sc.get("monitor_enabled", True))
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
    hooks = build_hooks(sc.get("attack"), agents, run_id, settings, record)
    outcome: RunOutcome = run_workflow(admin, agents, req, Behaviour(**sc.get("behaviour", {})),
                                       hooks=hooks, step_delay=step_delay, make_agent=make_agent)
    state = admin.tool_state()
    detail, passed = evaluate(sc.get("expect", {}), state, req.customer_id, outcome.status, None)
    return {"run_id": run_id, "scenario": name, "mode": mode, "engine": "plain",
            "outcome": outcome.__dict__, "expect_passed": passed, "expect_detail": detail,
            "attack_attempts": record["attack_attempts"],
            "attack_blocked": record["attack_blocked"], "outage_start_ts": None, "halt_ts": None,
            "halt_resp_ts": None,
            "tool_state_summary": {"payments": len(state["payments"]), "outbox": len(state["outbox"])},
            "events": admin.events(run_id=run_id)}


def main() -> None:
    """CLI entry point."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--mode", choices=["on", "off", "both"], required=True)
    ap.add_argument("--runs", type=int)
    ap.add_argument("--step-delay", type=float, default=0.0)
    ap.add_argument("--engine", choices=["plain", "langgraph"], default="plain")
    args = ap.parse_args()
    settings = load_settings()
    admin = AdminClient(settings.GATEWAY_URL, settings.ADMIN_TOKEN)
    names = all_scenarios() if args.scenario == "all" else [args.scenario]
    modes = ["off", "on"] if args.mode == "both" else [args.mode]
    raw_dir = Path(settings.RESULTS_DIR) / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        sc = load_scenario(name)
        for mode in modes:
            out = raw_dir / f"{name}-{mode}.jsonl"
            out.unlink(missing_ok=True)
            passed = 0
            runs = args.runs or sc["runs"]
            for i in range(runs):
                rec = run_once(admin, settings, sc, mode, i, args.step_delay)
                passed += rec["expect_passed"]
                with out.open("a") as f:
                    f.write(json.dumps(rec) + "\n")
            print(f"{name} [{mode}]: {passed}/{runs} successful")


if __name__ == "__main__":
    main()
