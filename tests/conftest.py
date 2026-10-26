import pytest
from fastapi.testclient import TestClient

from agents.admin import AdminClient
from agents.client import AgentClient
from common.config import Settings
from gateway.app import create_app
from tools.mock_tools import create_tools_app


@pytest.fixture
def tmp_settings(tmp_path) -> Settings:
    return Settings(LLM_MODE="mock", ADMIN_TOKEN="test-admin",
                    GATEWAY_DB_PATH=str(tmp_path / "gateway.db"),
                    TOOLS_DB_PATH=str(tmp_path / "tools.db"),
                    GATEWAY_KEY_PATH=str(tmp_path / "gateway_ed25519.pem"),
                    RESULTS_DIR=str(tmp_path / "results"))


@pytest.fixture
def tools_app(tmp_path):
    return create_tools_app(str(tmp_path / "tools.db"))


@pytest.fixture
def tools_http(tools_app):
    return TestClient(tools_app, base_url="http://tools")


@pytest.fixture
def gateway_app(tmp_settings, tools_http):
    return create_app(tmp_settings, tools_http=tools_http)


@pytest.fixture
def gw_http(gateway_app):
    return TestClient(gateway_app, base_url="http://gateway")


@pytest.fixture
def admin(gw_http) -> AdminClient:
    client = AdminClient("http://gateway", "test-admin", http=gw_http)
    client.reset(mode="on", scenario={"name": "test", "chaos": [], "seed": 42},
                 approval_mode="auto_reject", monitor_enabled=True)
    return client


@pytest.fixture
def make_agent(gw_http, admin):
    def factory(role: str, agent_id: str | None = None) -> AgentClient:
        agent = AgentClient(agent_id or f"{role}-1", role, "http://gateway", "test-admin", http=gw_http)
        agent.register()
        return agent
    return factory
