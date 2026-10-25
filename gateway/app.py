"""WardAIn Gateway FastAPI app."""
import httpx
from fastapi import Body, FastAPI, Header
from fastapi.responses import JSONResponse

from common.config import Settings, load_settings
from common.envelope import RunRequest
from gateway.identity import IdentityError, IdentityService
from gateway.llm import LLMBackend
from gateway.pipeline import handle_call
from gateway.store import GatewayStore
from gateway.tools_client import ToolsClient


def create_app(settings: Settings | None = None, tools_http: httpx.Client | None = None) -> FastAPI:
    """Build all components and the FastAPI app."""
    settings = settings or load_settings()
    tools_http = tools_http or httpx.Client(base_url=settings.TOOLS_URL, timeout=10)
    app = FastAPI()
    st = app.state
    st.settings = settings
    st.store = GatewayStore(settings.GATEWAY_DB_PATH)
    st.identity = IdentityService(st.store, settings.GATEWAY_KEY_PATH)
    st.tools = ToolsClient(tools_http)
    st.llm = LLMBackend(settings, st.store)

    def admin_ok(token: str | None) -> bool:
        return token == settings.ADMIN_TOKEN

    def denied() -> JSONResponse:
        return JSONResponse({"error": "admin_auth"}, status_code=401)

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

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

    @app.post("/runs")
    def create_run(req: RunRequest, x_admin_token: str | None = Header(None)):
        if not admin_ok(x_admin_token):
            return denied()
        st.store.add_run(req.run_id, req.scenario, st.store.get_config("mode"), req.customer_id,
                         round(req.amount, 2), req.reason)
        st.store.add_event("run_started", req.run_id, detail={"scenario": req.scenario})
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

    @app.get("/admin/tool_state")
    def tool_state(x_admin_token: str | None = Header(None)):
        return st.tools.state() if admin_ok(x_admin_token) else denied()

    return app
