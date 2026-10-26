"""WardAIn dashboard: live events, kill-switch controls, scenario runner, scorecard."""
import datetime
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # `streamlit run` only adds dashboard/

from agents.admin import AdminClient
from common.config import load_settings

SETTINGS = load_settings()
RESULTS = Path(SETTINGS.RESULTS_DIR)
SCENARIO_DIR = Path(__file__).resolve().parent.parent / "scenarios" / "definitions"
STOP_PREFIXES = ("identity_failed", "replay_blocked", "policy_blocked", "guard_blocked", "agent_not_active")
WARN_PREFIXES = ("anomaly_detected", "agent_quarantined", "rollback_", "compensation_applied")


def get_admin() -> AdminClient:
    """Build an AdminClient from settings."""
    return AdminClient(SETTINGS.GATEWAY_URL, SETTINGS.ADMIN_TOKEN)


def event_icon(etype: str) -> str:
    """Emoji prefix for an event type."""
    if etype.startswith(STOP_PREFIXES):
        return "🛑 "
    if etype.startswith(WARN_PREFIXES):
        return "⚠️ "
    if etype == "side_effect":
        return "💸 "
    return ""


def fmt_ts(ts: float) -> str:
    """Format a unix timestamp as HH:MM:SS.mmm."""
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3]


def events_frame(events: list[dict]) -> pd.DataFrame:
    """Turn raw events into display rows, newest first."""
    rows = []
    for e in reversed(events):
        detail = e.get("detail") or json.loads(e.get("detail_json") or "{}")
        rows.append({"time": fmt_ts(e["ts"]), "type": event_icon(e["type"]) + e["type"],
                     "agent": e.get("agent_id") or "", "tool": e.get("tool") or "",
                     "reason": detail.get("reason", "")})
    return pd.DataFrame(rows, columns=["time", "type", "agent", "tool", "reason"])


def render_agents(admin: AdminClient) -> None:
    """Agent status tiles with per-agent controls and global halt."""
    agents = admin.agents()
    cfg = admin.get_config()
    if cfg.get("global_halt"):
        st.error("GLOBAL HALT is active")
    cols = st.columns(max(len(agents), 1) + 1)
    for col, a in zip(cols, agents):
        with col:
            st.metric(a["agent_id"], a["status"])
            for action in ("pause", "resume", "quarantine", "halt"):
                if st.button(action.capitalize(), key=f"{a['agent_id']}-{action}"):
                    admin.control(a["agent_id"], action)
                    st.rerun()
    with cols[-1]:
        if st.button("HALT ALL", type="primary"):
            admin.halt_all()
            st.rerun()
        if st.button("Resume all"):
            admin.resume_all()
            st.rerun()


def render_approvals(admin: AdminClient) -> None:
    """Pending approvals with Approve / Reject buttons."""
    st.subheader("Approvals")
    pending = admin.approvals()
    if not pending:
        st.caption("No pending approvals")
    for ap in pending:
        st.write(f"{ap['approval_id']}: {ap['agent_id']} -> {ap['tool']}")
        c1, c2 = st.columns(2)
        if c1.button("Approve", key=f"ap-{ap['approval_id']}"):
            admin.decide(ap["approval_id"], "approve")
            st.rerun()
        if c2.button("Reject", key=f"rj-{ap['approval_id']}"):
            admin.decide(ap["approval_id"], "reject")
            st.rerun()


def page_live() -> None:
    """Live page: agents, event stream, approvals, tool state."""
    st.title("WardAIn: Live")
    live = st.checkbox("Live", value=True)
    admin = get_admin()
    render_agents(admin)
    st.subheader("Event stream (last 50)")
    st.dataframe(events_frame(admin.events(limit=50)), use_container_width=True, hide_index=True)
    render_approvals(admin)
    st.subheader("Tool state")
    state = admin.tool_state()
    c1, c2 = st.columns(2)
    c1.write("Payments")
    c1.dataframe(pd.DataFrame(state["payments"]), use_container_width=True, hide_index=True)
    c2.write("Outbox")
    c2.dataframe(pd.DataFrame(state["outbox"]), use_container_width=True, hide_index=True)
    if live:
        time.sleep(1)
        st.rerun()


def page_run() -> None:
    """Run scenario page: launch the runner for one run."""
    st.title("Run scenario")
    names = sorted(p.stem for p in SCENARIO_DIR.glob("*.yaml"))
    name = st.selectbox("Scenario", names)
    mode = st.radio("Mode", ["on", "off"], horizontal=True)
    engine = st.radio("Engine", ["plain"], horizontal=True)
    delay = st.number_input("Step delay (s)", value=1.0, min_value=0.0, step=0.5)
    if st.button("Run once"):
        cmd = [sys.executable, "-m", "scenarios.runner", "--scenario", name, "--mode", mode,
               "--runs", "1", "--step-delay", str(delay), "--engine", engine, "--demo"]
        with st.spinner("Running..."):
            proc = subprocess.run(cmd, capture_output=True, text=True, env=os.environ.copy())
        st.code(proc.stdout + proc.stderr)


def page_scorecard() -> None:
    """Scorecard page: markdown table plus OFF vs ON success chart."""
    st.title("Resilience Scorecard")
    md, js = RESULTS / "scorecard.md", RESULTS / "scorecard.json"
    if not md.exists():
        st.info("No scorecard yet. Run: python -m scenarios.runner --scenario all --mode both")
        return
    st.markdown(md.read_text())
    if js.exists():
        data = json.loads(js.read_text())
        rows = {s["scenario"]: {"OFF": s.get("task_success_rate_off"), "ON": s.get("task_success_rate_on")}
                for s in data.get("scenarios", [])}
        if rows:
            st.bar_chart(pd.DataFrame(rows).T)


def main() -> None:
    """Dashboard entry point."""
    st.set_page_config(page_title="WardAIn", layout="wide")
    page = st.sidebar.radio("Page", ["Live", "Run scenario", "Scorecard"])
    {"Live": page_live, "Run scenario": page_run, "Scorecard": page_scorecard}[page]()


main()
