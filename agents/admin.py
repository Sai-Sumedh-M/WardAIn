"""Admin API client used by the harness and dashboard."""
import httpx

from common.envelope import RunRequest


class AdminError(Exception):
    """An admin API call returned a non-2xx status."""


def _json(resp: httpx.Response):
    """Return parsed JSON or raise AdminError showing the status code and body."""
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError:
        raise AdminError(f"{resp.request.method} {resp.request.url.path} -> {resp.status_code}: {resp.text}")
    return resp.json()


class AdminClient:
    def __init__(self, gateway_url: str, admin_token: str, http: httpx.Client | None = None):
        """Create a client with the admin header preset."""
        self.http = http or httpx.Client(base_url=gateway_url, timeout=30)
        self.headers = {"X-Admin-Token": admin_token}

    def _get(self, path: str, params: dict | None = None):
        """GET and return parsed JSON."""
        return _json(self.http.get(path, headers=self.headers, params=params))

    def _post(self, path: str, body: dict | None = None):
        """POST and return parsed JSON."""
        return _json(self.http.post(path, headers=self.headers, json=body or {}))

    def reset(self, mode: str, scenario: dict, approval_mode: str, monitor_enabled: bool,
              nvidia_guard: str = "off", hitl: str = "off") -> dict:
        """Reset gateway and tools state."""
        return self._post("/admin/reset", {"mode": mode, "scenario": scenario,
                                           "approval_mode": approval_mode,
                                           "monitor_enabled": monitor_enabled,
                                           "nvidia_guard": nvidia_guard, "hitl": hitl})

    def create_run(self, req: RunRequest) -> dict:
        """Register a run."""
        return self._post("/runs", req.model_dump())

    def recovery(self, run_id: str) -> dict:
        """Recovery status of a run."""
        return self._get(f"/recovery/{run_id}")

    def control(self, agent_id: str, action: str) -> dict:
        """pause | resume | quarantine | halt one agent."""
        return self._post(f"/control/{agent_id}/{action}")

    def halt_all(self) -> dict:
        """Global halt."""
        return self._post("/control/halt_all")

    def resume_all(self) -> dict:
        """Clear global halt."""
        return self._post("/control/resume_all")

    def events(self, run_id: str | None = None, since_ts: float = 0, limit: int = 500) -> list[dict]:
        """Events ordered by ts."""
        params = {"since_ts": since_ts, "limit": limit}
        if run_id:
            params["run_id"] = run_id
        return self._get("/admin/events", params)

    def agents(self) -> list[dict]:
        """All agents."""
        return self._get("/admin/agents")

    def runs(self) -> list[dict]:
        """Last 100 runs."""
        return self._get("/admin/runs")

    def steps(self, run_id: str) -> list[dict]:
        """Steps of a run."""
        return self._get(f"/admin/steps/{run_id}")

    def tool_state(self) -> dict:
        """Customers, payments and outbox."""
        return self._get("/admin/tool_state")

    def outage(self, seconds: float) -> dict:
        """Start a tools outage."""
        return self._post("/admin/tools/outage", {"seconds": seconds})

    def approvals(self) -> list[dict]:
        """Pending approvals."""
        return self._get("/admin/approvals")

    def decide(self, approval_id: str, decision: str) -> dict:
        """approve | reject a pending approval."""
        return self._post(f"/admin/approvals/{approval_id}/{decision}")

    def get_config(self) -> dict:
        """All config keys."""
        return self._get("/admin/config")

    def set_config(self, partial: dict) -> dict:
        """Merge config keys."""
        return self._post("/admin/config", partial)
