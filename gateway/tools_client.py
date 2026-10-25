"""HTTP client for the downstream tools service."""
import httpx


class ToolsUnavailable(Exception):
    """The tools service could not be reached."""


class ToolsClient:
    def __init__(self, http: httpx.Client):
        """Wrap an httpx client already pointed at the tools service."""
        self.http = http

    def call(self, tool: str, args: dict) -> tuple[int, str]:
        """POST /tools/{tool}; return (status_code, raw_text)."""
        try:
            resp = self.http.post(f"/tools/{tool}", json={"args": args})
        except httpx.HTTPError as e:
            raise ToolsUnavailable(str(e))
        return resp.status_code, resp.text

    def reset(self) -> None:
        """Reseed the tools database."""
        self.http.post("/admin/reset")

    def outage(self, seconds: float) -> None:
        """Start a tools outage."""
        self.http.post("/admin/outage", json={"seconds": seconds})

    def state(self) -> dict:
        """Return the full tools state."""
        return self.http.get("/admin/state").json()
