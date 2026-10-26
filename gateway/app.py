"""WardAIn Gateway FastAPI app."""
import json
from pathlib import Path

import httpx
from fastapi import Body, FastAPI, Header
from fastapi.responses import FileResponse, JSONResponse

from common.config import Settings, load_settings
from common.envelope import RunRequest
from gateway.identity import IdentityError, IdentityService
from gateway.chaos import ChaosEngine
from gateway.killswitch import ACTIONS, KillSwitch
from gateway.llm import LLMBackend
from gateway.nvidia_guard import NvidiaGuard
from gateway.monitor import CircuitBreaker, Monitor
from gateway.recovery import Recovery
from gateway.pipeline import decide_approval, expire_approval, handle_call
from gateway.policy import PolicyEngine
from gateway.store import GatewayStore
from gateway.tools_client import ToolsClient


DASHBOARD_HTML = Path(__file__).parent / "static" / "dashboard.html"


def create_app(settings: Settings | None = None, tools_http: httpx.Client | None = None) -> FastAPI:
    """Build all components and the FastAPI app."""
    settings = settings or load_settings()
    tools_http = tools_http or httpx.Client(base_url=settings.TOOLS_URL, timeout=10)
    app = FastAPI()
    st = app.state
    st.settings = settings
    st.store = GatewayStore(settings.GATEWAY_DB_PATH)
    st.identity = IdentityService(st.store, settings.GATEWAY_KEY_PATH)
    st.policy = PolicyEngine(st.store)
    st.killswitch = KillSwitch(st.store, st.identity, st.policy)
    st.chaos = ChaosEngine(st.store)
    st.circuit = CircuitBreaker(st.store, st.policy.cfg["circuit"])
    st.tools = ToolsClient(tools_http)
    st.llm = LLMBackend(settings, st.store)
    st.nvidia_guard = NvidiaGuard(settings)
    st.store.set_config("nvidia_guard", settings.NVIDIA_GUARD)
    st.recovery = Recovery(st.store, st.tools)
    st.monitor = Monitor(st.store, st.killswitch, st.recovery, st.policy)

    def admin_ok(token: str | None) -> bool:
        return token == settings.ADMIN_TOKEN

    def denied() -> JSONResponse:
        return JSONResponse({"error": "admin_auth"}, status_code=401)

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.get("/ui")
    def ui() -> FileResponse:
        return FileResponse(DASHBOARD_HTML, media_type="text/html")

    @app.post("/identity/register")
    def register(body: dict = Body(...), x_admin_token: str | None = Header(None)):
        if not admin_ok(x_admin_token):
            return denied()
        st.identity.register(body["agent_id"], body["role"], body["public_key"])
        return {"ok": True}

    @app.post("/identity/token")
    def token(body: dict = Body(...)):
        try:
            return st.identity.issue_token(body["agent_id"], int(body["timestamp"]), body["signature"])
        except IdentityError as e:
            return JSONResponse({"ok": False, "error": e.reason}, status_code=401)

    @app.post("/call")
    def call(body: dict = Body(...), authorization: str | None = Header(None)):
        code, resp = handle_call(st, authorization, body)
        return JSONResponse(resp, status_code=code)

    @app.get("/approvals/{approval_id}")
    def approval_status(approval_id: str, authorization: str | None = Header(None)):
        try:
            st.identity.decode_token(authorization)
        except IdentityError as e:
            return JSONResponse({"ok": False, "error": e.reason}, status_code=401)
        expire_approval(st, approval_id)
        ap = st.store.get_approval(approval_id)
        if ap is None:
            return JSONResponse({"error": "unknown_approval"}, status_code=404)
        return {"status": ap["status"], "status_code": ap["status_code"], "response": ap["response"]}

    @app.post("/runs")
    def create_run(req: RunRequest, x_admin_token: str | None = Header(None)):
        if not admin_ok(x_admin_token):
            return denied()
        st.store.add_run(req.run_id, req.scenario, st.store.get_config("mode"), req.customer_id,
                         round(req.amount, 2), req.reason)
        st.store.add_event("run_started", req.run_id, detail={"scenario": req.scenario})
        return {"ok": True}

    @app.get("/recovery/{run_id}")
    def recovery(run_id: str, x_admin_token: str | None = Header(None)):
        if not admin_ok(x_admin_token):
            return denied()
        return st.recovery.status(run_id)

    @app.post("/control/halt_all")
    def halt_all(x_admin_token: str | None = Header(None)):
        if not admin_ok(x_admin_token):
            return denied()
        st.killswitch.halt_all()
        return {"ok": True}

    @app.post("/control/resume_all")
    def resume_all(x_admin_token: str | None = Header(None)):
        if not admin_ok(x_admin_token):
            return denied()
        st.killswitch.resume_all()
        return {"ok": True}

    @app.post("/control/{agent_id}/{action}")
    def control(agent_id: str, action: str, x_admin_token: str | None = Header(None)):
        if not admin_ok(x_admin_token):
            return denied()
        if action not in ACTIONS:
            return JSONResponse({"error": "bad_action"}, status_code=400)
        if st.store.get_agent(agent_id) is None:
            return JSONResponse({"error": "unknown_agent"}, status_code=404)
        st.killswitch.set_status(agent_id, action)
        return {"ok": True, "agent_id": agent_id, "status": ACTIONS[action][0]}

    @app.get("/admin/approvals")
    def approvals(x_admin_token: str | None = Header(None)):
        if not admin_ok(x_admin_token):
            return denied()
        for ap in st.store.pending_approvals():
            expire_approval(st, ap["approval_id"])
        return st.store.pending_approvals()

    @app.post("/admin/approvals/{approval_id}/{decision}")
    def decide(approval_id: str, decision: str, x_admin_token: str | None = Header(None)):
        if not admin_ok(x_admin_token):
            return denied()
        if decision not in ("approve", "reject"):
            return JSONResponse({"error": "bad_decision"}, status_code=400)
        ap = st.store.get_approval(approval_id)
        if ap is None or ap["status"] != "pending":
            return JSONResponse({"error": "not_pending"}, status_code=404)
        decide_approval(st, approval_id, decision)
        return {"ok": True}

    @app.post("/admin/reset")
    def reset(body: dict = Body(...), x_admin_token: str | None = Header(None)):
        if not admin_ok(x_admin_token):
            return denied()
        st.store.reset_state()
        for key in ("mode", "scenario", "approval_mode", "monitor_enabled"):
            if key in body:
                st.store.set_config(key, body[key])
        st.store.set_config("global_halt", False)
        st.store.set_config("nvidia_guard", body.get("nvidia_guard", settings.NVIDIA_GUARD))
        st.chaos.reset()
        st.circuit.reset()
        st.tools.reset()
        return {"ok": True}

    @app.get("/admin/config")
    def get_config(x_admin_token: str | None = Header(None)):
        return st.store.all_config() if admin_ok(x_admin_token) else denied()

    @app.post("/admin/config")
    def set_config(body: dict = Body(...), x_admin_token: str | None = Header(None)):
        if not admin_ok(x_admin_token):
            return denied()
        for key, value in body.items():
            st.store.set_config(key, value)
        return st.store.all_config()

    @app.get("/admin/events")
    def events(run_id: str | None = None, since_ts: float = 0.0, limit: int = 500,
               x_admin_token: str | None = Header(None)):
        return st.store.get_events(run_id, since_ts, limit) if admin_ok(x_admin_token) else denied()

    @app.get("/admin/agents")
    def agents(x_admin_token: str | None = Header(None)):
        return st.store.list_agents() if admin_ok(x_admin_token) else denied()

    @app.get("/admin/runs")
    def runs(x_admin_token: str | None = Header(None)):
        return st.store.list_runs() if admin_ok(x_admin_token) else denied()

    @app.get("/admin/steps/{run_id}")
    def steps(run_id: str, x_admin_token: str | None = Header(None)):
        return st.store.get_steps(run_id) if admin_ok(x_admin_token) else denied()

    @app.post("/admin/tools/outage")
    def outage(body: dict = Body(...), x_admin_token: str | None = Header(None)):
        if not admin_ok(x_admin_token):
            return denied()
        st.tools.outage(float(body["seconds"]))
        st.store.add_event("outage_started", detail={"seconds": float(body["seconds"])})
        return {"ok": True}

    @app.get("/admin/tool_state")
    def tool_state(x_admin_token: str | None = Header(None)):
        return st.tools.state() if admin_ok(x_admin_token) else denied()

    @app.get("/admin/scorecard")
    def scorecard(x_admin_token: str | None = Header(None)):
        if not admin_ok(x_admin_token):
            return denied()
        try:
            return json.loads((Path(settings.RESULTS_DIR) / "scorecard.json").read_text())
        except (OSError, ValueError):
            return JSONResponse({"error": "no_scorecard"}, status_code=404)

    @app.get("/admin/redteam")
    def redteam(x_admin_token: str | None = Header(None)):
        if not admin_ok(x_admin_token):
            return denied()
        try:
            return json.loads((Path(settings.RESULTS_DIR) / "redteam" / "redteam_report.json").read_text())
        except (OSError, ValueError):
            return JSONResponse({"error": "no_redteam"}, status_code=404)

    return app
